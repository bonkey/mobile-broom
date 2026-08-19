"""Finder registry. A finder is `def f(env, cfg) -> Iterable[Finding]`.

The engine knows nothing about domains; all domain knowledge lives in the finders.
Adding a category: add it to model.GROUPS and register a function here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from ..config import Config
from ..env import Env
from ..model import CATEGORY_GROUP, GROUPS, Finding

Finder = Callable[[Env, Config], Iterable[Finding]]
REGISTRY: dict[str, Finder] = {}


def register(category: str):
    if category not in CATEGORY_GROUP:
        raise KeyError(f"{category} is not in model.GROUPS")

    def deco(fn: Finder) -> Finder:
        REGISTRY[category] = fn
        return fn

    return deco


def resolve(selectors: list[str] | None) -> list[str]:
    """Turn ['ios', 'gradle-caches'] into an ordered, deduped category list."""
    if not selectors:
        return [c for cs in GROUPS.values() for c in cs]
    out: list[str] = []
    for s in selectors:
        if s in GROUPS:
            cats = GROUPS[s]
        elif s in CATEGORY_GROUP:
            cats = [s]
        else:
            raise KeyError(s)
        for c in cats:
            if c not in out:
                out.append(c)
    return out


def run(categories: list[str], env: Env, cfg: Config) -> list[Finding]:
    from . import android, general, ios, worktrees  # noqa: F401  (registration side effect)

    findings: list[Finding] = []
    for c in categories:
        fn = REGISTRY.get(c)
        if fn is None:
            continue
        for f in fn(env, cfg):
            if any(cfg.is_protected(p) for p in f.paths):
                continue
            findings.append(f)
    return findings
