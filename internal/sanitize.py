#!/usr/bin/env python3
"""Sanitize Jenkins console logs before they leave the internal network.

Patterns are applied in order.  Each pattern is a ``(compiled_regex, replacement)``
tuple where *replacement* may be a string or a callable (for same-length redaction).
"""

from __future__ import annotations

import re
from typing import Callable, List, Tuple, Union

Replacement = Union[str, Callable[[re.Match], str]]
PatternList = List[Tuple[re.Pattern, Replacement]]

SECRET_PATTERNS: PatternList = [
    # ── Credentials ───────────────────────────────────────────────────────
    (re.compile(r"(?i)(authorization:\s*)\S+"), r"\1***"),
    (re.compile(r"(?i)(token\s*[=:]\s*)\S+"), r"\1***"),
    (re.compile(r"(?i)(password\s*[=:]\s*)\S+"), r"\1***"),
    (re.compile(r"(?i)(secret\s*[=:]\s*)\S+"), r"\1***"),
    # ── Device hardware identifiers (cnmon output) ────────────────────────
    (re.compile(r"(SN\s*:\s*)\S+"), r"\1***"),
    (re.compile(r"(UUID\s*:\s*)\S+"), r"\1***"),
    (re.compile(r"(Firmware\s*:\s*)\S+"), r"\1***"),
    # MLU card model names — preserve MLU prefix, same-length replacement
    #   MLU590-M9DK  →  MLU********
    #   MLU500       →  MLU***
    (re.compile(r"MLU\d{3}(?:-[A-Za-z0-9]+)?"),
     lambda m: "MLU" + "*" * (len(m.group()) - 3)),
    # ── Internal infrastructure names ─────────────────────────────────────
    (re.compile(r"\S+\.svc\.cluster\.local\S*"), "***"),
    (re.compile(r"cam-test-ai\d+"), "***"),
    # ── Kubernetes-generated pod names ────────────────────────────────────
    # Pod suffixes always contain digits (e.g. wjk09, s59c1), so require
    # at least one digit in each trailing random segment.  The base part
    # may also contain digits (mlu500) — use a lazy quantifier so the base
    # doesn't eat the two suffix segments.  This avoids matching English-
    # word compounds like pydantic-extra-types.
    (re.compile(
        r"(cncl/)[a-z][a-z0-9]*(?:-[a-z][a-z0-9]*)*?"
        r"-[a-z0-9]*[0-9][a-z0-9]*-[a-z0-9]*[0-9][a-z0-9]*",
    ), r"\1***"),
    (re.compile(
        r"\b[a-z][a-z0-9]*(?:-[a-z][a-z0-9]*)*?"
        r"-[a-z0-9]*[0-9][a-z0-9]*-[a-z0-9]*[0-9][a-z0-9]*\b",
    ), "***"),
    # Pod template name (single random suffix, e.g. mlu500-h4dt5)
    (re.compile(
        r"(from template\s+)[a-z][a-z0-9]*(?:-[a-z][a-z0-9]*)*?"
        r"-[a-z0-9]*[0-9][a-z0-9]*\b",
    ), r"\1***"),
]


def redact_log(text: str) -> str:
    """Apply all SECRET_PATTERNS and return the sanitised string."""
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text
