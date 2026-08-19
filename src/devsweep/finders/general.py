"""General dev caches — reported for completeness; `mo clean` is the better actor for most.
Only the version-orphan cases (mise, JetBrains) carry an action."""

from __future__ import annotations

import os
import re
from collections import defaultdict
from pathlib import Path

from ..model import Action, Finding
from . import register
from ._util import listdir, mtime_of, version_tuple, when

MO_NOTE = "`mo clean` also covers this"


def _report(category, label, path: Path, note: str, verdict="review", action=None):
    return Finding(
        category=category,
        group="general",
        label=label,
        paths=[str(path)],
        evidence=f"{note}; modified {when(mtime_of(path))}",
        verdict=verdict,
        action=action,
    )


@register("npm")
def find_npm(env, cfg):
    for rel, note in (
        (("npm",), "npm cache; `npm cache clean --force` or " + MO_NOTE),
        (("Library", "Caches", "pnpm"), "pnpm store cache; `pnpm store prune`"),
        (("Library", "pnpm"), "pnpm content-addressable store; `pnpm store prune`"),
        (("Library", "Caches", "Yarn"), "yarn cache; `yarn cache clean`"),
        ((".bun", "install", "cache"), "bun install cache; `bun pm cache rm`"),
    ):
        path = env.p(*rel) if rel[0] != "npm" else env.p(".npm")
        if path.exists():
            yield _report("npm", "/".join(rel) if rel[0] != "npm" else ".npm", path, note)


@register("docker")
def find_docker(env, cfg):
    for rel, note in (
        (
            (".colima",),
            "colima VM disks; `colima prune` / `docker system prune` inside, or delete the profile",
        ),
        ((".docker",), "docker CLI config + buildx cache"),
        (
            ("Library", "Containers", "com.docker.docker", "Data", "vms"),
            "Docker Desktop VM disk; `docker system prune -a`",
        ),
        (("Library", "Application Support", "OrbStack"), "OrbStack data; `orb prune`"),
    ):
        path = env.p(*rel)
        if path.exists():
            yield _report("docker", "/".join(rel), path, note)


@register("homebrew")
def find_homebrew(env, cfg):
    cache = env.environ.get("HOMEBREW_CACHE") or str(env.p("Library", "Caches", "Homebrew"))
    path = Path(cache)
    if path.exists():
        yield _report(
            "homebrew",
            "Homebrew cache",
            path,
            "downloads + old bottles; `brew cleanup -s` or " + MO_NOTE,
        )
    logs = env.p("Library", "Logs", "Homebrew")
    if logs.exists():
        yield _report("homebrew", "Homebrew logs", logs, "`brew cleanup` or " + MO_NOTE)


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
            if active:
                verdict, ev, action = (
                    "review",
                    f"{tool}: current ({'symlink target' if v in link_targets else 'newest'}) of {len(real_versions)} installed",
                    None,
                )
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
                verdict=verdict,
                action=action,
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
                if name == newest:
                    verdict, ev, action = (
                        "review",
                        f"{ide} current {kind}; "
                        + (MO_NOTE if kind != "config" else "settings — keep"),
                        None,
                    )
                else:
                    verdict = "stale" if kind != "config" else "review"
                    ev = f"{ide} {kind} for an older IDE build; newest is {newest}"
                    action = Action(kind="trash", path=str(path)) if kind != "config" else None
                yield Finding(
                    category="jetbrains",
                    group="general",
                    label=f"{kind}/{name}",
                    paths=[str(path)],
                    evidence=f"{ev}; modified {when(mtime_of(path))}",
                    verdict=verdict,
                    action=action,
                )
