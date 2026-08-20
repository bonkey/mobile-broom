import os
import time

from conftest import mkfile

from mobile_broom.finders import run as run_finders
from mobile_broom.finders.android import WRAPPER_VER


def by_label(findings):
    return {f.label: f for f in findings}


def make_avd(home, name, sysdir, booted_days_ago=None, snapshots=0, display=None):
    avd_home = home / ".android/avd"
    d = avd_home / f"{name}.avd"
    mkfile(
        avd_home / f"{name}.ini",
        text=f"avd.ini.encoding=UTF-8\npath={d}\npath.rel=avd/{name}.avd\ntarget=android-34\n",
    )
    cfg = f"image.sysdir.1={sysdir}/\nhw.device.name=pixel\n"
    if display:
        cfg += f"avd.ini.displayname={display}\n"
    mkfile(d / "config.ini", text=cfg)
    if booted_days_ago is not None:
        p = mkfile(d / "userdata-qemu.img", size=1024)
        t = time.time() - booted_days_ago * 86400
        os.utime(p, (t, t))
    for i in range(snapshots):
        mkfile(d / "snapshots" / f"snap{i}" / "ram.img", size=1024)
    return d


def test_avd_default_never_booted_is_dead(env, cfg, home):
    make_avd(home, "Medium_Phone", "system-images/android-34/google_apis/arm64-v8a")
    make_avd(
        home,
        "Pixel_10_Pro",
        "system-images/android-35/google_apis/arm64-v8a",
        booted_days_ago=2,
        snapshots=2,
        display="Pixel 10 Pro",
    )
    make_avd(
        home, "Old_Tablet", "system-images/android-33/google_apis/arm64-v8a", booted_days_ago=60
    )
    fs = by_label(run_finders(["avd", "avd-snapshots"], env, cfg))
    assert fs["Medium_Phone"].verdict == "dead"
    assert "auto-created default" in fs["Medium_Phone"].evidence
    assert (
        fs["Pixel 10 Pro"].verdict == "review" and "has snapshots/" in fs["Pixel 10 Pro"].evidence
    )
    assert fs["Old_Tablet"].verdict == "stale"
    # no avdmanager on PATH → manual action that names the official CLI
    assert fs["Medium_Phone"].action.kind == "print"
    assert fs["Medium_Phone"].action.argv[-3:] == ["avd", "-n", "Medium_Phone"]
    snaps = fs["Pixel 10 Pro snapshots"]
    assert (
        snaps.category == "avd-snapshots"
        and snaps.verdict == "shared"
        and snaps.action.kind == "trash"
    )
    assert "2 snapshot(s)" in snaps.evidence


def test_avd_with_avdmanager_on_path(env, cfg, home):
    make_avd(home, "Medium_Phone", "system-images/android-34/google_apis/arm64-v8a")
    env.tools["avdmanager"] = "/sdk/cmdline-tools/latest/bin/avdmanager"
    fs = by_label(run_finders(["avd"], env, cfg))
    assert fs["Medium_Phone"].action.kind == "argv"
    assert fs["Medium_Phone"].action.argv[0] == "/sdk/cmdline-tools/latest/bin/avdmanager"


def test_system_images_cross_reference(env, cfg, home):
    sdk = home / "Library/Android/sdk"
    mkfile(sdk / "system-images/android-34/google_apis/arm64-v8a/system.img", size=10)
    mkfile(sdk / "system-images/android-35/google_apis_playstore/arm64-v8a/system.img", size=10)
    mkfile(sdk / "system-images/android-30/default/package.xml", size=10)  # partial, no abi dir
    make_avd(
        home,
        "Pixel",
        "system-images/android-34/google_apis/arm64-v8a",
        booted_days_ago=1,
        display="Pixel",
    )
    env.tools["sdkmanager"] = "/sdk/bin/sdkmanager"
    fs = by_label(run_finders(["system-images"], env, cfg))
    assert fs["system-images;android-34;google_apis;arm64-v8a"].verdict == "review"
    assert "Pixel" in fs["system-images;android-34;google_apis;arm64-v8a"].evidence
    dead = fs["system-images;android-35;google_apis_playstore;arm64-v8a"]
    assert dead.verdict == "dead"
    assert dead.action.argv == [
        "/sdk/bin/sdkmanager",
        "--uninstall",
        "system-images;android-35;google_apis_playstore;arm64-v8a",
    ]
    assert fs["system-images;android-30;default"].verdict == "dead"


