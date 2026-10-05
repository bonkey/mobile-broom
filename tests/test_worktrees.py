import plistlib
import subprocess

from conftest import days_ago, mkfile, simctl_devices_json

from mobile_broom.finders import run as run_finders

DT = "com.apple.CoreSimulator.SimDeviceType."


def git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(cwd),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


def make_repo(home, name="ios-sdk"):
    repo = home / "Projects" / name
    mkfile(repo / "README.md", text="x")
    mkfile(repo / ".gitignore", text=".build\nPods\nnode_modules\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def test_worktree_artifacts_one_finding_per_ignored_dir(env, cfg, home):
    """Each artifact dir is its own finding, removable on its own when git ignores it;
    the worktree itself never gets an action."""
    repo = make_repo(home)
    mkfile(repo / ".build" / "x", size=10)
    mkfile(repo / "ios" / "Pods" / "x", size=10)
    mkfile(repo / "DerivedData" / "x", size=10)  # not in .gitignore → locked
    wt = home / "Projects" / "ios-sdk.feature"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(wt))
    mkfile(wt / "node_modules" / "x", size=10)
    mkfile(wt / "dirty.txt", text="uncommitted")
    fs = {f.label: f for f in run_finders(["worktree-artifacts"], env, cfg)}
    assert set(fs) == {
        "ios-sdk @ main: .build",
        "ios-sdk @ main: ios/Pods",
        "ios-sdk @ main: DerivedData",
        "ios-sdk @ feature: node_modules",
    }
    build = fs["ios-sdk @ main: .build"]
    assert build.verdict == "shared" and build.action.kind == "remove"
    assert build.action.path == str(repo / ".build") and build.paths == [str(repo / ".build")]
    assert (
        "git-ignored" in build.evidence and "worktree DIRTY" in build.evidence
    )  # untracked DerivedData
    assert build.extra["ignored"] is True and build.extra["artifact"] == ".build"
    pods = fs["ios-sdk @ main: ios/Pods"]
    assert pods.action.path == str(repo / "ios" / "Pods") and "pod install" in pods.evidence
    dd = fs["ios-sdk @ main: DerivedData"]
    assert dd.verdict == "review" and dd.action is None
    assert "not git-ignored" in dd.locked and "NOT git-ignored" in dd.evidence
    feat = fs["ios-sdk @ feature: node_modules"]
    assert feat.action.kind == "remove" and feat.action.path == str(wt / "node_modules")
    assert "DIRTY" in feat.evidence and feat.extra["dirty"] is True


def test_worktree_artifacts_stale_when_idle(env, cfg, home):
    import os
    import time

    repo = make_repo(home)
    d = mkfile(repo / "node_modules" / "x", size=10).parent
    old = time.time() - 60 * 86400
    os.utime(d, (old, old))
    fs = run_finders(["worktree-artifacts"], env, cfg)
    assert [f.verdict for f in fs] == ["stale"]


def test_worktree_artifacts_dedupe_case_insensitive_fs(env, cfg, home):
    repo = make_repo(home)
    mkfile(repo / "build" / "x", size=10)
    fs = run_finders(["worktree-artifacts"], env, cfg)
    # on a case-insensitive FS `build`/`Build` are one dir → one finding, never two
    assert len(fs) == 1


def test_orphan_derived_data_cross_reference(env, cfg, home):
    dd = home / "Library/Developer/Xcode/DerivedData"
    (home / "Projects").mkdir()
    for name, ws in (
        ("SDK-gone", str(home / "Projects/ios-sdk.old-branch/App.xcodeproj")),
        ("Else-gone", "/somewhere/else/App.xcodeproj"),
    ):
        mkfile(dd / name / "Build/x", size=1)
        with open(dd / name / "info.plist", "wb") as fh:
            plistlib.dump(
                {"WorkspacePath": ws, "LastAccessedDate": days_ago(3).replace(tzinfo=None)}, fh
            )
    fs = run_finders(["orphan-derived-data"], env, cfg)
    assert len(fs) == 1
    assert fs[0].label.startswith("ios-sdk.old-branch")
    assert fs[0].action is None and "clean derived-data" in fs[0].evidence
    assert "derived-data" in fs[0].locked


