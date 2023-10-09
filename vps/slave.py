#!/usr/bin/env python3
"""Local task store for SGLang MLU CI.

The slave keeps task state for the bridge and master. It persists state to a
small JSON file so the master can restart without losing submitted Jenkins jobs.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import cfg, json_response, load_config, make_task_id, read_json_body


TERMINAL_STATUSES = {
    "success",
    "test_fail",
    "clone_fail",
    "lint_check_fail",
    "build_fail",
    "search_case_fail",
    "internal_error",
    "internel_error",
    "unstable",
    "error",
}


@dataclass
class Task:
    timestamp: str
    repo: str
    pr_id: str
    repo_url: str
    git_ref: str
    commit_sha: str
    trigger_type: str
    trigger_id: str
    repeat_times: str
    status: str = "running"
    log: str = ""
    id: str = ""
    inner_id: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def __post_init__(self):
        if not self.id:
            self.id = make_task_id(
                [
                    self.timestamp,
                    self.repo,
                    self.pr_id,
                    self.repo_url,
                    self.git_ref,
                    self.commit_sha,
                    self.trigger_type,
                    self.trigger_id,
                ]
            )

    @classmethod
    def from_dict(cls, data):
        return cls(
            timestamp=str(data.get("timestamp", "")),
            repo=str(data.get("repo", "")),
            pr_id=str(data.get("pr_id", "")),
            repo_url=str(data.get("repo_url", "")),
            git_ref=str(data.get("git_ref", "")),
            commit_sha=str(data.get("commit_sha", "")),
            trigger_type=str(data.get("trigger_type", "ci")),
            trigger_id=str(data.get("trigger_id", "")),
            repeat_times=str(data.get("repeat_times", "1")),
            status=str(data.get("status", "running")),
            log=str(data.get("log", "")),
            id=str(data.get("id", "")),
            inner_id=str(data.get("inner_id", "")),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
        )

    def is_active(self) -> bool:
        return self.status not in TERMINAL_STATUSES


class TaskStore:
    def __init__(
        self,
        db_path: Path,
        terminal_retention_seconds: int,
        active_task_timeout_seconds: int,
    ):
        self.db_path = db_path
        self.terminal_retention_seconds = terminal_retention_seconds
        self.active_task_timeout_seconds = active_task_timeout_seconds
        self.lock = threading.RLock()
        self.tasks: Dict[str, Task] = {}
        self.load()
        self.cleanup_expired()

    def load(self):
        with self.lock:
            if not self.db_path.is_file():
                return
            data = json.loads(self.db_path.read_text(encoding="utf-8"))
            self.tasks = {item["id"]: Task.from_dict(item) for item in data.get("tasks", [])}

    def save(self):
        with self.lock:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.db_path.with_suffix(self.db_path.suffix + ".tmp")
            tmp_path.write_text(
                json.dumps({"tasks": [asdict(t) for t in self.tasks.values()]}, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(self.db_path)

    def cleanup_expired(self) -> int:
        """Mark timed-out active tasks as errors and delete old terminal tasks."""
        with self.lock:
            now = time.time()
            changed = 0
            for task_id, task in list(self.tasks.items()):
                if task.is_active():
                    if (
                        self.active_task_timeout_seconds > 0
                        and now - task.created_at >= self.active_task_timeout_seconds
                    ):
                        task.status = "error"
                        task.log = (
                            "SGLang MLU CI task timed out in the external runner "
                            f"after {self.active_task_timeout_seconds} seconds."
                        )
                        task.updated_at = now
                        changed += 1
                    continue

                if (
                    self.terminal_retention_seconds > 0
                    and now - task.updated_at >= self.terminal_retention_seconds
                ):
                    del self.tasks[task_id]
                    changed += 1

            if changed:
                self.save()
            return changed

    def upsert(self, task: Task):
        with self.lock:
            self.cleanup_expired()
            old = self.tasks.get(task.id)
            if old and old.status in {"waiting", "working"}:
                task.status = old.status
                task.log = old.log
                task.inner_id = old.inner_id
                task.created_at = old.created_at
            task.updated_at = time.time()
            self.tasks[task.id] = task
            self.save()
            return task

    def update(self, task_id: str, **updates):
        with self.lock:
            self.cleanup_expired()
            task = self.tasks[task_id]
            for key, value in updates.items():
                if hasattr(task, key) and value is not None:
                    setattr(task, key, str(value))
            task.updated_at = time.time()
            self.save()
            return task

    def get(self, task_id: str):
        with self.lock:
            self.cleanup_expired()
            return self.tasks.get(task_id)

    def active_tasks(self):
        with self.lock:
            self.cleanup_expired()
            return [task for task in self.tasks.values() if task.is_active()]

    def delete(self, task_id: str) -> bool:
        with self.lock:
            existed = task_id in self.tasks
            self.tasks.pop(task_id, None)
            self.save()
            return existed


class SlaveHandler(BaseHTTPRequestHandler):
    store: TaskStore

    def log_message(self, fmt, *args):  # noqa: D401 - BaseHTTPRequestHandler hook
        print("[slave] " + fmt % args)

    def do_POST(self):
        try:
            data = read_json_body(self)
            source = data.get("source")
            if source == "bridge":
                task = self.store.upsert(Task.from_dict(data))
                json_response(self, 200, {"status": "success", "id": task.id})
                return

            if source == "master":
                task_id = str(data.get("id", ""))
                if not self.store.get(task_id):
                    json_response(self, 404, {"status": "error", "error": f"unknown task: {task_id}"})
                    return
                task = self.store.update(
                    task_id,
                    status=data.get("status"),
                    log=data.get("log"),
                    inner_id=data.get("inner_id"),
                )
                json_response(
                    self,
                    200,
                    {"status": "success", "id": task.id, "task_status": task.status, "log": task.log},
                )
                return

            json_response(self, 400, {"status": "error", "error": f"unknown source: {source}"})
        except Exception as exc:
            print(f"[slave] POST failed: {exc}")
            json_response(self, 500, {"status": "error", "error": str(exc)})

    def do_GET(self):
        try:
            params = {k: v[0] for k, v in parse_qs(urlparse(self.path).path.lstrip("/")).items()}
            source = params.get("source")
            aiming = params.get("aiming")
            task_id = params.get("id", "")

            if source == "master" and aiming == "get_data":
                json_response(self, 200, {"tasks": [asdict(task) for task in self.store.active_tasks()]})
                return

            if source == "bridge" and aiming == "get_status":
                task = self.store.get(task_id)
                if not task:
                    json_response(self, 404, {"status": "error", "error": f"unknown task: {task_id}"})
                    return
                json_response(
                    self,
                    200,
                    {"id": task.id, "status": task.status, "log": task.log, "inner_id": task.inner_id},
                )
                return

            if source == "bridge" and aiming == "end_job":
                deleted = self.store.delete(task_id)
                json_response(self, 200 if deleted else 404, {"status": "success" if deleted else "missing", "id": task_id})
                return

            json_response(self, 400, {"status": "error", "error": "unsupported request"})
        except Exception as exc:
            print(f"[slave] GET failed: {exc}")
            json_response(self, 500, {"status": "error", "error": str(exc)})


def main() -> int:
    parser = argparse.ArgumentParser(description="Run SGLang MLU CI slave")
    parser.add_argument("conf", nargs="?", help="Path to external_ci.conf")
    args = parser.parse_args()

    config = load_config(args.conf or str(Path(__file__).with_name("external_ci.conf")))
    slave_bind_host = cfg(config, "SlaveServer", "bind_host", "")
    slave_port = int(cfg(config, "SlaveServer", "port", "14548"))
    default_db = str(Path(__file__).with_name("sglang_tasks.json"))
    db_path = Path(cfg(config, "SlaveServer", "db_path", default_db))
    terminal_retention_seconds = int(
        cfg(config, "SlaveServer", "terminal_retention_seconds", "86400")
    )
    active_task_timeout_seconds = int(
        cfg(config, "SlaveServer", "active_task_timeout_seconds", "172800")
    )
    SlaveHandler.store = TaskStore(
        db_path,
        terminal_retention_seconds=terminal_retention_seconds,
        active_task_timeout_seconds=active_task_timeout_seconds,
    )

    server = ThreadingHTTPServer((slave_bind_host, slave_port), SlaveHandler)
    print(
        f"[slave] listening on {slave_bind_host or '0.0.0.0'}:{slave_port}, db={db_path}, "
        f"terminal_retention_seconds={terminal_retention_seconds}, "
        f"active_task_timeout_seconds={active_task_timeout_seconds}"
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
