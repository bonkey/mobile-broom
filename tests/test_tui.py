"""TUI logic against a fake curses screen: tree rows, the locked explainer, live act table,
and the background scan that fills the tree while keys keep working."""

import curses
import fcntl
import os
import re
import select
import shutil
import struct
import subprocess
import sys
import termios
import threading
from datetime import UTC, datetime, timedelta

import pytest

from mobile_broom import actions, finders, tui
from mobile_broom.model import Action, Finding, Unlock


class FakeScreen:
    def __init__(self, h=30, w=160):
        self.h, self.w = h, w
        self.lines: dict[int, str] = {}
        self.attrs: list[tuple] = []
        self.frames: list[dict[int, str]] = []
        self.keys: list[int] = []
        self.delay = None

    def getmaxyx(self):
        return self.h, self.w

    def erase(self):
        self.lines = {}

    def addnstr(self, y, x, text, n, attr=0):
        cur = self.lines.get(y, "").ljust(x)
        self.lines[y] = (cur[:x] + text[:n]).rstrip()
        self.attrs.append((y, text[:n], attr))

    def refresh(self):
        self.frames.append(dict(self.lines))

    def getch(self):
        return self.keys.pop(0) if self.keys else ord("q")

    def keypad(self, _flag):
        pass

    def timeout(self, ms):
        self.delay = ms

    def text(self):
        return "\n".join(self.lines[y] for y in sorted(self.lines))


@pytest.fixture
def browser(monkeypatch):
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    monkeypatch.setattr(curses, "curs_set", lambda _v: None)
    scr = FakeScreen()
    b = tui.Browser(scr, env=None, cfg=None, selectors=None)
    b.findings = [
        Finding(
            "derived-data",
            "ios",
            "Dead-abc",
            ["/dd/Dead-abc"],
            "orphan",
            "dead",
            size=10,
            action=Action("remove", path="/dd/Dead-abc"),
            last=datetime.now(UTC) - timedelta(days=33),
        ),
        Finding(
            "sim-data",
            "ios",
            "iPhone · iOS 27",
            ["/sim"],
            "BOOTED now",
            "review",
            size=5,
            locked="device is booted; shut it down first",
        ),
    ]
    b.open = {"ios", "ios/derived-data", "ios/sim-data"}
    return b, scr


def test_locked_finding_shows_reason_and_refuses_marking(browser):
    b, scr = browser
    rows = b.rows()
    assert [r.kind for r in rows] == ["root", "group", "cat", "finding", "cat", "finding"]
    b.cur = 3  # the booted device (sim-data sorts before derived-data)
    b.draw(rows)
    assert "[-] review" in scr.text()
    assert "locked: device is booted; shut it down first" in scr.text()
    scr.keys = [ord(" "), ord("q")]
    b.collect = lambda refresh: None
    b.loop()
    assert b.marked == set()
    assert "not removable: device is booted" in scr.text()


class RunEnv:
    def __init__(self, returncode=0, stderr=""):
        self.calls: list[list[str]] = []
        self.returncode, self.stderr = returncode, stderr

    def run(self, argv, timeout=120):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, self.returncode, "", self.stderr)


def _booted(b, env):
    """The fixture's booted device, with the shutdown its finder offers."""
    b.env = env
    f = b.findings[1]
    f.unlock = Unlock(
        ["xcrun", "simctl", "shutdown", "U1"],
        Action("argv", argv=["xcrun", "simctl", "erase", "U1"]),
        "iOS 27; last booted today",
    )
    return f


def test_space_on_booted_device_offers_shutdown_then_marks(browser):
    b, scr = browser
    f = _booted(b, RunEnv())
    rows = b.rows()
    b.cur = 3
    scr.keys = [ord("y")]
    b.handle_key(ord(" "), rows)
    prompt = "\n".join(scr.frames[-2].values())
    assert "device is booted; shut it down first" in prompt and " y  run it, then mark" in prompt
    assert "$ xcrun simctl shutdown U1" in prompt
    assert b.env.calls == [["xcrun", "simctl", "shutdown", "U1"]]
    assert b.marked == {f.key} and b.cur == 4 and b.msg == ""
    assert f.action.argv == ["xcrun", "simctl", "erase", "U1"]
    assert f.locked is None and f.unlock is None and f.evidence == "iOS 27; last booted today"
    b.cur = 3
    b.draw(b.rows())
    assert "[x] review" in scr.text() and "xcrun simctl erase U1" in scr.text()


def test_declining_shutdown_leaves_device_locked(browser):
    b, scr = browser
    f = _booted(b, RunEnv())
    b.cur = 3
    scr.keys = [ord("n")]
    b.handle_key(ord(" "), b.rows())
    assert b.env.calls == [] and b.marked == set() and f.unlock is not None
    assert b.msg == "not removable: device is booted; shut it down first"


def test_failed_shutdown_reports_and_stays_locked(browser):
    b, scr = browser
    f = _booted(b, RunEnv(returncode=164, stderr="Unable to shutdown device: Shutdown\n"))
    b.cur = 3
    scr.keys = [ord("y")]
    b.handle_key(ord(" "), b.rows())
    assert b.marked == set() and f.action is None and f.locked and f.unlock is not None
    assert b.msg == "xcrun simctl shutdown U1 failed: Unable to shutdown device: Shutdown"
    assert b.cur == 3


