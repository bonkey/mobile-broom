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


def test_worktree_artifacts_report_only(env, cfg, home):
    repo = make_repo(home)
    mkfile(repo / ".build" / "x", size=10)
    mkfile(repo / "ios" / "Pods" / "x", size=10)
    wt = home / "Projects" / "ios-sdk.feature"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(wt))
    mkfile(wt / "node_modules" / "x", size=10)
    mkfile(wt / "dirty.txt", text="uncommitted")
    fs = {f.label: f for f in run_finders(["worktree-artifacts"], env, cfg)}
    assert set(fs) == {"ios-sdk @ main", "ios-sdk @ feature"}
    assert all(f.action is None and f.verdict == "review" for f in fs.values())
    main = fs["ios-sdk @ main"]
    assert sorted(p.split("/")[-1] for p in main.paths) == [".build", "Pods"]
    assert "clean" in main.evidence
    feat = fs["ios-sdk @ feature"]
    assert feat.paths == [str(wt / "node_modules")]
    assert "DIRTY" in feat.evidence and feat.extra["dirty"] is True


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
