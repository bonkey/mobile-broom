"""devsweep — semantic disk audit for iOS/Android dev machines.

  devsweep                     interactive browser (tty) / full audit (pipe)
  devsweep audit [ios|android|worktrees|general|<category>...] [--json]
  devsweep ios                 shorthand for `audit ios`
  devsweep clean ios           act on the dead findings in a group, with confirm
  devsweep clean derived-data --dry-run
  devsweep clean android --yes --stale
  devsweep audit --json | jq

Verdicts: dead (provably unreferenced/superseded) · stale (idle past threshold)
          · shared (no owner; safe to drop, costs a rebuild) · review (facts only)
`clean` acts on dead; add --stale / --shared to widen. review is never acted on
outside the TUI. User paths go to ~/.Trash unless --purge. No sudo, ever.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from . import __version__, actions, engine
from .config import Config, config_path, load
from .env import Env
from .model import CATEGORY_GROUP, GROUPS
from .render import render
from .sizer import Sizer


def die(msg: str, code: int = 2):
    print(f"devsweep: {msg}", file=sys.stderr)
    sys.exit(code)


def _selectors(values: list[str]) -> list[str]:
    for v in values:
        if v not in GROUPS and v not in CATEGORY_GROUP:
            die(
                f"unknown group/category {v!r}. groups: {', '.join(GROUPS)}; "
                f"categories: {', '.join(CATEGORY_GROUP)}"
            )
    return values


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="devsweep", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--version", action="version", version=f"devsweep {__version__}")
    sub = ap.add_subparsers(dest="cmd")

    a = sub.add_parser("audit", help="report findings (default when piped)")
    a.add_argument("what", nargs="*", help="groups or categories; default all")
    a.add_argument("--json", action="store_true", help="one JSON record per finding")
    a.add_argument("--refresh", action="store_true", help="ignore the size cache")
    a.add_argument("--no-size", action="store_true", help="skip sizing (fast)")
    a.add_argument("--only", metavar="VERDICTS", help="comma list: dead,stale,shared,review")

    c = sub.add_parser("clean", help="act on findings (dead by default)")
    c.add_argument("what", nargs="+", help="groups or categories")
    c.add_argument(
        "--dry-run", "-n", action="store_true", help="print what would happen, touch nothing"
    )
    c.add_argument("--yes", "-y", action="store_true", help="skip confirmation")
    c.add_argument("--stale", action="store_true", help="also act on stale findings")
    c.add_argument("--shared", action="store_true", help="also act on shared caches")
    c.add_argument("--purge", action="store_true", help="delete instead of moving to ~/.Trash")
    c.add_argument("--refresh", action="store_true", help="ignore the size cache")

    t = sub.add_parser("tui", help="interactive browser")
    t.add_argument("what", nargs="*")
    t.add_argument("--refresh", action="store_true")

    k = sub.add_parser("config", help="show config path / open in $EDITOR")
    k.add_argument("--edit", action="store_true")
    return ap


def rewrite_argv(argv: list[str]) -> list[str]:
    """`devsweep ios` → `devsweep audit ios`; flags-only → tui/audit decided later."""
    if argv and (argv[0] in GROUPS or argv[0] in CATEGORY_GROUP):
        return ["audit", *argv]
    return argv


def cmd_audit(args, env: Env, cfg: Config) -> int:
    sizer = None if args.no_size else Sizer(refresh=args.refresh)
    findings = engine.collect(
        _selectors(args.what) or None, env, cfg, sizer, progress=not args.json
    )
    if args.only:
        want = {v.strip() for v in args.only.split(",")}
        findings = [f for f in findings if f.verdict in want]
    if args.json:
        json.dump([f.to_json() for f in findings], sys.stdout, indent=1, default=str)
        sys.stdout.write("\n")
    else:
        render(findings)
        if env.simctl_error:
            print(f"\nwarning: {env.simctl_error}", file=sys.stderr)
    return 0


def cmd_clean(args, env: Env, cfg: Config) -> int:
    sizer = Sizer(refresh=args.refresh)
    findings = engine.collect(_selectors(args.what), env, cfg, sizer, progress=True)
    todo = engine.select_for_clean(findings, stale=args.stale, shared=args.shared)
    if not todo:
        print("nothing to clean" + ("" if args.stale else " (dead only; try --stale / --shared)"))
        return 0
    steps = actions.plan(todo)
    print("plan:" if not args.dry_run else "dry-run plan:")
    actions.describe_plan(steps)
    if args.dry_run:
        print("\nwould:")
        actions.execute(todo, dry_run=True, purge=args.purge)
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            die("refusing to act without a tty; pass --yes", 1)
        verb = "DELETE (purge)" if args.purge else "trash/run"
        ans = input(f"\n{verb} {len(steps)} action(s)? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("aborted")
            return 1
    print()
    results = actions.execute(todo, dry_run=False, purge=args.purge)
    failed = [r for r in results if not r.ok and r.note != "manual"]
    manual = [r for r in results if r.note == "manual"]
    print(
        f"\ndone: {len(results) - len(failed) - len(manual)} ok, {len(failed)} failed, {len(manual)} manual"
    )
    return 1 if failed else 0


def cmd_config(args) -> int:
    p = config_path()
    load(create=True)
    if args.edit:
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        return subprocess.call([editor, str(p)])
    print(p)
    print(p.read_text(), end="")
    return 0


def cmd_tui(args, env: Env, cfg: Config) -> int:
    from . import tui

    return tui.run(_selectors(args.what) or None, env, cfg, refresh=args.refresh)


def main(argv: list[str] | None = None, env: Env | None = None) -> int:
    argv = rewrite_argv(list(sys.argv[1:] if argv is None else argv))
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.cmd == "config":
        return cmd_config(args)
    env = env or Env()
    cfg = load(create=True)
    if args.cmd is None:
        if sys.stdin.isatty() and sys.stdout.isatty():
            args = ap.parse_args(["tui"])
        else:
            args = ap.parse_args(["audit"])
    if args.cmd == "audit":
        return cmd_audit(args, env, cfg)
    if args.cmd == "clean":
        return cmd_clean(args, env, cfg)
    if args.cmd == "tui":
        return cmd_tui(args, env, cfg)
    ap.print_help()
    return 2


def entry():  # console_scripts target
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    entry()