def test_removable_finding_marks_and_shows_action(browser):
    b, scr = browser
    b.cur = 5
    b.draw(b.rows())
    assert "[ ] dead" in scr.text() and "delete /dd/Dead-abc" in scr.text()
    scr.keys = [ord(" "), ord("q")]
    b.collect = lambda refresh: None
    b.loop()
    assert b.marked == {"derived-data:Dead-abc"}


def test_pending_sizes_show_indicator(browser):
    b, scr = browser
    b.findings[0].size = None
    b.status = "sizing 0/1"
    b.draw(b.rows())
    txt = scr.text()
    assert "⠋ sizing 0/1" in txt
    assert "   ⠋  " in txt and "Dead-abc" in txt  # size column indicator on the row
    assert "> all  5B total · dead 0B ⠋  " in txt and "derived-data  0B total · dead 0B ⠋" in txt


def test_pending_marks_spin_together_one_frame_per_scan_tick(browser):
    """Every pending mark and the scan progress show the same spinner frame. The loop steps
    it on each tick of a running scan; a key press redraws on the same frame."""
    b, scr = browser
    b.findings[0].size = None
    b.status = "sizing 0/1"
    b.loading = True
    b.collect = lambda refresh: None
    scr.keys = [-1, -1, ord("j"), ord("q")]
    b.loop()
    assert b.spin == 2
    spins = [[c for c in "".join(f.values()) if c in tui.SPIN] for f in scr.frames]
    assert [set(f) for f in spins] == [{"⠋"}, {"⠙"}, {"⠹"}, {"⠹"}]
    assert len(spins[0]) == 5  # all, ios, derived-data, the finding's size, the progress


def _dim_lines(scr):
    return {y for y, _t, a in scr.attrs if a & curses.A_DIM and y < scr.h - 3}


def test_rows_still_being_scanned_are_grey(browser):
    """An unsized finding, every heading above it, and a group with a category whose finder
    has not returned yet are drawn dim. Done rows keep their normal look."""
    b, scr = browser
    b.cur = 3
    b.draw(b.rows())
    assert _dim_lines(scr) == set()
    b.findings[0].size = None  # Dead-abc, under ios/derived-data
    scr.attrs.clear()
    rows = b.rows()
    b.draw(rows)
    assert [r.key for r in rows] == [
        "all",
        "ios",
        "ios/sim-data",
        "sim-data:iPhone · iOS 27",
        "ios/derived-data",
        "derived-data:Dead-abc",
    ]
    assert _dim_lines(scr) == {0, 1, 4, 5}
    b.findings[0].size = 10
    b._collecting = frozenset({"archives"})  # an ios finder still running
    scr.attrs.clear()
    b.draw(b.rows())
    assert _dim_lines(scr) == {0, 1}
    assert scr.lines[1] == "  ▾ ios  15B total · dead 10B ⠋"


def test_root_row_totals_everything_and_a_on_it_marks_all_dead(browser):
    """The top `all` row totals every finding. `a` on it marks dead findings in every group,
    `a` on a group only that group's; space on it marks everything removable. It does not
    fold, and it is not drawn when nothing was found."""
    b, scr = browser
    b.findings.append(_finding("npm", "general", "old-npm", size=3))
    b.findings.append(
        Finding(
            "gradle-caches",
            "android",
            "gradle",
            ["/g"],
            "e",
            "stale",
            size=2,
            action=Action("remove", path="/g"),
        )
    )
    b.findings.sort(key=tui.sort_key)
    rows = b.rows()
    assert (rows[0].kind, rows[0].key) == ("root", "all")
    b.draw(rows)
    assert scr.lines[0] == "> all  20B total · dead 13B".ljust(140) + "sort: verdict    ?"
    dead = {"derived-data:Dead-abc", "npm:old-npm"}
    b.handle_key(ord("a"), rows)
    assert b.marked == dead
    b.handle_key(ord("a"), rows)
    assert b.marked == set()
    b.cur = 1  # ios: scoped as before
    b.handle_key(ord("a"), rows)
    assert b.marked == {"derived-data:Dead-abc"}
    b.marked.clear()
    b.cur = 0
    b.handle_key(ord(" "), rows)
    assert b.marked == dead | {"gradle-caches:gradle"}
    b.handle_key(ord("n"), rows)  # no unmark-all key: space on `all` again does it
    assert b.marked == dead | {"gradle-caches:gradle"}
    b.handle_key(ord(" "), rows)
    assert b.marked == set()
    opened, closed = set(b.open), set(b.closed)
    for k in (curses.KEY_RIGHT, curses.KEY_LEFT, ord("l"), ord("h")):
        b.handle_key(k, rows)
    assert (b.open, b.closed, b.rows()[0].key) == (opened, closed, "all")
    b.findings = []
    assert b.rows() == []


