#!/usr/bin/env python3
"""External-facing bridge for SGLang MLU CI tasks.

GitHub Actions posts a small task payload to this service. The bridge validates
and forwards it to the local slave service, then proxies status/end_job queries
back to GitHub Actions.
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import cfg, json_response, load_config, read_json_body


class BridgeHandler(BaseHTTPRequestHandler):
    slave_url = ""
    allowed_repo = "sglang"

    def log_message(self, fmt, *args):  # noqa: D401 - BaseHTTPRequestHandler hook
        print("[bridge] " + fmt % args)

    def do_POST(self):
        try:
            data = read_json_body(self)
            repo = str(data.get("repo", ""))
            if self.allowed_repo and repo != self.allowed_repo:
                json_response(
                    self,
                    400,
                    {"status": "error", "error": f"unsupported repo: {repo}"},
                )
                return

            payload = {
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
            required = ["timestamp", "repo", "trigger_type", "trigger_id"]
            missing = [key for key in required if not payload.get(key)]
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
            json_response(
                self,
                200,
                {"status": str(response.status_code), "id": slave_payload["id"]},
            )
        except Exception as exc:  # Keep the runner alive on malformed requests.
            print(f"[bridge] POST failed: {exc}")
            json_response(self, 500, {"status": "error", "error": str(exc)})

    def do_GET(self):
        try:
            params = {k: v[0] for k, v in parse_qs(urlparse(self.path).path.lstrip("/")).items()}
            aiming = params.get("aiming")
            task_id = params.get("id", "")
            if not aiming or not task_id:
                json_response(self, 400, {"status": "error", "error": "aiming and id are required"})
                return

            if aiming == "get_status":
                response = requests.get(
                    f"{self.slave_url}/source=bridge&aiming=get_status&id={task_id}",
                    timeout=30,
                )
                response.raise_for_status()
                json_response(self, 200, response.json())
                return

            if aiming == "end_job":
                response = requests.get(
                    f"{self.slave_url}/source=bridge&aiming=end_job&id={task_id}",
                    timeout=30,
                )
                if response.status_code == 200:
                    json_response(self, 200, {"status": "success", "id": task_id})
                else:
                    json_response(self, response.status_code, {"status": "error", "id": task_id})
                return

            json_response(self, 400, {"status": "error", "error": f"unknown aiming: {aiming}"})
        except Exception as exc:
            print(f"[bridge] GET failed: {exc}")
            json_response(self, 500, {"status": "error", "error": str(exc)})


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
