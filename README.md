# mobile-broom

Semantic disk audit for iOS/Android dev machines. Answers *"what here is provably dead"*
from metadata — `lastUsedAt`, dangling back-references, version strings nothing points at —
not from size. Report by default; official CLIs (`simctl`, `avdmanager`, `sdkmanager`, `mise`)
do the deleting where one exists; `--trash` moves paths to `~/.Trash` instead of deleting;
never `sudo`.

```
$ mobile-broom ios
ios
  runtimes  (4, 32.9G)
    dead     8.4G  2026-07-22   27d  iOS 27.0 (24A5370g)  — superseded by 24A5408d; last used 2026-07-22 (27d)
    dead     8.0G  2026-08-16    3d  iOS 27.0 (24A5390f)  — superseded by 24A5408d; last used 2026-08-16 (3d)
    review   8.5G  2026-08-19    0d  iOS 26.5 (23F77)     — newest iOS 26.5; last used 2026-08-19 (0d)
    review   8.0G  2026-08-19    0d  iOS 27.0 (24A5408d)  — newest iOS 27.0; last used 2026-08-19 (0d); 2 older image(s) superseded
  device-support  (8, 50.8G)
    dead     7.0G  2026-07-30   20d  iOS iPhone18,4 27.0 (24A5390f)  — superseded by 27.0 (24A5408d) for iPhone18,4; modified 2026-07-30 (20d)
    ...
candidates: dead 42.3G (6) · stale 5.9G (1) · shared 11.5G (10) · review 50.5G (11)
  (each path sized on its own; APFS clones may overlap — not a reclaim promise)
```

## Install

Python ≥ 3.11, standard library only.

```sh
# run without installing (always the latest commit)
uvx --from git+https://github.com/bonkey/mobile-broom mobile-broom

# install as a tool (commands: mobile-broom, mbroom)
uv tool install git+https://github.com/bonkey/mobile-broom
uv tool upgrade mobile-broom

# mise (pipx backend, GitHub shorthand — resolves versions from git tags,
# so the repo must have a v* tag; plain git+https URLs don't resolve @latest)
mise use -g "pipx:bonkey/mobile-broom"
```

## Use

```
mobile-broom                          # TUI on a tty; full audit when piped
mobile-broom audit                    # every group
mobile-broom audit ios                # one group (ios · android · worktrees · general)
mobile-broom ios                      # shorthand for `audit ios`
mobile-broom audit runtimes avd       # individual categories
mobile-broom audit --json | jq        # one record per finding
mobile-broom clean ios --dry-run      # print intended actions, touch nothing
mobile-broom clean derived-data       # act on dead findings, with confirm
mobile-broom clean android --yes      # skip confirm (scripts)
mobile-broom clean ios --stale        # also act on stale; --shared for shared caches
mobile-broom clean ios --trash        # move paths to ~/.Trash instead of deleting
mobile-broom config --edit            # ~/.config/mobile-broom/config.json
```

TUI: `↑↓`/`jk` move · `→`/enter expand · `←`/`h` collapse · space mark · `a` mark all dead in
group · `n` unmark all · `d` act on marked (confirm screen shows the exact commands; `y` deletes, `t` trashes) · `s` cycle sort (verdict → size ↓ → date ↑, within each branch) · `o` reveal the finding's path in Finder · `r` rescan · `?` keys · `q`.

The scan runs in the background, one worker per category: branches appear as their finder
returns and sizes replace the `…` indicators one row at a time (same on `r`). You can move,
expand and mark while it runs; `d` waits for the scan to finish. Every group and category
row shows `total · dead · count`; every finding shows the date its verdict is based on
(last use or modification) and its age in days. Simulator categories (`sim-devices`,
`sim-data`, `worktree-simulators`) group their devices under one heading per runtime,
newest first; the heading takes space/`a` like a category and `←` folds it. `[-]` marks a finding that cannot be acted on; the bottom line says why
(`locked: device is booted; shut it down first`), and space on it repeats the reason. On a
booted simulator device space asks first, then runs `xcrun simctl shutdown <udid>` and marks it. `d` shows
every position with a live status (`· wait` → `⟳ busy` → `✓ ok` / `✗ FAIL` / `! manual`) as
it runs; `clean` prints the same one line per finding.

### Verdicts

| verdict | meaning | `clean` acts? |
|---|---|---|
| `dead` | provably unreferenced or superseded | yes |
| `stale` | idle longer than `stale_days` (default 30) | with `--stale` |
| `shared` | no owning project; safe to drop, costs a rebuild | with `--shared` |
| `review` | facts attached, decision is yours (TUI can still mark it) | never |

A finding with no action always carries a `locked` reason (booted simulator, not git-ignored,
VM disk image, …) — shown in the TUI, the text report and `--json`.

Totals are labelled **candidates**: each path is sized on its own (`st_blocks`), so APFS
clones can overlap and the sum is not a reclaim promise.

## What it checks and why

