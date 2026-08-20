import io

from conftest import mkfile

from mobile_broom import actions
from mobile_broom.model import Action, Finding


def f(label, action, size=1, verdict="dead"):
    return Finding(
        category="derived-data",
        group="ios",
        label=label,
        paths=[],
        evidence="e",
        verdict=verdict,
        size=size,
        action=action,
    )


def test_trash_moves_and_handles_collisions(tmp_path, monkeypatch):
    monkeypatch.setenv("MOBILE_BROOM_TRASH", str(tmp_path / "trash"))
    a = mkfile(tmp_path / "x" / "thing" / "f", size=10).parent
    b = mkfile(tmp_path / "y" / "thing" / "f", size=10).parent
    assert actions.trash(str(a)).startswith("→ ")
    assert not a.exists()
    note = actions.trash(str(b))
    assert not b.exists()
    assert len(list((tmp_path / "trash").iterdir())) == 2, note
    assert actions.trash(str(b)) == "already gone"


def test_trash_purge_deletes(tmp_path, monkeypatch):
    monkeypatch.setenv("MOBILE_BROOM_TRASH", str(tmp_path / "trash"))
    a = mkfile(tmp_path / "x" / "f", size=10).parent
    assert actions.trash(str(a), purge=True) == "deleted"
    assert not a.exists() and not (tmp_path / "trash").exists()


def test_plan_dedupes_shared_argv():
    shared = Action(kind="argv", argv=["xcrun", "simctl", "delete", "unavailable"])
    steps = actions.plan([f("a", shared), f("b", shared), f("c", Action(kind="trash", path="/p"))])
    assert len(steps) == 2
    assert [len(fs) for _a, fs in steps] == [2, 1]


def test_root_owned_trash_becomes_manual(monkeypatch):
    monkeypatch.setattr(actions, "is_root_owned", lambda p: True)
    out = io.StringIO()
    res = actions.execute([f("r", Action(kind="trash", path="/Library/root-thing"))], out=out)
    assert res[0].note == "manual" and "sudo rm -rf /Library/root-thing" in out.getvalue()


def test_dry_run_runs_only_dry_run_argv(tmp_path):
    calls = []

    class R:
        def __init__(self, argv):
            self.returncode, self.stdout, self.stderr = 0, "Would delete X", ""

    def runner(argv):
        calls.append(argv)
        return R(argv)

    victim = mkfile(tmp_path / "v" / "f", size=1).parent
    out = io.StringIO()
    res = actions.execute(
        [
            f(
                "rt",
                Action(
                    kind="argv",
                    argv=["xcrun", "simctl", "runtime", "delete", "U"],
                    dry_run_argv=["xcrun", "simctl", "runtime", "delete", "U", "--dry-run"],
                ),
            ),
            f("dd", Action(kind="trash", path=str(victim))),
        ],
        dry_run=True,
        out=out,
        runner=runner,
    )
    assert calls == [["xcrun", "simctl", "runtime", "delete", "U", "--dry-run"]]
    assert victim.exists()
    assert all(r.ok and r.note == "dry-run" for r in res)
    assert "would" in out.getvalue() and "Would delete X" in out.getvalue()


def test_execute_runs_argv_and_trashes(tmp_path, monkeypatch):
    monkeypatch.setenv("MOBILE_BROOM_TRASH", str(tmp_path / "trash"))
    calls = []

    class R:
        def __init__(self, argv, rc):
            self.returncode, self.stdout, self.stderr = rc, "ok", "boom"

    def runner(argv):
        calls.append(argv)
        return R(argv, 1 if "fail" in argv else 0)

    victim = mkfile(tmp_path / "v" / "f", size=1).parent
    res = actions.execute(
        [
            f("ok", Action(kind="argv", argv=["tool", "go"])),
            f("bad", Action(kind="argv", argv=["tool", "fail"])),
            f("dd", Action(kind="trash", path=str(victim))),
        ],
        out=io.StringIO(),
        runner=runner,
    )
    assert calls == [["tool", "go"], ["tool", "fail"]]
    assert [r.ok for r in res] == [True, False, True]
    assert not victim.exists()
