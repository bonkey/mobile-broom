"""Machine access: paths, subprocess wrapper, tool discovery, simctl availability."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from functools import cache
from pathlib import Path

SIMCTL_DENIED = re.compile(
    r"Connection invalid|connection became invalid|Connection refused|Operation not permitted|sandbox|not permitted|CoreSimulatorService",
    re.IGNORECASE,
)


class Env:
    """Everything a finder needs from the outside world. Tests swap in a fake."""

    def __init__(self, home: Path | None = None, environ: dict | None = None):
        self.environ = dict(os.environ if environ is None else environ)
        self.home = Path(home or self.environ.get("HOME") or Path.home())
        self.simctl_error: str | None = None
        self._simctl_probed = False
        self._cache: dict = {}
        self._lock = threading.RLock()  # finders run concurrently in the TUI

    # -- paths -----------------------------------------------------------
    def p(self, *parts: str) -> Path:
        return self.home.joinpath(*parts)

    @property
    def library(self) -> Path:
        return self.p("Library")

    @property
    def xcode(self) -> Path:
        return self.library / "Developer" / "Xcode"

    @property
    def derived_data(self) -> Path:
        return self.xcode / "DerivedData"

    @property
    def images_plist(self) -> Path:
        root = self.environ.get("MOBILE_BROOM_IMAGES_PLIST")
        return Path(root) if root else Path("/Library/Developer/CoreSimulator/Images/images.plist")

    @property
    def android_home(self) -> Path:
        return Path(
            self.environ.get("ANDROID_HOME")
            or self.environ.get("ANDROID_SDK_ROOT")
            or self.library / "Android" / "sdk"
        )

    @property
    def avd_home(self) -> Path:
        if self.environ.get("ANDROID_AVD_HOME"):
            return Path(self.environ["ANDROID_AVD_HOME"])
        if self.environ.get("ANDROID_USER_HOME"):
            return Path(self.environ["ANDROID_USER_HOME"]) / "avd"
        return self.p(".android", "avd")

    @property
    def gradle_home(self) -> Path:
        return Path(self.environ.get("GRADLE_USER_HOME") or self.p(".gradle"))

    # -- processes -------------------------------------------------------
    def run(self, argv: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout, env=self.environ, check=False
            )
        except FileNotFoundError as e:
            return subprocess.CompletedProcess(argv, 127, "", str(e))
        except subprocess.TimeoutExpired as e:
            return subprocess.CompletedProcess(argv, 124, "", f"timeout after {e.timeout}s")

    def run_json(self, argv: list[str], key: str | None = None):
        """Run argv, parse stdout as JSON; memoised per Env. Returns None on failure."""
        ck = tuple(argv)
        with self._lock:
            if ck in self._cache:
                return self._cache[ck]
            out = self.run(argv)
            val = None
            if out.returncode == 0 and out.stdout.strip():
                try:
                    val = json.loads(out.stdout)
                except json.JSONDecodeError:
                    val = None
            self._cache[ck] = val
            return val

    def which(self, name: str) -> str | None:
        found = shutil.which(name, path=self.environ.get("PATH"))
        if found:
            return found
        # Android cmdline tools are often not on PATH.
        for cand in (
            self.android_home / "cmdline-tools" / "latest" / "bin" / name,
            self.android_home / "tools" / "bin" / name,
            self.android_home / "emulator" / name,
        ):
            if cand.exists() and os.access(cand, os.X_OK):
                return str(cand)
        return None

    # -- disk ------------------------------------------------------------
    def disk_usage(self) -> tuple[int, int] | None:
        """(free, total) bytes of the volume that holds HOME, from statvfs; None if unreadable.
        On APFS free leaves out purgeable space, which Finder counts as available."""
        try:
            u = shutil.disk_usage(self.home)
        except OSError:
            return None
        return (u.free, u.total) if u.total else None

    # -- simctl ----------------------------------------------------------
    def simctl_ok(self) -> bool:
        """Probe CoreSimulatorService once. Under a restrictive sandbox simctl fails
        with 'Connection invalid' / 'Operation not permitted' and would otherwise
        look like 'no runtimes installed'."""
        with self._lock:
            if self._simctl_probed:
                return self.simctl_error is None
            self._simctl_probed = True
            out = self.run(["xcrun", "simctl", "list", "runtimes", "--json"])
            if out.returncode != 0:
                full = (out.stderr or out.stdout or "").strip()
                msg = full.splitlines()[-1] if full else f"exit {out.returncode}"
                if SIMCTL_DENIED.search(full):
                    self.simctl_error = (
                        "simctl cannot reach CoreSimulatorService (XPC denied — sandbox?); "
                        "run mobile-broom outside the sandbox. Runtimes below come from images.plist; "
                        "actions are printed, not run"
                    )
                else:
                    self.simctl_error = f"simctl failed: {msg[:200]}"
            else:
                self._cache[("xcrun", "simctl", "list", "runtimes", "--json")] = (
                    json.loads(out.stdout) if out.stdout.strip() else None
                )
        return self.simctl_error is None

    def simctl_devices(self) -> list[dict]:
        """Flat list of devices from `simctl list devices --json`, each tagged with its runtime id."""
        if not self.simctl_ok():
            return []
        data = self.run_json(["xcrun", "simctl", "list", "devices", "--json"]) or {}
        out = []
        for rt, devs in (data.get("devices") or {}).items():
            for d in devs:
                d = dict(d)
                d["runtime"] = rt
                out.append(d)
        return out

    def simctl_device_types(self) -> dict[str, str]:
        """deviceTypeIdentifier → the name Xcode gives that type by default. A device
        named anything else was created by hand (per-task/worktree simulator)."""
        if not self.simctl_ok():
            return {}
        data = self.run_json(["xcrun", "simctl", "list", "devicetypes", "--json"]) or {}
        return {
            t["identifier"]: t.get("name") or ""
            for t in (data.get("devicetypes") or [])
            if t.get("identifier")
        }

    def simctl_runtime_images(self) -> list[dict]:
        if not self.simctl_ok():
            return []
        data = self.run_json(["xcrun", "simctl", "runtime", "list", "--json"]) or {}
        return list(data.values()) if isinstance(data, dict) else []

    def booted_runtimes(self) -> set[str]:
        return {d["runtime"] for d in self.simctl_devices() if d.get("state") == "Booted"}


@cache
def is_root_owned(path: str) -> bool:
    try:
        return os.lstat(path).st_uid == 0
    except OSError:
        return False
