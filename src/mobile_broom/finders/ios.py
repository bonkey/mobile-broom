"""iOS / Xcode finders. Every finding names the exact signal it rests on."""

from __future__ import annotations

import os
import plistlib
import re
from collections import defaultdict
from pathlib import Path

from ..model import Action, Finding
from . import register
from ._util import age_days, build_key, file_url_path, listdir, mtime_of, version_tuple, when

SIM_DATA_MIN = 32 * 1000 * 1000  # below this a device's data dir is noise, not a candidate
SHARED_DD = {"SDKExplicitPrecompiledModules"}  # plus every *.noindex


def _simctl_down(category: str, env) -> Finding:
    return Finding(
        category=category,
        group="ios",
        label="simctl unavailable",
        paths=[],
        size=0,
        evidence=env.simctl_error or "simctl failed",
        verdict="review",
        locked="simctl unreachable; nothing to act on until it works",
        extra={"error": env.simctl_error},
    )


def runtime_name(bundle_id: str) -> str:
    # com.apple.CoreSimulator.SimRuntime.iOS-27-0 -> iOS 27.0
    tail = bundle_id.rsplit(".", 1)[-1]
    m = re.match(r"([A-Za-z]+)-(\d+)-(\d+)(?:-(\d+))?", tail)
    if not m:
        return tail
    plat, a, b, c = m.groups()
    return f"{plat} {a}.{b}" + (f".{c}" if c else "")


def platform_version(bundle_id: str) -> tuple[str, tuple[int, ...]]:
    # com.apple.CoreSimulator.SimRuntime.iOS-26-4 -> ("iOS", (26, 4))
    tail = bundle_id.rsplit(".", 1)[-1]
    m = re.match(r"([A-Za-z]+)-([\d-]+)$", tail)
    if not m:
        return tail, ()
    plat, ver = m.groups()
    return plat, version_tuple(ver.replace("-", "."))


def last_boot(d: dict) -> tuple:
    """(when, signal) for a simulator device. Xcode 26+ simctl omits lastBootedAt;
    launchd_sim rewrites data/var/run on every boot."""
    last = d.get("lastBootedAt")
    if last:
        return last, "lastBootedAt"
    if d.get("dataPath"):
        run_dir = Path(d["dataPath"]) / "var" / "run"
        if run_dir.exists():
            return mtime_of(run_dir), "var/run mtime"
    return None, "lastBootedAt"


def load_images(env) -> list[dict]:
    """Images from images.plist (plistlib, no simctl). Falls back to simctl runtime list."""
    p = env.images_plist
    images: list[dict] = []
    try:
        with open(p, "rb") as fh:
            data = plistlib.load(fh)
        for im in data.get("images", []):
            ri = im.get("runtimeInfo") or {}
            parent = file_url_path(im.get("sourceParentBundleMountURL"))
            asset = None
            if parent:
                parent = parent.rstrip("/")
                asset = os.path.dirname(parent) if parent.endswith("AssetData") else parent
            images.append(
                {
                    "bundle_id": ri.get("bundleIdentifier") or "",
                    "build": ri.get("build") or "",
                    "last_used": im.get("lastUsedAt"),
                    "uuid": im.get("uuid"),
                    "asset": asset or file_url_path(im.get("path")),
                    "source": "images.plist",
                }
            )
        return images
    except (OSError, plistlib.InvalidFileException, ValueError):
        pass
    for im in env.simctl_runtime_images():
        images.append(
            {
                "bundle_id": im.get("runtimeIdentifier") or "",
                "build": im.get("build") or "",
                "last_used": im.get("lastUsedAt"),
                "uuid": im.get("identifier"),
                "asset": im.get("parentMountPath") or im.get("path"),
                "size": im.get("sizeBytes"),
                "source": "simctl",
            }
        )
    return images


