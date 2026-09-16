"""curses browser: collect into the tree gradually → expand → mark → act, watching every
position finish. Conventions from simslim-profile.py."""

from __future__ import annotations

import curses
import io
import os
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
    ("→ ⏎", "expand"),
    ("← h", "collapse"),
    ("space", "mark"),
    ("a", "mark dead"),
    ("n", "unmark"),
    ("d", "act"),
    ("s", "sort"),
    ("o", "reveal"),
    ("r", "rescan"),
    ("?", "keys"),
    ("q", "quit"),
]
LEGEND = [
    "[ ] removable — space marks it",
    "[x] marked",
    "[-] locked — not removable; the bottom line says why",
    "…   size not computed yet (rescan in progress)",
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


def _totals(fs: list[Finding]) -> str:
    total = sum(f.size or 0 for f in fs)
    dead = sum(f.size or 0 for f in fs if f.verdict == "dead")
    return f"{human(total).strip()} total · dead {human(dead).strip()} · {len(fs)} findings"


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
    def __init__(self, kind, key, label, finding=None, depth=0, pending=False):
        self.kind, self.key, self.label, self.finding, self.depth = kind, key, label, finding, depth
        self.pending = pending


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
        self.status = ""  # collecting/sizing progress shown in the header
        self.loading = False  # a background scan is filling self.findings
        self.anchor: str | None = None  # row key to keep the cursor on while rows shift
        self._lock = threading.Lock()  # guards findings/marked/status/progress counters
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()  # set on quit: workers bail out, walks abandon
        self._errors: list[str] = []
        self._prog = [0, 0, 0, 0]  # cats done, cats total, sized, to size
        self.colors = {}
        self.key_attr = curses.A_BOLD | curses.A_REVERSE
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

    def _collect(self, refresh: bool):
        cats = finders.resolve(self.selectors)
        sizer = Sizer(refresh=refresh, stop=self._stop.is_set)
        with self._lock:
            self._prog[1] = len(cats)
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
        with self._lock:
            self.marked &= {f.key for f in self.findings}
            self.loading = False
            self.status = ""
            bits = [f"{len(self.findings)} findings"]
            if self.env is not None and self.env.simctl_error:
                bits.append(self.env.simctl_error)
            bits.extend(self._errors)
            self.msg = " · ".join(bits)

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
        findings = self.findings  # one snapshot: the scan thread swaps the list, never mutates it
        for g in GROUPS:
            gf = [f for f in findings if f.group == g]
            if not gf:
                continue
            out.append(
                Row("group", g, f"{g}  {_totals(gf)}", pending=any(f.size is None for f in gf))
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
                        f"{cat}  {_totals(cf)}",
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
                        Row("finding", f.key, f.label, finding=f, depth=2)
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
                            f"{b}  {_totals(bf)}",
                            depth=2,
                            pending=any(f.size is None for f in bf),
                        )
                    )
                    if bkey in self.closed:
                        continue
                    out.extend(
                        Row("finding", f.key, f.label, finding=f, depth=3)
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

    def draw(self, rows):
        scr = self.scr
        scr.erase()
        h, _w = scr.getmaxyx()
        findings = self.findings
        marked = [f for f in findings if f.key in self.marked]
        msize = sum(f.size or 0 for f in marked)
        head = (
            f"mobile-broom — {len(findings)} findings · marked {len(marked)} "
            f"({human(msize).strip()}) · sort: {SORTS[self.sort][0]}"
        )
        if self.status:
            head += f"   ⟳ {self.status}"
        _put(scr, 0, 0, head, curses.A_BOLD)
        top = _hints(scr, 1, 0, HELP, self.key_attr) + 2  # hints may wrap; tree starts below
        body = max(1, h - top - 3)
        self.cur = max(0, min(self.cur, max(0, len(rows) - 1)))
        self.off = min(self.off, self.cur)
        if self.cur >= self.off + body:
            self.off = self.cur - body + 1
        for i, row in enumerate(rows[self.off : self.off + body]):
            idx = self.off + i
            y = top + i
            sel = curses.A_REVERSE if idx == self.cur else 0
            indent = "  " * row.depth
            if row.kind == "finding":
                f = row.finding
                mark = "x" if f.key in self.marked else (" " if f.action else "-")
                attr = self.colors.get(f.verdict, 0)
                _put(scr, y, 0, f"{'>' if idx == self.cur else ' '} {indent}[{mark}] ", sel)
                x = 2 + len(indent) + 4
                _put(scr, y, x, f"{f.verdict:<6}", attr | sel)
                _put(scr, y, x + 7, f"{_size(f.size)}  {last_col(f.last)}  {f.display_label}", sel)
            else:
                arrow = "▾" if self.is_open(row) else "▸"
                text = f"{'>' if idx == self.cur else ' '} {indent}{arrow} {row.label}"
                if row.pending:
                    text += " …"
                _put(scr, y, 0, text, sel | (curses.A_BOLD if row.kind == "group" else 0))
        # detail: evidence · action-or-lock · message
        if rows and rows[self.cur].kind == "finding":
            f = rows[self.cur].finding
            _put(scr, h - 3, 0, f"— {f.evidence}", curses.A_DIM)
            if f.action:
                _put(scr, h - 2, 0, f"  {f.action.describe()}", curses.A_DIM)
            else:
                _put(scr, h - 2, 0, f"  locked: {f.locked}", self.colors.get("stale", 0))
        if self.msg:
            _put(scr, h - 1, 0, self.msg, curses.A_DIM)
        scr.refresh()

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
        draw(actions.summary(results), done=True)
        scr.getch()
        self.msg = actions.summary(results)

    def show_text(self, title, lines, hints=(("any key", "continue"),)):
        scr = self.scr
        scr.erase()
        h, _w = scr.getmaxyx()
        _hints(scr, 0, 0, list(hints), self.key_attr, lead=title + "   ")
        for i, ln in enumerate(lines[: h - 2]):
            _put(scr, 2 + i, 0, ln)
        scr.refresh()
        scr.getch()

    def show_keys(self):
        scr = self.scr
        scr.erase()
        _hints(scr, 0, 0, [("any key", "continue")], self.key_attr, lead="keys   ")
        width = max(len(k) for k, _d in HELP) + 2
        for i, (key, desc) in enumerate(HELP):
            _put(scr, 2 + i, 2, f" {key} ".ljust(width), self.key_attr)
            _put(scr, 2 + i, 2 + width + 1, desc)
        y = 3 + len(HELP)
        _put(scr, y, 0, "marks", curses.A_BOLD)
        for i, ln in enumerate(LEGEND):
            _put(scr, y + 1 + i, 2, ln)
        scr.refresh()
        scr.getch()

    # -- loop ------------------------------------------------------------
    def loop(self):
        curses.curs_set(0)
        self.scr.keypad(True)
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
            if row.kind != "finding":
                self.toggle(row)
        elif k in (curses.KEY_LEFT, ord("h")):
            if row.kind == "finding":
                parent = self.parent_key(row.finding)
                self.cur = next(i for i, r in enumerate(rows) if r.key == parent)
                self.close(rows[self.cur])
            else:
                self.close(row)
        elif k == ord(" "):
            if row.kind == "finding":
                if row.finding.action:
                    self.marked.symmetric_difference_update({row.finding.key})
                    self.cur += 1
                else:
                    self.msg = f"not removable: {row.finding.locked}"
            else:
                self._toggle_all(row, lambda f: f.action is not None)
        elif k == ord("a"):
            target = row if row.kind != "finding" else None
            self._toggle_all(target, lambda f: f.verdict == "dead" and f.action is not None)
        elif k == ord("n"):
            self.marked.clear()
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
        elif k == ord("?"):
            self.show_keys()
        return True

    def _toggle_all(self, row, pred):
        if row is None:
            pool = self.findings
        elif row.kind == "group":
            pool = [f for f in self.findings if f.group == row.key]
        elif row.kind == "bucket":
            g, c, b = row.key.split("/", 2)
            pool = [f for f in self.findings if (f.group, f.category, f.bucket) == (g, c, b)]
        else:
            g, c = row.key.split("/", 1)
            pool = [f for f in self.findings if f.group == g and f.category == c]
        keys = {f.key for f in pool if pred(f)}
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
