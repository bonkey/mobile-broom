"""curses browser: groups collapsed → expand → mark → act. Conventions from simslim-profile.py."""

from __future__ import annotations

import curses
import io

from . import actions, engine
from .model import GROUPS, Finding
from .sizer import Sizer, human

ENTER_KEYS = (curses.KEY_ENTER, 10, 13)
HELP = "↑↓/jk move · →/enter expand · ←/h collapse · space mark · a mark dead in group · d act · r resize · q quit"


def _put(scr, y, x, text, attr=0):
    h, w = scr.getmaxyx()
    if 0 <= y < h and x < w:
        try:
            scr.addnstr(y, x, text, w - x - 1, attr)
        except curses.error:
            pass


class Row:
    def __init__(self, kind, key, label, finding=None, depth=0):
        self.kind, self.key, self.label, self.finding, self.depth = kind, key, label, finding, depth


class Browser:
    def __init__(self, scr, findings: list[Finding], env, cfg, selectors, sizer: Sizer):
        self.scr, self.env, self.cfg, self.selectors, self.sizer = scr, env, cfg, selectors, sizer
        self.findings = findings
        self.open: set[str] = set()
        self.marked: set[str] = set()
        self.cur = self.off = 0
        self.msg = ""
        self.colors = {}
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            for i, (v, c) in enumerate(
                (
                    ("dead", curses.COLOR_RED),
                    ("stale", curses.COLOR_YELLOW),
                    ("shared", curses.COLOR_BLUE),
                ),
                start=1,
            ):
                curses.init_pair(i, c, -1)
                self.colors[v] = curses.color_pair(i)
            self.colors["review"] = curses.A_DIM

    # -- rows ------------------------------------------------------------
    def rows(self) -> list[Row]:
        out: list[Row] = []
        for g in GROUPS:
            gf = [f for f in self.findings if f.group == g]
            if not gf:
                continue
            gsize = sum(f.size or 0 for f in gf)
            dead = sum(f.size or 0 for f in gf if f.verdict == "dead")
            out.append(
                Row(
                    "group",
                    g,
                    f"{g}  {human(gsize).strip()} total · dead {human(dead).strip()} · {len(gf)} findings",
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
                out.append(Row("cat", key, f"{cat}  {human(csize).strip()} · {len(cf)}", depth=1))
                if key not in self.open:
                    continue
                for f in cf:
                    out.append(Row("finding", f.key, f.label, finding=f, depth=2))
        return out

    def draw(self, rows):
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        marked = [f for f in self.findings if f.key in self.marked]
        msize = sum(f.size or 0 for f in marked)
        _put(
            scr,
            0,
            0,
            f"devsweep — {len(self.findings)} findings · marked {len(marked)} ({human(msize).strip()})",
            curses.A_BOLD,
        )
        _put(scr, 1, 0, HELP, curses.A_DIM)
        body = h - 4
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
                mark = "x" if f.key in self.marked else (" " if f.action else "·")
                attr = self.colors.get(f.verdict, 0)
                _put(scr, y, 0, f"{'>' if idx == self.cur else ' '} {indent}[{mark}] ", sel)
                x = 2 + len(indent) + 4
                _put(scr, y, x, f"{f.verdict:<6}", attr | sel)
                _put(scr, y, x + 7, f"{human(f.size)}  {f.label}", sel)
            else:
                arrow = "▾" if row.key in self.open else "▸"
                _put(
                    scr,
                    y,
                    0,
                    f"{'>' if idx == self.cur else ' '} {indent}{arrow} {row.label}",
                    sel | (curses.A_BOLD if row.kind == "group" else 0),
                )
        # detail line for the current finding
        if rows and rows[self.cur].kind == "finding":
            f = rows[self.cur].finding
            act = f.action.describe() if f.action else "no action (report-only)"
            _put(scr, h - 2, 0, f"— {f.evidence}"[: w - 1], curses.A_DIM)
            _put(scr, h - 1, 0, f"  {act}"[: w - 1], curses.A_DIM)
        elif self.msg:
            _put(scr, h - 1, 0, self.msg[: w - 1], curses.A_DIM)
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
        while True:
            scr.erase()
            h, _w = scr.getmaxyx()
            _put(
                scr,
                0,
                0,
                f"about to run {len(steps)} action(s) — y confirm · p purge (no trash) · any other key back",
                curses.A_BOLD,
            )
            for i, ln in enumerate(lines[: h - 3]):
                _put(scr, 2 + i, 0, ln)
            scr.refresh()
            k = scr.getch()
            if k in (ord("y"), ord("Y"), ord("p"), ord("P")):
                purge = k in (ord("p"), ord("P"))
                out = io.StringIO()
                results = actions.execute(marked, dry_run=False, purge=purge, out=out)
                done = {r.finding.key for r in results if r.ok}
                self.findings = [f for f in self.findings if f.key not in done]
                self.marked -= done
                self.show_text("results — any key to continue", out.getvalue().splitlines())
                return
            return

    def show_text(self, title, lines):
        scr = self.scr
        scr.erase()
        h, _w = scr.getmaxyx()
        _put(scr, 0, 0, title, curses.A_BOLD)
        for i, ln in enumerate(lines[: h - 2]):
            _put(scr, 2 + i, 0, ln)
        scr.refresh()
        scr.getch()

    def resize_all(self):
        sizer = Sizer(refresh=True)
        for f in self.findings:
            f.size = None if f.paths else f.size
        self.msg = "sizing…"
        self.draw(self.rows())
        sizer.size_findings(self.findings)
        self.msg = "sizes refreshed"

    # -- loop ------------------------------------------------------------
    def loop(self):
        curses.curs_set(0)
        self.scr.keypad(True)
        while True:
            rows = self.rows()
            if not rows:
                self.show_text("nothing found — any key to quit", [])
                return
            self.cur = max(0, min(self.cur, len(rows) - 1))
            self.draw(rows)
            k = self.scr.getch()
            row = rows[self.cur]
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
                    self.open.discard(f"{row.finding.group}/{row.finding.category}")
                    self.cur = next(
                        i
                        for i, r in enumerate(rows)
                        if r.key == f"{row.finding.group}/{row.finding.category}"
                    )
                elif row.kind == "cat":
                    self.open.discard(row.key)
                else:
                    self.open.discard(row.key)
            elif k == ord(" "):
                if row.kind == "finding" and row.finding.action:
                    self.marked.symmetric_difference_update({row.finding.key})
                    self.cur += 1
                elif row.kind in ("group", "cat"):
                    self._toggle_all(row, lambda f: f.action is not None)
            elif k == ord("a"):
                target = row if row.kind != "finding" else None
                self._toggle_all(target, lambda f: f.verdict == "dead" and f.action is not None)
            elif k == ord("n"):
                self.marked.clear()
            elif k == ord("d"):
                self.confirm_and_act()
            elif k == ord("r"):
                self.resize_all()
            elif k == ord("?"):
                self.show_text("keys", HELP.split(" · "))

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

    sys.stderr.write("devsweep: collecting…\n")
    sizer = Sizer(refresh=refresh)
    findings = engine.collect(selectors, env, cfg, sizer, progress=True)

    def main(scr):
        Browser(scr, findings, env, cfg, selectors, sizer).loop()

    try:
        curses.wrapper(main)
    except curses.error:
        print("devsweep: TUI needs an interactive terminal", file=sys.stderr)
        return 2
    return 0
