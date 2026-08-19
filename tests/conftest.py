"""Fixtures: a fake HOME tree and an Env whose subprocess calls are canned."""

from __future__ import annotations

import json
import plistlib
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from devsweep.config import Config
from devsweep.env import Env

NOW = datetime.now(UTC)


def days_ago(n: int) -> datetime:
    return NOW - timedelta(days=n)


class FakeEnv(Env):
    """Env with canned subprocess results keyed by argv tuple (prefix match allowed)."""

    def __init__(self, home: Path, environ: dict | None = None):
        super().__init__(
            home=home, environ={"HOME": str(home), "PATH": "/usr/bin:/bin", **(environ or {})}
        )
        self.responses: dict[tuple, subprocess.CompletedProcess] = {}
        self.calls: list[list[str]] = []
        self.tools: dict[str, str] = {}

    def respond(self, argv: list[str], stdout="", returncode=0, stderr=""):
        if not isinstance(stdout, str):
            stdout = json.dumps(stdout)
        self.responses[tuple(argv)] = subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    def run(self, argv, timeout=120):
        self.calls.append(list(argv))
        key = tuple(argv)
        if key in self.responses:
            return self.responses[key]
        return subprocess.CompletedProcess(argv, 127, "", f"{argv[0]}: not found")

    def which(self, name):
        return self.tools.get(name)


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("DEVSWEEP_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("DEVSWEEP_TRASH", str(tmp_path / "trash"))
    monkeypatch.setenv("DEVSWEEP_IMAGES_PLIST", str(tmp_path / "images.plist"))
    return h


@pytest.fixture
def env(home, tmp_path):
    e = FakeEnv(
        home,
        environ={
            "DEVSWEEP_IMAGES_PLIST": str(tmp_path / "images.plist"),
            "ANDROID_HOME": str(home / "Library/Android/sdk"),
        },
    )
    return e


@pytest.fixture
def cfg(tmp_path, home):
    return Config(stale_days=30, scan_roots=[str(home / "Projects")], protected=[])


def write_images_plist(path: Path, images: list[dict]):
    """images: [{bundle_id, build, last_used (datetime|None), uuid, asset}]"""
    data = {"images": [], "standaloneBundles": []}
    for im in images:
        entry = {
            "runtimeInfo": {
                "bundleIdentifier": im["bundle_id"],
                "build": im["build"],
                "version": 1,
            },
            "uuid": im.get("uuid", im["build"]),
            "sourceParentBundleMountURL": {"relative": f"file://{im['asset']}/AssetData/"},
            "path": {"relative": f"file://{im['asset']}/AssetData/"},
            "state": 5,
        }
        if im.get("last_used") is not None:
            entry["lastUsedAt"] = im["last_used"].replace(tzinfo=None)
        data["images"].append(entry)
    with open(path, "wb") as fh:
        plistlib.dump(data, fh)


def mkfile(path: Path, size: int = 0, text: str | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if text is not None:
        path.write_text(text)
    else:
        with open(path, "wb") as fh:
            fh.write(b"\0" * size)
    return path


def simctl_devices_json(devices: list[dict]) -> dict:
    out: dict = {"devices": {}}
    for d in devices:
        out["devices"].setdefault(d.pop("runtime"), []).append(d)
    return out
