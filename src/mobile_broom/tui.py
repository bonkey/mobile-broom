"""curses browser: collect into the tree gradually → expand → mark → act, watching every
position finish. Conventions from simslim-profile.py."""

from __future__ import annotations

import curses
import io
import os
import shlex
import subprocess
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor

from . import actions, finders
from .model import GROUPS, VERDICT_RANK, Finding, bucket_key, sort_key
from .render import last_col
from .sizer import Cancelled, Sizer, human

TICK_MS = 100  # how often the loop redraws while a scan fills the tree in the background
# `s` cycles these; each orders the findings inside a category / runtime heading
SORTS = [
    ("verdict", lambda f: (VERDICT_RANK[f.verdict], -(f.size or 0), f.label)),
    ("size ↓", lambda f: (-(f.size or 0), f.label)),
    ("date ↑", lambda f: ((0, f.last.timestamp()) if f.last else (1, 0.0), f.label)),
]

ENTER_KEYS = (curses.KEY_ENTER, 10, 13)
# (key, what it does) — keys are drawn as highlighted chips so they stand out from the prose
HELP = [
    ("↑↓ jk", "move"),
    ("→ ⏎", "expand one level"),
    ("← h", "collapse"),
    ("e", "expand all levels"),
    ("space", "mark / unmark (on a heading: everything under it)"),
    ("a", "mark dead"),
    ("d", "act"),
    ("s", "cycle sort: " + ", ".join(name for name, _key in SORTS)),
    ("o", "reveal in Finder"),
    ("r", "rescan"),
    ("?", "keys"),
    ("q", "quit"),
]
LEGEND = [
    "[ ] removable — space marks it",
    "[x] marked",
    "[-] locked — not removable; the lines at the bottom say why",
    "    (space offers to shut a booted sim down)",
    "…   grey: still being collected or sized (scan in progress)",
    "all top row — space and a on it cover every finding",
    "    free → N: the free space once the marked findings are gone is at most N",
    "date column: last use/modification the verdict is based on, and its age in days",
]
ICON = {
    actions.PENDING: "·",
    actions.RUNNING: "⟳",
    actions.OK: "✓",
    actions.FAIL: "✗",
    actions.MANUAL: "!",
    actions.DRY: "~",
}


def _put(scr, y, x, text, attr=0):
    h, w = scr.getmaxyx()
    if 0 <= y < h and x < w:
        try:
            scr.addnstr(y, x, text, w - x - 1, attr)
        except curses.error:
            pass


def _size(n: int | None) -> str:
    return "   …  " if n is None else human(n)


def _totals(fs: list[Finding], marked: set[str]) -> list[tuple[str, str]]:
    """'122.4G total · dead 12.2G · marked 35.9G' as (text, part) pieces: a dead size above
    zero is part `dead`, the marked size part `marked`."""
    total = sum(f.size or 0 for f in fs)
    dead = sum(f.size or 0 for f in fs if f.verdict == "dead")
    pieces = [
        (f"{human(total).strip()} total · ", ""),
        (f"dead {human(dead).strip()}", "dead" if dead else ""),
    ]
    picked = [f for f in fs if f.key in marked]
    if picked:
        pieces += [
            (" · ", ""),
            (f"marked {human(sum(f.size or 0 for f in picked)).strip()}", "marked"),
        ]
    return pieces


def _free(usage: tuple[int, int], marked: int) -> str:
    """'444.0G free', plus '→ 456.2G' while something is marked: the free space once the
    marked findings are gone. That is a ceiling: marked sizes are candidates, and clones or
    shared files can free less. The marked part is capped at the used space."""
    free, total = usage
    text = f"{human(free).strip()} free"
    if marked:
        text += f" → {human(free + min(marked, total - free)).strip()}"
    return text


def _controls(status: str, sort: str, room: int) -> list[tuple[str, str]]:
    """The right end of the `all` row as (text, part) pieces drawn three spaces apart: scan
    progress, sort, `?` chip. When they need more than `room` columns, the progress goes
    first, then the sort. The `?` chip always stays."""
    pieces = [(f"⟳ {status}", "status")] if status else []
    pieces += [(f"sort: {sort}", "sort"), (" ? ", "help")]
    while len(pieces) > 1 and sum(len(t) + 3 for t, _p in pieces) - 3 > room:
        pieces.pop(0)
    return pieces


