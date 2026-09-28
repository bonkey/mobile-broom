"""TUI logic against a fake curses screen: tree rows, the locked explainer, live act table,
and the background scan that fills the tree while keys keep working."""

import curses
import threading
from datetime import UTC, datetime, timedelta

import pytest

from mobile_broom import actions, finders, tui
from mobile_broom.model import Action, Finding


class FakeScreen:
    def __init__(self, h=30, w=160):
        self.h, self.w = h, w
        self.lines: dict[int, str] = {}
        self.attrs: list[tuple] = []
        self.frames: list[dict[int, str]] = []
        self.keys: list[int] = []

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

    def timeout(self, _ms):
        pass

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
    assert [r.kind for r in rows] == ["group", "cat", "finding", "cat", "finding"]
    b.cur = 2  # the booted device (sim-data sorts before derived-data)
    b.draw(rows)
    assert "[-] review" in scr.text()
    assert "locked: device is booted; shut it down first" in scr.text()
    scr.keys = [ord(" "), ord("q")]
    b.collect = lambda refresh: None
    b.loop()
    assert b.marked == set()
    assert "not removable: device is booted" in scr.text()


def test_removable_finding_marks_and_shows_action(browser):
    b, scr = browser
    b.cur = 4
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
    assert "⟳ sizing 0/1" in txt
    assert "…  " in txt and "Dead-abc" in txt  # size column indicator on the row
    assert "2 findings …" in txt and "derived-data  0B total · dead 0B · 1 findings …" in txt


def test_category_rows_show_dead_size_and_findings_show_last_date(browser):
    b, scr = browser
    b.draw(b.rows())
    txt = scr.text()
    assert "ios  15B total · dead 10B · 2 findings" in txt
    assert "derived-data  10B total · dead 10B · 1 findings" in txt
    assert "sim-data  5B total · dead 0B · 1 findings" in txt
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
    assert [r.key for r in rows] == ["ios", "ios/derived-data", "derived-data:A-dd"]
    b.handle_key(curses.KEY_DOWN, rows)
    b.handle_key(curses.KEY_DOWN, rows)
    assert b.cur == 2
    b.handle_key(ord(" "), rows)  # marking works mid-scan
    assert b.marked == {"derived-data:A-dd"}
    b.handle_key(ord("d"), rows)
    assert "scan still running" in b.msg
    b.handle_key(ord("r"), rows)
    assert "already running" in b.msg
    # a tick anchors the cursor on the finding's key, then B lands and rows grow
    b.cur = 2
    b.anchor = rows[b.cur].key
    release.set()
    b.wait(5)
    assert not b.loading and b.status == "" and "2 findings" in b.msg
    rows = b.rows()
    assert [r.key for r in rows] == [
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
    assert b.cur == 2 and rows[b.cur].key == "derived-data:A-dd"


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


def test_key_hints_are_drawn_as_highlighted_chips(browser):
    """Every key on the help line is a chip (bold+reverse), the description is dim."""
    b, scr = browser
    b.draw(b.rows())
    chips = [(t, a) for y, t, a in scr.attrs if y == 1 and a == b.key_attr]
    assert [t for t, _a in chips] == [f" {k} " for k, _d in tui.HELP]
    assert b.key_attr & curses.A_REVERSE and b.key_attr & curses.A_BOLD
    descs = [t for y, t, a in scr.attrs if y == 1 and a == curses.A_DIM]
    assert " move" in descs and " quit" in descs
    # a narrow terminal wraps the hints onto the spare line instead of cutting chips off
    scr.attrs.clear()
    scr.w = 60
    b.draw(b.rows())
    chips = [(y, t) for y, t, a in scr.attrs if a == b.key_attr and 0 < y < 5]
    assert [t for _y, t in chips] == [f" {k} " for k, _d in tui.HELP]
    assert {y for y, _t in chips} == {1, 2, 3}
    assert scr.lines[5].startswith("> ▾ ios")  # the tree starts below the wrapped hints
    scr.w = 160
    scr.attrs.clear()
    b.show_keys()
    assert sum(1 for _y, _t, a in scr.attrs if a == b.key_attr) == len(tui.HELP) + 1


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
    assert "▾ iOS 27.0  9B total · dead 5B · 2 findings" in txt
    assert "iPhone 17\n" in txt + "\n" and "iPhone 17 · iOS 27.0" not in txt  # suffix folded
    # space on the bucket marks everything in it; ← on a finding folds its bucket
    b.cur = 2
    b.handle_key(ord(" "), rows)
    assert b.marked == {"sim-devices:iPhone 17 · iOS 27.0", "sim-devices:iPad · iOS 27.0"}
    b.cur = 6
    b.handle_key(curses.KEY_LEFT, rows)
    assert b.cur == 5 and "ios/sim-devices/iOS 26.0" in b.closed
    assert [r.key for r in b.rows()][5:7] == [
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
    b.cur = 0  # group row: nothing to reveal
    b.handle_key(ord("o"), rows)
    assert calls == [] and "nothing to reveal" in b.msg
    b.cur = 4  # Dead-abc
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
    b.cur = 3
    b.handle_key(ord("s"), rows)
    assert labels() == ["big-new", "mid-dead", "small-old"] and b.anchor == rows[3].key
    b.draw(b.rows())
    assert "sort: size ↓" in scr.text()
    b.handle_key(ord("s"), b.rows())
    assert labels() == ["small-old", "big-new", "mid-dead"]  # oldest first, undated last
    b.handle_key(ord("s"), b.rows())
    assert labels() == ["mid-dead", "big-new", "small-old"]


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


def test_marked_size_shows_in_tree_rows_and_header_chip(browser):
    b, scr = browser
    b.draw(b.rows())
    assert "marked" not in scr.lines[3]  # nothing marked → rows stay as they were
    b.marked = {"derived-data:Dead-abc"}
    scr.attrs.clear()
    b.draw(b.rows())
    txt = scr.text()
    assert "ios  15B total · dead 10B · marked 10B · 2 findings" in txt
    assert "derived-data  10B total · dead 10B · marked 10B · 1 findings" in txt
    assert "sim-data  5B total · dead 0B · 1 findings" in txt
    chip = next((t, a) for y, t, a in scr.attrs if y == 0 and "marked" in t)
    assert chip[0] == " marked 1 · 10B " and chip[1] & curses.A_REVERSE