@register("runtimes")
def find_runtimes(env, cfg):
    images = load_images(env)
    if not images:
        if env.simctl_error:
            yield _simctl_down("runtimes", env)
        return
    # Official sizes + delete identifiers, when simctl is reachable.
    by_build = {im.get("build"): im for im in env.simctl_runtime_images()}
    booted = env.booted_runtimes()
    groups: dict[str, list[dict]] = defaultdict(list)
    for im in images:
        if im["bundle_id"] and im["build"]:
            groups[im["bundle_id"]].append(im)
    for bid, ims in sorted(groups.items()):
        ims.sort(key=lambda i: build_key(i["build"]))
        newest = ims[-1]
        name = runtime_name(bid)
        for im in ims:
            sim = by_build.get(im["build"], {})
            ident = sim.get("identifier") or im.get("uuid") or im["build"]
            size = sim.get("sizeBytes", im.get("size"))
            paths = [im["asset"]] if im.get("asset") else []
            last = im.get("last_used")
            used = f"last used {when(last)}" if last else "never used"
            superseded = im is not newest
            is_booted = bid in booted
            if superseded:
                verdict = "dead"
                evidence = f"superseded by {newest['build']}; {used}"
            else:
                others = len(ims) - 1
                age = age_days(last)
                if age is not None and age > cfg.stale_days:
                    verdict = "stale"
                    evidence = f"newest {name}; {used} — idle > {cfg.stale_days}d"
                else:
                    verdict = "review"
                    evidence = f"newest {name}; {used}" + (
                        f"; {others} older image(s) superseded" if others else ""
                    )
            action = Action(
                kind="argv",
                argv=["xcrun", "simctl", "runtime", "delete", ident],
                dry_run_argv=["xcrun", "simctl", "runtime", "delete", ident, "--dry-run"],
            )
            locked = None
            if is_booted:
                verdict = "review"
                evidence = "BOOTED now; " + evidence
                action = None
                locked = "a simulator on this runtime is booted; shut it down first"
            elif env.simctl_error:
                action = Action(kind="print", argv=action.argv)
            yield Finding(
                category="runtimes",
                group="ios",
                label=f"{name} ({im['build']})",
                paths=paths,
                size=size,
                evidence=evidence,
                last=last,
                verdict=verdict,
                action=action,
                locked=locked,
                extra={
                    "bundle_id": bid,
                    "build": im["build"],
                    "last_used": str(last) if last else None,
                    "identifier": ident,
                    "superseded": superseded,
                    "source": im["source"],
                },
            )


@register("sim-devices")
def find_sim_devices(env, cfg):
    """Every device simctl knows, graded by how provably spare it is."""
    if not env.simctl_ok():
        yield _simctl_down("sim-devices", env)
        return
    devices = env.simctl_devices()
    default_names = env.simctl_device_types()
    newest: dict[str, tuple] = {}
    for d in devices:
        if not d.get("isAvailable", True):
            continue
        plat, ver = platform_version(d.get("runtime", ""))
        if ver and ver > newest.get(plat, ()):
            newest[plat] = ver
    for d in devices:
        rt = runtime_name(d.get("runtime", ""))
        label = f"{d.get('name')} · {rt}"
        paths = [d["dataPath"]] if d.get("dataPath") else []
        extra = {"udid": d.get("udid"), "runtime": d.get("runtime")}
        if not d.get("isAvailable", True):
            err = d.get("availabilityError") or "runtime not installed"
            yield Finding(
                category="sim-devices",
                group="ios",
                label=label,
                paths=paths,
                size=d.get("dataPathSize", 0),
                evidence=f"isAvailable=false: {err}",
                last=last_boot(d)[0] or (mtime_of(d["dataPath"]) if d.get("dataPath") else None),
                verdict="dead",
                action=Action(kind="argv", argv=["xcrun", "simctl", "delete", "unavailable"]),
                extra=extra | {"adhoc": False, "superseded": False},
            )
            continue
        last, src = last_boot(d)
        if last is None and d.get("dataPath"):
            # A device created and never booted still dates itself by its data dir.
            last, src = mtime_of(d["dataPath"]), "data dir mtime"
        used = f"last booted {when(last)} ({src})" if last else "no boot record"
        default = default_names.get(d.get("deviceTypeIdentifier") or "")
        adhoc = default is not None and d.get("name") != default
        origin = f'custom name (device type default: "{default}")' if adhoc else "default name"
        plat, ver = platform_version(d.get("runtime", ""))
        superseded = bool(ver) and plat in newest and ver < newest[plat]
        marks = [origin]
        if superseded:
            # The runtime is still installed, so the device is as usable as that runtime.
            marks.append(
                f"runtime older than the newest installed {plat} "
                f"{'.'.join(str(n) for n in newest[plat])}"
            )
        age = age_days(last)
        idle = age is not None and age > cfg.stale_days
        action = Action(kind="argv", argv=["xcrun", "simctl", "delete", d["udid"]])
        locked = None
        if d.get("state") == "Booted":
            verdict, action = "review", None
            evidence = "; ".join(["BOOTED now", *marks, used])
            locked = "device is booted; shut it down first"
        elif idle:
            verdict = "stale"
            evidence = "; ".join([*marks, f"{used} — idle > {cfg.stale_days}d"])
        else:
            verdict = "review"
            evidence = "; ".join([*marks, used])
        yield Finding(
            category="sim-devices",
            group="ios",
            label=label,
            paths=paths,
            size=d.get("dataPathSize"),
            evidence=evidence,
            last=last,
            verdict=verdict,
            action=action,
            locked=locked,
            extra=extra | {"adhoc": adhoc, "superseded": superseded, "state": d.get("state")},
        )


