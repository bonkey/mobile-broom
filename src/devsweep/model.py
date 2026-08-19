"""Core data types shared by finders, engine, renderers and the TUI."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

VERDICTS = ("dead", "stale", "shared", "review")
VERDICT_RANK = {v: i for i, v in enumerate(VERDICTS)}

# Group → ordered categories. Adding a category = add it here + register a finder.
GROUPS: dict[str, list[str]] = {
    "ios": [
        "runtimes",
        "sim-devices",
        "sim-data",
        "device-support",
        "derived-data",
        "derived-data-shared",
        "archives",
        "spm-cache",
        "doc-cache",
        "previews",
    ],
    "android": [
        "avd",
        "avd-snapshots",
        "system-images",
        "gradle-caches",
        "gradle-dists",
        "gradle-jdks",
        "gradle-build-cache",
    ],
    "worktrees": ["worktree-artifacts", "orphan-derived-data"],
    "general": ["npm", "docker", "homebrew", "mise", "jetbrains"],
}
CATEGORY_GROUP = {c: g for g, cs in GROUPS.items() for c in cs}


@dataclass
class Action:
    """What `clean` does for a finding.

    kind: "argv"  run a command (official CLI first: simctl, avdmanager, sdkmanager, mise)
          "trash" move a user-owned path to ~/.Trash (or rm -rf with --purge)
          "print" no safe actor available (root-owned, no CLI): print the command and stop
    """

    kind: str
    argv: list[str] | None = None
    path: str | None = None
    dry_run_argv: list[str] | None = None

    def describe(self) -> str:
        if self.kind == "trash":
            return f"trash {self.path}"
        if self.kind == "print":
            return "manual: " + " ".join(self.argv or [])
        return " ".join(self.argv or [])


@dataclass
class Finding:
    category: str
    group: str
    label: str
    paths: list[str]
    evidence: str
    verdict: str
    size: int | None = None
    action: Action | None = None
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.verdict not in VERDICTS:
            raise ValueError(f"bad verdict {self.verdict!r} for {self.label}")
        if self.category not in CATEGORY_GROUP:
            raise ValueError(f"unknown category {self.category!r}")
        if self.group != CATEGORY_GROUP[self.category]:
            raise ValueError(
                f"{self.category} belongs to {CATEGORY_GROUP[self.category]}, not {self.group}"
            )

    @property
    def key(self) -> str:
        return f"{self.category}:{self.label}"

    def to_json(self) -> dict:
        d = asdict(self)
        d["action"] = self.action.describe() if self.action else None
        d["action_kind"] = self.action.kind if self.action else None
        return d


def sort_key(f: Finding):
    groups = list(GROUPS)
    return (
        groups.index(f.group),
        GROUPS[f.group].index(f.category),
        VERDICT_RANK[f.verdict],
        -(f.size or 0),
        f.label,
    )
