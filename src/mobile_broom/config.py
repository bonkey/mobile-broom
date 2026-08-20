"""~/.config/mobile-broom/config.json — created on first run, merged over defaults."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULTS: dict = {
    "stale_days": 30,
    "scan_roots": ["~/Projects"],
    "protected": [],
    "scan_prune": [".git", "node_modules", "build", ".build", "Pods", "DerivedData", ".gradle"],
}


@dataclass
class Config:
    stale_days: int = 30
    scan_roots: list[str] = field(default_factory=lambda: ["~/Projects"])
    protected: list[str] = field(default_factory=list)
    scan_prune: list[str] = field(default_factory=lambda: list(DEFAULTS["scan_prune"]))
    path: Path | None = None

    @property
    def roots(self) -> list[Path]:
        return [Path(os.path.expanduser(r)) for r in self.scan_roots]

    def is_protected(self, path: str) -> bool:
        import fnmatch

        p = os.path.expanduser(path)
        for pat in self.protected:
            pat = os.path.expanduser(pat)
            if fnmatch.fnmatch(p, pat) or p == pat or p.startswith(pat.rstrip("/") + "/"):
                return True
        return False


def config_dir() -> Path:
    return Path(
        os.environ.get("MOBILE_BROOM_CONFIG_DIR") or Path.home() / ".config" / "mobile-broom"
    )


def config_path() -> Path:
    return config_dir() / "config.json"


def load(create: bool = True) -> Config:
    p = config_path()
    data = dict(DEFAULTS)
    if p.exists():
        try:
            data.update(json.loads(p.read_text()))
        except json.JSONDecodeError as e:
            raise SystemExit(f"mobile-broom: bad config {p}: {e}")
    elif create:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(DEFAULTS, indent=2) + "\n")
        except OSError:
            pass
    known = {k: data[k] for k in DEFAULTS if k in data}
    return Config(**known, path=p)
