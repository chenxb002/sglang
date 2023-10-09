#!/usr/bin/env python3
"""Jenkins launcher/monitor for SGLang MLU CI tasks."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, Optional
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from requests import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import cfg, env_or_cfg, load_config, normalize_url


FIELD_MAP = {
    "repo": "repo",
    "timestamp": "timestamp",
    "pr_id": "pr_id",
    "repo_url": "repo_url",
    "git_ref": "git_ref",
    "commit_sha": "commit_sha",
    "task_id": "id",
    "id": "id",
    "trigger_type": "trigger_type",
    "trigger_id": "trigger_id",
    "repeat_times": "repeat_times",
}

TERMINAL_JENKINS_RESULTS = {"SUCCESS", "FAILURE", "UNSTABLE", "ABORTED"}


class JenkinsClient:
    def __init__(self, base_url: str, user: str, token: str):
        self.base_url = normalize_url(base_url) + "/"
        self.auth = (user, token) if user or token else None
        self.session = requests.Session()
        self._crumb_headers: Optional[Dict[str, str]] = None

    def root_url(self) -> str:
        parsed = urlparse(self.base_url)
        root_path = parsed.path.split("/job/")[0].rstrip("/") + "/"
        return urlunparse((parsed.scheme, parsed.netloc, root_path, "", "", ""))

    def crumb_headers(self) -> Dict[str, str]:
        if self._crumb_headers is not None:
            return self._crumb_headers
        try:
            response = self.session.get(
                urljoin(self.root_url(), "crumbIssuer/api/json"),
                auth=self.auth,
                timeout=30,
            )
            if response.status_code == 404:
                self._crumb_headers = {}
                return self._crumb_headers
            response.raise_for_status()
            data = response.json()
            self._crumb_headers = {data["crumbRequestField"]: data["crumb"]}
        except Exception as exc:
            print(f"[master] failed to get Jenkins crumb, continue without it: {exc}")
            self._crumb_headers = {}
        return self._crumb_headers

    def build(self, params: Dict[str, str]) -> Optional[str]:
        response = self.session.post(
            urljoin(self.base_url, "buildWithParameters"),
            auth=self.auth,
            data=params,
            headers=self.crumb_headers(),
            allow_redirects=False,
            timeout=60,
        )
        response.raise_for_status()
        queue_url = response.headers.get("Location", "")
        if queue_url:
            return self.wait_for_queue_executable(queue_url)
        return None

    def wait_for_queue_executable(self, queue_url: str, timeout_seconds: int = 120) -> Optional[str]:
        deadline = time.time() + timeout_seconds
        api_url = urljoin(queue_url.rstrip("/") + "/", "api/json")
        while time.time() < deadline:
            response = self.session.get(api_url, auth=self.auth, timeout=60)
            response.raise_for_status()
            data = response.json()
            if data.get("cancelled"):
                raise RuntimeError(f"Jenkins queue item was cancelled: {queue_url}")
            executable = data.get("executable") or {}
            build_number = executable.get("number")
            if build_number is not None:
                return str(build_number)
            time.sleep(2)
        return None

    def builds(self):
        response = self.session.get(
            urljoin(self.base_url, "api/json?tree=builds[url,result,id]"),
            auth=self.auth,
            timeout=60,
        )
        response.raise_for_status()
        return response.json().get("builds", [])

    def build_info(self, build_url_or_id: str):
        if build_url_or_id.startswith("http://") or build_url_or_id.startswith("https://"):
            url = urljoin(build_url_or_id.rstrip("/") + "/", "api/json")
        else:
            url = urljoin(self.base_url, f"{build_url_or_id}/api/json")
        response = self.session.get(url, auth=self.auth, timeout=60)
        response.raise_for_status()
        return response.json(), response.text

    def find_build_id_by_task_id(self, task_id: str) -> Optional[str]:
        for build in self.builds():
            build_url = build.get("url")
            if not build_url:
                continue
            info, _ = self.build_info(build_url)
            for action in info.get("actions", []):
                for param in action.get("parameters", []) or []:
                    if param.get("name") == "task_id" and str(param.get("value")) == task_id:
                        return str(info.get("id") or build.get("id") or "")
        return None


def post_update(slave_url: str, task_id: str, status: str, log: str, inner_id: str = "") -> None:
    payload = {
        "source": "master",
        "id": task_id,
        "status": status,
        "log": log,
        "inner_id": inner_id,
    }
    for _ in range(10):
        try:
            response = requests.post(slave_url, json=payload, timeout=30)
            if response.status_code == 200:
                return
            print(f"[master] slave update returned {response.status_code}: {response.text}")
        except Exception as exc:
            print(f"[master] slave update failed: {exc}")
        time.sleep(3)
    raise RuntimeError(f"failed to update slave task {task_id} -> {status}")


def classify_failure(response_text: str) -> tuple[str, str]:
    if "stage4" in response_text or "mlu_ci_task" in response_text:
        return (
            "test_fail",
            "SGLang MLU Jenkins job failed during test. Check the archived logs and Jenkins build details.",
        )
    if "stage0" in response_text or "clone_sglang_task" in response_text:
        return "clone_fail", "SGLang MLU Jenkins job failed while cloning or checking out code."
    if "stage1" in response_text:
        return "lint_check_fail", "SGLang MLU Jenkins job failed during lint."
    if "stage2" in response_text:
        return "build_fail", "SGLang MLU Jenkins job failed during build/install."
    if "stage3" in response_text:
        return "search_case_fail", "SGLang MLU Jenkins job failed while preparing test cases."
    return "internal_error", "SGLang MLU Jenkins job failed, but the failed stage could not be identified."


def params_for_task(task: Dict[str, str], jenkins_params: Iterable[str]) -> Dict[str, str]:
    params = {}
    for name in jenkins_params:
        source_field = FIELD_MAP.get(name, name)
        params[name] = str(task.get(source_field, ""))
    return params


def process_once(slave_url: str, jenkins: JenkinsClient, jenkins_params: list[str]) -> None:
    response = requests.get(f"{slave_url}/source=master&aiming=get_data", timeout=60)
    response.raise_for_status()
    tasks = response.json().get("tasks", [])
    if not tasks:
        print("[master] no active task")
        return

    for task in tasks:
        task_id = task["id"]
        status = task.get("status", "running")
        inner_id = task.get("inner_id", "")

        if status == "running" and not inner_id:
            params = params_for_task(task, jenkins_params)
            print(f"[master] submit Jenkins task_id={task_id}, trigger_type={task.get('trigger_type', 'ci')}")
            try:
                inner_id = jenkins.build(params) or ""
            except HTTPError as exc:
                status_code = exc.response.status_code if exc.response is not None else 0
                if status_code in {401, 403}:
                    post_update(
                        slave_url,
                        task_id,
                        "error",
                        "SGLang MLU CI failed before Jenkins launch: Jenkins "
                        f"authentication/permission check returned HTTP {status_code}.",
                    )
                    continue
                raise
            if inner_id:
                log = f"Jenkins build id is {inner_id}. The task is running in the SGLang MLU Jenkins pipeline."
                post_update(slave_url, task_id, "working", log, inner_id)
            else:
                post_update(slave_url, task_id, "waiting", "Jenkins job submitted; waiting for build id.")

        if not inner_id:
            inner_id = jenkins.find_build_id_by_task_id(task_id)
            if inner_id:
                log = f"Jenkins build id is {inner_id}. The task is running in the SGLang MLU Jenkins pipeline."
                post_update(slave_url, task_id, "working", log, inner_id)
            else:
                print(f"[master] waiting for Jenkins build id, task_id={task_id}")
                continue

        info, text = jenkins.build_info(inner_id)
        result = info.get("result")
        if info.get("building"):
            print(f"[master] Jenkins build {inner_id} still running, result={result}")
            continue
        if result not in TERMINAL_JENKINS_RESULTS:
            print(f"[master] Jenkins build {inner_id} still running, result={result}")
            continue

        inner_msg = f" Jenkins build id: {inner_id}."
        if result == "SUCCESS":
            post_update(slave_url, task_id, "success", "SGLang MLU CI passed." + inner_msg, inner_id)
        elif result == "UNSTABLE":
            post_update(slave_url, task_id, "unstable", "SGLang MLU CI is unstable." + inner_msg, inner_id)
        elif result == "ABORTED":
            post_update(slave_url, task_id, "error", "SGLang MLU CI Jenkins build was aborted." + inner_msg, inner_id)
        else:
            ci_status, message = classify_failure(text)
            post_update(slave_url, task_id, ci_status, message + inner_msg, inner_id)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run SGLang MLU CI Jenkins master")
    parser.add_argument("conf", nargs="?", help="Path to internal_master.conf")
    parser.add_argument("--once", action="store_true", help="Process active tasks once and exit")
    parser.add_argument("--interval", type=int, default=10, help="Loop interval in seconds")
    args = parser.parse_args()

    config = load_config(args.conf or str(Path(__file__).with_name("internal_master.conf")))
    slave_host = cfg(config, "SlaveServer", "host", "localhost")
    slave_port = cfg(config, "SlaveServer", "port", "14548")
    slave_url = f"http://{slave_host}:{slave_port}"

    jenkins_path = env_or_cfg(config, "SGLANG_JENKINS_PATH", "MasterServer", "jenkins_path")
    jenkins_user = env_or_cfg(config, "SGLANG_JENKINS_USER", "MasterServer", "jenkins_user")
    jenkins_token = env_or_cfg(config, "SGLANG_JENKINS_TOKEN", "MasterServer", "jenkins_token")
    if not jenkins_path:
        raise SystemExit("jenkins_path is required in conf or SGLANG_JENKINS_PATH")

    jenkins_params = [
        p.strip()
        for p in cfg(
            config,
            "MasterServer",
            "jenkins_params",
            "repo;timestamp;pr_id;task_id;trigger_type;repo_url;git_ref;commit_sha",
        ).split(";")
        if p.strip()
    ]
    jenkins = JenkinsClient(jenkins_path, jenkins_user, jenkins_token)

    while True:
        try:
            process_once(slave_url, jenkins, jenkins_params)
        except Exception as exc:
            print(f"[master] loop failed: {exc}")
        if args.once:
            break
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
