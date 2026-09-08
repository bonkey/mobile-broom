# mobile-broom — agent notes

Semantic disk audit for iOS/Android dev machines. `README.md` covers usage; this file covers
how to work on the repo.

## Releasing

**Release often — every push to `main` is a release.** Installs come from GitHub
(`uvx --from git+…` follows `main`, but `mise` uses `pipx:bonkey/mobile-broom`, which resolves
versions from **GitHub releases** — a bare git tag or an unreleased push is invisible to it).
A push without a release means mise users silently stay on the old version.

Steps for every release (patch bump is the default; don't agonize over semver):

1. Bump the version in **both** places, keep them identical:
   - `pyproject.toml` → `[project] version`
   - `src/mobile_broom/__init__.py` → `__version__`
2. Commit the bump together with your changes.
3. `just release` — runs fmt + tests, tags `v<version>` from pyproject, pushes with tags,
   and creates the GitHub release (`gh release create … --generate-notes`).

Manual equivalent if `just` is unavailable:

```sh
uv run pytest -q
git tag "v$(uv run python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")"
git push && git push --tags
gh release create "v<version>" --title "v<version>" --generate-notes   # REQUIRED for mise
```

Verify: `mise ls-remote pipx:bonkey/mobile-broom` must list the new version.

## Repo conventions

- Python ≥ 3.11, **standard library only** at runtime; hatchling `src/` layout; `uv` for everything.
- `just test` / `just fmt` (ruff) / `just run <args>` / `just check` before any push.
- Adding a category = add it to `model.GROUPS` + write one `@register("<category>")` finder in
  `src/mobile_broom/finders/`. Finders hold all domain knowledge; the engine only sizes/sorts/renders.
- Tests never touch the real machine: `FakeEnv` (canned subprocess JSON) + fake `HOME` trees in
  `tests/conftest.py`.
- Correctness rules that must not regress: never traverse a mountpoint (`st_dev` guard in
  `sizer.walk_size`), never size the mounted `CoreSimulator/Volumes/*` (size the `AssetsV2` asset
  or use simctl `sizeBytes`), totals are "candidates" not "reclaimable", detect a blocked `simctl`
  instead of reporting "no runtimes", a finding without an `action` must carry a `locked` reason
  (`model.Finding` raises otherwise — the TUI shows it), worktrees are never removed (only
  git-ignored artifact dirs inside them are).

## This machine's git quirks (Claude/agent sessions)

Commit signing is 1Password `op-ssh-sign` and ssh auth is the 1Password agent. Both work
from agent sessions (sandboxed or not) as long as `SSH_AUTH_SOCK` points at 1Password's
socket — the Bash tool inherits the stock launchd agent socket, which has no identities:

```sh
export SSH_AUTH_SOCK="$HOME/Library/Group Containers/2BUA8C4S2C.com.1password/t/agent.sock"
git commit -m …        # signed, as from the user's shell
git push               # ssh, as from the user's shell
```

Never fall back to `--no-gpg-sign` or https-via-gh; if signing fails, 1Password is locked
or the socket moved — say so and stop.