def test_category_rows_show_dead_size_and_findings_show_last_date(browser):
    b, scr = browser
    b.draw(b.rows())
    txt = scr.text()
    assert "findings" not in txt
    assert scr.lines[1] == "  ▾ ios  15B total · dead 10B"
    assert "  ▾ derived-data  10B total · dead 10B\n" in txt
    assert "  ▾ sim-data  5B total · dead 0B\n" in txt
    day = (datetime.now(UTC) - timedelta(days=33)).date().isoformat()
    assert f"{day}   33d  Dead-abc" in txt
    assert "                  iPhone · iOS 27" in txt  # no date known → blank column


def _finding(cat, group, label, size=None):
    return Finding(
        cat,
        group,
        label,
        [f"/{label}"],
        "e",
        "dead",
        size=size,
        action=Action("remove", path=f"/{label}"),
    )


def test_scan_runs_in_background_and_keys_work_meanwhile(browser, monkeypatch):
    """Category A lands at once; category B blocks until released. While B is pending the
    loop keeps taking keys, `d` refuses to act, and the cursor stays on its node as rows shift."""
    b, scr = browser
    b.findings = []
    release = threading.Event()
    started = threading.Event()

    def fake_run(cats, env, cfg):
        (cat,) = cats
        if cat == "derived-data":
            return [_finding("derived-data", "ios", "A-dd")]
        started.set()
        release.wait(5)
        return [_finding("npm", "general", "B-npm")]

    class FakeSizer:
        workers = 2

        def __init__(self, refresh=False, stop=None):
            pass

        def size_paths(self, paths):
            return 7 * len(paths)

        def save(self):
            pass

    monkeypatch.setattr(finders, "resolve", lambda sel: ["derived-data", "npm"])
    monkeypatch.setattr(finders, "run", fake_run)
    monkeypatch.setattr(tui, "Sizer", FakeSizer)
    b.open = {"ios", "general", "ios/derived-data", "general/npm"}
    b.collect(refresh=False)
    assert started.wait(5)
    # A is visible and sized, B is still collecting → interaction is enabled, acting is not
    for _ in range(50):
        if any(f.label == "A-dd" and f.size == 7 for f in b.findings):
            break
        threading.Event().wait(0.02)
    rows = b.rows()
    assert b.loading and "collecting 1/2" in b.status
    assert [r.key for r in rows] == ["all", "ios", "ios/derived-data", "derived-data:A-dd"]
    assert rows[0].pending and not rows[1].pending  # npm is still collecting
    for _ in range(3):
        b.handle_key(curses.KEY_DOWN, rows)
    assert b.cur == 3
    b.handle_key(ord(" "), rows)  # marking works mid-scan
    assert b.marked == {"derived-data:A-dd"}
    b.handle_key(ord("d"), rows)
    assert "scan still running" in b.msg
    b.handle_key(ord("r"), rows)
    assert "already running" in b.msg
    # a tick anchors the cursor on the finding's key, then B lands and rows grow
    b.cur = 3
    b.anchor = rows[b.cur].key
    release.set()
    b.wait(5)
    assert not b.loading and b.status == "" and b.msg == ""
    rows = b.rows()
    assert not rows[0].pending
    assert [r.key for r in rows] == [
        "all",
        "ios",
        "ios/derived-data",
        "derived-data:A-dd",
        "general",
        "general/npm",
        "npm:B-npm",
    ]
    assert all(f.size == 7 for f in b.findings)
    # the loop applies the anchor before drawing
    scr.keys = [ord("q")]
    b.collect = lambda refresh: None
    b.loop()
    assert b.cur == 3 and rows[b.cur].key == "derived-data:A-dd"


def test_broken_finder_is_reported_not_fatal(browser, monkeypatch):
    b, _scr = browser
    b.findings = []

    def fake_run(cats, env, cfg):
        raise RuntimeError("boom")

    class FakeSizer:
        workers = 1

        def __init__(self, refresh=False, stop=None):
            pass

        def save(self):
            pass

    monkeypatch.setattr(finders, "resolve", lambda sel: ["npm"])
    monkeypatch.setattr(finders, "run", fake_run)
    monkeypatch.setattr(tui, "Sizer", FakeSizer)
    b.collect(refresh=False)
    b.wait(5)
    assert not b.loading and b.findings == [] and "npm: boom" in b.msg


def test_act_draws_live_status_per_position(browser, monkeypatch):
    b, scr = browser
    marked = [b.findings[0]]
    monkeypatch.setattr(actions, "remove", lambda p, trash=False: "deleted")
    b.act(marked, trash=False)
    frames = ["\n".join(f.values()) for f in scr.frames]
    assert any("deleting 0/1" in f and "· wait" in f for f in frames)
    assert any("⟳ busy" in f and "derived-data/Dead-abc" in f for f in frames)
    assert "✓ ok" in frames[-1] and "deleted" in frames[-1]
    assert "done: 1 ok, 0 failed, 0 manual" in frames[-1]
    assert [f.label for f in b.findings] == ["iPhone · iOS 27"]


