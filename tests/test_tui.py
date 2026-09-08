"""TUI logic against a fake curses screen: tree rows, the locked explainer, live act table."""

import curses

import pytest

from mobile_broom import actions, tui
from mobile_broom.model import Action, Finding


class FakeScreen:
    def __init__(self, h=30, w=160):
        self.h, self.w = h, w
        self.lines: dict[int, str] = {}
        self.frames: list[dict[int, str]] = []
        self.keys: list[int] = []

    def getmaxyx(self):
        return self.h, self.w

    def erase(self):
        self.lines = {}

    def addnstr(self, y, x, text, n, attr=0):
        cur = self.lines.get(y, "").ljust(x)
        self.lines[y] = (cur[:x] + text[:n]).rstrip()

    def refresh(self):
        self.frames.append(dict(self.lines))

    def getch(self):
        return self.keys.pop(0) if self.keys else ord("q")

    def keypad(self, _flag):
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
    assert "…    Dead-abc" in txt  # size column indicator on the row
    assert "2 findings …" in txt and "derived-data  0B · 1 …" in txt


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
