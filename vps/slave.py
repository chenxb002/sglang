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
from typing import Dict, Optional
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import cfg, json_response, load_config, make_task_id, read_json_body, text_response


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
LOG_TAIL_LINES = 200


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
    log_offset: str = "0"
    log_status: str = "pending"
    log_error: str = ""
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
            log_offset=str(data.get("log_offset", "0")),
            log_status=str(data.get("log_status", "pending")),
            log_error=str(data.get("log_error", "")),
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
        log_dir: Path,
    ):
        self.db_path = db_path
        self.log_dir = log_dir
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
                        task.log_status = "failed"
                        task.log_error = task.log
                        task.updated_at = now
                        changed += 1
                    continue

                if (
                    self.terminal_retention_seconds > 0
                    and now - task.updated_at >= self.terminal_retention_seconds
                ):
                    del self.tasks[task_id]
                    self.delete_log(task_id)
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
                task.log_offset = old.log_offset
                task.log_status = old.log_status
                task.log_error = old.log_error
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
            self.delete_log(task_id)
            self.save()
            return existed

    def log_path(self, task_id: str) -> Path:
        return self.log_dir / f"{task_id}.log"

    def append_log_chunk(
        self,
        task_id: str,
        chunk: str,
        start_offset: str,
        next_offset: str,
    ):
        with self.lock:
            self.cleanup_expired()
            task = self.tasks.get(task_id)
            if not task:
                return None, 0
            if next_offset and task.log_offset == next_offset:
                return task, self.log_size(task_id)
            if start_offset and task.log_offset != start_offset:
                raise ValueError(
                    f"log offset mismatch: current={task.log_offset}, "
                    f"start={start_offset}, next={next_offset}"
                )
            self.log_dir.mkdir(parents=True, exist_ok=True)
            if chunk:
                with self.log_path(task_id).open("a", encoding="utf-8") as f:
                    f.write(chunk)
                task.log = "\n".join(
                    (task.log + "\n" + chunk).strip("\n").splitlines()[-LOG_TAIL_LINES:]
                )
            if next_offset:
                task.log_offset = next_offset
            task.log_status = "syncing"
            task.log_error = ""
            task.updated_at = time.time()
            self.save()
            return task, self.log_size(task_id)

    def read_log(self, task_id: str, tail_lines: Optional[int] = None) -> str:
        path = self.log_path(task_id)
        if not path.is_file():
            return ""
        content = path.read_text(encoding="utf-8", errors="replace")
        if tail_lines is None or tail_lines <= 0:
            return content
        lines = content.splitlines()
        tail = "\n".join(lines[-tail_lines:])
        if tail and content.endswith("\n"):
            tail += "\n"
        return tail

    def log_size(self, task_id: str) -> int:
        path = self.log_path(task_id)
        return path.stat().st_size if path.is_file() else 0

    def delete_log(self, task_id: str) -> None:
        self.log_path(task_id).unlink(missing_ok=True)


def request_params(path: str) -> Dict[str, str]:
    parsed = urlparse(path)
    parts = [part for part in (parsed.path.lstrip("/"), parsed.query) if part]
    return {k: v[0] for k, v in parse_qs("&".join(parts)).items()}