def test_bottom_lines_hold_message_and_detail_and_the_only_chip_is_help(browser):
    """The last three lines hold the message, the finding's evidence and its action, with no
    rule and no key line. They stay reserved on a heading: the tree never reaches them. The
    only chip on the screen is `?`, at the right end of line 0."""
    b, scr = browser
    b.cur = 5  # Dead-abc
    b.msg = "revealed /dd/Dead-abc"
    b.draw(b.rows())
    h = scr.h
    assert [scr.lines[y] for y in (h - 3, h - 2, h - 1)] == [
        "revealed /dd/Dead-abc",
        "— orphan",
        "  delete /dd/Dead-abc",
    ]
    assert not any("--" in ln for ln in scr.lines.values())
    assert [(y, t) for y, t, a in scr.attrs if a == b.key_attr] == [(0, " ? ")]
    assert scr.lines[0].endswith("    ?") and len(scr.lines[0]) == scr.w - 2
    assert b.key_attr & curses.A_REVERSE and b.key_attr & curses.A_BOLD
    scr.h, b.cur, b.msg = 8, 1, ""  # 6 rows; room for the header and 4 tree rows
    b.draw(b.rows())
    assert sorted(scr.lines) == [0, 1, 2, 3, 4]


def test_all_row_stays_on_line_0_while_the_tree_scrolls(browser):
    b, scr = browser
    scr.h = 8  # header, 4 tree rows, 3 bottom lines
    rows = b.rows()
    b.cur = 5  # Dead-abc, the last row
    b.draw(rows)
    assert scr.lines[0].startswith("  all  15B total · dead 10B  ")
    assert scr.lines[1] == "    ▾ sim-data  5B total · dead 0B"
    assert scr.lines[4].startswith(">     [ ] dead") and scr.lines[4].endswith("Dead-abc")
    b.cur = 0
    b.draw(rows)
    assert scr.lines[0].startswith("> all  ") and scr.lines[1] == "  ▾ ios  15B total · dead 10B"


def test_narrow_header_drops_progress_then_sort_and_keeps_the_help_chip(browser):
    b, scr = browser
    b.status = "collecting 3/20 · sizing 5/40"
    left = "> all  15B total · dead 10B"
    full = "⠋ collecting 3/20 · sizing 5/40   sort: verdict    ?"
    for w, tail in ((83, full), (82, "sort: verdict    ?"), (49, "sort: verdict    ?"), (48, " ?")):
        scr.w = w
        b.draw(b.rows())
        assert scr.lines[0] == left.ljust(w - 2 - len(tail)) + tail, w
    scr.w = 20  # not even the chip fits beside the totals: they are cut short
    b.draw(b.rows())
    assert scr.lines[0] == "> all  15B tot   ?"


def _dev(name, rt, size, verdict="review"):
    return Finding(
        "sim-devices",
        "ios",
        f"{name} · {rt}",
        [f"/sim/{name}-{rt}"],
        "e",
        verdict,
        size=size,
        bucket=rt,
        action=Action("argv", argv=["xcrun", "simctl", "delete", name]),
    )


def test_sim_devices_are_grouped_by_runtime(browser):
    """Buckets sort newest first, open by default, carry totals, and fold with ← / mark with space."""
    b, scr = browser
    b.findings = sorted(
        [
            _dev("iPhone 17", "iOS 26.0", 3),
            _dev("iPhone 17", "iOS 27.0", 5, "dead"),
            _dev("iPad", "iOS 27.0", 4),
            _dev("Apple Watch", "watchOS 12.0", 1),
        ],
        key=tui.sort_key,
    )
    b.open = {"ios", "ios/sim-devices"}
    rows = b.rows()
    assert [(r.kind, r.key) for r in rows] == [
        ("root", "all"),
        ("group", "ios"),
        ("cat", "ios/sim-devices"),
        ("bucket", "ios/sim-devices/iOS 27.0"),
        ("finding", "sim-devices:iPhone 17 · iOS 27.0"),
        ("finding", "sim-devices:iPad · iOS 27.0"),
        ("bucket", "ios/sim-devices/iOS 26.0"),
        ("finding", "sim-devices:iPhone 17 · iOS 26.0"),
        ("bucket", "ios/sim-devices/watchOS 12.0"),
        ("finding", "sim-devices:Apple Watch · watchOS 12.0"),
    ]
    b.draw(rows)
    txt = scr.text()
    assert "▾ iOS 27.0  9B total · dead 5B\n" in txt
    assert "iPhone 17\n" in txt + "\n" and "iPhone 17 · iOS 27.0" not in txt  # suffix folded
    # space on the bucket marks everything in it; ← on a finding folds its bucket
    b.cur = 3
    b.handle_key(ord(" "), rows)
    assert b.marked == {"sim-devices:iPhone 17 · iOS 27.0", "sim-devices:iPad · iOS 27.0"}
    b.cur = 7
    b.handle_key(curses.KEY_LEFT, rows)
    assert b.cur == 6 and "ios/sim-devices/iOS 26.0" in b.closed
    assert [r.key for r in b.rows()][6:8] == [
        "ios/sim-devices/iOS 26.0",
        "ios/sim-devices/watchOS 12.0",
    ]
    b.handle_key(curses.KEY_RIGHT, b.rows())
    assert "ios/sim-devices/iOS 26.0" not in b.closed


