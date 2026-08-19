"""Android finders: AVDs, system images, Gradle homes. Signals are version cross-references:
what is installed vs what any project under the scan roots actually asks for."""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

from ..model import Action, Finding
from . import register
from ._util import age_days, listdir, mtime_of, version_tuple, when
from .scan import project_files

DEFAULT_AVD_NAMES = ("Medium_Phone", "Pixel_Fold", "Resizable")
VERSION_DIR = re.compile(r"^\d+(\.\d+)+$")
WRAPPER_VER = re.compile(r"gradle-(\d+(?:\.\d+)+)-(?:bin|all)")
JDK_REFS = (
    re.compile(r"JavaLanguageVersion\.of\(\s*(\d+)\s*\)"),
    re.compile(r"jvmToolchain\(\s*(\d+)\s*\)"),
    re.compile(r"jvmTarget\s*=\s*[\"']?(\d+)"),
    re.compile(r"JavaVersion\.VERSION_(\d+)"),
    re.compile(r"(?m)^\s*(?:java|jdk|jvm)[-_]?(?:version|target|toolchain)?\s*=\s*[\"']?(\d+)"),
)


def _read_ini(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text(errors="replace").splitlines():
            if "=" in line and not line.lstrip().startswith(("#", ";")):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def avds(env) -> list[dict]:
    """Every AVD: name, .ini, .avd dir, config, sysdir, last boot."""
    home = env.avd_home
    out = []
    for name in listdir(home):
        if not name.endswith(".ini"):
            continue
        ini = home / name
        meta = _read_ini(ini)
        avd_dir = Path(meta.get("path") or "")
        if not avd_dir.is_absolute() or not avd_dir.exists():
            rel = meta.get("path.rel")
            avd_dir = (home.parent / rel) if rel else home / (name[:-4] + ".avd")
        cfg = _read_ini(avd_dir / "config.ini")
        userdata = avd_dir / "userdata-qemu.img"
        out.append(
            {
                "name": name[:-4],
                "ini": ini,
                "dir": avd_dir,
                "config": cfg,
                "sysdir": (cfg.get("image.sysdir.1") or "").strip("/"),
                "last_boot": mtime_of(userdata) if userdata.exists() else None,
                "snapshots": avd_dir / "snapshots",
                "display": cfg.get("avd.ini.displayname") or name[:-4],
            }
        )
    return out


def _avd_action(env, name: str) -> Action:
    tool = env.which("avdmanager")
    argv = [tool or "avdmanager", "delete", "avd", "-n", name]
    return Action(kind="argv" if tool else "print", argv=argv)


@register("avd")
def find_avd(env, cfg):
    for a in avds(env):
        is_default = any(a["name"].startswith(d) for d in DEFAULT_AVD_NAMES)
        last = a["last_boot"]
        age = age_days(last)
        booted = f"last booted {when(last)}" if last else "never booted"
        img = a["sysdir"] or a["config"].get("target", "?")
        bits = [img, booted]
        if is_default:
            bits.append("Android Studio auto-created default")
        if a["snapshots"].is_dir() and listdir(a["snapshots"]):
            bits.append("has snapshots/ (see avd-snapshots)")
        if last is None and is_default:
            verdict = "dead"
        elif age is not None and age > cfg.stale_days:
            verdict = "stale"
        else:
            verdict = "review"
        yield Finding(
            category="avd",
            group="android",
            label=a["display"],
            paths=[str(a["dir"])],
            evidence="; ".join(bits),
            verdict=verdict,
            action=_avd_action(env, a["name"]),
            extra={
                "name": a["name"],
                "sysdir": a["sysdir"],
                "last_boot": str(last) if last else None,
            },
        )


@register("avd-snapshots")
def find_avd_snapshots(env, cfg):
    for a in avds(env):
        snap = a["snapshots"]
        if not snap.is_dir() or not listdir(snap):
            continue
        last = a["last_boot"]
        age = age_days(last)
        verdict = "stale" if age is not None and age > cfg.stale_days else "shared"
        yield Finding(
            category="avd-snapshots",
            group="android",
            label=f"{a['display']} snapshots",
            paths=[str(snap)],
            evidence=f"{len(listdir(snap))} snapshot(s); AVD last booted {when(last) if last else 'never'}; drop = cold boot next time",
            verdict=verdict,
            action=Action(kind="trash", path=str(snap)),
            extra={"avd": a["name"]},
        )


def installed_system_images(env) -> list[tuple[str, Path]]:
    """('system-images/android-34/google_apis/arm64-v8a', path) per installed image."""
    root = env.android_home / "system-images"
    out = []
    for api in listdir(root):
        for tag in listdir(root / api):
            tag_dir = root / api / tag
            if not tag_dir.is_dir():
                continue
            abis = [a for a in listdir(tag_dir) if (tag_dir / a).is_dir()]
            if abis:
                for abi in abis:
                    out.append((f"system-images/{api}/{tag}/{abi}", tag_dir / abi))
            else:
                out.append((f"system-images/{api}/{tag}", tag_dir))
    return out


@register("system-images")
def find_system_images(env, cfg):
    all_avds = avds(env)
    referenced = {a["sysdir"] for a in all_avds if a["sysdir"]}
    tool = env.which("sdkmanager")
    for key, path in installed_system_images(env):
        used_by = [a["display"] for a in all_avds if a["sysdir"] == key]
        pkg = key.replace("/", ";")
        argv = [tool or "sdkmanager", "--uninstall", pkg]
        action = Action(kind="argv" if tool else "print", argv=argv)
        if key in referenced:
            verdict = "review"
            evidence = f"used by AVD(s): {', '.join(used_by)}; modified {when(mtime_of(path))}"
        else:
            verdict = "dead"
            evidence = f"no AVD targets it (checked {len(all_avds)} AVD config.ini); modified {when(mtime_of(path))}"
        yield Finding(
            category="system-images",
            group="android",
            label=pkg,
            paths=[str(path)],
            evidence=evidence,
            verdict=verdict,
            action=action,
            extra={"package": pkg},
        )


# -- gradle ----------------------------------------------------------------


@cache
def _referenced_gradle(roots: tuple[str, ...], prune: tuple[str, ...]) -> tuple[frozenset, tuple]:
    vers: set[str] = set()
    files: list[str] = []
    for f in project_files(roots, prune, names=("gradle-wrapper.properties",)):
        files.append(f)
        try:
            m = WRAPPER_VER.search(Path(f).read_text(errors="replace"))
        except OSError:
            continue
        if m:
            vers.add(m.group(1))
    return frozenset(vers), tuple(files)


def referenced_gradle_versions(cfg) -> tuple[frozenset, tuple]:
    return _referenced_gradle(tuple(str(r) for r in cfg.roots), tuple(cfg.scan_prune))


@cache
def _referenced_jdks(roots: tuple[str, ...], prune: tuple[str, ...]) -> tuple[frozenset, int]:
    majors: set[int] = set()
    n = 0
    names = (
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "settings.gradle.kts",
        "gradle.properties",
        "libs.versions.toml",
        ".java-version",
        ".sdkmanrc",
    )
    for f in project_files(roots, prune, names=names):
        n += 1
        try:
            text = Path(f).read_text(errors="replace")
        except OSError:
            continue
        if f.endswith(".java-version"):
            m = re.match(r"\s*(\d+)", text)
            if m:
                majors.add(int(m.group(1)))
            continue
        for rx in JDK_REFS:
            for m in rx.finditer(text):
                majors.add(int(m.group(1)))
    return frozenset(majors), n


def referenced_jdk_majors(cfg) -> tuple[frozenset, int]:
    return _referenced_jdks(tuple(str(r) for r in cfg.roots), tuple(cfg.scan_prune))


def _version_finding(category, cfg, path: Path, version: str, kind: str):
    referenced, files = referenced_gradle_versions(cfg)
    roots = ", ".join(cfg.scan_roots)
    if version in referenced:
        verdict = "review"
        evidence = f"referenced by gradle-wrapper.properties under {roots}; modified {when(mtime_of(path))}"
    else:
        verdict = "dead"
        evidence = (
            f"no gradle-wrapper.properties under {roots} references {version} "
            f"(scanned {len(files)}; in use: {', '.join(sorted(referenced, key=version_tuple)) or 'none'}); "
            f"modified {when(mtime_of(path))}"
        )
    return Finding(
        category=category,
        group="android",
        label=f"{kind} {version}",
        paths=[str(path)],
        evidence=evidence,
        verdict=verdict,
        action=Action(kind="trash", path=str(path)),
        extra={"version": version, "referenced": version in referenced},
    )


@register("gradle-caches")
def find_gradle_caches(env, cfg):
    caches = env.gradle_home / "caches"
    for name in listdir(caches):
        path = caches / name
        if not path.is_dir():
            continue
        if VERSION_DIR.match(name):
            yield _version_finding("gradle-caches", cfg, path, name, "caches")
        elif name.startswith("build-cache"):
            continue  # gradle-build-cache
        else:
            yield Finding(
                category="gradle-caches",
                group="android",
                label=f"caches/{name}",
                paths=[str(path)],
                evidence=f"shared dependency/transform cache; modified {when(mtime_of(path))}; re-downloaded on next build",
                verdict="shared",
                action=Action(kind="trash", path=str(path)),
            )


@register("gradle-dists")
def find_gradle_dists(env, cfg):
    dists = env.gradle_home / "wrapper" / "dists"
    for name in listdir(dists):
        m = WRAPPER_VER.match(name)
        path = dists / name
        if not m or not path.is_dir():
            continue
        yield _version_finding("gradle-dists", cfg, path, m.group(1), "dist")


@register("gradle-jdks")
def find_gradle_jdks(env, cfg):
    jdks = env.gradle_home / "jdks"
    for name in listdir(jdks):
        path = jdks / name
        if not path.is_dir():
            continue
        m = re.search(r"-(\d{1,2})-", name) or re.search(
            r"(?:jdk|java)[-_]?(\d{1,2})", name, re.IGNORECASE
        )
        major = int(m.group(1)) if m else None
        referenced, nfiles = referenced_jdk_majors(cfg)
        if major is None or not referenced:
            verdict = "review"
            evidence = (
                f"toolchain JDK; could not match against project toolchain requirements "
                f"(scanned {nfiles} build files, found {sorted(referenced) or 'no'} JDK refs)"
            )
        elif major in referenced:
            verdict = "review"
            evidence = f"JDK {major} referenced by project toolchain settings"
        else:
            verdict = "dead"
            evidence = f"no project toolchain asks for JDK {major} (projects reference {sorted(referenced)}); modified {when(mtime_of(path))}"
        yield Finding(
            category="gradle-jdks",
            group="android",
            label=name,
            paths=[str(path)],
            evidence=evidence,
            verdict=verdict,
            action=Action(kind="trash", path=str(path)),
            extra={"major": major},
        )


@register("gradle-build-cache")
def find_gradle_build_cache(env, cfg):
    caches = env.gradle_home / "caches"
    for name in listdir(caches):
        if not name.startswith("build-cache"):
            continue
        path = caches / name
        yield Finding(
            category="gradle-build-cache",
            group="android",
            label=f"caches/{name}",
            paths=[str(path)],
            evidence=f"local build cache; modified {when(mtime_of(path))}; gradle prunes entries > 7d idle itself",
            verdict="shared",
            action=Action(kind="trash", path=str(path)),
        )
