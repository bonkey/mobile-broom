"""Sizing with the three rules that keep numbers honest:
1. never cross a mountpoint (st_dev must match the root) — cryptex volumes are mounts, not files
2. each candidate sized on its own; totals are 'candidates', not 'reclaimable' (APFS clones overlap)
3. cache by (path, mtime) in ~/.config/mobile-broom/sizes.json; --refresh bypasses
"""

from __future__ import annotations

import json
import os
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config

CACHE_FILE = "sizes.json"
BLOCK = 512


class Cancelled(Exception):
    """A walk was abandoned because its `stop` callable returned True."""


def walk_size(root: str | os.PathLike, honour_cachedir_tag: bool = False, stop=None) -> int:
    """Allocated bytes under root. Never follows symlinks, never crosses st_dev.
    `stop()` is polled once per directory; True raises Cancelled (the TUI quitting)."""
    root = os.fspath(root)
    try:
        st = os.lstat(root)
    except OSError:
        return 0
    if not stat.S_ISDIR(st.st_mode):
        return st.st_blocks * BLOCK
    dev = st.st_dev
    total = st.st_blocks * BLOCK
    stack = [root]
    while stack:
        if stop is not None and stop():
            raise Cancelled(root)
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    s = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if s.st_dev != dev:
                    continue  # mountpoint: skip entirely
                total += s.st_blocks * BLOCK
                if stat.S_ISDIR(s.st_mode):
                    if honour_cachedir_tag and os.path.exists(os.path.join(e.path, "CACHEDIR.TAG")):
                        continue
                    stack.append(e.path)
    return total


def dir_mtime(path: str) -> float:
    try:
        return os.lstat(path).st_mtime
    except OSError:
        return 0.0


class Sizer:
    def __init__(
        self,
        refresh: bool = False,
        workers: int | None = None,
        cache_path: Path | None = None,
        stop=None,
    ):
        self.refresh = refresh
        self.stop = stop  # polled while walking; True abandons the walk with Cancelled
        self.workers = workers or min(8, os.cpu_count() or 4)
        self.cache_path = cache_path or (config.config_dir() / CACHE_FILE)
        self._cache: dict[str, dict] = {}
        self._dirty = False
        if not refresh:
            try:
                self._cache = json.loads(self.cache_path.read_text())
            except (OSError, json.JSONDecodeError):
                self._cache = {}

    def size_path(self, path: str) -> int:
        mt = dir_mtime(path)
        hit = self._cache.get(path)
        if hit and not self.refresh and hit.get("mtime") == mt:
            return int(hit["size"])
        size = walk_size(path, stop=self.stop)
        self._cache[path] = {"mtime": mt, "size": size, "at": time.time()}
        self._dirty = True
        return size

    def size_paths(self, paths: list[str]) -> int:
        return sum(self.size_path(p) for p in paths)

    def size_findings(self, findings, progress=None):
        todo = [f for f in findings if f.size is None and f.paths]
        if not todo:
            return
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            futs = {ex.submit(self.size_paths, f.paths): f for f in todo}
            for i, fut in enumerate(futs, 1):
                f = futs[fut]
                try:
                    f.size = fut.result()
                except Exception:  # noqa: BLE001 - a single bad path must not kill the report
                    f.size = 0
                if progress:
                    progress(i, len(todo), f)
        self.save()

    def save(self):
        if not self._dirty:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self._cache))
            self._dirty = False
        except OSError:
            pass


def human(n: int | None) -> str:
    if n is None:
        return "   ?  "
    f = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if f < 1000 or unit == "T":
            return f"{f:5.1f}{unit}" if unit != "B" else f"{int(f):5d}B"
        f /= 1000
    return f"{f:.1f}T"
