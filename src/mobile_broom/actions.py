"""Acting on findings. Official CLIs first, real delete by default (--trash moves to
~/.Trash instead), never sudo, dry-run everywhere. Manual commands are shell-quoted."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .env import is_root_owned
from .model import Action, Finding
from .sizer import human


@dataclass
class Result:
    finding: Finding
    ok: bool
    note: str


def trash_dir() -> Path:
    return Path(os.environ.get("MOBILE_BROOM_TRASH") or Path.home() / ".Trash")


def remove(path: str, trash: bool = False) -> str:
    p = Path(path)
    if not p.exists() and not p.is_symlink():
        return "already gone"
    if not trash:
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()
        return "deleted"
    dest_dir = trash_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / p.name
    if dest.exists() or dest.is_symlink():
        dest = dest_dir / f"{p.name} {time.strftime('%H.%M.%S')}"
        n = 1
        while dest.exists():
            n += 1
            dest = dest_dir / f"{p.name} {time.strftime('%H.%M.%S')}-{n}"
    try:
        os.rename(p, dest)
    except OSError:
        shutil.move(str(p), str(dest))
    return f"→ {dest}"


def effective(action: Action) -> Action:
    """Downgrade a remove action on a root-owned path to a printed command."""
    if action.kind == "remove" and action.path and is_root_owned(action.path):
        return Action(kind="print", argv=["sudo", "rm", "-rf", action.path])
    return action


def plan(findings: list[Finding]) -> list[tuple[Action, list[Finding]]]:
    """Dedupe identical actions (e.g. `simctl delete unavailable` shared by N devices)."""
    seen: dict[tuple, tuple[Action, list[Finding]]] = {}
    for f in findings:
        if not f.action:
            continue
        a = effective(f.action)
        key = (a.kind, tuple(a.argv or []), a.path, tuple(a.extra_paths or []))
        if key in seen:
            seen[key][1].append(f)
        else:
            seen[key] = (a, [f])
    return list(seen.values())


def describe_plan(steps, out=None) -> None:
    out = out or sys.stdout
    total = 0
    for a, fs in steps:
        size = sum(f.size or 0 for f in fs)
        total += size
        labels = ", ".join(f.label for f in fs[:3]) + (f" +{len(fs) - 3}" if len(fs) > 3 else "")
        out.write(f"  {human(size)}  {a.describe()}    [{labels}]\n")
    out.write(f"  candidates: {human(total).strip()} across {len(steps)} action(s)\n")


def execute(
    findings: list[Finding], dry_run: bool = False, trash: bool = False, out=None, runner=None
) -> list[Result]:
    out = out or sys.stdout
    runner = runner or (
        lambda argv: subprocess.run(argv, capture_output=True, text=True, check=False)
    )
    results: list[Result] = []
    for a, fs in plan(findings):
        if a.kind == "print":
            out.write(f"  manual  {shlex.join(a.argv or [])}   (no safe actor; run yourself)\n")
            results.extend(Result(f, False, "manual") for f in fs)
            continue
        if dry_run:
            if a.kind == "argv" and a.dry_run_argv:
                r = runner(a.dry_run_argv)
                tail = (r.stdout or r.stderr or "").strip().splitlines()
                out.write(f"  would   {a.describe()}" + (f"  ⇒ {tail[-1]}" if tail else "") + "\n")
            elif a.kind == "remove" and trash:
                out.write(f"  would   trash {a.path}\n")
            else:
                out.write(f"  would   {a.describe()}\n")
            results.extend(Result(f, True, "dry-run") for f in fs)
            continue
        try:
            if a.kind == "remove":
                notes = [remove(pp, trash=trash) for pp in [a.path, *(a.extra_paths or [])]]
                note = "; ".join(notes)
                ok = True
            else:
                r = runner(a.argv)
                ok = r.returncode == 0
                note = ((r.stdout if ok else r.stderr) or "").strip().splitlines()
                note = note[-1] if note else ("ok" if ok else f"exit {r.returncode}")
        except Exception as e:  # noqa: BLE001
            ok, note = False, str(e)
        out.write(f"  {'ok  ' if ok else 'FAIL'}    {a.describe()}  {note}\n")
        results.extend(Result(f, ok, note) for f in fs)
    return results
