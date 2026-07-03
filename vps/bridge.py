#!/usr/bin/env python3
"""External-facing bridge for SGLang MLU CI tasks.

GitHub Actions posts a small task payload to this service. The bridge validates
and forwards it to the local slave service, then proxies status, log, and
end_job queries back to GitHub Actions.
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import cfg, json_response, load_config, read_json_body, text_response


def request_params(path: str) -> Dict[str, str]:
    parsed = urlparse(path)
    parts = [part for part in (parsed.path.lstrip("/"), parsed.query) if part]
    return {k: v[0] for k, v in parse_qs("&".join(parts)).items()}


class BridgeHandler(BaseHTTPRequestHandler):
    slave_url = ""
    allowed_repo = "sglang"

    def log_message(self, fmt, *args):  # noqa: D401 - BaseHTTPRequestHandler hook
        print("[bridge] " + fmt % args)

    def do_POST(self):
        try:
            self.handle_submit_task(read_json_body(self))
        except Exception as exc:  # Keep the runner alive on malformed requests.
            print(f"[bridge] POST failed: {exc}")
            json_response(self, 500, {"status": "error", "error": str(exc)})

    def handle_submit_task(self, data: Dict[str, Any]) -> None:
        """Handle a new CI task submitted by GitHub Actions.

        Scenario: GitHub Actions posts task metadata to bridge.py. The bridge
        validates the allowed repo and required fields, marks the payload as
        source=bridge, forwards the lightweight JSON payload to slave.py, and
        returns the slave-generated task id for later polling.
        """
        repo = str(data.get("repo", ""))
        if self.allowed_repo and repo != self.allowed_repo:
            json_response(self, 400, {"status": "error", "error": f"unsupported repo: {repo}"})
            return

        payload = self.build_submit_payload(data, repo)
        missing = self.missing_required_fields(payload)
        if missing:
            json_response(
                self,
                400,
                {"status": "error", "error": f"missing required fields: {missing}"},
            )
            return

        response = requests.post(self.slave_url, json=payload, timeout=30)
        response.raise_for_status()
        slave_payload = response.json()
        json_response(self, 200, {"status": str(response.status_code), "id": slave_payload["id"]})

    def build_submit_payload(self, data: Dict[str, Any], repo: str) -> Dict[str, str]:
        """Build the slave-facing JSON payload for a GitHub Actions task."""
        return {
            "source": "bridge",
            "timestamp": str(data.get("timestamp", "")),
            "repo": repo,
            "pr_id": str(data.get("pr_id", "")),
            "repo_url": str(data.get("repo_url", "")),
            "git_ref": str(data.get("git_ref", "")),
            "commit_sha": str(data.get("commit_sha", "")),
            "trigger_type": str(data.get("trigger_type", "ci")),
            "trigger_id": str(data.get("trigger_id", "")),
            "repeat_times": str(data.get("repeat_times", "1")),
            "status": str(data.get("status", "running")),
            "log": "",
        }

    def missing_required_fields(self, payload: Dict[str, str]) -> list[str]:
        """Return required GitHub Actions submission fields that are empty."""
        required = ["timestamp", "repo", "trigger_type", "trigger_id"]
        return [key for key in required if not payload.get(key)]

    def do_GET(self):
        try:
            params = request_params(self.path)
            aiming = params.get("aiming")
            task_id = params.get("id", "")
            if not aiming or not task_id:
                json_response(self, 400, {"status": "error", "error": "aiming and id are required"})
                return

            if aiming == "get_status":
                self.handle_get_status(task_id)
                return

            if aiming == "get_log":
                self.handle_get_log(params, task_id)
                return

            if aiming == "end_job":
                self.handle_end_job(task_id)
                return

            json_response(self, 400, {"status": "error", "error": f"unknown aiming: {aiming}"})
        except Exception as exc:
            print(f"[bridge] GET failed: {exc}")
            json_response(self, 500, {"status": "error", "error": str(exc)})

    def handle_get_status(self, task_id: str) -> None:
        """Handle GitHub Actions polling for lightweight task status.

        Scenario: GitHub Actions asks bridge.py for the current task state. The
        bridge proxies the request to slave.py and returns the slave JSON body,
        which includes status, recent log tail, Jenkins build id, and log size.
        """
        response = requests.get(
            self.slave_url,
            params={"source": "bridge", "aiming": "get_status", "id": task_id},
            timeout=30,
        )
        response.raise_for_status()
        json_response(self, 200, response.json())

    def handle_get_log(self, params: Dict[str, str], task_id: str) -> None:
        """Handle GitHub Actions downloading full or tail Jenkins logs.

        Scenario: GitHub Actions downloads the complete Jenkins console log for
        artifact upload, or passes tail=N to print recent failure context. The
        bridge proxies slave.py's plain-text log response and never JSON-wraps a
        successful log body.
        """
        slave_params = {"source": "bridge", "aiming": "get_log", "id": task_id}
        if params.get("tail"):
            slave_params["tail"] = params["tail"]
        if params.get("start"):
            slave_params["start"] = params["start"]
        response = requests.get(self.slave_url, params=slave_params, timeout=60)
        if response.status_code == 200:
            text_response(self, 200, response.text)
        else:
            json_response(
                self,
                response.status_code,
                {"status": "error", "id": task_id, "error": response.text},
            )

    def handle_end_job(self, task_id: str) -> None:
        """Handle GitHub Actions cleanup after logs have been saved.

        Scenario: GitHub Actions has reached a terminal task state and has
        already downloaded the full log artifact. The bridge asks slave.py to
        delete the task state and local log file immediately.
        """
        response = requests.get(
            self.slave_url,
            params={"source": "bridge", "aiming": "end_job", "id": task_id},
            timeout=30,
        )
        if response.status_code == 200:
            json_response(self, 200, {"status": "success", "id": task_id})
        else:
            json_response(self, response.status_code, {"status": "error", "id": task_id})


def main() -> int:
    parser = argparse.ArgumentParser(description="Run SGLang MLU CI bridge")
    parser.add_argument("conf", nargs="?", help="Path to external_ci.conf")
    args = parser.parse_args()

    config = load_config(args.conf or str(Path(__file__).with_name("external_ci.conf")))
    bridge_host = cfg(config, "BridgeServer", "host", "")
    bridge_port = int(cfg(config, "BridgeServer", "port", "14547"))
    slave_host = cfg(config, "SlaveServer", "host", "localhost")
    slave_port = cfg(config, "SlaveServer", "port", "14548")
    BridgeHandler.slave_url = f"http://{slave_host}:{slave_port}"
    BridgeHandler.allowed_repo = cfg(config, "BridgeServer", "repo", "sglang")

    server = ThreadingHTTPServer((bridge_host, bridge_port), BridgeHandler)
    print(f"[bridge] listening on {bridge_host or '0.0.0.0'}:{bridge_port}, forwarding to {BridgeHandler.slave_url}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