def test_o_reveals_the_finding_path(browser, monkeypatch):
    b, _scr = browser
    calls = []
    monkeypatch.setattr(tui, "reveal", lambda p: calls.append(p))
    rows = b.rows()
    b.cur = 1  # group row: nothing to reveal
    b.handle_key(ord("o"), rows)
    assert calls == [] and "nothing to reveal" in b.msg
    b.cur = 5  # Dead-abc
    b.handle_key(ord("o"), rows)
    assert calls == ["/dd/Dead-abc"] and b.msg == "revealed /dd/Dead-abc"


def test_reveal_reports_missing_path_and_spawns_open(monkeypatch, tmp_path):
    assert tui.reveal(str(tmp_path / "nope")).startswith("gone: ")
    spawned = []
    monkeypatch.setattr(tui.subprocess, "Popen", lambda argv, **kw: spawned.append(argv))
    monkeypatch.setattr(tui.sys, "platform", "darwin")
    assert tui.reveal(str(tmp_path)) is None
    assert spawned == [["open", "-R", str(tmp_path)]]


def test_s_cycles_sort_within_a_branch(browser):
    b, scr = browser
    old = datetime.now(UTC) - timedelta(days=300)
    b.findings = sorted(
        [
            _dev("small-old", "iOS 27.0", 1, "review"),
            _dev("big-new", "iOS 27.0", 9, "review"),
            _dev("mid-dead", "iOS 27.0", 5, "dead"),
        ],
        key=tui.sort_key,
    )
    b.findings[[f.label for f in b.findings].index("small-old · iOS 27.0")].last = old
    b.findings[[f.label for f in b.findings].index("big-new · iOS 27.0")].last = datetime.now(UTC)
    b.open = {"ios", "ios/sim-devices"}

    def labels():
        return [r.finding.display_label for r in b.rows() if r.kind == "finding"]

    assert labels() == ["mid-dead", "big-new", "small-old"]  # verdict, then size
    rows = b.rows()
    b.cur = 4
    b.handle_key(ord("s"), rows)
    assert labels() == ["big-new", "mid-dead", "small-old"] and b.anchor == rows[4].key
    b.draw(b.rows())
    assert "sort: size ↓" in scr.text()
    b.handle_key(ord("s"), b.rows())
    assert labels() == ["small-old", "big-new", "mid-dead"]  # oldest first, undated last
    b.handle_key(ord("s"), b.rows())
    assert labels() == ["mid-dead", "big-new", "small-old"]


def test_e_expands_and_folds_only_the_branch_under_the_cursor(browser):
    """`e` on a heading opens everything under it while any finding there is hidden. Once they
    all show, it closes the row each of them hangs from, so categories and runtime buckets stay
    listed. Siblings keep their state. On `all` it covers the whole tree; on a finding, the
    finding's own branch. The cursor keeps its row, or moves to the finding's parent."""
    b, _scr = browser
    b.findings = sorted(
        [
            *b.findings,
            _dev("iPhone 17", "iOS 27.0", 5),
            _dev("iPad", "iOS 26.0", 4),
            _finding("npm", "general", "old-npm", size=3),
        ],
        key=tui.sort_key,
    )
    b.open = {"ios"}  # general and every category folded
    b.closed = {"ios/sim-devices/iOS 26.0"}

    def f_on(key):
        rows = b.rows()
        b.cur = next(i for i, r in enumerate(rows) if r.key == key)
        b.handle_key(ord("e"), rows)

    def shown():
        return [r.key for r in b.rows() if r.kind == "finding"]

    f_on("all")
    assert len(shown()) == len(b.findings) == 5 and b.anchor == "all"
    f_on("ios/derived-data")  # its siblings keep their findings
    assert len(shown()) == 4 and "derived-data:Dead-abc" not in shown()
    assert b.anchor == "ios/derived-data"
    f_on("ios")  # derived-data is hidden, so the group unfolds
    assert len(shown()) == 5 and b.anchor == "ios"
    f_on("ios")  # all of ios shows: its findings fold away, general keeps npm open
    assert [(r.kind, r.key) for r in b.rows()] == [
        ("root", "all"),
        ("group", "ios"),
        ("cat", "ios/sim-devices"),
        ("bucket", "ios/sim-devices/iOS 27.0"),
        ("bucket", "ios/sim-devices/iOS 26.0"),
        ("cat", "ios/sim-data"),
        ("cat", "ios/derived-data"),
        ("group", "general"),
        ("cat", "general/npm"),
        ("finding", "npm:old-npm"),
    ]
    f_on("npm:old-npm")  # a finding: its own branch, and the cursor moves to it
    assert shown() == [] and b.anchor == "general/npm"
    f_on("all")
    assert len(shown()) == 5