def test_worktree_simulators_are_dead_when_the_worktree_is_gone(env, cfg, home):
    """A per-task simulator outlives its worktree; nothing else in the tool notices."""
    repo = make_repo(home)
    git(
        repo,
        "worktree",
        "add",
        "-q",
        "-b",
        "caregiver-elig",
        str(home / "Projects/ios-sdk.caregiver-elig"),
    )
    env.respond(["xcrun", "simctl", "list", "runtimes", "--json"], {"runtimes": []})
    env.respond(
        ["xcrun", "simctl", "list", "devicetypes", "--json"],
        {"devicetypes": [{"identifier": f"{DT}iPhone-17-Pro", "name": "iPhone 17 Pro"}]},
    )

    def device(udid, name, **kw):
        return {
            "runtime": "com.apple.CoreSimulator.SimRuntime.iOS-27-0",
            "udid": udid,
            "name": name,
            "deviceTypeIdentifier": f"{DT}iPhone-17-Pro",
            "state": "Shutdown",
            "isAvailable": True,
            "dataPath": f"/d/{udid}",
            "dataPathSize": 900_000_000,
            "lastBootedAt": days_ago(3).isoformat(),
            **kw,
        }

    env.respond(
        ["xcrun", "simctl", "list", "devices", "--json"],
        simctl_devices_json(
            [
                device("U-GONE", "MSP2-140-roi-accordion"),
                device("U-LIVE", "caregiver-elig"),
                device("U-BOOT", "old-branch-sim", state="Booted"),
                device("U-STOCK", "iPhone 17 Pro"),
                device("U-VARIANT", "iPhone 17 Pro (iOS 26)"),
            ]
        ),
    )
    fs = {f.extra["udid"]: f for f in run_finders(["worktree-simulators"], env, cfg)}
    # Left alone: the live worktree, the stock name, and a hand-labelled stock variant.
    assert set(fs) == {"U-GONE", "U-BOOT"}
    gone = fs["U-GONE"]
    assert gone.verdict == "dead" and gone.size == 900_000_000
    assert gone.action.argv == ["xcrun", "simctl", "delete", "U-GONE"]
    assert 'no live worktree or branch named "MSP2-140-roi-accordion"' in gone.evidence
    assert "iOS 27.0 stays installed" in gone.evidence
    boot = fs["U-BOOT"]
    assert boot.verdict == "review" and boot.action is None and "booted" in boot.locked
    assert boot.evidence.startswith("BOOTED now; no live worktree")
    # The TUI offers to shut it down; afterwards it deletes like U-GONE.
    assert boot.unlock.argv == ["xcrun", "simctl", "shutdown", "U-BOOT"]
    assert boot.unlock.action.argv == ["xcrun", "simctl", "delete", "U-BOOT"]
    assert boot.unlock.evidence == boot.evidence.removeprefix("BOOTED now; ")
    assert gone.unlock is None


def test_worktree_simulators_need_a_worktree_to_compare_against(env, cfg, home):
    """No worktrees under the scan roots means no cross-reference, never a mass condemnation."""
    env.respond(["xcrun", "simctl", "list", "runtimes", "--json"], {"runtimes": []})
    env.respond(
        ["xcrun", "simctl", "list", "devicetypes", "--json"],
        {"devicetypes": [{"identifier": f"{DT}iPhone-17-Pro", "name": "iPhone 17 Pro"}]},
    )
    env.respond(
        ["xcrun", "simctl", "list", "devices", "--json"],
        simctl_devices_json(
            [
                {
                    "runtime": "com.apple.CoreSimulator.SimRuntime.iOS-27-0",
                    "udid": "U-GONE",
                    "name": "some-task",
                    "deviceTypeIdentifier": f"{DT}iPhone-17-Pro",
                    "state": "Shutdown",
                    "isAvailable": True,
                    "dataPath": "/d/gone",
                    "dataPathSize": 1,
                }
            ]
        ),
    )
    assert run_finders(["worktree-simulators"], env, cfg) == []
