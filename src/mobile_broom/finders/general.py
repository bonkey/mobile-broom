"""General dev caches. Package-manager caches (npm, npx, pnpm, yarn, bun, Homebrew) are pure
re-downloadable caches → shared + delete. Docker/colima disks are VM images, not caches →
report-only. mise / JetBrains: older versions → stale. Trash entries are already thrown
away → shared + delete."""

from __future__ import annotations

import os
import re
from collections import defaultdict
from pathlib import Path

from ..model import Action, Finding
from . import register
from ._util import listdir, mtime_of, version_tuple, when

MO_NOTE = "`mo clean` also covers this"


def _report(category, label, path: Path, note: str, verdict="review", action=None, locked=None):
    return Finding(
        category=category,
        group="general",
        label=label,
        paths=[str(path)],
        evidence=f"{note}; modified {when(mtime_of(path))}",
        last=mtime_of(path),
        verdict=verdict,
        action=action,
        locked=locked,
    )


def _cache(category, label, path: Path, note: str, refill="re-downloaded on demand"):
    """A pure download cache: nothing references it, the tool refills it on demand."""
    return _report(
        category,
        label,
        path,
        f"{note}; {refill}",
        verdict="shared",
        action=Action(kind="remove", path=str(path)),
    )


@register("npm")
def find_npm(env, cfg):
    for rel, note in (
        ((".npm", "_cacache"), "npm download cache (= `npm cache clean --force`)"),
        ((".npm", "_npx"), "npx package cache"),
        (("Library", "Caches", "pnpm"), "pnpm metadata cache"),
        (
            ("Library", "pnpm"),
            "pnpm content-addressable store (installed node_modules keep their hard links)",
        ),
        (("Library", "Caches", "Yarn"), "yarn cache (= `yarn cache clean`)"),
        ((".yarn", "berry", "cache"), "yarn berry global cache"),
        ((".bun", "install", "cache"), "bun install cache (= `bun pm cache rm`)"),
    ):
        path = env.p(*rel)
        if path.is_dir():
            yield _cache("npm", "/".join(rel), path, note)


@register("docker")
def find_docker(env, cfg):
    for rel, note, why in (
        (
            (".colima",),
            "colima VM disks; `colima prune` / `docker system prune` inside, or delete the profile",
            "VM disk image with your images and volumes inside; prune from within",
        ),
        (
            (".docker",),
            "docker CLI config + buildx cache",
            "holds contexts and credentials next to the cache; prune with `docker buildx prune`",
        ),
        (
            ("Library", "Containers", "com.docker.docker", "Data", "vms"),
            "Docker Desktop VM disk; `docker system prune -a`",
            "VM disk image with your images and volumes inside; prune from within",
        ),
        (
            ("Library", "Application Support", "OrbStack"),
            "OrbStack data; `orb prune`",
            "holds machines and volumes, not just cache; prune with `orb prune`",
        ),
    ):
        path = env.p(*rel)
        if path.exists():
            yield _report("docker", "/".join(rel), path, note, locked=why)


@register("homebrew")
def find_homebrew(env, cfg):
    cache = env.environ.get("HOMEBREW_CACHE") or str(env.p("Library", "Caches", "Homebrew"))
    path = Path(cache)
    if path.is_dir():
        yield _cache(
            "homebrew", "Homebrew cache", path, "downloads + old bottles (= `brew cleanup -s`)"
        )
    logs = env.p("Library", "Logs", "Homebrew")
    if logs.is_dir():
        yield _cache(
            "homebrew",
            "Homebrew logs",
            logs,
            "build logs (= `brew cleanup`)",
            "recreated on next brew",
        )


@register("mise")
def find_mise(env, cfg):
    installs = (
        Path(env.environ.get("MISE_DATA_DIR") or env.p(".local", "share", "mise")) / "installs"
    )
    for tool in listdir(installs):
        tdir = installs / tool
        if not tdir.is_dir():
            continue
        real_versions = []
        link_targets = set()
        for v in listdir(tdir):
            vp = tdir / v
            if vp.is_symlink():
                try:
                    link_targets.add(os.path.basename(os.path.realpath(vp)))
                except OSError:
                    pass
            elif vp.is_dir():
                real_versions.append(v)
        if len(real_versions) <= 1:
            continue
        real_versions.sort(key=version_tuple)
        newest = real_versions[-1]
        for v in real_versions:
            vp = tdir / v
            active = v in link_targets or v == newest
            locked = None
            if active:
                how = "symlink target" if v in link_targets else "newest"
                verdict, ev, action = (
                    "review",
                    f"{tool}: current ({how}) of {len(real_versions)} installed",
                    None,
                )
                locked = f"current {tool} ({how}); `mise uninstall` it yourself if unwanted"
            else:
                verdict = "stale"
                ev = f"{tool}: older version; newest installed is {newest}; not a `latest`/alias target"
                action = Action(kind="argv", argv=["mise", "uninstall", f"{tool}@{v}"])
            yield Finding(
                category="mise",
                group="general",
                label=f"{tool}@{v}",
                paths=[str(vp)],
                evidence=f"{ev}; modified {when(mtime_of(vp))}",
                last=mtime_of(vp),
                verdict=verdict,
                action=action,
                locked=locked,
                extra={"tool": tool, "version": v},
            )


