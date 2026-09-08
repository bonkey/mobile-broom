"""curses browser: collect into the tree gradually → expand → mark → act, watching every
position finish. Conventions from simslim-profile.py."""

from __future__ import annotations

import curses
import io

from . import actions, finders
from .model import GROUPS, Finding, sort_key
from .sizer import Sizer, human

ENTER_KEYS = (curses.KEY_ENTER, 10, 13)
HELP = (
    "↑↓/jk move · →/enter expand · ←/h collapse · space mark · a mark dead in group · "
    "n unmark all · d act · r rescan · ? keys · q quit"
)
LEGEND = [
    "[ ] removable — space marks it",
    "[x] marked",
    "[-] locked — not removable; the bottom line says why",
    "…   size not computed yet (rescan in progress)",
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
        self.marked: set[str] = set()
        self.cur = self.off = 0
        self.msg = ""
        self.status = ""  # collecting/sizing progress shown in the header
        self.colors = {}
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            for i, (v, c) in enumerate(
                (
                    ("dead", curses.COLOR_RED),
                    ("stale", curses.COLOR_YELLOW),
                    ("shared", curses.COLOR_BLUE),
                    ("ok", curses.COLOR_GREEN),
                ),
                start=1,
            ):
                curses.init_pair(i, c, -1)
                self.colors[v] = curses.color_pair(i)
            self.colors["review"] = curses.A_DIM

    # -- collecting ------------------------------------------------------
    def collect(self, refresh: bool):
        """Run finders one category at a time, then size one finding at a time, redrawing
        after every step so the tree fills in and sizes replace the … indicators."""
        cats = finders.resolve(self.selectors)
        self.findings = []
        for i, cat in enumerate(cats, 1):
            self.status = f"collecting {i}/{len(cats)}: {cat}"
            self.draw(self.rows())
            self.findings.extend(finders.run([cat], self.env, self.cfg))
            self.findings.sort(key=sort_key)
        sizer = Sizer(refresh=refresh)
        todo = sum(1 for f in self.findings if f.size is None and f.paths)

        def sized(i, n, f):
            self.status = f"sizing {i}/{n}"
            self.draw(self.rows())

        self.status = f"sizing 0/{todo}"
        self.draw(self.rows())
        sizer.size_findings(self.findings, progress=sized)
        self.findings.sort(key=sort_key)
        self.marked &= {f.key for f in self.findings}
        self.status = ""
        self.msg = f"{len(self.findings)} findings" + (
            f" · {self.env.simctl_error}" if self.env.simctl_error else ""
        )

    # -- rows ------------------------------------------------------------
    def rows(self) -> list[Row]:
        out: list[Row] = []
        for g in GROUPS:
            gf = [f for f in self.findings if f.group == g]
            if not gf:
                continue
            gsize = sum(f.size or 0 for f in gf)
            dead = sum(f.size or 0 for f in gf if f.verdict == "dead")
            gpend = any(f.size is None for f in gf)
            out.append(
                Row(
                    "group",
                    g,
                    f"{g}  {human(gsize).strip()} total · dead {human(dead).strip()} · {len(gf)} findings",
                    pending=gpend,
                )
            )
            if g not in self.open:
                continue
            for cat in GROUPS[g]:
                cf = [f for f in gf if f.category == cat]
                if not cf:
                    continue
                csize = sum(f.size or 0 for f in cf)
                key = f"{g}/{cat}"
                out.append(
                    Row(
                        "cat",
                        key,
                        f"{cat}  {human(csize).strip()} · {len(cf)}",
                        depth=1,
                        pending=any(f.size is None for f in cf),
                    )
                )
                if key not in self.open:
                    continue
                for f in cf:
                    out.append(Row("finding", f.key, f.label, finding=f, depth=2))
        return out

    def draw(self, rows):
        scr = self.scr
        scr.erase()
        h, _w = scr.getmaxyx()
        marked = [f for f in self.findings if f.key in self.marked]
        msize = sum(f.size or 0 for f in marked)
        head = f"mobile-broom — {len(self.findings)} findings · marked {len(marked)} ({human(msize).strip()})"
        if self.status:
            head += f"   ⟳ {self.status}"
        _put(scr, 0, 0, head, curses.A_BOLD)
        _put(scr, 1, 0, HELP, curses.A_DIM)
        body = max(1, h - 6)
        self.cur = max(0, min(self.cur, max(0, len(rows) - 1)))
        self.off = min(self.off, self.cur)
        if self.cur >= self.off + body:
            self.off = self.cur - body + 1
        for i, row in enumerate(rows[self.off : self.off + body]):
            idx = self.off + i
            y = 3 + i
            sel = curses.A_REVERSE if idx == self.cur else 0
            indent = "  " * row.depth
            if row.kind == "finding":
                f = row.finding
                mark = "x" if f.key in self.marked else (" " if f.action else "-")
                attr = self.colors.get(f.verdict, 0)
                _put(scr, y, 0, f"{'>' if idx == self.cur else ' '} {indent}[{mark}] ", sel)
                x = 2 + len(indent) + 4
                _put(scr, y, x, f"{f.verdict:<6}", attr | sel)
                _put(scr, y, x + 7, f"{_size(f.size)}  {f.label}", sel)
            else:
                arrow = "▾" if row.key in self.open else "▸"
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
        _put(
            scr,
            0,
            0,
            f"about to run {len(steps)} action(s) — y delete · t move to ~/.Trash instead · any other key back",
            curses.A_BOLD,
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

        def draw(title):
            scr.erase()
            h, _w = scr.getmaxyx()
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
        draw(actions.summary(results) + " — any key to continue")
        scr.getch()
        self.msg = actions.summary(results)

    def show_text(self, title, lines):
        scr = self.scr
        scr.erase()
        h, _w = scr.getmaxyx()
        _put(scr, 0, 0, title, curses.A_BOLD)
        for i, ln in enumerate(lines[: h - 2]):
            _put(scr, 2 + i, 0, ln)
        scr.refresh()
        scr.getch()

    # -- loop ------------------------------------------------------------
    def loop(self):
        curses.curs_set(0)
        self.scr.keypad(True)
        self.collect(refresh=self.refresh)
        while True:
            rows = self.rows()
            if not rows:
                self.show_text("nothing found — any key to quit", [])
                return
            self.cur = max(0, min(self.cur, len(rows) - 1))
            self.draw(rows)
            k = self.scr.getch()
            row = rows[self.cur]
            self.msg = ""
            if k in (ord("q"), 27):
                return
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
                    self.open.symmetric_difference_update({row.key})
            elif k in (curses.KEY_LEFT, ord("h")):
                if row.kind == "finding":
                    parent = f"{row.finding.group}/{row.finding.category}"
                    self.open.discard(parent)
                    self.cur = next(i for i, r in enumerate(rows) if r.key == parent)
                else:
                    self.open.discard(row.key)
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
                self.confirm_and_act()
            elif k == ord("r"):
                self.collect(refresh=True)
            elif k == ord("?"):
                self.show_text("keys — any key to continue", HELP.split(" · ") + [""] + LEGEND)

    def _toggle_all(self, row, pred):
        if row is None:
            pool = self.findings
        elif row.kind == "group":
            pool = [f for f in self.findings if f.group == row.key]
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
        Browser(scr, env, cfg, selectors, refresh=refresh).loop()

    try:
        curses.wrapper(main)
    except curses.error:
        print("mobile-broom: TUI needs an interactive terminal", file=sys.stderr)
        return 2
    return 0
