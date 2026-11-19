from conftest import mkfile

from mobile_broom.finders import run as run_finders


def test_package_manager_caches_are_removable(env, cfg, home):
    mkfile(home / ".npm" / "_cacache" / "index-v5" / "x", size=10)
    mkfile(home / ".npm" / "_npx" / "abc" / "x", size=10)
    mkfile(home / ".npm" / "_logs" / "x.log", size=10)  # not a cache → not reported
    mkfile(home / ".bun" / "install" / "cache" / "x", size=10)
    mkfile(home / "Library" / "Caches" / "Yarn" / "x", size=10)
    fs = {f.label: f for f in run_finders(["npm"], env, cfg)}
    assert set(fs) == {".npm/_cacache", ".npm/_npx", ".bun/install/cache", "Library/Caches/Yarn"}
    for f in fs.values():
        assert f.verdict == "shared" and f.locked is None
        assert f.action.kind == "remove" and f.action.path == f.paths[0]
    assert "npm cache clean" in fs[".npm/_cacache"].evidence
    assert "bun pm cache rm" in fs[".bun/install/cache"].evidence


def test_homebrew_cache_is_removable(env, cfg, home):
    mkfile(home / "Library" / "Caches" / "Homebrew" / "x", size=10)
    fs = run_finders(["homebrew"], env, cfg)
    assert [f.verdict for f in fs] == ["shared"] and fs[0].action.kind == "remove"


def test_docker_disks_are_locked_with_a_reason(env, cfg, home):
    mkfile(home / ".colima" / "default" / "disk", size=10)
    fs = run_finders(["docker"], env, cfg)
    assert len(fs) == 1 and fs[0].action is None
    assert "VM disk" in fs[0].locked


def test_every_actionless_finding_explains_itself():
    """Model-level guarantee behind the TUI explainer: no action ⇒ a `locked` reason."""
    import pytest

    from mobile_broom.model import Action, Finding

    with pytest.raises(ValueError, match="locked"):
        Finding("npm", "general", "x", ["/x"], "e", "review")
    with pytest.raises(ValueError, match="locked"):
        Finding(
            "npm",
            "general",
            "x",
            ["/x"],
            "e",
            "review",
            action=Action("remove", path="/x"),
            locked="why",
        )
    ok = Finding("npm", "general", "x", ["/x"], "e", "review", locked="why")
    assert ok.to_json()["locked"] == "why"


def test_trash_entries_are_one_removable_finding_each(env, cfg, home):
    mkfile(home / ".Trash" / "old.dmg", size=10)
    mkfile(home / ".Trash" / "Stuff" / "x", size=10)
    mkfile(home / ".Trash" / ".DS_Store", size=10)  # Finder's own file, not an entry
    fs = {f.label: f for f in run_finders(["trash"], env, cfg)}
    assert set(fs) == {"old.dmg · ~/.Trash", "Stuff · ~/.Trash"}
    f = fs["Stuff · ~/.Trash"]
    assert f.verdict == "shared" and f.bucket == "~/.Trash" and f.display_label == "Stuff"
    assert f.paths == [str(home / ".Trash" / "Stuff")]
    assert f.action.kind == "remove" and f.action.path == f.paths[0] and f.action.in_trash
    assert f.last is not None and "in the Trash since" in f.evidence


def test_no_trash_folder_yields_nothing(env, cfg):
    assert run_finders(["trash"], env, cfg) == []


def test_volume_trashes_are_their_own_buckets(env, cfg, home):
    import os

    vols = home.parent / "Volumes"
    uid = str(os.getuid())
    mkfile(home / ".Trash" / "same", size=10)
    mkfile(vols / "T7" / ".Trashes" / uid / "same", size=10)
    mkfile(vols / "T7" / ".Trashes" / "999999" / "theirs", size=10)  # another user's Trash
    (vols / "Macintosh HD").symlink_to(home)  # the boot volume, already covered by ~/.Trash
    mkfile(home / ".Trashes" / uid / "via-symlink", size=10)
    fs = run_finders(["trash"], env, cfg)
    t7 = str(vols / "T7" / ".Trashes" / uid)
    assert {(f.bucket, f.display_label) for f in fs} == {("~/.Trash", "same"), (t7, "same")}
    assert len({f.key for f in fs}) == 2  # TUI marks are keyed on it


def test_unreadable_trash_is_locked_not_empty(env, cfg, home, monkeypatch):
    import os

    from mobile_broom.finders import general

    mkfile(home / ".Trash" / "x", size=10)
    real = os.listdir

    def listdir(path):
        if str(path) == str(home / ".Trash"):
            raise PermissionError(1, "Operation not permitted")
        return real(path)

    monkeypatch.setattr(general.os, "listdir", listdir)
    [f] = run_finders(["trash"], env, cfg)
    assert f.action is None and f.verdict == "review"
    assert "Full Disk Access" in f.locked
