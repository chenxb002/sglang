#!/usr/bin/env python3
"""Shared helpers for the SGLang MLU CI bridge."""

from __future__ import annotations

import configparser
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlparse


DEFAULT_CONF = Path(__file__).with_name("sglang_ci.conf")


def load_config(conf_path: Optional[str]) -> configparser.ConfigParser:
    path = Path(conf_path) if conf_path else DEFAULT_CONF
    if path.suffix != ".conf":
        raise SystemExit(f"Configuration file must end with .conf: {path}")
    if not path.is_file():
        raise SystemExit(f"Configuration file not found: {path}")

    config = configparser.ConfigParser()
    config.read(path)
    return config


def cfg(config: configparser.ConfigParser, section: str, option: str, default: str = "") -> str:
    if not config.has_option(section, option):
        return default
    return config.get(section, option).strip().strip('"').strip("'")


def make_task_id(values: Iterable[Optional[str]]) -> str:
    raw = "".join(value or "" for value in values)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def json_response(handler, status_code: int, payload: Dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def text_response(handler, status_code: int, text: str) -> None:
    body = text.encode("utf-8")
    handler.send_response(status_code)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def read_json_body(handler) -> Dict[str, Any]:
    content_length = int(handler.headers.get("Content-Length", "0"))
    if content_length <= 0:
        return {}
    body = handler.rfile.read(content_length).decode("utf-8")
    return json.loads(body)


def normalize_url(value: str, default_scheme: str = "http") -> str:
    value = value.strip().strip('"').strip("'")
    if not value:
        return value
    parsed = urlparse(value)
    if not parsed.scheme:
        value = f"{default_scheme}://{value}"
    return value.rstrip("/")


def env_or_cfg(config: configparser.ConfigParser, env_name: str, section: str, option: str, default: str = "") -> str:
    return os.environ.get(env_name) or cfg(config, section, option, default)
