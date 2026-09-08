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


def test_remove_deletes_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("MOBILE_BROOM_TRASH", str(tmp_path / "trash"))
    a = mkfile(tmp_path / "x" / "f", size=10).parent
    assert actions.remove(str(a)) == "deleted"
    assert not a.exists() and not (tmp_path / "trash").exists()
    assert actions.remove(str(a)) == "already gone"


def test_remove_trash_moves_and_handles_collisions(tmp_path, monkeypatch):
    monkeypatch.setenv("MOBILE_BROOM_TRASH", str(tmp_path / "trash"))
    a = mkfile(tmp_path / "x" / "thing" / "f", size=10).parent
    b = mkfile(tmp_path / "y" / "thing" / "f", size=10).parent
    assert actions.remove(str(a), trash=True).startswith("→ ")
    assert not a.exists()
    note = actions.remove(str(b), trash=True)
    assert not b.exists()
    assert len(list((tmp_path / "trash").iterdir())) == 2, note


def test_plan_dedupes_shared_argv():
    shared = Action(kind="argv", argv=["xcrun", "simctl", "delete", "unavailable"])
    steps = actions.plan([f("a", shared), f("b", shared), f("c", Action(kind="remove", path="/p"))])
    assert len(steps) == 2
    assert [len(fs) for _a, fs in steps] == [2, 1]


def test_root_owned_remove_becomes_manual_and_is_quoted(monkeypatch):
    monkeypatch.setattr(actions, "is_root_owned", lambda p: True)
    out = io.StringIO()
    res = actions.execute([f("r", Action(kind="remove", path="/Library/has space;semi"))], out=out)
    assert res[0].status == actions.MANUAL and not res[0].ok
    assert "sudo rm -rf '/Library/has space;semi'" in out.getvalue()


def test_manual_argv_is_shell_quoted():
    out = io.StringIO()
    actions.execute(
        [
            f(
                "si",
                Action(
                    kind="print",
                    argv=[
                        "sdkmanager",
                        "--uninstall",
                        "system-images;android-34;google_apis;arm64-v8a",
                    ],
                ),
            )
        ],
        out=out,
    )
    assert "'system-images;android-34;google_apis;arm64-v8a'" in out.getvalue()


def test_remove_extra_paths(tmp_path):
    d = mkfile(tmp_path / "Pixel.avd" / "config.ini", text="x").parent
    ini = mkfile(tmp_path / "Pixel.ini", text="x")
    res = actions.execute(
        [f("avd", Action(kind="remove", path=str(d), extra_paths=[str(ini)]))], out=io.StringIO()
    )
    assert res[0].ok and not d.exists() and not ini.exists()


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
            f("dd", Action(kind="remove", path=str(victim))),
        ],
        dry_run=True,
        out=out,
        runner=runner,
    )
    assert calls == [["xcrun", "simctl", "runtime", "delete", "U", "--dry-run"]]
    assert victim.exists()
    assert all(r.ok and r.status == actions.DRY for r in res)
    assert "would" in out.getvalue() and "Would delete X" in out.getvalue()
    assert actions.summary(res) == "dry-run: 2 would run"


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
            f("dd", Action(kind="remove", path=str(victim))),
        ],
        out=io.StringIO(),
        runner=runner,
    )
    assert calls == [["tool", "go"], ["tool", "fail"]]
    assert [r.ok for r in res] == [True, False, True]
    assert not victim.exists()
    assert actions.summary(res) == "done: 2 ok, 1 failed, 0 manual"


def test_execute_reports_each_finding_and_calls_progress(tmp_path):
    """One status line per finding — even when several share one deduped action — and
    the progress callback sees busy → ok/FAIL per finding, in order."""
    shared = Action(kind="argv", argv=["xcrun", "simctl", "delete", "unavailable"])
    victim = mkfile(tmp_path / "v" / "f", size=1).parent
    fs = [f("dev-a", shared), f("dev-b", shared), f("dd", Action(kind="remove", path=str(victim)))]
    seen = []

    class R:
        returncode, stdout, stderr = 0, "done", ""

    out = io.StringIO()
    res = actions.execute(
        fs,
        out=out,
        runner=lambda argv: R(),
        progress=lambda f, st, note: seen.append((f.label, st)),
    )
    lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 3
    assert [ln.split()[0] for ln in lines] == ["ok", "ok", "ok"]
    assert "derived-data/dev-a" in lines[0] and "derived-data/dev-b" in lines[1]
    assert "deleted" in lines[2]
    assert seen == [
        ("dev-a", actions.RUNNING),
        ("dev-b", actions.RUNNING),
        ("dev-a", actions.OK),
        ("dev-b", actions.OK),
        ("dd", actions.RUNNING),
        ("dd", actions.OK),
    ]
    assert [r.status for r in res] == [actions.OK] * 3
