"""Worktree finders — report-only. `wt list` owns lifecycle; this adds the disk dimension."""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

from ..model import Finding
from . import register
from ._util import listdir, when
from .scan import git_dirs

ARTIFACT_DIRS = (
    ".build",
    "build",
    "node_modules",
    "Pods",
    "DerivedData",
    ".gradle",
    "SourcePackages",
    "Carthage/Build",
)


def _git(cwd: str, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout if out.returncode == 0 else ""


def worktrees_of(repo: str) -> list[dict]:
    """[{path, branch, bare, detached}] from `git worktree list --porcelain`."""
    out = []
    cur: dict = {}
    for line in _git(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            if cur:
                out.append(cur)
            cur = {"path": line[9:], "branch": None, "bare": False, "detached": False}
        elif line.startswith("branch "):
            cur["branch"] = line[7:].replace("refs/heads/", "")
        elif line == "bare":
            cur["bare"] = True
        elif line == "detached":
            cur["detached"] = True
    if cur:
        out.append(cur)
    return out


def artifact_dirs(wt: str) -> list[str]:
    found = []
    for name in ARTIFACT_DIRS:
        p = os.path.join(wt, name)
        if os.path.isdir(p) and not os.path.islink(p):
            found.append(p)
    # one level down (ios/Pods, app/build, packages/*/node_modules)
    for sub in listdir(wt):
        if sub.startswith(".") or sub in ARTIFACT_DIRS:
            continue
        sp = os.path.join(wt, sub)
        if not os.path.isdir(sp) or os.path.islink(sp):
            continue
        for name in ARTIFACT_DIRS:
            p = os.path.join(sp, name)
            if os.path.isdir(p) and not os.path.islink(p):
                found.append(p)
    return found


def all_worktrees(cfg) -> dict[str, list[dict]]:
    """common git dir -> worktrees (deduped across linked worktrees found by the scan)."""
    seen: dict[str, list[dict]] = {}
    for d in git_dirs([str(r) for r in cfg.roots], cfg.scan_prune):
        common = _git(d, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
        if not common or common in seen:
            continue
        seen[common] = worktrees_of(d)
    return seen


@register("worktree-artifacts")
def find_worktree_artifacts(env, cfg):
    for common, wts in all_worktrees(cfg).items():
        repo_name = Path(common).parent.name if Path(common).name == ".git" else Path(common).name
        for wt in wts:
            if wt["bare"]:
                continue
            arts = artifact_dirs(wt["path"])
            if not arts:
                continue
            dirty = bool(_git(wt["path"], "status", "--porcelain").strip())
            last = _git(wt["path"], "log", "-1", "--format=%cI").strip()
            label = f"{repo_name} @ {wt['branch'] or ('detached' if wt['detached'] else '?')}"
            bits = [
                f"{len(arts)} artifact dir(s): "
                + ", ".join(os.path.relpath(a, wt["path"]) for a in arts)
            ]
            bits.append("DIRTY (uncommitted changes)" if dirty else "clean")
            if last:
                bits.append(f"last commit {when(last)}")
            bits.append("lifecycle: `wt list`")
            yield Finding(
                category="worktree-artifacts",
                group="worktrees",
                label=label,
                paths=arts,
                evidence="; ".join(bits),
                verdict="review",
                action=None,
                extra={
                    "worktree": wt["path"],
                    "branch": wt["branch"],
                    "dirty": dirty,
                    "repo": repo_name,
                },
            )


@register("orphan-derived-data")
def find_orphan_derived_data(env, cfg):
    """DerivedData entries keyed to a workspace path under a scan root that no longer exists —
    the residue `wt remove` leaves. Acting on them belongs to `clean derived-data`."""
    roots = [str(r) for r in cfg.roots]
    dd = env.derived_data
    for name in listdir(dd):
        info = dd / name / "info.plist"
        if not info.exists():
            continue
        try:
            with open(info, "rb") as fh:
                d = plistlib.load(fh)
        except (OSError, plistlib.InvalidFileException, ValueError):
            continue
        ws = d.get("WorkspacePath")
        if not ws or os.path.exists(ws):
            continue
        root = next((r for r in roots if ws.startswith(r.rstrip("/") + "/")), None)
        if root is None:
            continue
        rel = os.path.relpath(ws, root).split(os.sep)
        guess = "/".join(rel[:2])
        yield Finding(
            category="orphan-derived-data",
            group="worktrees",
            label=f"{guess} → {name}",
            paths=[str(dd / name)],
            evidence=f"WorkspacePath gone: {ws}; last accessed {when(d.get('LastAccessedDate'))}; act via `devsweep clean derived-data`",
            verdict="review",
            action=None,
            extra={"workspace": ws, "derived_data": name},
        )
