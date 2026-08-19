"""Run finders → size → sort. Shared by the CLI and the TUI."""

from __future__ import annotations

import sys

from . import finders
from .config import Config
from .env import Env
from .model import Finding, sort_key
from .sizer import Sizer


def collect(
    selectors: list[str] | None,
    env: Env,
    cfg: Config,
    sizer: Sizer | None = None,
    progress: bool = False,
) -> list[Finding]:
    cats = finders.resolve(selectors)
    found = finders.run(cats, env, cfg)
    if sizer is not None:
        cb = None
        if progress and sys.stderr.isatty():

            def cb(i, n, f):
                sys.stderr.write(f"\r  sizing {i}/{n} {f.label[:50]:<50}")
                sys.stderr.flush()
                if i == n:
                    sys.stderr.write("\r" + " " * 70 + "\r")

        sizer.size_findings(found, progress=cb)
    found.sort(key=sort_key)
    return found


def select_for_clean(
    findings: list[Finding], stale: bool = False, shared: bool = False
) -> list[Finding]:
    want = {"dead"}
    if stale:
        want.add("stale")
    if shared:
        want.add("shared")
    return [f for f in findings if f.verdict in want and f.action is not None]