class SlaveHandler(BaseHTTPRequestHandler):
    store: TaskStore

    def log_message(self, fmt, *args):  # noqa: D401 - BaseHTTPRequestHandler hook
        print("[slave] " + fmt % args)

    def read_text_body(self) -> str:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            return ""
        return self.rfile.read(content_length).decode("utf-8", errors="replace")

    def do_POST(self):
        try:
            params = request_params(self.path)
            if params.get("source") == "master" and params.get("aiming") == "append_log":
                self.handle_master_append_log(params)
                return

            data = read_json_body(self)
            source = data.get("source")
            if source == "bridge":
                self.handle_bridge_submit_task(data)
                return

            if source == "master":
                self.handle_master_update_task(data)
                return

            json_response(self, 400, {"status": "error", "error": f"unknown source: {source}"})
        except Exception as exc:
            print(f"[slave] POST failed: {exc}")
            json_response(self, 500, {"status": "error", "error": str(exc)})

    def handle_master_append_log(self, params: Dict[str, str]) -> None:
        """Handle a Jenkins log chunk appended by internal master.py.

        Scenario: master has pulled incremental Jenkins console text and sends
        it as text/plain, not JSON. The slave appends it to the task log file,
        updates the recent log tail and log offset, and rejects stale offsets so
        master retries do not duplicate log chunks.
        """
        task_id = params.get("id", "")
        chunk = self.read_text_body()
        try:
            task, log_size = self.store.append_log_chunk(
                task_id,
                chunk,
                start_offset=params.get("log_start_offset", ""),
                next_offset=params.get("log_offset", ""),
            )
        except ValueError as exc:
            json_response(self, 409, {"status": "error", "error": str(exc)})
            return
        if not task:
            json_response(self, 404, {"status": "error", "error": f"unknown task: {task_id}"})
            return
        json_response(
            self,
            200,
            {
                "status": "success",
                "id": task.id,
                "log_offset": task.log_offset,
                "log_size": log_size,
            },
        )

    def handle_bridge_submit_task(self, data) -> None:
        """Handle a new CI task submitted by bridge.py.

        Scenario: bridge forwards GitHub Actions metadata as JSON. The slave
        converts it to a Task, upserts it into the local DB, and returns the
        task id that GitHub Actions will use for status polling.
        """
        task = self.store.upsert(Task.from_dict(data))
        json_response(self, 200, {"status": "success", "id": task.id})

    def handle_master_update_task(self, data) -> None:
        """Handle a lightweight task status update from internal master.py.

        Scenario: master updates CI status metadata such as working/final
        status, Jenkins build id, short result text, and independent log sync
        state. Full Jenkins log chunks and log offsets are handled by
        handle_master_append_log() and are not sent here.
        """
        task_id = str(data.get("id", ""))
        if not self.store.get(task_id):
            json_response(self, 404, {"status": "error", "error": f"unknown task: {task_id}"})
            return
        task = self.store.update(
            task_id,
            status=data.get("status"),
            log=data.get("log"),
            inner_id=data.get("inner_id"),
            log_status=data.get("log_status"),
            log_error=data.get("log_error"),
        )
        json_response(
            self,
            200,
            {
                "status": "success",
                "id": task.id,
                "task_status": task.status,
                "log": task.log,
                "log_offset": task.log_offset,
                "log_status": task.log_status,
                "log_error": task.log_error,
                "log_size": self.store.log_size(task_id),
            },
        )

    def do_GET(self):
        try:
            params = request_params(self.path)
            source = params.get("source")
            aiming = params.get("aiming")
            task_id = params.get("id", "")

            if source == "master" and aiming == "get_data":
                self.handle_master_get_data()
                return

            if source == "bridge" and aiming == "get_status":
                self.handle_bridge_get_status(task_id)
                return

            if source == "bridge" and aiming == "get_log":
                self.handle_bridge_get_log(params, task_id)
                return

            if source == "bridge" and aiming == "end_job":
                self.handle_bridge_end_job(task_id)
                return

            json_response(self, 400, {"status": "error", "error": "unsupported request"})
        except Exception as exc:
            print(f"[slave] GET failed: {exc}")
            json_response(self, 500, {"status": "error", "error": str(exc)})

    def handle_master_get_data(self) -> None:
        """Handle internal master.py polling for active tasks.

        Scenario: master asks for runnable tasks. The slave returns every task
        whose status is not terminal, including enough metadata for master to
        submit or continue monitoring the corresponding Jenkins build.
        """
        json_response(self, 200, {"tasks": [asdict(task) for task in self.store.active_tasks()]})

    def handle_bridge_get_status(self, task_id: str) -> None:
        """Handle bridge.py status queries from GitHub Actions.

        Scenario: GitHub Actions polls task status through bridge. The slave
        returns lightweight metadata only: status, recent log tail, Jenkins
        build id, log sync state, log offset, and complete log size.
        """
        task = self.store.get(task_id)
        if not task:
            json_response(self, 404, {"status": "error", "error": f"unknown task: {task_id}"})
            return
        json_response(
            self,
            200,
            {
                "id": task.id,
                "status": task.status,
                "log": task.log,
                "inner_id": task.inner_id,
                "log_offset": task.log_offset,
                "log_status": task.log_status,
                "log_error": task.log_error,
                "log_size": self.store.log_size(task_id),
            },
        )

    def handle_bridge_get_log(self, params: Dict[str, str], task_id: str) -> None:
        """Handle bridge.py full-log download requests from GitHub Actions.

        Scenario: GitHub Actions downloads the Jenkins console log for artifact
        upload, or requests tail=N for failure output. The response is plain
        text and never JSON-wraps the log body.
        """
        if not self.store.get(task_id):
            json_response(self, 404, {"status": "error", "error": f"unknown task: {task_id}"})
            return
        try:
            tail = int(params.get("tail", "0") or "0")
        except ValueError:
            json_response(self, 400, {"status": "error", "error": "tail must be an integer"})
            return
        text_response(self, 200, self.store.read_log(task_id, tail_lines=tail or None))

    def handle_bridge_end_job(self, task_id: str) -> None:
        """Handle bridge.py cleanup after GitHub Actions has downloaded logs.

        Scenario: GitHub Actions has reached a terminal state and has already
        downloaded the full log. The slave deletes the task and its local log
        file immediately instead of waiting for retention cleanup.
        """
        deleted = self.store.delete(task_id)
        json_response(
            self,
            200 if deleted else 404,
            {"status": "success" if deleted else "missing", "id": task_id},
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run SGLang MLU CI slave")
    parser.add_argument("conf", nargs="?", help="Path to external_ci.conf")
    args = parser.parse_args()

    config = load_config(args.conf or str(Path(__file__).with_name("external_ci.conf")))
    slave_bind_host = cfg(config, "SlaveServer", "bind_host", "")
    slave_port = int(cfg(config, "SlaveServer", "port", "14548"))
    default_db = str(Path(__file__).with_name("sglang_tasks.json"))
    db_path = Path(cfg(config, "SlaveServer", "db_path", default_db))
    default_log_dir = str(db_path.parent / "logs")
    log_dir = Path(cfg(config, "SlaveServer", "log_dir", default_log_dir))
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
        log_dir=log_dir,
    )

    server = ThreadingHTTPServer((slave_bind_host, slave_port), SlaveHandler)
    print(
        f"[slave] listening on {slave_bind_host or '0.0.0.0'}:{slave_port}, db={db_path}, "
        f"log_dir={log_dir}, "
        f"terminal_retention_seconds={terminal_retention_seconds}, "
        f"active_task_timeout_seconds={active_task_timeout_seconds}"
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