IDE_VER = re.compile(r"^([A-Za-z]+)(\d{4}(?:\.\d+)*)$")


@register("jetbrains")
def find_jetbrains(env, cfg):
    for base, kind in (
        (env.p("Library", "Caches", "JetBrains"), "caches"),
        (env.p("Library", "Logs", "JetBrains"), "logs"),
        (env.p("Library", "Application Support", "JetBrains"), "config"),
    ):
        if not base.is_dir():
            continue
        by_ide: dict[str, list[tuple]] = defaultdict(list)
        for name in listdir(base):
            m = IDE_VER.match(name)
            if m and (base / name).is_dir():
                by_ide[m.group(1)].append((version_tuple(m.group(2)), name))
        for ide, items in by_ide.items():
            items.sort()
            newest = items[-1][1]
            for _v, name in items:
                path = base / name
                locked = None
                if name == newest:
                    verdict, ev, action = (
                        "review",
                        f"{ide} current {kind}; "
                        + (MO_NOTE if kind != "config" else "settings — keep"),
                        None,
                    )
                    locked = (
                        f"{kind} of the installed {ide} build; in use"
                        if kind != "config"
                        else "IDE settings, not a cache"
                    )
                else:
                    verdict = "stale" if kind != "config" else "review"
                    ev = f"{ide} {kind} for an older IDE build; newest is {newest}"
                    action = Action(kind="remove", path=str(path)) if kind != "config" else None
                    if kind == "config":
                        locked = (
                            "old IDE settings; JetBrains imports them on upgrade — delete by hand"
                        )
                yield Finding(
                    category="jetbrains",
                    group="general",
                    label=f"{kind}/{name}",
                    paths=[str(path)],
                    evidence=f"{ev}; modified {when(mtime_of(path))}",
                    last=mtime_of(path),
                    verdict=verdict,
                    action=action,
                    locked=locked,
                )


def _trashes(env):
    """(folder, bucket) for the user's Trash on every mounted volume: `~/.Trash` on the home
    volume, `.Trashes/<uid>` on the others. A symlinked volume (`Macintosh HD -> /`) is the
    boot volume, which `~/.Trash` already covers."""
    yield env.p(".Trash"), "~/.Trash"
    uid = str(os.getuid())
    for vol in listdir(env.volumes):
        root = env.volumes / vol
        if not root.is_symlink():
            trash = root / ".Trashes" / uid
            yield trash, str(trash)


@register("trash")
def find_trash(env, cfg):
    """One finding per Trash entry, so a root-owned app gets its own printed `sudo rm` and an
    entry that cannot be deleted does not hold up the rest."""
    for trash, bucket in _trashes(env):
        try:
            names = sorted(os.listdir(trash))
        except PermissionError:
            yield Finding(
                category="trash",
                group="general",
                label=bucket,
                paths=[str(trash)],
                evidence="macOS denied listing this Trash",
                verdict="review",
                locked="give your terminal Full Disk Access, or empty the Trash in Finder",
            )
            continue
        except OSError:
            continue
        for name in names:
            if name == ".DS_Store":  # Finder's own file
                continue
            path = trash / name
            # Moving an entry into the Trash is a rename, which sets its ctime. A later chmod
            # or xattr change moves it too, so the date is approximate.
            try:
                trashed = os.lstat(path).st_ctime
            except OSError:
                continue
            yield Finding(
                category="trash",
                group="general",
                label=f"{name} · {bucket}",
                paths=[str(path)],
                evidence=f"in the Trash since about {when(trashed)}",
                last=trashed,
                verdict="shared",
                action=Action(kind="remove", path=str(path), in_trash=True),
                bucket=bucket,
            )
