"""Helpers shared by finders."""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime

from ..model import as_utc

BUILD_RE = re.compile(r"^(\d+)([A-Z])(\d+)([a-z]*)$")


def build_key(build: str) -> tuple:
    """Order Apple build strings: 23F77 < 23F84 < 24A5370g < 24A5390f < 24A5408d."""
    m = BUILD_RE.match(build or "")
    if not m:
        return (0, "", 0, build or "")
    major, letter, num, suffix = m.groups()
    return (int(major), letter, int(num), suffix)


def version_tuple(v: str) -> tuple[int, ...]:
    parts = []
    for p in (v or "").split("."):
        try:
            parts.append(int(p))
        except ValueError:
            break
    return tuple(parts)


def now() -> datetime:
    return datetime.now(UTC)


def age_days(dt) -> int | None:
    dt = as_utc(dt)
    if dt is None:
        return None
    return max(0, (now() - dt).days)


def when(dt) -> str:
    """'2026-07-17 (33d)' or 'never'."""
    dt = as_utc(dt)
    if dt is None:
        return "never"
    return f"{dt.date().isoformat()} ({age_days(dt)}d)"


def mtime_of(path) -> datetime | None:
    try:
        return datetime.fromtimestamp(os.lstat(path).st_mtime, tz=UTC)
    except OSError:
        return None


def file_url_path(v) -> str | None:
    """images.plist stores URLs as {'relative': 'file:///...'}; accept str too."""
    if isinstance(v, dict):
        v = v.get("relative") or v.get("path")
    if not isinstance(v, str):
        return None
    if v.startswith("file://"):
        from urllib.parse import unquote, urlparse

        return unquote(urlparse(v).path)
    return v


def listdir(path) -> list[str]:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []
