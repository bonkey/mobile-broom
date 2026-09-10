import os
import plistlib

from conftest import days_ago, mkfile, simctl_devices_json, write_images_plist

from mobile_broom.finders import run as run_finders
from mobile_broom.finders.ios import runtime_name


def by_label(findings):
    return {f.label: f for f in findings}


def setup_runtimes(env, tmp_path, home):
    assets = tmp_path / "assets"
    for b in ("23F77", "24A5370g", "24A5390f", "24A5408d"):
        mkfile(assets / f"{b}.asset" / "AssetData" / "blob", size=4096)
    write_images_plist(
        tmp_path / "images.plist",
        [
            {
                "bundle_id": "com.apple.CoreSimulator.SimRuntime.iOS-26-5",
                "build": "23F77",
                "last_used": days_ago(1),
                "asset": str(assets / "23F77.asset"),
            },
            {
                "bundle_id": "com.apple.CoreSimulator.SimRuntime.iOS-27-0",
                "build": "24A5370g",
                "last_used": days_ago(33),
                "asset": str(assets / "24A5370g.asset"),
            },
            {
                "bundle_id": "com.apple.CoreSimulator.SimRuntime.iOS-27-0",
                "build": "24A5390f",
                "last_used": days_ago(3),
                "asset": str(assets / "24A5390f.asset"),
            },
            {
                "bundle_id": "com.apple.CoreSimulator.SimRuntime.iOS-27-0",
                "build": "24A5408d",
                "last_used": None,
                "asset": str(assets / "24A5408d.asset"),
            },
        ],
    )
    return assets


def test_runtime_name():
    assert runtime_name("com.apple.CoreSimulator.SimRuntime.iOS-27-0") == "iOS 27.0"
    assert runtime_name("com.apple.CoreSimulator.SimRuntime.watchOS-11-2-1") == "watchOS 11.2.1"


def test_runtimes_superseded_without_simctl(env, cfg, tmp_path, home):
    """images.plist alone is enough for verdicts; simctl blocked → actions become manual."""
    setup_runtimes(env, tmp_path, home)
    env.respond(
        ["xcrun", "simctl", "list", "runtimes", "--json"], returncode=1, stderr="Connection invalid"
    )
    fs = by_label(run_finders(["runtimes"], env, cfg))
    assert fs["iOS 27.0 (24A5370g)"].verdict == "dead"
    assert "superseded by 24A5408d" in fs["iOS 27.0 (24A5370g)"].evidence
    assert fs["iOS 27.0 (24A5390f)"].verdict == "dead"
    assert fs["iOS 27.0 (24A5408d)"].verdict == "review"
    assert "never used" in fs["iOS 27.0 (24A5408d)"].evidence
    assert fs["iOS 26.5 (23F77)"].verdict == "review"
    # simctl blocked: still found, action downgraded to print, error surfaced
    assert env.simctl_error and "sandbox" in env.simctl_error
    assert fs["iOS 27.0 (24A5370g)"].action.kind == "print"
    # sizes: no simctl → walk the asset dir (paths set, size None until sizer runs)
    assert fs["iOS 27.0 (24A5370g)"].size is None
    assert fs["iOS 27.0 (24A5370g)"].paths[0].endswith("24A5370g.asset")


def test_runtimes_use_simctl_sizes_and_identifiers(env, cfg, tmp_path, home):
    setup_runtimes(env, tmp_path, home)
    env.respond(["xcrun", "simctl", "list", "runtimes", "--json"], {"runtimes": []})
    env.respond(["xcrun", "simctl", "list", "devices", "--json"], {"devices": {}})
    env.respond(
        ["xcrun", "simctl", "runtime", "list", "--json"],
        {
            "A": {
                "build": "24A5370g",
                "identifier": "UUID-5370",
                "sizeBytes": 8_389_757_281,
                "runtimeIdentifier": "x",
            },
            "B": {
                "build": "24A5408d",
                "identifier": "UUID-5408",
                "sizeBytes": 8_006_076_769,
                "runtimeIdentifier": "x",
            },
        },
    )
    fs = by_label(run_finders(["runtimes"], env, cfg))
    f = fs["iOS 27.0 (24A5370g)"]
    assert f.size == 8_389_757_281
    assert f.action.kind == "argv"
    assert f.action.argv == ["xcrun", "simctl", "runtime", "delete", "UUID-5370"]
    assert f.action.dry_run_argv[-1] == "--dry-run"


def test_runtimes_booted_is_protected(env, cfg, tmp_path, home):
    setup_runtimes(env, tmp_path, home)
    env.respond(["xcrun", "simctl", "list", "runtimes", "--json"], {"runtimes": []})
    env.respond(["xcrun", "simctl", "runtime", "list", "--json"], {})
    env.respond(
        ["xcrun", "simctl", "list", "devices", "--json"],
        simctl_devices_json(
            [
                {
                    "runtime": "com.apple.CoreSimulator.SimRuntime.iOS-27-0",
                    "udid": "U1",
                    "name": "iPhone 17 Pro",
                    "state": "Booted",
                    "isAvailable": True,
                    "dataPath": "/x",
                    "dataPathSize": 1,
                },
            ]
        ),
    )
    fs = by_label(run_finders(["runtimes"], env, cfg))
    assert fs["iOS 27.0 (24A5370g)"].verdict == "review"
    assert fs["iOS 27.0 (24A5370g)"].action is None
    assert "BOOTED" in fs["iOS 27.0 (24A5370g)"].evidence


def test_sim_devices_and_data(env, cfg):
    env.respond(["xcrun", "simctl", "list", "runtimes", "--json"], {"runtimes": []})
    env.respond(
        ["xcrun", "simctl", "list", "devices", "--json"],
        simctl_devices_json(
            [
                {
                    "runtime": "com.apple.CoreSimulator.SimRuntime.iOS-26-4",
                    "udid": "U-OLD",
                    "name": "iPhone 17 Pro",
                    "state": "Shutdown",
                    "isAvailable": False,
                    "availabilityError": "runtime profile not found",
                    "dataPath": "/d/old",
                    "dataPathSize": 500_000_000,
                },
                {
                    "runtime": "com.apple.CoreSimulator.SimRuntime.iOS-27-0",
                    "udid": "U-STALE",
                    "name": "iPad",
                    "state": "Shutdown",
                    "isAvailable": True,
                    "dataPath": "/d/stale",
                    "dataPathSize": 900_000_000,
                    "lastBootedAt": days_ago(45).isoformat(),
                },
                {
                    "runtime": "com.apple.CoreSimulator.SimRuntime.iOS-27-0",
                    "udid": "U-BOOT",
                    "name": "iPhone Air",
                    "state": "Booted",
                    "isAvailable": True,
                    "dataPath": "/d/boot",
                    "dataPathSize": 900_000_000,
                    "lastBootedAt": days_ago(0).isoformat(),
                },
                {
                    "runtime": "com.apple.CoreSimulator.SimRuntime.iOS-27-0",
                    "udid": "U-TINY",
                    "name": "iPhone 17e",
                    "state": "Shutdown",
                    "isAvailable": True,
                    "dataPath": "/d/tiny",
                    "dataPathSize": 18_000_000,
                },
            ]
        ),
    )
    fs = run_finders(["sim-devices", "sim-data"], env, cfg)
    dev = {f.extra["udid"]: f for f in fs if f.category == "sim-devices"}
    assert dev["U-OLD"].verdict == "dead" and "iOS 26.4" in dev["U-OLD"].label
    assert dev["U-OLD"].action.argv == ["xcrun", "simctl", "delete", "unavailable"]
    assert dev["U-STALE"].verdict == "stale"
    assert dev["U-STALE"].action.argv == ["xcrun", "simctl", "delete", "U-STALE"]
    assert dev["U-BOOT"].verdict == "review" and dev["U-BOOT"].action is None
    # A small data dir is noise for sim-data, but the device itself is still removable.
    assert dev["U-TINY"].verdict == "review"
    assert dev["U-TINY"].action.argv == ["xcrun", "simctl", "delete", "U-TINY"]
    data = {f.extra["udid"]: f for f in fs if f.category == "sim-data"}
    assert "U-OLD" not in data and "U-TINY" not in data
    assert data["U-STALE"].verdict == "stale" and data["U-STALE"].size == 900_000_000
    assert data["U-STALE"].action.argv == ["xcrun", "simctl", "erase", "U-STALE"]
    assert data["U-BOOT"].verdict == "review" and data["U-BOOT"].action is None


DT = "com.apple.CoreSimulator.SimDeviceType."