| category | signal | actor |
|---|---|---|
| `runtimes` | `/Library/Developer/CoreSimulator/Images/images.plist` (plistlib, no simctl needed): same `bundleIdentifier`, older `build` → superseded. `lastUsedAt` per image; absence flagged. Sizes from `simctl runtime list --json` `sizeBytes`, fallback = walk of the `AssetsV2/*.asset` store — **never** the mounted `Volumes/`. | `xcrun simctl runtime delete <uuid>` |
| `sim-devices` | every device in `simctl list devices --json`: `isAvailable: false` → dead; idle > `stale_days` → stale; otherwise review with the facts attached — custom name (vs its device type's default in `simctl list devicetypes`), runtime older than the newest installed one for that platform. Idle age = `lastBootedAt`, else `data/var/run` mtime, else the data dir mtime; booted → locked | `xcrun simctl delete <udid>` (`delete unavailable` for the unavailable ones) |
| `sim-data` | `dataPathSize` + `lastBootedAt` (or `data/var/run` mtime on Xcode 26+, which omits it) | `xcrun simctl erase <udid>` |
| `device-support` | `<model> <os> (<build>)`: only the newest per model is kept; older builds are dead | delete |
| `derived-data` | `info.plist` → `WorkspacePath` gone = orphan (what `wt remove` leaves); `LastAccessedDate` | delete |
| `derived-data-shared` | `*.noindex`, `SDKExplicitPrecompiledModules` — no owner | delete |
| `previews` | `UserData/Previews/Simulator Devices` and the `Simulator%20Devices` duplicate; skipped if a preview sim is booted | delete |
| `archives` `spm-cache` `doc-cache` | size + mtime | delete / report |
| `avd` | `~/.android/avd/*.ini` → `config.ini` target image; last boot = `userdata-qemu.img` mtime; `Medium_Phone*` = Android Studio default | `avdmanager delete avd -n` (falls back to deleting `.avd` + `.ini` if not installed) |
| `avd-snapshots` | `snapshots/` under an AVD — where the bloat lives | delete |
| `system-images` | installed under `$ANDROID_HOME/system-images` vs every AVD's `image.sysdir.1` | `sdkmanager --uninstall` (falls back to deleting the image dir) |
| `gradle-caches` `gradle-dists` | versions in `~/.gradle/caches/<v>` + `wrapper/dists` vs `gradle-wrapper.properties` under the scan roots | delete |
| `gradle-jdks` | `~/.gradle/jdks` majors vs `jvmToolchain(..)` / `JavaLanguageVersion.of(..)` in build files (inconclusive → review) | delete |
| `worktree-artifacts` | `git worktree list --porcelain` per repo under the scan roots; one finding per artifact dir inside each worktree — `.build`, `build/`, `DerivedData` (and `.derivedData`, `derived_data`), `Derived` (tuist), `SourcePackages`, `node_modules`, `Pods`, `.gradle`, `Carthage/{Build,Checkouts}`, `vendor/bundle`, at the root or one level down. `git check-ignore` decides: ignored → shared (stale past `stale_days`), else review + locked. The worktree itself is never touched — `wt` owns lifecycle. | delete the artifact dir |
| `worktree-simulators` | per-task simulators that outlived their worktree: the name is not its device type's default name and does not embed it (`iPhone 16 Pro (iOS 18)` is a variant, not a task), and no live worktree dir, `<repo>.<name>` suffix or branch under the scan roots matches it — a longer name containing it counts as a match. Skipped entirely when the scan finds no worktrees, so an empty scan never condemns anything. The runtime stays, so `simctl create` remakes the device | `xcrun simctl delete <udid>` |
| `orphan-derived-data` | DerivedData keyed to a vanished worktree path — report-only cross-reference (act via `derived-data`) | — |
| `npm` `homebrew` | `~/.npm/_cacache`, `_npx`, pnpm store/cache, yarn, `~/.bun/install/cache`, Homebrew cache + logs — pure download caches → shared | delete |
| `docker` `mise` `jetbrains` | colima/Docker/OrbStack disks (VM images → locked), mise (older versions → stale), JetBrains (older IDE builds → stale) | `mise uninstall`, delete |

## Safety

- Report is the default. Deleting needs `clean` (or a TUI selection) plus a confirm.
- Official CLI first — runtimes always go through `simctl`; AVDs/system images use
  `avdmanager`/`sdkmanager` when installed and fall back to removing the exact dirs they would.
- No sudo. Root-owned paths get the command printed instead (shell-quoted, copy-paste safe).
- Deletes by default; `--trash` (or `t` in the TUI confirm) moves to `~/.Trash` instead.
- Booted simulators (until you shut one down from the TUI), their runtimes, worktrees themselves (only git-ignored artifact dirs inside them) and `protected` globs from config are never acted on.
- Everything without an action says why (`locked`), so "not removable" is never a mystery.
- `--dry-run` on every `clean`; for runtimes it runs `simctl runtime delete --dry-run` and shows simctl's own answer.
- `simctl` needs an unsandboxed process (XPC to CoreSimulatorService). When blocked, mobile-broom says so and still reports runtimes from `images.plist`, with the commands printed for you to run.

## Config

`~/.config/mobile-broom/config.json` (created on first run):

```json
{
  "stale_days": 30,
  "scan_roots": ["~/Projects"],
  "protected": [],
  "scan_prune": [".git", "node_modules", "build", ".build", "Pods", "DerivedData", ".gradle"]
}
```

Sizes are cached in `~/.config/mobile-broom/sizes.json` keyed by path + mtime; `--refresh` bypasses.

## Develop

```sh
uv sync
just test        # pytest — finders run against fixture trees, no real simctl needed
just fmt
uv run mobile-broom audit ios
```

## Out of scope

Worktree lifecycle (`wt` owns it), scheduling, and generic macOS caches (`mo clean` does those —
`general` only reports them).
