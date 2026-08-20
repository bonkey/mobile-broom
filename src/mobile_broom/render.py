"""Text report. Totals are 'candidates' — each path sized on its own, APFS clones may overlap."""

from __future__ import annotations

import shlex
import sys
import textwrap
from collections import defaultdict

from .model import GROUPS, VERDICTS, Finding
from .sizer import human

COLORS = {"dead": "\033[31m", "stale": "\033[33m", "shared": "\033[34m", "review": "\033[2m"}
RESET = "\033[0m"
BOLD = "\033[1m"


def _c(verdict: str, text: str, color: bool) -> str:
    return f"{COLORS[verdict]}{text}{RESET}" if color else text


def render(
    findings: list[Finding], out=None, color: bool | None = None, width: int | None = None
) -> None:
    out = out or sys.stdout
    tty = out.isatty()
    if color is None:
        color = tty
    if width is None:
        try:
            import shutil

            width = shutil.get_terminal_size().columns
        except (OSError, ValueError):
            width = 120
    by_group: dict[str, dict[str, list[Finding]]] = defaultdict(lambda: defaultdict(list))
    for f in findings:
        by_group[f.group][f.category].append(f)
    totals: dict[str, int] = defaultdict(int)
    counts: dict[str, int] = defaultdict(int)
    for g in GROUPS:
        if g not in by_group:
            continue
        out.write((BOLD if color else "") + g + (RESET if color else "") + "\n")
        for cat in GROUPS[g]:
            items = by_group[g].get(cat)
            if not items:
                continue
            cat_total = sum(f.size or 0 for f in items)
            out.write(f"  {cat}  ({len(items)}, {human(cat_total).strip()})\n")
            for f in items:
                totals[f.verdict] += f.size or 0
                counts[f.verdict] += 1
                tag = _c(f.verdict, f"{f.verdict:<6}", color)
                head = f"    {tag} {human(f.size)}  {f.label}"
                plain_len = len(head) - (len(COLORS[f.verdict]) + len(RESET) if color else 0)
                ev = f.evidence
                if not tty or plain_len + 3 + len(ev) <= width:
                    pad = max(1, 48 - len(f.label)) if tty else 2
                    out.write(head + " " * pad + (f"— {ev}" if ev else "") + "\n")
                else:
                    out.write(head + "\n")
                    for i, ln in enumerate(textwrap.wrap(ev, max(20, width - 20))):
                        out.write(" " * 18 + ("— " if i == 0 else "  ") + ln + "\n")
                if f.action and f.action.kind == "print":
                    out.write(f"           manual: {shlex.join(f.action.argv or [])}\n")
        out.write("\n")
    if not findings:
        out.write("nothing found\n")
        return
    parts = [
        f"{_c(v, v, color)} {human(totals[v]).strip()} ({counts[v]})" for v in VERDICTS if counts[v]
    ]
    out.write("candidates: " + " · ".join(parts) + "\n")
    out.write("  (each path sized on its own; APFS clones may overlap — not a reclaim promise)\n")