def _edge(title: str, width: int) -> str:
    """'+-- title -----+', `width` columns wide."""
    return ("+--" + title).ljust(width - 1, "-")[: width - 1] + "+"


def _hints(scr, y, x, items, key_attr, lead="", lead_attr=curses.A_BOLD) -> int:
    """Draw `lead` then `[key] desc` pairs, keys as chips, wrapping to the next line when
    the terminal is too narrow. Returns the y of the last line used."""
    _h, w = scr.getmaxyx()
    if lead:
        _put(scr, y, x, lead, lead_attr)
        x += len(lead)
    for i, (key, desc) in enumerate(items):
        if i:
            x += 2
        if x + len(key) + len(desc) + 4 > w:
            y, x = y + 1, 0
        chip = f" {key} "
        _put(scr, y, x, chip, key_attr)
        x += len(chip)
        _put(scr, y, x, f" {desc}", curses.A_DIM)
        x += len(desc) + 1
    return y


def reveal(path: str) -> str | None:
    """Show `path` in Finder (selected in its parent) or the desktop's file manager.
    Returns an error message, or None."""
    if not os.path.lexists(path):
        return f"gone: {path}"
    argv = ["open", "-R", path] if sys.platform == "darwin" else ["xdg-open", path]
    try:
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        return f"cannot open {path}: {e}"
    return None


class Row:
    def __init__(self, kind, key, label, finding=None, depth=0, pending=False, totals=()):
        self.kind, self.key, self.label, self.finding, self.depth = kind, key, label, finding, depth
        self.pending = pending
        self.totals = list(totals)  # a heading's _totals pieces, drawn after its label


