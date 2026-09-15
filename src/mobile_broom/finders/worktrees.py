"""Worktree finders. The worktree itself is never touched (`wt` owns lifecycle); what gets
an action is the build residue inside it — `.build`, `DerivedData`, `Pods`, `node_modules`, … —
one finding per artifact dir, removable on its own when git ignores it."""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
from pathlib import Path

from ..model import Action, Finding
from . import register
from ._util import age_days, listdir, mtime_of, when
from .ios import last_boot, runtime_name
from .scan import git_dirs

# dir (relative to the worktree, or to one of its top-level subdirs) → what regenerates it
ARTIFACT_DIRS: dict[str, str] = {
    ".build": "swift build / xcodebuild",
    "build": "a rebuild",
    "DerivedData": "an Xcode rebuild",
    ".derivedData": "an Xcode rebuild",
    "derivedData": "an Xcode rebuild",
    "derived_data": "an Xcode rebuild",
    "Derived": "tuist generate",
    "SourcePackages": "Xcode package resolution",
    "node_modules": "npm/yarn/pnpm/bun install",
    "Pods": "pod install",
    ".gradle": "a gradle build",
    "Carthage/Build": "carthage bootstrap",
    "Carthage/Checkouts": "carthage bootstrap",
    "vendor/bundle": "bundle install",
}


def _git(cwd: str, *args: str, stdin: str | None = None) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            input=stdin,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    # check-ignore exits 1 when nothing matched — still a valid (empty) answer
    return out.stdout if out.returncode in (0, 1) else ""


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
    """Artifact dirs at the worktree root and one level down (ios/Pods, app/build,
    packages/*/node_modules). Deduped by inode: APFS is case-insensitive, so `build`
    and `Build` are the same directory."""
    found: list[str] = []
    seen: set[tuple] = set()

    def add(p: str):
        if not os.path.isdir(p) or os.path.islink(p):
            return
        try:
            st = os.stat(p)
        except OSError:
            return
        if (st.st_dev, st.st_ino) in seen:
            return
        seen.add((st.st_dev, st.st_ino))
        found.append(p)

    for name in ARTIFACT_DIRS:
        add(os.path.join(wt, name))
    for sub in listdir(wt):
        if sub.startswith(".") or sub in ARTIFACT_DIRS:
            continue
        sp = os.path.join(wt, sub)
        if not os.path.isdir(sp) or os.path.islink(sp):
            continue
        for name in ARTIFACT_DIRS:
            add(os.path.join(sp, name))
    return found


def ignored_paths(wt: str, paths: list[str]) -> set[str]:
    """Subset of `paths` that git ignores in this worktree (one `check-ignore --stdin` call).
    A tracked or un-ignored dir is not an artifact we can prove regenerable."""
    if not paths:
        return set()
    rel = [os.path.relpath(p, wt) for p in paths]
    out = _git(wt, "check-ignore", "--stdin", stdin="\n".join(rel) + "\n")
    hit = {ln.strip() for ln in out.splitlines() if ln.strip()}
    return {p for p, r in zip(paths, rel) if r in hit}


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
            ignored = ignored_paths(wt["path"], arts)
            dirty = bool(_git(wt["path"], "status", "--porcelain").strip())
            last = _git(wt["path"], "log", "-1", "--format=%cI").strip()
            branch = wt["branch"] or ("detached" if wt["detached"] else "?")
            wt_bits = [
                "worktree DIRTY (uncommitted changes)" if dirty else "worktree clean",
                f"last commit {when(last)}" if last else "no commits",
                "worktree itself untouched (`wt` owns lifecycle)",
            ]
            for art in arts:
                rel = os.path.relpath(art, wt["path"])
                parts = rel.split(os.sep)
                key = next(
                    (k for k in ("/".join(parts[-2:]), parts[-1]) if k in ARTIFACT_DIRS), None
                )
                regen = ARTIFACT_DIRS.get(key, "a rebuild")
                mt = mtime_of(art)
                age = age_days(mt)
                bits = [f"{regen} recreates it", f"modified {when(mt)}"]
                if art in ignored:
                    verdict = "stale" if age is not None and age > cfg.stale_days else "shared"
                    action, locked = Action(kind="remove", path=art), None
                    bits.insert(0, "git-ignored build artifact")
                else:
                    verdict, action = "review", None
                    locked = "not git-ignored, so it may hold tracked or hand-made files"
                    bits.insert(0, "NOT git-ignored")
                yield Finding(
                    category="worktree-artifacts",
                    group="worktrees",
                    label=f"{repo_name} @ {branch}: {rel}",
                    paths=[art],
                    evidence="; ".join(bits + wt_bits),
                    last=mt,
                    verdict=verdict,
                    action=action,
                    locked=locked,
                    extra={
                        "worktree": wt["path"],
                        "branch": wt["branch"],
                        "dirty": dirty,
                        "repo": repo_name,
                        "artifact": rel,
                        "ignored": art in ignored,
                    },
                )


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def live_worktree_names(cfg) -> set[str]:
    """Slugs a per-task simulator could be named after: every live worktree dir, the part
    after the `<repo>.` prefix `wt` gives it, and its branch."""
    names: set[str] = set()
    for wts in all_worktrees(cfg).values():
        for wt in wts:
            base = Path(wt["path"]).name
            names.add(slug(base))
            if "." in base:
                names.add(slug(base.split(".", 1)[1]))
            if wt["branch"]:
                names.add(slug(wt["branch"]))
    return {n for n in names if n}