def test_sim_devices_flag_leftovers_and_superseded_runtimes(env, cfg, tmp_path):
    never = tmp_path / "sim" / "U-NEVER" / "data"
    never.mkdir(parents=True)
    old = days_ago(60).timestamp()
    os.utime(never, (old, old))
    env.respond(["xcrun", "simctl", "list", "runtimes", "--json"], {"runtimes": []})
    env.respond(
        ["xcrun", "simctl", "list", "devicetypes", "--json"],
        {
            "devicetypes": [
                {"identifier": f"{DT}iPhone-17-Pro", "name": "iPhone 17 Pro"},
                {"identifier": f"{DT}iPhone-16-Pro", "name": "iPhone 16 Pro"},
            ]
        },
    )
    env.respond(
        ["xcrun", "simctl", "list", "devices", "--json"],
        simctl_devices_json(
            [
                {
                    "runtime": "com.apple.CoreSimulator.SimRuntime.iOS-27-0",
                    "udid": "U-TASK",
                    "name": "MSP7-67-fix-login",
                    "deviceTypeIdentifier": f"{DT}iPhone-17-Pro",
                    "state": "Shutdown",
                    "isAvailable": True,
                    "dataPath": "/d/task",
                    "dataPathSize": 900_000_000,
                    "lastBootedAt": days_ago(40).isoformat(),
                },
                {
                    "runtime": "com.apple.CoreSimulator.SimRuntime.iOS-27-0",
                    "udid": "U-NEVER",
                    "name": "wt-old-branch",
                    "deviceTypeIdentifier": f"{DT}iPhone-17-Pro",
                    "state": "Shutdown",
                    "isAvailable": True,
                    "dataPath": str(never),
                    "dataPathSize": 400_000_000,
                },
                {
                    "runtime": "com.apple.CoreSimulator.SimRuntime.iOS-27-0",
                    "udid": "U-TODAY",
                    "name": "wt-current-branch",
                    "deviceTypeIdentifier": f"{DT}iPhone-17-Pro",
                    "state": "Shutdown",
                    "isAvailable": True,
                    "dataPath": "/d/today",
                    "dataPathSize": 300_000_000,
                    "lastBootedAt": days_ago(1).isoformat(),
                },
                {
                    "runtime": "com.apple.CoreSimulator.SimRuntime.iOS-26-5",
                    "udid": "U-STOCK-OLD",
                    "name": "iPhone 16 Pro",
                    "deviceTypeIdentifier": f"{DT}iPhone-16-Pro",
                    "state": "Shutdown",
                    "isAvailable": True,
                    "dataPath": "/d/stock-old",
                    "dataPathSize": 800_000_000,
                    "lastBootedAt": days_ago(2).isoformat(),
                },
                {
                    "runtime": "com.apple.CoreSimulator.SimRuntime.iOS-27-0",
                    "udid": "U-STOCK",
                    "name": "iPhone 17 Pro",
                    "deviceTypeIdentifier": f"{DT}iPhone-17-Pro",
                    "state": "Shutdown",
                    "isAvailable": True,
                    "dataPath": "/d/stock",
                    "dataPathSize": 800_000_000,
                    "lastBootedAt": days_ago(2).isoformat(),
                },
            ]
        ),
    )
    dev = {f.extra["udid"]: f for f in run_finders(["sim-devices"], env, cfg)}
    assert dev["U-TASK"].verdict == "stale" and dev["U-TASK"].extra["adhoc"]
    assert 'custom name (device type default: "iPhone 17 Pro")' in dev["U-TASK"].evidence
    assert dev["U-TASK"].action.argv == ["xcrun", "simctl", "delete", "U-TASK"]
    # Never booted, so the data dir dates it.
    assert dev["U-NEVER"].verdict == "stale" and "data dir mtime" in dev["U-NEVER"].evidence
    assert dev["U-TODAY"].verdict == "review" and dev["U-TODAY"].extra["adhoc"]
    assert dev["U-TODAY"].action.argv == ["xcrun", "simctl", "delete", "U-TODAY"]
    # An older runtime is still installed, so its devices are usable: marked, not condemned.
    assert dev["U-STOCK-OLD"].verdict == "review" and dev["U-STOCK-OLD"].extra["superseded"]
    assert "runtime older than the newest installed iOS 27.0" in dev["U-STOCK-OLD"].evidence
    assert dev["U-STOCK"].verdict == "review" and not dev["U-STOCK"].extra["adhoc"]


def test_simctl_blocked_is_reported_not_silent(env, cfg):
    env.respond(
        ["xcrun", "simctl", "list", "runtimes", "--json"],
        returncode=1,
        stderr="Operation not permitted",
    )
    fs = run_finders(["sim-devices", "sim-data"], env, cfg)
    assert len(fs) == 2
    assert all(f.label == "simctl unavailable" and "sandbox" in f.evidence for f in fs)