@register("sim-data")
def find_sim_data(env, cfg):
    if not env.simctl_ok():
        yield _simctl_down("sim-data", env)
        return
    for d in env.simctl_devices():
        if not d.get("isAvailable", True):
            continue
        size = d.get("dataPathSize")
        if size is not None and size < SIM_DATA_MIN:
            continue
        rt = runtime_name(d.get("runtime", ""))
        last, src = last_boot(d)
        state = d.get("state")
        age = age_days(last)
        used = f"last booted {when(last)} ({src})" if last else "no boot record"
        locked = None
        if state == "Booted":
            verdict, action = "review", None
            evidence = f"BOOTED now; {rt}; {used}"
            locked = "device is booted; shut it down first"
        else:
            verdict = "stale" if age is not None and age > cfg.stale_days else "review"
            evidence = f"{rt}; {used}"
            action = Action(kind="argv", argv=["xcrun", "simctl", "erase", d["udid"]])
        yield Finding(
            category="sim-data",
            group="ios",
            label=f"{d.get('name')} · {rt}",
            paths=[d["dataPath"]] if d.get("dataPath") else [],
            size=size,
            evidence=evidence,
            last=last,
            verdict=verdict,
            action=action,
            locked=locked,
            extra={"udid": d.get("udid"), "state": state, "last_booted": last},
        )


DS_NAME = re.compile(r"^(?P<model>\S+) (?P<os>\d+(?:\.\d+)*) \((?P<build>[^)]+)\)(?P<rest>.*)$")


@register("device-support")
def find_device_support(env, cfg):
    for plat_dir in sorted(env.xcode.glob("* DeviceSupport")):
        entries: dict[str, list[tuple]] = defaultdict(list)
        odd: list[str] = []
        for name in listdir(plat_dir):
            m = DS_NAME.match(name)
            if not m:
                odd.append(name)
                continue
            entries[m["model"]].append((version_tuple(m["os"]), build_key(m["build"]), name, m))
        for model, items in sorted(entries.items()):
            items.sort(key=lambda t: (t[0], t[1]))
            newest = items[-1]
            for item in items:
                _ver, _bk, name, m = item
                path = plat_dir / name
                mt = mtime_of(path)
                if item is not newest:
                    verdict = "dead"
                    evidence = f"superseded by {newest[3]['os']} ({newest[3]['build']}) for {model}; modified {when(mt)}"
                else:
                    age = age_days(mt)
                    verdict = "stale" if age is not None and age > cfg.stale_days else "review"
                    evidence = f"newest symbols for {model}; modified {when(mt)}" + (
                        f"; {len(items) - 1} older superseded" if len(items) > 1 else ""
                    )
                yield Finding(
                    category="device-support",
                    group="ios",
                    label=f"{plat_dir.name.split()[0]} {name}",
                    paths=[str(path)],
                    evidence=evidence,
                    last=mt,
                    verdict=verdict,
                    action=Action(kind="remove", path=str(path)),
                    extra={
                        "model": model,
                        "os": m["os"],
                        "build": m["build"],
                        "platform": plat_dir.name,
                    },
                )
        for name in odd:
            path = plat_dir / name
            yield Finding(
                category="device-support",
                group="ios",
                label=f"{plat_dir.name.split()[0]} {name}",
                paths=[str(path)],
                evidence=f"unparsed name; modified {when(mtime_of(path))}",
                last=mtime_of(path),
                verdict="review",
                action=Action(kind="remove", path=str(path)),
            )