def test_quitting_mid_scan_stops_workers_promptly(browser, monkeypatch):
    """Finder B never returns on its own: it polls the stop flag. `q` → stop() sets it, drops the
    queued sizing tasks, and joins the scan thread before the loop returns."""
    b, scr = browser
    b.findings = []
    started = threading.Event()
    sized = []

    def fake_run(cats, env, cfg):
        (cat,) = cats
        if cat == "derived-data":
            return [_finding("derived-data", "ios", f"dd-{i}") for i in range(20)]
        started.set()
        while not b._stop.is_set():
            threading.Event().wait(0.005)
        return [_finding("npm", "general", "late")]

    class SlowSizer:
        workers = 2

        def __init__(self, refresh=False, stop=None):
            self.stop = stop

        def size_paths(self, paths):
            for _ in range(200):  # a long walk that polls stop like walk_size does
                if self.stop():
                    raise tui.Cancelled(paths[0])
                threading.Event().wait(0.002)
            sized.append(paths[0])
            return 1

        def save(self):
            pass

    monkeypatch.setattr(finders, "resolve", lambda sel: ["derived-data", "npm"])
    monkeypatch.setattr(finders, "run", fake_run)
    monkeypatch.setattr(tui, "Sizer", SlowSizer)
    scr.getch = lambda: (started.wait(5), ord("q"))[1]  # press q once B is mid-flight
    t0 = datetime.now(UTC)
    b.loop()
    b.stop()
    assert started.is_set()
    assert not b._thread.is_alive()
    assert (datetime.now(UTC) - t0).total_seconds() < 3  # not 20 walks × 0.4s
    assert len(sized) < 20 and not b.loading
    assert threading.active_count() == 1


def test_marked_size_shows_in_tree_rows_and_the_all_row(browser):
    b, scr = browser
    b.draw(b.rows())
    assert "marked" not in scr.text()  # nothing marked → rows stay as they were
    b.marked = {"derived-data:Dead-abc"}
    b.draw(b.rows())
    txt = scr.text()
    assert scr.lines[0].startswith("> all  15B total · dead 10B · marked 10B  ")
    assert scr.lines[1] == "  ▾ ios  15B total · dead 10B · marked 10B"
    assert "  ▾ derived-data  10B total · dead 10B · marked 10B\n" in txt
    assert "  ▾ sim-data  5B total · dead 0B\n" in txt


G = 1_000_000_000


def test_all_row_is_the_header_with_sort_and_help_chip(browser):
    """Line 0 is the `all` row: totals on the left, the sort and the `?` chip at the right
    end. No title, no marked chip, no disk bar: the tree starts on line 1."""
    b, scr = browser
    b.draw(b.rows())
    assert scr.lines[0] == "> all  15B total · dead 10B".ljust(140) + "sort: verdict    ?"
    assert scr.lines[1].startswith("  ▾ ios")


def test_scan_progress_sits_left_of_the_sort_on_the_all_row(colour_browser):
    """Scan progress in the key colour, then the sort, then the `?` chip. The row is drawn
    before the first finding lands, dim while the scan runs."""
    b, scr = colour_browser
    b.findings = [_finding("npm", "general", "old-npm", size=3)]
    b.disk = (400 * G, 1000 * G)
    b.status, b.msg = "collecting 3/20 · sizing 5/40", "revealed /dd/Dead-abc"
    b.draw(b.rows())
    tail = "⠋ collecting 3/20 · sizing 5/40   sort: verdict    ?"
    left = "> all  3B total · dead 3B · 400.0G free"
    assert scr.lines[0] == left.ljust(scr.w - 2 - len(tail)) + tail
    sel = curses.A_REVERSE | curses.A_BOLD
    assert [(t, a) for y, t, a in scr.attrs if y == 0] == [
        ("> all", sel),
        ("  ", sel),
        ("3B total · ", sel),
        ("dead 3B", sel | b.colors["dead"]),
        (" · ", sel),
        ("400.0G free", sel),
        ("⠋ collecting 3/20 · sizing 5/40", b.colors["key"] | curses.A_BOLD),
        ("sort: verdict", curses.A_BOLD),
        (" ? ", b.key_attr),
    ]
    assert scr.lines[scr.h - 3] == "revealed /dd/Dead-abc"
    b.findings, b.loading, b.status = [], True, "starting"
    scr.attrs.clear()
    b.draw(b.rows())
    tail = "⠋ starting   sort: verdict    ?"
    assert scr.lines[0] == "  all  400.0G free ⠋".ljust(scr.w - 2 - len(tail)) + tail
    assert scr.attrs[0][2] == curses.A_DIM


@pytest.fixture
def colour_browser(monkeypatch):
    monkeypatch.setattr(curses, "has_colors", lambda: True)
    monkeypatch.setattr(curses, "start_color", lambda: None)
    monkeypatch.setattr(curses, "use_default_colors", lambda: None)
    monkeypatch.setattr(curses, "init_pair", lambda i, fg, bg: None)
    monkeypatch.setattr(curses, "color_pair", lambda i: i << 8)
    monkeypatch.setattr(curses, "curs_set", lambda _v: None)
    scr = FakeScreen()
    return tui.Browser(scr, env=None, cfg=None, selectors=None), scr