class Browser:
    def __init__(self, scr, env, cfg, selectors, refresh=False):
        self.scr, self.env, self.cfg, self.selectors = scr, env, cfg, selectors
        self.refresh = refresh
        self.findings: list[Finding] = []
        self.open: set[str] = set(GROUPS)  # groups open, categories collapsed
        self.closed: set[str] = set()  # buckets (e.g. one iOS version) are open unless closed
        self.sort = 0  # index into SORTS
        self.marked: set[str] = set()
        self.cur = self.off = 0
        self.msg = ""
        self.status = ""  # collecting/sizing progress shown above the tree
        self.disk: tuple[int, int] | None = None  # (free, total) of HOME's volume, for the bar
        self.loading = False  # a background scan is filling self.findings
        self.anchor: str | None = None  # row key to keep the cursor on while rows shift
        self._lock = threading.Lock()  # guards findings/marked/status/progress/collecting
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()  # set on quit: workers bail out, walks abandon
        self._errors: list[str] = []
        self._prog = [0, 0, 0, 0]  # cats done, cats total, sized, to size
        # categories whose finder has not returned; replaced, never mutated, so rows() can
        # read it without the lock
        self._collecting: frozenset[str] = frozenset()
        self.colors = {}
        self.key_attr = curses.A_BOLD | curses.A_REVERSE
        self.mark_attr = curses.A_BOLD | curses.A_REVERSE
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            for i, (v, c) in enumerate(
                (
                    ("dead", curses.COLOR_RED),
                    ("stale", curses.COLOR_YELLOW),
                    ("shared", curses.COLOR_BLUE),
                    ("ok", curses.COLOR_GREEN),
                    ("key", curses.COLOR_CYAN),
                ),
                start=1,
            ):
                curses.init_pair(i, c, -1)
                self.colors[v] = curses.color_pair(i)
            self.colors["review"] = curses.A_DIM
            self.key_attr = self.colors["key"] | curses.A_BOLD | curses.A_REVERSE
            self.mark_attr = self.colors["stale"] | curses.A_BOLD | curses.A_REVERSE
        # marking keys are yellow; `d` deletes, so it is red
        self.chip_attrs = {
            "space": self.mark_attr,
            "a": self.mark_attr,
            "d": self.colors.get("dead", 0) | curses.A_BOLD | curses.A_REVERSE,
        }
        # the dead and marked sizes in heading totals take the same colours
        self.part_attrs = {
            "dead": self.colors.get("dead", 0),
            "marked": self.colors.get("stale", 0),
        }

    # -- collecting ------------------------------------------------------
    def collect(self, refresh: bool):
        """Start a background scan. Every category runs on its own worker: finder first, then
        one sizing task per finding, so branches appear and fill in independently while the
        loop keeps taking keys. `d` and `r` wait until the scan is done."""
        if self.loading:
            self.msg = "scan already running"
            return
        with self._lock:
            self.findings = []
            self._errors = []
            self._prog = [0, 0, 0, 0]
            self.loading = True
            self.status = "starting…"
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._collect_worker, args=(refresh,), daemon=True, name="mobile-broom-scan"
        )
        self._thread.start()

    def wait(self, timeout: float | None = None):
        """Block until the running scan finishes (tests, and `act` after the scan)."""
        if self._thread is not None:
            self._thread.join(timeout)

    def read_disk(self):
        """Read the disk bar's figures: at startup, when a scan finishes and after `d`.
        Redraws reuse them."""
        if self.env is not None:
            self.disk = self.env.disk_usage()

    def stop(self, timeout: float = 10.0):
        """Abandon a running scan: queued tasks are dropped, walks in flight bail at their next
        directory, and the scan thread is joined so the interpreter has nothing left to wait
        for after curses hands the terminal back."""
        self._stop.set()
        self.wait(timeout)

    def _collect_worker(self, refresh: bool):
        try:
            self._collect(refresh)
        except Exception as e:  # noqa: BLE001 - never let a scan take the process down
            with self._lock:
                self.msg = f"scan failed: {e!r}"
        finally:
            with self._lock:
                self.loading = False
                self.status = ""
                self._collecting = frozenset()

    def _collect(self, refresh: bool):
        cats = finders.resolve(self.selectors)
        sizer = Sizer(refresh=refresh, stop=self._stop.is_set)
        with self._lock:
            self._prog[1] = len(cats)
            self._collecting = frozenset(cats)
        self._set_status()
        ex = ThreadPoolExecutor(max_workers=sizer.workers, thread_name_prefix="mobile-broom")
        try:
            cat_futs = [ex.submit(self._load_cat, cat, sizer, ex) for cat in cats]
            size_futs: list[Future] = []
            for fut in cat_futs:
                size_futs.extend(fut.result())
            for fut in size_futs:
                fut.result()
        finally:
            ex.shutdown(wait=True, cancel_futures=self._stop.is_set())
        sizer.save()  # whatever was sized before a stop is still a valid cache entry
        if self._stop.is_set():
            return
        self.read_disk()
        with self._lock:
            self.marked &= {f.key for f in self.findings}
            self.loading = False
            self.status = ""
            bits = [self.env.simctl_error] if self.env is not None and self.env.simctl_error else []
            self.msg = " · ".join(bits + self._errors)

    def _load_cat(self, cat: str, sizer: Sizer, ex: ThreadPoolExecutor) -> list[Future]:
        """Run one finder, publish its findings, queue their sizing. Never blocks on sizing."""
        if self._stop.is_set():
            return []
        try:
            found = finders.run([cat], self.env, self.cfg)
        except Exception as e:  # noqa: BLE001 - one broken finder must not kill the browser
            found = []
            with self._lock:
                self._errors.append(f"{cat}: {e}")
        todo = [f for f in found if f.size is None and f.paths]
        with self._lock:
            self.findings = sorted(self.findings + found, key=sort_key)
            self._collecting = self._collecting - {cat}
            self._prog[0] += 1
            self._prog[3] += len(todo)
        self._set_status()
        return [ex.submit(self._size_one, sizer, f) for f in todo]

    def _size_one(self, sizer: Sizer, f: Finding):
        if self._stop.is_set():
            return
        try:
            size = sizer.size_paths(f.paths)
        except Cancelled:
            return
        except Exception:  # noqa: BLE001 - a single bad path must not kill the scan
            size = 0
        with self._lock:
            f.size = size
            self._prog[2] += 1
            # keep verdict/size order stable for the rows the loop is drawing
            self.findings = sorted(self.findings, key=sort_key)
        self._set_status()

    def _set_status(self):
        with self._lock:
            done, total, sized, to_size = self._prog
            parts = [f"collecting {done}/{total}"]
            if to_size:
                parts.append(f"sizing {sized}/{to_size}")
            self.status = " · ".join(parts)

    # -- rows ------------------------------------------------------------
    def rows(self) -> list[Row]:
        out: list[Row] = []
        # snapshots: the scan thread swaps these, never mutates them
        findings, collecting = self.findings, self._collecting
        if not findings:
            return out
        out.append(
            Row(
                "root",
                "all",
                "all",  # draw_header writes this row's text
                pending=bool(collecting) or any(f.size is None for f in findings),
            )
        )
        for g in GROUPS:
            gf = [f for f in findings if f.group == g]
            if not gf:
                continue
            out.append(
                Row(
                    "group",
                    g,
                    g,
                    totals=_totals(gf, self.marked),
                    pending=bool(collecting & set(GROUPS[g])) or any(f.size is None for f in gf),
                )
            )
            if g not in self.open:
                continue
            for cat in GROUPS[g]:
                cf = [f for f in gf if f.category == cat]
                if not cf:
                    continue
                key = f"{g}/{cat}"
                out.append(
                    Row(
                        "cat",
                        key,
                        cat,
                        totals=_totals(cf, self.marked),
                        depth=1,
                        pending=any(f.size is None for f in cf),
                    )
                )
                if key not in self.open:
                    continue
                buckets = sorted({f.bucket for f in cf}, key=bucket_key)
                order = SORTS[self.sort][1]
                if buckets == [None]:
                    out.extend(
                        Row("finding", f.key, f.label, f, depth=2, pending=f.size is None)
                        for f in sorted(cf, key=order)
                    )
                    continue
                for b in buckets:
                    bf = [f for f in cf if f.bucket == b]
                    bkey = f"{key}/{b}"
                    out.append(
                        Row(
                            "bucket",
                            bkey,
                            b,
                            totals=_totals(bf, self.marked),
                            depth=2,
                            pending=any(f.size is None for f in bf),
                        )
                    )
                    if bkey in self.closed:
                        continue
                    out.extend(
                        Row("finding", f.key, f.label, f, depth=3, pending=f.size is None)
                        for f in sorted(bf, key=order)
                    )
        return out

    def is_open(self, row: Row) -> bool:
        return row.key not in self.closed if row.kind == "bucket" else row.key in self.open

    def toggle(self, row: Row):
        if row.kind == "bucket":
            self.closed.symmetric_difference_update({row.key})
        else:
            self.open.symmetric_difference_update({row.key})

    def close(self, row: Row):
        if row.kind == "bucket":
            self.closed.add(row.key)
        else:
            self.open.discard(row.key)

    @staticmethod
    def parent_key(f: Finding) -> str:
        key = f"{f.group}/{f.category}"
        return f"{key}/{f.bucket}" if f.bucket else key

    def fold_all(self, rows: list[Row], row: Row):
        """`e` on a heading: while any finding under it is hidden, open everything under it;
        once they all show, close the row each of them hangs from (its bucket, or its
        category). On a finding it acts on the finding's own branch. Other branches keep their
        state. The cursor stays on its row, or moves to the parent of the finding it was on."""
        if row.kind == "finding":
            parent = self.parent_key(row.finding)
            fs = [f for f in self.findings if self.parent_key(f) == parent]
        else:
            fs = self._under(row)
        keys = {f.key for f in fs}
        if sum(r.kind == "finding" and r.key in keys for r in rows) < len(fs):
            self.open |= {f.group for f in fs} | {f"{f.group}/{f.category}" for f in fs}
            self.closed -= {self.parent_key(f) for f in fs if f.bucket}
            self.anchor = row.key
            return
        self.closed |= {self.parent_key(f) for f in fs if f.bucket}
        self.open -= {self.parent_key(f) for f in fs if not f.bucket}
        self.anchor = self.parent_key(row.finding) if row.kind == "finding" else row.key

    def draw(self, rows):
        scr = self.scr
        scr.erase()
        h, _w = scr.getmaxyx()
        self.draw_header(rows[0] if rows else None)
        # fixed heights: the `all` row on top; message, evidence and action-or-lock at the bottom
        top = 1
        body = max(1, h - top - 3)
        self.cur = max(0, min(self.cur, max(0, len(rows) - 1)))
        self.off = max(1, min(self.off, self.cur))
        if self.cur >= self.off + body:
            self.off = self.cur - body + 1
        for i, row in enumerate(rows[self.off : self.off + body]):
            idx = self.off + i
            y = top + i
            sel = curses.A_REVERSE if idx == self.cur else 0
            dim = curses.A_DIM if row.pending else 0  # still being collected or sized
            indent = "  " * row.depth
            if row.kind == "finding":
                f = row.finding
                mark = "x" if f.key in self.marked else (" " if f.action else "-")
                attr = self.colors.get(f.verdict, 0)
                _put(scr, y, 0, f"{'>' if idx == self.cur else ' '} {indent}[{mark}] ", sel | dim)
                x = 2 + len(indent) + 4
                _put(scr, y, x, f"{f.verdict:<6}", attr | sel | dim)
                _put(
                    scr,
                    y,
                    x + 7,
                    f"{_size(f.size)}  {last_col(f.last)}  {f.display_label}",
                    sel | dim,
                )
            else:
                arrow = "▾ " if self.is_open(row) else "▸ "
                head = f"{'>' if idx == self.cur else ' '} {indent}{arrow}{row.label}  "
                pieces = [(head, ""), *row.totals] + ([(" …", "")] if row.pending else [])
                bold = curses.A_BOLD if row.kind == "group" else 0
                self.draw_pieces(y, 0, pieces, sel | (dim or bold))
        if self.msg:
            _put(scr, h - 3, 0, self.msg)
        if rows and rows[self.cur].kind == "finding":
            f = rows[self.cur].finding
            _put(scr, h - 2, 0, f"— {f.evidence}", curses.A_DIM)
            if f.action:
                _put(scr, h - 1, 0, f"  {f.action.describe()}", curses.A_DIM)
            else:
                _put(scr, h - 1, 0, f"  locked: {f.locked}", self.colors.get("stale", 0))
        scr.refresh()

    def draw_header(self, root: Row | None):
        """Line 0 is the `all` row, pinned while the tree scrolls: totals and free space on the
        left; scan progress, sort and the `?` chip at the right end. It is drawn before the
        first finding lands too. When even the chip alone does not fit beside the totals, the
        totals are cut short."""
        scr = self.scr
        _h, w = scr.getmaxyx()
        findings, disk = self.findings, self.disk  # snapshots: the scan thread replaces them
        body = _totals(findings, self.marked) if findings else []
        if disk:
            marked = sum(f.size or 0 for f in findings if f.key in self.marked)
            body += [(" · ", "")] if body else []
            body.append((_free(disk, marked), ""))
        sel = root is not None and self.cur == 0
        pending = root.pending if root else self.loading
        left = [(f"{'>' if sel else ' '} all", "")] + ([("  ", ""), *body] if body else [])
        left += [(" …", "")] if pending else []
        right = _controls(self.status, SORTS[self.sort][0], w - 3 - sum(len(t) for t, _p in left))
        x = w - 1 - sum(len(t) + 3 for t, _p in right) + 3
        attr = (curses.A_REVERSE if sel else 0) | (curses.A_DIM if pending else curses.A_BOLD)
        self.draw_pieces(0, 0, left, attr, end=x - 2)
        attrs = {
            "status": self.colors.get("key", 0) | curses.A_BOLD,
            "sort": curses.A_BOLD,
            "help": self.key_attr,
        }
        for text, part in right:
            _put(scr, 0, x, text, attrs[part])
            x += len(text) + 3

    def draw_pieces(self, y: int, x: int, pieces: list[tuple[str, str]], attr: int, end=None):
        """Draw (text, part) pieces left to right over `attr`, colouring the `dead` and
        `marked` parts. Nothing is drawn from column `end` on."""
        for text, part in pieces:
            if end is not None:
                text = text[: max(0, end - x)]
            if text:
                _put(self.scr, y, x, text, attr | self.part_attrs.get(part, 0))
            x += len(text)

    # -- actions ---------------------------------------------------------
    def confirm_and_act(self):
        marked = [f for f in self.findings if f.key in self.marked and f.action]
        if not marked:
            self.msg = "nothing marked"
            return
        steps = actions.plan(marked)
        buf = io.StringIO()
        actions.describe_plan(steps, out=buf)
        lines = buf.getvalue().splitlines()
        scr = self.scr
        scr.erase()
        h, _w = scr.getmaxyx()
        _hints(
            scr,
            0,
            0,
            [("y", "delete"), ("t", "move to ~/.Trash instead"), ("any other key", "back")],
            self.key_attr,
            lead=f"about to run {len(steps)} action(s)   ",
        )
        for i, ln in enumerate(lines[: h - 3]):
            _put(scr, 2 + i, 0, ln)
        _put(scr, h - 1, 0, "~/.Trash keeps the space until it is emptied", curses.A_DIM)
        scr.refresh()
        k = scr.getch()
        if k not in (ord("y"), ord("Y"), ord("t"), ord("T")):
            return
        self.act(marked, trash=k in (ord("t"), ord("T")))

    def act(self, marked: list[Finding], trash: bool):
        """Run the plan with a live per-position status table."""
        state = {f.key: (actions.PENDING, "") for f in marked}
        scr = self.scr

        def draw(title, done=False):
            scr.erase()
            h, _w = scr.getmaxyx()
            if done:
                _hints(scr, 0, 0, [("any key", "continue")], self.key_attr, lead=title + "   ")
            else:
                _put(scr, 0, 0, title, curses.A_BOLD)
            body = max(1, h - 3)
            # keep the position being worked on in view
            active = next(
                (i for i, f in enumerate(marked) if state[f.key][0] == actions.PENDING),
                len(marked) - 1,
            )
            off = max(0, active - body + 1)
            for i, f in enumerate(marked[off : off + body]):
                st, note = state[f.key]
                attr = {
                    actions.OK: self.colors.get("ok", 0),
                    actions.FAIL: self.colors.get("dead", 0),
                    actions.MANUAL: self.colors.get("stale", 0),
                    actions.RUNNING: curses.A_BOLD,
                    actions.PENDING: curses.A_DIM,
                }.get(st, 0)
                _put(scr, 2 + i, 0, f"{ICON[st]} {st:<6}", attr)
                _put(scr, 2 + i, 9, f"{human(f.size)}  {f.category}/{f.label}", attr)
                if note:
                    _put(
                        scr,
                        2 + i,
                        9 + 8 + len(f.category) + 1 + len(f.label) + 3,
                        note,
                        curses.A_DIM,
                    )
            scr.refresh()

        n = len(marked)
        verb = "trashing" if trash else "deleting"

        def progress(f, st, note):
            state[f.key] = (st, note)
            done = sum(1 for s, _n in state.values() if s not in (actions.PENDING, actions.RUNNING))
            draw(f"{verb} {done}/{n} …")

        draw(f"{verb} 0/{n} …")
        results = actions.execute(marked, trash=trash, out=io.StringIO(), progress=progress)
        gone = {r.finding.key for r in results if r.ok}
        self.findings = [f for f in self.findings if f.key not in gone]
        self.marked -= gone
        self.read_disk()
        draw(actions.summary(results), done=True)
        scr.getch()
        self.msg = actions.summary(results)

    def unlock(self, f: Finding):
        """Offer to run `f.unlock` (e.g. shut a booted simulator down). On success the finding
        takes the action it frees; a failure is left in `msg`."""
        scr = self.scr
        cmd = shlex.join(f.unlock.argv)
        scr.erase()
        _hints(
            scr,
            0,
            0,
            [("y", "run it, then mark"), ("any other key", "back")],
            self.key_attr,
            lead=f"{f.locked}   ",
        )
        _put(scr, 2, 0, f"  {f.label}")
        _put(scr, 3, 0, f"  $ {cmd}")
        scr.refresh()
        scr.timeout(-1)  # the loop ticks while a scan runs; wait for a real answer here
        if scr.getch() not in (ord("y"), ord("Y")):
            return
        _put(scr, 5, 0, "running …", curses.A_BOLD)
        scr.refresh()
        r = self.env.run(f.unlock.argv)
        if r.returncode != 0:
            lines = (r.stderr or r.stdout or "").strip().splitlines()
            self.msg = f"{cmd} failed: {lines[-1] if lines else f'exit {r.returncode}'}"
            return
        f.action, f.evidence = f.unlock.action, f.unlock.evidence
        f.locked = f.unlock = None

    def show_text(self, title, lines, hints=(("any key", "continue"),)):
        scr = self.scr
        scr.erase()
        h, _w = scr.getmaxyx()
        _hints(scr, 0, 0, list(hints), self.key_attr, lead=title + "   ")
        for i, ln in enumerate(lines[: h - 2]):
            _put(scr, 2 + i, 0, ln)
        scr.refresh()
        scr.getch()

    def draw_keys(self):
        """The `?` box over the screen: every key as a chip with what it does, then what the
        marks mean. The border is ASCII: ncurses may send a run of one character as the
        character plus terminfo `rep`, and macOS's ncurses 6.0 then sends only the low byte of
        a non-ASCII one, which a terminal shows as one invalid character per cell."""
        scr = self.scr
        h, w = scr.getmaxyx()
        kw = max(len(k) for k, _d in HELP) + 2  # chip width
        lines = [("key", k, d) for k, d in HELP] + [
            ("text", "", 0),
            ("text", "marks", curses.A_BOLD),
        ]
        lines += [("text", ln, 0) for ln in LEGEND]
        inner = max(kw + 1 + max(len(d) for _k, d in HELP), *(len(ln) for ln in LEGEND))
        bw = min(w - 1, inner + 6)  # "|  " + inner + "  |"
        inner = bw - 6
        bh = min(h, len(lines) + 2)
        y0, x0 = (h - bh) // 2, (w - 1 - bw) // 2
        _put(scr, y0, x0, _edge(" keys ", bw), curses.A_BOLD)
        # left to right, so each line ends with its right border
        for i, (kind, a, b) in enumerate(lines[: bh - 2]):
            y = y0 + 1 + i
            _put(scr, y, x0, "|  ")
            if kind == "key":
                _put(
                    scr,
                    y,
                    x0 + 3,
                    f" {a} ".ljust(kw)[:inner],
                    self.chip_attrs.get(a, self.key_attr),
                )
                _put(scr, y, x0 + 3 + kw, f" {b}".ljust(inner - kw)[: max(0, inner - kw)])
            else:
                _put(scr, y, x0 + 3, a.ljust(inner)[:inner], b)
            _put(scr, y, x0 + bw - 3, "  |")
        _put(scr, y0 + bh - 1, x0, _edge(" any key closes ", bw))

    def show_keys(self):
        self.draw_keys()
        self.scr.refresh()
        self.scr.timeout(-1)  # the loop ticks while a scan runs; wait for a real key here
        self.scr.getch()

    # -- loop ------------------------------------------------------------
    def loop(self):
        curses.curs_set(0)
        self.scr.keypad(True)
        self.read_disk()
        self.collect(refresh=self.refresh)
        while True:
            rows = self.rows()
            if not rows and not self.loading:
                self.show_text("nothing found", [], hints=[("any key", "quit")])
                return
            if self.anchor is not None:
                # rows shifted under us (scan tick): stay on the same node, not the same index
                self.cur = next((i for i, r in enumerate(rows) if r.key == self.anchor), self.cur)
            self.cur = max(0, min(self.cur, max(0, len(rows) - 1)))
            self.draw(rows)
            self.scr.timeout(TICK_MS if self.loading else -1)
            k = self.scr.getch()
            if k == -1:  # tick: nothing pressed, redraw with whatever the scan produced
                self.anchor = rows[self.cur].key if rows else None
                continue
            self.anchor = None
            if not self.handle_key(k, rows):
                return  # handle_key may set a new anchor (sort) for the next pass

    def handle_key(self, k: int, rows: list[Row]) -> bool:
        """Apply one key to the browser state. Returns False to quit."""
        self.cur = max(0, min(self.cur, max(0, len(rows) - 1)))
        row = rows[self.cur] if rows else None
        self.msg = ""
        if k in (ord("q"), 27):
            return False
        if k == ord("?"):
            self.show_keys()
            return True
        if row is None:
            return True
        if k in (curses.KEY_UP, ord("k")):
            self.cur -= 1
        elif k in (curses.KEY_DOWN, ord("j")):
            self.cur += 1
        elif k == curses.KEY_NPAGE:
            self.cur += 10
        elif k == curses.KEY_PPAGE:
            self.cur -= 10
        elif k in ENTER_KEYS or k in (curses.KEY_RIGHT, ord("l")):
            if row.kind not in ("finding", "root"):
                self.toggle(row)
        elif k in (curses.KEY_LEFT, ord("h")):
            if row.kind == "finding":
                parent = self.parent_key(row.finding)
                self.cur = next(i for i, r in enumerate(rows) if r.key == parent)
                self.close(rows[self.cur])
            elif row.kind != "root":
                self.close(row)
        elif k == ord("e"):
            self.fold_all(rows, row)
        elif k == ord(" "):
            if row.kind == "finding":
                f = row.finding
                if not f.action and f.unlock:
                    self.unlock(f)
                if f.action:
                    self.marked.symmetric_difference_update({f.key})
                    self.cur += 1
                elif not self.msg:
                    self.msg = f"not removable: {f.locked}"
            else:
                self._toggle_all(row, lambda f: f.action is not None)
        elif k == ord("a"):
            target = row if row.kind != "finding" else None
            self._toggle_all(target, lambda f: f.verdict == "dead" and f.action is not None)
        elif k == ord("d"):
            if self.loading:
                self.msg = "scan still running — wait for it before acting"
            else:
                self.confirm_and_act()
        elif k == ord("s"):
            self.sort = (self.sort + 1) % len(SORTS)
            self.anchor = row.key  # same node, new position
        elif k == ord("o"):
            if row.kind != "finding" or not row.finding.paths:
                self.msg = "nothing to reveal here — pick a finding with a path"
            else:
                path = row.finding.paths[0]
                self.msg = reveal(path) or f"revealed {path}"
        elif k == ord("r"):
            if self.loading:
                self.msg = "scan already running"
            else:
                self.collect(refresh=True)
        return True

    def _under(self, row) -> list[Finding]:
        """The findings under a heading row; every finding for `all` or None."""
        if row is None or row.kind == "root":
            return self.findings
        if row.kind == "group":
            return [f for f in self.findings if f.group == row.key]
        if row.kind == "bucket":
            g, c, b = row.key.split("/", 2)
            return [f for f in self.findings if (f.group, f.category, f.bucket) == (g, c, b)]
        g, c = row.key.split("/", 1)
        return [f for f in self.findings if f.group == g and f.category == c]

    def _toggle_all(self, row, pred):
        keys = {f.key for f in self._under(row) if pred(f)}
        if keys and keys <= self.marked:
            self.marked -= keys
        else:
            self.marked |= keys


def run(selectors, env, cfg, refresh=False) -> int:
    import sys

    def main(scr):
        b = Browser(scr, env, cfg, selectors, refresh=refresh)
        try:
            b.loop()
        finally:
            b.stop()  # q or ^C mid-scan: drop queued work and join before curses returns

    try:
        curses.wrapper(main)
    except curses.error:
        print("mobile-broom: TUI needs an interactive terminal", file=sys.stderr)
        return 2
    return 0
