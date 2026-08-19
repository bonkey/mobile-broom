"""Walk the configured scan roots once, pruned, and answer 'which files named X exist'
and 'which git repos / worktrees exist'."""

from __future__ import annotations

import os
from functools import cache

MAX_DEPTH = 8


@cache
def _walk(
    roots: tuple[str, ...], prune: tuple[str, ...]
) -> tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...]:
    """(dirpath, subdirs, files) for every directory under roots, pruned. Cached per (roots, prune)."""
    out = []
    pruneset = set(prune)
    for root in roots:
        root = os.path.expanduser(root)
        if not os.path.isdir(root):
            continue
        base_depth = root.rstrip("/").count("/")
        for dp, dns, fns in os.walk(root):
            depth = dp.count("/") - base_depth
            out.append((dp, tuple(dns), tuple(fns)))
            # prune in place
            dns[:] = [
                d
                for d in dns
                if d not in pruneset
                and not d.startswith(".")
                and not os.path.exists(os.path.join(dp, d, "CACHEDIR.TAG"))
                and not os.path.islink(os.path.join(dp, d))
            ]
            if depth >= MAX_DEPTH:
                dns[:] = []
    return tuple(out)


def project_files(roots, prune, names: tuple[str, ...]) -> list[str]:
    wanted = set(names)
    return [
        os.path.join(dp, f)
        for dp, _d, fns in _walk(tuple(roots), tuple(prune))
        for f in fns
        if f in wanted
    ]


def git_dirs(roots, prune) -> list[str]:
    """Directories that contain a .git (dir or file) — repos and linked worktrees."""
    # .git is hidden so the prune above never descends into it; we only need to see it listed.
    out = []
    for dp, dns, fns in _walk(tuple(roots), tuple(prune)):
        if ".git" in dns or ".git" in fns:
            out.append(dp)
    return out