def test_dead_size_is_red_and_marked_size_yellow_in_heading_totals(colour_browser):
    """A dead size above zero takes the dead colour, the marked size the marking colour. The
    rest of a heading keeps the row's look, and `dead 0B` stays plain."""
    b, scr = colour_browser
    brew = Finding(
        "homebrew",
        "general",
        "brew",
        ["/b"],
        "e",
        "review",
        size=5,
        action=Action("remove", path="/b"),
    )
    b.findings = sorted([_finding("npm", "general", "old-npm", size=3), brew], key=tui.sort_key)
    b.marked = {"npm:old-npm"}
    b.cur = 1  # the group row: selected, bold
    b.draw(b.rows())
    red, yellow = b.colors["dead"], b.colors["stale"]
    sel = curses.A_REVERSE | curses.A_BOLD

    def line(y):
        return [(t, a) for yy, t, a in scr.attrs if yy == y]

    assert line(1) == [
        ("> ▾ general  ", sel),
        ("8B total · ", sel),
        ("dead 3B", sel | red),
        (" · ", sel),
        ("marked 3B", sel | yellow),
    ]
    assert [r.key for r in b.rows()][2:] == ["general/npm", "general/homebrew"]
    assert line(2)[1:] == [
        ("3B total · ", 0),
        ("dead 3B", red),
        (" · ", 0),
        ("marked 3B", yellow),
    ]
    assert line(3)[1:] == [("5B total · ", 0), ("dead 0B", 0)]


def test_keys_box_lists_every_key_over_the_tree_and_any_key_closes_it(browser):
    """`?` draws an ASCII box in the middle of the screen, over the tree: one key per line as
    a chip, then what the marks mean. It waits for a key even while a scan ticks the loop."""
    b, scr = browser
    b.draw(b.rows())
    scr.keys = [ord("x")]
    b.handle_key(ord("?"), b.rows())
    assert scr.keys == [] and scr.delay == -1
    box = scr.frames[-1]
    assert box[0].startswith("> all  ")  # the box leaves the header in view
    top, bottom = [y for y, ln in box.items() if "+--" in ln]
    x0 = box[top].index("+--")
    width = len(box[top]) - x0
    assert box[top][x0:].startswith("+-- keys --") and box[top].endswith("-+")
    assert box[bottom][x0:].startswith("+-- any key closes --") and len(box[bottom]) == x0 + width
    inside = [box[y][x0:] for y in range(top + 1, bottom)]
    assert all(ln.startswith("|  ") and ln.endswith("  |") and len(ln) == width for ln in inside)
    texts = [ln[3:-3].rstrip() for ln in inside]
    assert texts[: len(tui.HELP)] == [
        f" {k} ".ljust(max(len(k) for k, _d in tui.HELP) + 2) + f" {d}" for k, d in tui.HELP
    ]
    assert texts[len(tui.HELP) :] == ["", "marks", *tui.LEGEND]
    assert x0 == (scr.w - 1 - width) // 2
    b.findings, scr.delay = [], None  # before the first finding lands
    b.handle_key(ord("?"), [])
    assert scr.delay == -1 and "+-- keys --" in "\n".join(scr.frames[-1].values())


def test_marking_keys_are_yellow_and_d_is_red(colour_browser):
    b, scr = colour_browser
    red = b.colors["dead"] | curses.A_BOLD | curses.A_REVERSE
    b.draw([])
    assert [(t, a) for y, t, a in scr.attrs if a & curses.A_REVERSE] == [(" ? ", b.key_attr)]
    scr.attrs.clear()
    b.show_keys()
    keys = {t.strip(): a for y, t, a in scr.attrs if a & curses.A_REVERSE}
    assert list(keys) == [k for k, _d in tui.HELP]
    assert keys["space"] == keys["a"] == b.mark_attr and keys["d"] == red
    assert {keys[k] for k in ("↑↓ jk", "e", "s", "?", "q")} == {b.key_attr}
    assert len({b.mark_attr, red, b.key_attr}) == 3


def test_without_colour_every_chip_is_bold_reverse(browser):
    b, scr = browser
    b.draw([])
    b.show_keys()
    chips = {a for _y, _t, a in scr.attrs if a & curses.A_REVERSE}
    assert chips == {curses.A_BOLD | curses.A_REVERSE}


def test_free_space_projects_the_marked_size_capped_at_the_used_space():
    assert tui._free((400 * G, 1000 * G), 0) == "400.0G free"
    assert tui._free((400 * G, 1000 * G), 100 * G) == "400.0G free → 500.0G"
    assert tui._free((400 * G, 1000 * G), 700 * G) == "400.0G free → 1.0T"


class DiskEnv:
    """Hands out one (free, total) reading per call; an extra call fails the scan."""

    simctl_error = None

    def __init__(self, *readings):
        self.readings = list(readings)

    def disk_usage(self):
        return self.readings.pop(0)