def test_gradle_version_set_difference(env, cfg, home):
    g = home / ".gradle"
    for v in ("9.5.0", "9.6.1", "8.14"):
        mkfile(g / "caches" / v / "x", size=10)
    mkfile(g / "caches/modules-2/files-2.1/x", size=10)
    mkfile(g / "caches/build-cache-1/x", size=10)
    mkfile(g / "wrapper/dists/gradle-9.6.1-bin/abc/x", size=10)
    mkfile(g / "wrapper/dists/gradle-9.5.0-all/abc/x", size=10)
    mkfile(g / "jdks/eclipse_adoptium-17-aarch64-os_x/bin/java", size=10)
    mkfile(g / "jdks/jetbrains-21-aarch64-os_x/bin/java", size=10)
    proj = home / "Projects/app"
    mkfile(
        proj / "gradle/wrapper/gradle-wrapper.properties",
        text="distributionUrl=https\\://services.gradle.org/distributions/gradle-9.6.1-bin.zip\n",
    )
    mkfile(proj / "build.gradle.kts", text="kotlin { jvmToolchain(17) }\n")
    mkfile(
        home / "Projects/other/gradle/wrapper/gradle-wrapper.properties",
        text="distributionUrl=https\\://services.gradle.org/distributions/gradle-8.14-all.zip\n",
    )
    # a wrapper under a pruned dir must not count
    mkfile(
        home / "Projects/app/build/gradle/wrapper/gradle-wrapper.properties",
        text="distributionUrl=https\\://services.gradle.org/distributions/gradle-9.5.0-bin.zip\n",
    )
    fs = by_label(
        run_finders(
            ["gradle-caches", "gradle-dists", "gradle-jdks", "gradle-build-cache"], env, cfg
        )
    )
    assert fs["caches 9.5.0"].verdict == "dead"
    assert "9.6.1" in fs["caches 9.5.0"].evidence and "8.14" in fs["caches 9.5.0"].evidence
    assert fs["caches 9.6.1"].verdict == "review"
    assert fs["caches 8.14"].verdict == "review"
    assert (
        fs["caches/modules-2"].verdict == "shared"
        and fs["caches/modules-2"].category == "gradle-caches"
    )
    assert fs["caches/build-cache-1"].category == "gradle-build-cache"
    assert fs["dist 9.5.0"].verdict == "dead" and fs["dist 9.6.1"].verdict == "review"
    assert fs["eclipse_adoptium-17-aarch64-os_x"].verdict == "review"
    assert fs["jetbrains-21-aarch64-os_x"].verdict == "dead"


def test_gradle_jdks_inconclusive_without_refs(env, cfg, home):
    mkfile(home / ".gradle/jdks/jetbrains-21-aarch64-os_x/bin/java", size=10)
    (home / "Projects").mkdir()
    fs = by_label(run_finders(["gradle-jdks"], env, cfg))
    assert fs["jetbrains-21-aarch64-os_x"].verdict == "review"
    assert "could not match" in fs["jetbrains-21-aarch64-os_x"].evidence


def test_wrapper_regex():
    assert WRAPPER_VER.search("gradle-9.6.1-bin.zip").group(1) == "9.6.1"
    assert WRAPPER_VER.search("gradle-8.14-all.zip").group(1) == "8.14"


def test_android_absent_is_quiet(env, cfg, home):
    assert (
        run_finders(
            [
                "avd",
                "avd-snapshots",
                "system-images",
                "gradle-caches",
                "gradle-dists",
                "gradle-jdks",
                "gradle-build-cache",
            ],
            env,
            cfg,
        )
        == []
    )