def test_device_support_keeps_newest_per_model(env, cfg, home):
    ds = home / "Library/Developer/Xcode/iOS DeviceSupport"
    for name in (
        "iPhone18,4 27.0 (24A5380h)",
        "iPhone18,4 27.0 (24A5390f)",
        "iPhone18,4 27.0 (24A5408d)",
        "iPhone13,3 26.5 (23F77)",
        "iPhone13,3 26.5.2 (23F84)",
        "iPad8,9 26.6 (23G5043d)",
        "Weird",
    ):
        mkfile(ds / name / "Symbols" / "x", size=10)
    fs = by_label(run_finders(["device-support"], env, cfg))
    assert fs["iOS iPhone18,4 27.0 (24A5380h)"].verdict == "dead"
    assert fs["iOS iPhone18,4 27.0 (24A5390f)"].verdict == "dead"
    assert "superseded by 27.0 (24A5408d)" in fs["iOS iPhone18,4 27.0 (24A5390f)"].evidence
    assert fs["iOS iPhone18,4 27.0 (24A5408d)"].verdict == "review"
    assert fs["iOS iPhone13,3 26.5 (23F77)"].verdict == "dead"
    assert fs["iOS iPhone13,3 26.5.2 (23F84)"].verdict == "review"
    assert fs["iOS iPad8,9 26.6 (23G5043d)"].verdict == "review"  # only one for that model, fresh
    assert fs["iOS Weird"].verdict == "review"
    assert all(f.action.kind == "remove" for f in fs.values())


def test_derived_data_orphans_and_shared(env, cfg, home):
    dd = home / "Library/Developer/Xcode/DerivedData"
    live_ws = home / "Projects/App/App.xcodeproj"
    live_ws.mkdir(parents=True)
    for name, ws, last in (
        ("App-live", str(live_ws), days_ago(2)),
        ("App-stale", str(live_ws), days_ago(90)),
        ("Gone-orphan", str(home / "Projects/wt.gone/App.xcodeproj"), days_ago(5)),
    ):
        mkfile(dd / name / "Build" / "x", size=10)
        with open(dd / name / "info.plist", "wb") as fh:
            plistlib.dump({"WorkspacePath": ws, "LastAccessedDate": last.replace(tzinfo=None)}, fh)
    mkfile(dd / "NoPlist-abc" / "Build" / "x", size=10)
    mkfile(dd / "ModuleCache.noindex" / "x", size=10)
    mkfile(dd / "SDKExplicitPrecompiledModules" / "x", size=10)
    fs = by_label(run_finders(["derived-data", "derived-data-shared"], env, cfg))
    assert fs["App-live"].verdict == "review"
    assert fs["App-stale"].verdict == "stale"
    assert fs["Gone-orphan"].verdict == "dead" and "orphan" in fs["Gone-orphan"].evidence
    assert fs["NoPlist-abc"].verdict == "review" and "no info.plist" in fs["NoPlist-abc"].evidence
    assert (
        fs["ModuleCache.noindex"].category == "derived-data-shared"
        and fs["ModuleCache.noindex"].verdict == "shared"
    )
    assert fs["SDKExplicitPrecompiledModules"].category == "derived-data-shared"
    assert "ModuleCache.noindex" not in [
        f.label for f in fs.values() if f.category == "derived-data"
    ]


def test_previews_both_device_sets(env, cfg, home):
    root = home / "Library/Developer/Xcode/UserData/Previews"
    mkfile(root / "Simulator Devices" / "U1" / "x", size=10)
    mkfile(root / "Simulator%20Devices" / "U2" / "x", size=10)
    env.respond(["xcrun", "simctl", "list", "runtimes", "--json"], {"runtimes": []})
    env.respond(
        ["xcrun", "simctl", "--set", str(root / "Simulator Devices"), "list", "devices", "--json"],
        simctl_devices_json([{"runtime": "r", "udid": "U1", "state": "Booted", "name": "p"}]),
    )
    env.respond(
        [
            "xcrun",
            "simctl",
            "--set",
            str(root / "Simulator%20Devices"),
            "list",
            "devices",
            "--json",
        ],
        {"devices": {}},
    )
    fs = by_label(run_finders(["previews"], env, cfg))
    assert (
        fs["Previews/Simulator Devices"].verdict == "review"
        and fs["Previews/Simulator Devices"].action is None
    )
    assert fs["Previews/Simulator%20Devices"].verdict == "shared"
    assert "percent-encoded" in fs["Previews/Simulator%20Devices"].evidence


def test_misc_ios_caches(env, cfg, home):
    mkfile(home / "Library/Caches/org.swift.swiftpm/repositories/x", size=10)
    mkfile(home / "Library/Developer/Xcode/DocumentationCache/x", size=10)
    mkfile(
        home / "Library/Developer/Xcode/Archives/2026-01-02/App 2-1-26.xcarchive/Info.plist",
        size=10,
    )
    fs = by_label(run_finders(["spm-cache", "doc-cache", "archives"], env, cfg))
    assert fs["Library/Caches/org.swift.swiftpm"].verdict == "shared"
    assert fs["DocumentationCache"].verdict == "shared"
    assert fs["2026-01-02/App 2-1-26.xcarchive"].verdict == "review"
