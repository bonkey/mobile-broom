# devsweep

Semantic disk audit for iOS/Android dev machines. Answers *"what here is provably dead"*
from metadata — `lastUsedAt`, dangling back-references, version strings nothing points at —
not from size. Report by default; official CLIs (`simctl`, `avdmanager`, `sdkmanager`, `mise`)
do the deleting; user paths go to `~/.Trash`; never `sudo`.

```
$ devsweep ios
ios
  runtimes  (4, 32.9G)
    dead     8.4G  iOS 27.0 (24A5370g)  — superseded by 24A5408d; last used 2026-07-22 (27d)
    dead     8.0G  iOS 27.0 (24A5390f)  — superseded by 24A5408d; last used 2026-08-16 (3d)
    review   8.5G  iOS 26.5 (23F77)     — newest iOS 26.5; last used 2026-08-19 (0d)
    review   8.0G  iOS 27.0 (24A5408d)  — newest iOS 27.0; last used 2026-08-19 (0d); 2 older image(s) superseded
  device-support  (8, 50.8G)
    dead     7.0G  iOS iPhone18,4 27.0 (24A5390f)  — superseded by 27.0 (24A5408d) for iPhone18,4; modified 2026-07-30 (20d)
    ...
candidates: dead 42.3G (6) · stale 5.9G (1) · shared 11.5G (10) · review 50.5G (11)
  (each path sized on its own; APFS clones may overlap — not a reclaim promise)
```

## Install

Python ≥ 3.11, standard library only.

```sh
# run without installing (always the latest commit)
uvx --from git+https://github.com/bonkey/mobile-broom devsweep

# install as a tool
uv tool install git+https://github.com/bonkey/mobile-broom
uv tool upgrade devsweep

# mise (uses the pipx backend; works with `pipx.uvx = true`)
mise use -g "pipx:git+https://github.com/bonkey/mobile-broom"
```

## Use

```
devsweep                          # TUI on a tty; full audit when piped
devsweep audit                    # every group
devsweep audit ios                # one group (ios · android · worktrees · general)
devsweep ios                      # shorthand for `audit ios`
devsweep audit runtimes avd       # individual categories
devsweep audit --json | jq        # one record per finding
devsweep clean ios --dry-run      # print intended actions, touch nothing
devsweep clean derived-data       # act on dead findings, with confirm
devsweep clean android --yes      # skip confirm (scripts)
devsweep clean ios --stale        # also act on stale; --shared for shared caches; --purge = no Trash
devsweep config --edit            # ~/.config/devsweep/config.json
```

TUI: `↑↓`/`jk` move · `→`/enter expand · `←`/`h` collapse · space mark · `a` mark all dead in
group · `n` unmark all · `d` act on marked (confirm screen shows the exact commands) · `r` resize · `q`.

### Verdicts

| verdict | meaning | `clean` acts? |
|---|---|---|
| `dead` | provably unreferenced or superseded | yes |
| `stale` | idle longer than `stale_days` (default 30) | with `--stale` |
| `shared` | no owning project; safe to drop, costs a rebuild | with `--shared` |
| `review` | facts attached, decision is yours (TUI can still mark it) | never |

Totals are labelled **candidates**: each path is sized on its own (`st_blocks`), so APFS
clones can overlap and the sum is not a reclaim promise.

## What it checks and why

| category | signal | actor |
|---|---|---|
| `runtimes` | `/Library/Developer/CoreSimulator/Images/images.plist` (plistlib, no simctl needed): same `bundleIdentifier`, older `build` → superseded. `lastUsedAt` per image; absence flagged. Sizes from `simctl runtime list --json` `sizeBytes`, fallback = walk of the `AssetsV2/*.asset` store — **never** the mounted `Volumes/`. | `xcrun simctl runtime delete <uuid>` |
| `sim-devices` | `isAvailable: false` in `simctl list devices --json` | `xcrun simctl delete unavailable` |
| `sim-data` | `dataPathSize` + `lastBootedAt` (or `data/var/run` mtime on Xcode 26+, which omits it) | `xcrun simctl erase <udid>` |
| `device-support` | `<model> <os> (<build>)`: only the newest per model is kept; older builds are dead | trash |
| `derived-data` | `info.plist` → `WorkspacePath` gone = orphan (what `wt remove` leaves); `LastAccessedDate` | trash |
| `derived-data-shared` | `*.noindex`, `SDKExplicitPrecompiledModules` — no owner | trash |
| `previews` | `UserData/Previews/Simulator Devices` and the `Simulator%20Devices` duplicate; skipped if a preview sim is booted | trash |
| `archives` `spm-cache` `doc-cache` | size + mtime | trash / report |
| `avd` | `~/.android/avd/*.ini` → `config.ini` target image; last boot = `userdata-qemu.img` mtime; `Medium_Phone*` = Android Studio default | `avdmanager delete avd -n` |
| `avd-snapshots` | `snapshots/` under an AVD — where the bloat lives | trash |
| `system-images` | installed under `$ANDROID_HOME/system-images` vs every AVD's `image.sysdir.1` | `sdkmanager --uninstall` |
| `gradle-caches` `gradle-dists` | versions in `~/.gradle/caches/<v>` + `wrapper/dists` vs `gradle-wrapper.properties` under the scan roots | trash |
| `gradle-jdks` | `~/.gradle/jdks` majors vs `jvmToolchain(..)` / `JavaLanguageVersion.of(..)` in build files (inconclusive → review) | trash |
| `worktree-artifacts` | `git worktree list --porcelain` per repo under the scan roots; `.build`, `build/`, `node_modules`, `Pods`, … per worktree; dirty flagged. **Report-only** — `wt` owns lifecycle. | — |
| `orphan-derived-data` | DerivedData keyed to a vanished worktree path — report-only cross-reference | — |
| `general` | npm/bun/pnpm, colima/Docker, Homebrew, mise (older versions → stale), JetBrains (older IDE builds → stale) | `mise uninstall`, trash, or `mo clean` |

## Safety

- Report is the default. Deleting needs `clean` (or a TUI selection) plus a confirm.
- Official CLI first — runtimes, AVDs and system images are never `rm -rf`'d by hand.
- No sudo. Root-owned paths with no CLI get the command printed instead.
- Trash, not delete (`--purge` opts out).
- Booted simulators, their runtimes, dirty worktrees and `protected` globs from config are never acted on.
- `--dry-run` on every `clean`; for runtimes it runs `simctl runtime delete --dry-run` and shows simctl's own answer.
- `simctl` needs an unsandboxed process (XPC to CoreSimulatorService). When blocked, devsweep says so and still reports runtimes from `images.plist`, with the commands printed for you to run.

## Config

`~/.config/devsweep/config.json` (created on first run):

```json
{
  "stale_days": 30,
  "scan_roots": ["~/Projects"],
  "protected": [],
  "scan_prune": [".git", "node_modules", "build", ".build", "Pods", "DerivedData", ".gradle"]
}
```

Sizes are cached in `~/.config/devsweep/sizes.json` keyed by path + mtime; `--refresh` bypasses.

## Develop

```sh
uv sync
just test        # pytest — finders run against fixture trees, no real simctl needed
just fmt
uv run devsweep audit ios
```

## Out of scope

Worktree lifecycle (`wt` owns it), scheduling, and generic macOS caches (`mo clean` does those —
`general` only reports them).
