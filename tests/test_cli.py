import io
import json
import plistlib
import sys

import pytest
from conftest import days_ago, mkfile

from devsweep import cli


def test_rewrite_argv_group_shorthand():
    assert cli.rewrite_argv(["ios"]) == ["audit", "ios"]
    assert cli.rewrite_argv(["derived-data", "--json"]) == ["audit", "derived-data", "--json"]
    assert cli.rewrite_argv(["clean", "ios"]) == ["clean", "ios"]
    assert cli.rewrite_argv([]) == []


def seed_derived_data(home):
    dd = home / "Library/Developer/Xcode/DerivedData"
    mkfile(dd / "Gone-xyz" / "Build" / "x", size=4096)
    with open(dd / "Gone-xyz" / "info.plist", "wb") as fh:
        plistlib.dump(
            {
                "WorkspacePath": str(home / "nope/App.xcodeproj"),
                "LastAccessedDate": days_ago(3).replace(tzinfo=None),
            },
            fh,
        )
    mkfile(dd / "ModuleCache.noindex" / "x", size=4096)
    return dd


def test_audit_json_is_valid(env, cfg, home, capsys, monkeypatch):
    seed_derived_data(home)
    rc = cli.main(["audit", "derived-data", "derived-data-shared", "--json"], env=env)
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list) and len(data) == 2
    rec = {d["label"]: d for d in data}
    assert rec["Gone-xyz"]["verdict"] == "dead"
    assert rec["Gone-xyz"]["action_kind"] == "trash"
    assert isinstance(rec["Gone-xyz"]["size"], int) and rec["Gone-xyz"]["size"] > 0
    assert set(rec["Gone-xyz"]) >= {
        "category",
        "group",
        "label",
        "paths",
        "evidence",
        "verdict",
        "size",
        "action",
        "extra",
    }


def test_audit_empty_json_is_valid(env, cfg, home, capsys):
    rc = cli.main(["audit", "avd", "--json"], env=env)
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == []


def test_audit_text_report(env, cfg, home, capsys):
    seed_derived_data(home)
    rc = cli.main(["audit", "ios", "--no-size"], env=env)
    out = capsys.readouterr().out
    assert rc == 0
    assert "derived-data" in out and "Gone-xyz" in out and "candidates:" in out


def test_unknown_selector_dies(env, cfg, home):
    with pytest.raises(SystemExit) as e:
        cli.main(["audit", "nonsense"], env=env)
    assert e.value.code == 2


def test_clean_dry_run_touches_nothing(env, cfg, home, capsys):
    dd = seed_derived_data(home)
    before = sorted(p.name for p in dd.iterdir())
    rc = cli.main(["clean", "derived-data", "--dry-run"], env=env)
    out = capsys.readouterr().out
    assert rc == 0
    assert "would" in out and "Gone-xyz" in out
    assert sorted(p.name for p in dd.iterdir()) == before


def test_clean_yes_trashes_dead_only(env, cfg, home, tmp_path, capsys):
    dd = seed_derived_data(home)
    rc = cli.main(["clean", "derived-data", "derived-data-shared", "--yes"], env=env)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert not (dd / "Gone-xyz").exists()
    assert (dd / "ModuleCache.noindex").exists()  # shared needs --shared
    assert (tmp_path / "trash" / "Gone-xyz").exists()


def test_clean_shared_flag(env, cfg, home, tmp_path, capsys):
    dd = seed_derived_data(home)
    rc = cli.main(["clean", "derived-data-shared", "--yes", "--shared"], env=env)
    assert rc == 0
    assert not (dd / "ModuleCache.noindex").exists()


def test_clean_refuses_without_tty_or_yes(env, cfg, home, monkeypatch, capsys):
    seed_derived_data(home)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(SystemExit) as e:
        cli.main(["clean", "derived-data"], env=env)
    assert e.value.code == 1


def test_clean_nothing_to_do(env, cfg, home, capsys):
    rc = cli.main(["clean", "avd"], env=env)
    assert rc == 0 and "nothing to clean" in capsys.readouterr().out


def test_config_created_on_first_run(env, cfg, home, tmp_path, capsys):
    rc = cli.main(["config"], env=env)
    assert rc == 0
    p = tmp_path / "cfg" / "config.json"
    assert p.exists()
    assert json.loads(p.read_text())["stale_days"] == 30