def named_after_live_worktree(name: str, live: set[str]) -> bool:
    s = slug(name)
    if not s:
        return True  # unnamed: not provably spare
    # Contained counts too: a "caregiver-elig" simulator for branch "MSP2-183/caregiver-elig".
    return s in live or (len(s) >= 4 and any(s in n for n in live))


def _adhoc(d: dict, defaults: dict[str, str]) -> bool:
    """A device named after a task, not after hardware. A name that embeds its own device type
    ("iPhone 16 Pro (iOS 18)") is a hand-labelled variant of that device, not a task simulator."""
    default = defaults.get(d.get("deviceTypeIdentifier") or "")
    if default is None or d.get("name") == default:
        return False
    return slug(default) not in slug(d.get("name") or "")


@register("worktree-simulators")
def find_worktree_simulators(env, cfg):
    """Hand-made simulators — a name no device type carries by default — whose worktree and
    branch are both gone. The same cross-reference as orphan-derived-data, on simctl devices.
    Removing the device is safe: `simctl create` remakes it from the runtime, which stays."""
    if not env.simctl_ok():
        return
    defaults = env.simctl_device_types()
    if not defaults:
        return
    adhoc = [d for d in env.simctl_devices() if d.get("isAvailable", True) and _adhoc(d, defaults)]
    if not adhoc:
        return
    live = live_worktree_names(cfg)
    if not live:
        return  # no worktrees under the scan roots: nothing to cross-reference against
    for d in adhoc:
        name = d.get("name") or ""
        if named_after_live_worktree(name, live):
            continue
        last, src = last_boot(d)
        used = f"last booted {when(last)} ({src})" if last else "no boot record"
        rt = runtime_name(d.get("runtime", ""))
        bits = [
            f'no live worktree or branch named "{name}" ({len(live)} scanned)',
            f'custom name (device type default: "{defaults[d["deviceTypeIdentifier"]]}")',
            used,
            f"{rt} stays installed; simctl create remakes the device",
        ]
        locked = None
        if d.get("state") == "Booted":
            verdict, action = "review", None
            bits.insert(0, "BOOTED now")
            locked = "device is booted; shut it down first"
        else:
            verdict = "dead"
            action = Action(kind="argv", argv=["xcrun", "simctl", "delete", d["udid"]])
        yield Finding(
            category="worktree-simulators",
            group="worktrees",
            label=f"{name} · {rt}",
            bucket=rt,
            paths=[d["dataPath"]] if d.get("dataPath") else [],
            size=d.get("dataPathSize"),
            evidence="; ".join(bits),
            last=last,
            verdict=verdict,
            action=action,
            locked=locked,
            extra={
                "udid": d.get("udid"),
                "runtime": d.get("runtime"),
                "state": d.get("state"),
                "device_type": d.get("deviceTypeIdentifier"),
                "worktrees_scanned": len(live),
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
            evidence=f"WorkspacePath gone: {ws}; last accessed {when(d.get('LastAccessedDate'))}; act via `mobile-broom clean derived-data`",
            last=d.get("LastAccessedDate"),
            verdict="review",
            action=None,
            locked="cross-reference only; the same entry is actionable as ios/derived-data",
            extra={"workspace": ws, "derived_data": name},
        )
