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
