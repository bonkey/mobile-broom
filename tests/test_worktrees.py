import plistlib
import subprocess

from conftest import days_ago, mkfile

from mobile_broom.finders import run as run_finders


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