@register("derived-data")
def find_derived_data(env, cfg):
    dd = env.derived_data
    for name in listdir(dd):
        if name.endswith(".noindex") or name in SHARED_DD:
            continue
        path = dd / name
        if not path.is_dir():
            continue
        info = path / "info.plist"
        ws = None
        last = None
        if info.exists():
            try:
                with open(info, "rb") as fh:
                    d = plistlib.load(fh)
                ws = d.get("WorkspacePath")
                last = d.get("LastAccessedDate")
            except (OSError, plistlib.InvalidFileException, ValueError):
                pass
        if ws is None:
            yield Finding(
                category="derived-data",
                group="ios",
                label=name,
                paths=[str(path)],
                evidence=f"no info.plist/WorkspacePath; modified {when(mtime_of(path))}",
                last=mtime_of(path),
                verdict="review",
                action=Action(kind="remove", path=str(path)),
                extra={"workspace": None},
            )
            continue
        if not os.path.exists(ws):
            verdict = "dead"
            evidence = f"orphan: WorkspacePath gone — {ws}; last accessed {when(last)}"
        else:
            age = age_days(last)
            verdict = "stale" if age is not None and age > cfg.stale_days else "review"
            evidence = f"{ws}; last accessed {when(last)}"
        yield Finding(
            category="derived-data",
            group="ios",
            label=name,
            paths=[str(path)],
            evidence=evidence,
            last=last,
            verdict=verdict,
            action=Action(kind="remove", path=str(path)),
            extra={"workspace": ws, "last_accessed": str(last) if last else None},
        )


@register("derived-data-shared")
def find_derived_data_shared(env, cfg):
    dd = env.derived_data
    for name in listdir(dd):
        if not (name.endswith(".noindex") or name in SHARED_DD):
            continue
        path = dd / name
        yield Finding(
            category="derived-data-shared",
            group="ios",
            label=name,
            paths=[str(path)],
            evidence=f"shared cache, no owning project; modified {when(mtime_of(path))}; costs a rebuild",
            last=mtime_of(path),
            verdict="shared",
            action=Action(kind="remove", path=str(path)),
        )


@register("archives")
def find_archives(env, cfg):
    root = env.xcode / "Archives"
    for day in listdir(root):
        for name in listdir(root / day):
            if not name.endswith(".xcarchive"):
                continue
            path = root / day / name
            yield Finding(
                category="archives",
                group="ios",
                label=f"{day}/{name}",
                paths=[str(path)],
                evidence=f"archive from {day}; modified {when(mtime_of(path))}; holds dSYMs — keep if shipped",
                last=mtime_of(path),
                verdict="review",
                action=Action(kind="remove", path=str(path)),
            )


@register("spm-cache")
def find_spm_cache(env, cfg):
    for rel, verdict, note in (
        (
            "Library/Caches/org.swift.swiftpm",
            "shared",
            "package clones + manifests; re-fetched on next resolve",
        ),
        ("Library/org.swift.swiftpm", "review", "security fingerprints + collections; small, keep"),
    ):
        path = env.p(*rel.split("/"))
        if not path.exists():
            continue
        yield Finding(
            category="spm-cache",
            group="ios",
            label=rel,
            paths=[str(path)],
            evidence=f"{note}; modified {when(mtime_of(path))}",
            last=mtime_of(path),
            verdict=verdict,
            action=Action(kind="remove", path=str(path)) if verdict == "shared" else None,
            locked=None if verdict == "shared" else "tiny; holds package security fingerprints",
        )


@register("doc-cache")
def find_doc_cache(env, cfg):
    for name in ("DocumentationCache", "DocumentationIndex"):
        path = env.xcode / name
        if not path.exists():
            continue
        yield Finding(
            category="doc-cache",
            group="ios",
            label=name,
            paths=[str(path)],
            evidence=f"Xcode rebuilds on demand; modified {when(mtime_of(path))}",
            last=mtime_of(path),
            verdict="shared",
            action=Action(kind="remove", path=str(path)),
        )


@register("previews")
def find_previews(env, cfg):
    root = env.xcode / "UserData" / "Previews"
    for name in listdir(root):
        path = root / name
        if not path.is_dir():
            continue
        running = False
        if env.simctl_ok():
            data = (
                env.run_json(["xcrun", "simctl", "--set", str(path), "list", "devices", "--json"])
                or {}
            )
            running = any(
                d.get("state") == "Booted"
                for devs in (data.get("devices") or {}).values()
                for d in devs
            )
        note = (
            "percent-encoded duplicate set Xcode creates"
            if "%20" in name
            else "Xcode Previews device set"
        )
        if running:
            verdict, action, ev = "review", None, f"{note}; a preview device is BOOTED now"
            locked = "a preview device is booted; close the Xcode canvas first"
        else:
            verdict, action, ev, locked = (
                "shared",
                Action(kind="remove", path=str(path)),
                f"{note}; modified {when(mtime_of(path))}; recreated on next preview",
                None,
            )
        yield Finding(
            category="previews",
            group="ios",
            label=f"Previews/{name}",
            paths=[str(path)],
            evidence=ev,
            last=mtime_of(path),
            verdict=verdict,
            action=action,
            locked=locked,
        )