def test_disk_is_read_at_startup_when_a_scan_finishes_and_after_act(browser, monkeypatch):
    b, scr = browser
    found = b.findings
    b.env = DiskEnv((400 * G, 1000 * G), (390 * G, 1000 * G), (410_500_000_000, 1000 * G))

    class FakeSizer:
        workers = 1

        def __init__(self, refresh=False, stop=None):
            pass

        def save(self):
            pass

    monkeypatch.setattr(finders, "resolve", lambda sel: ["derived-data", "sim-data"])
    monkeypatch.setattr(
        finders, "run", lambda cats, env, cfg: [f for f in found if f.category in cats]
    )
    monkeypatch.setattr(tui, "Sizer", FakeSizer)
    b.loop()  # reads once at startup, starts the scan; q quits at once
    b.wait(5)
    assert b.disk == (390 * G, 1000 * G) and b.env.readings == [(410_500_000_000, 1000 * G)]
    b.marked = {"derived-data:Dead-abc"}
    next(f for f in b.findings if f.label == "Dead-abc").size = 5 * G
    for _ in range(3):  # redraws reuse the reading
        b.draw(b.rows())
    assert " · marked 5.0G · 390.0G free → 395.0G  " in scr.lines[0]
    monkeypatch.setattr(actions, "remove", lambda p, trash=False: "deleted")
    b.act([f for f in b.findings if f.label == "Dead-abc"], trash=False)
    b.draw(b.rows())
    assert scr.lines[0].startswith("> all  5B total · dead 0B · 410.5G free  ")
    assert b.env.readings == []


def test_confirm_screen_lists_the_plan_and_any_other_key_goes_back(browser, monkeypatch):
    b, scr = browser
    acted = []
    monkeypatch.setattr(b, "act", lambda marked, trash: acted.append(trash))
    b.marked = {"derived-data:Dead-abc"}
    scr.keys = [ord("n")]
    b.confirm_and_act()
    screen = scr.frames[-1]
    assert (
        screen[0]
        == "about to run 1 action(s)    y  delete   t  move to ~/.Trash instead   any other key  back"
    )
    assert "delete /dd/Dead-abc" in "\n".join(screen.values())
    assert screen[scr.h - 1] == "~/.Trash keeps the space until it is emptied"
    assert acted == []
    scr.keys = [ord("t")]
    b.confirm_and_act()
    assert acted == [True]


def test_screen_never_draws_a_run_of_one_non_ascii_character(browser):
    """ncurses may send a run of identical cells as the character once plus `rep`
    (CSI <n> b). macOS's ncurses 6.0 puts only the low byte of a wide character there, so such
    a run reaches the terminal as invalid UTF-8. Runs must stay ASCII."""
    b, scr = browser
    b.disk = (183_800_000_000, 994_610_155_520)
    b.marked = {"derived-data:Dead-abc"}
    b.findings[0].size = 50_000_000_000
    for w in (160, 80):
        scr.w = w
        b.draw(b.rows())
        b.draw_keys()
        for _y, text, _attr in scr.attrs:
            runs = re.findall(r"([^\x00-\x7f])\1", text)
            assert not runs, f"run of {runs[0]!r} in {text!r}"


REP_TERMINFO = (
    "mb-rep|xterm-256color plus rep,\n\trep=%p1%c\\E[%p2%{1}%-%db,\n\tuse=xterm-256color,\n"
)
PTY_CHILD = """
import curses
from mobile_broom import tui

def main(scr):
    b = tui.Browser(scr, env=None, cfg=None, selectors=None)
    b.disk = (183_800_000_000, 994_610_155_520)
    b.loading, b.status = True, "starting"  # the spinner goes out too
    b.draw([])
    b.draw_keys()
    scr.refresh()

curses.wrapper(main)
"""


@pytest.mark.skipif(shutil.which("tic") is None, reason="needs tic to compile a terminfo entry")
def test_screen_and_keys_box_reach_a_rep_terminal_as_valid_utf8(tmp_path):
    """The real curses, in a pty, with a terminfo entry that has `rep` (as Ghostty's
    xterm-ghostty does). The bytes must decode as UTF-8, and `rep` must follow only ASCII."""
    (tmp_path / "rep.ti").write_text(REP_TERMINFO)
    subprocess.run(
        ["tic", "-x", "-o", str(tmp_path / "terminfo"), str(tmp_path / "rep.ti")],
        check=True,
        capture_output=True,
    )
    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))
    env = dict(os.environ, TERM="mb-rep", TERMINFO=str(tmp_path / "terminfo"), LC_ALL="en_US.UTF-8")
    child = subprocess.Popen(
        [sys.executable, "-c", PTY_CHILD], stdin=slave, stdout=slave, stderr=slave, env=env
    )
    os.close(slave)
    out = b""
    while select.select([master], [], [], 10)[0]:
        try:
            chunk = os.read(master, 65536)
        except OSError:  # EIO once the child has exited
            break
        if not chunk:
            break
        out += chunk
    os.close(master)
    assert child.wait(10) == 0, out
    reps = re.findall(rb"(.)\x1b\[\d+b", out, re.DOTALL)
    assert reps, "the terminfo entry's rep was not used; the test proves nothing"
    assert all(c[0] < 0x80 for c in reps), reps
    assert b"-" in reps  # the box border arrived
    assert "⠋".encode() in out  # the spinner arrived
    out.decode("utf-8")  # raises on a run of U+2588, which arrives as b"\x88\x1b[32b"
