"""Optional per-repository configuration, read from (and partly written to) `.blamest.toml` at the repo root."""

from __future__ import annotations

import fnmatch
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit

CONFIG_FILE = ".blamest.toml"

DEFAULT_IGNORE = [
    # lockfiles
    "*.lock", "package-lock.json", "pnpm-lock.yaml", "go.sum", "npm-shrinkwrap.json",
    # build output / vendored code (incl. .NET bin/obj that sometimes gets committed by accident)
    "vendor/*", "node_modules/*", "dist/*", "build/*", "third_party/*", "bin/*", "obj/*",
    "project.assets.json", "*.nuget.dgspec.json", "*.nuget.g.props", "*.nuget.g.targets", "*.deps.json",
    # minified / generated
    "*.min.js", "*.min.css", "*.map", "*.snap", "*.pb.go", "*_pb2.py", "*_pb2_grpc.py",
    "*.generated.*", "*.g.dart", "*.designer.cs", "*modelsnapshot.cs", "*.pbxproj",
]

DEFAULT_BOTS = {
    "dependabot", "dependabot-preview", "renovate", "renovate-bot", "github-actions",
    "copilot", "copilot-pull-request-reviewer", "copilot-swe-agent", "codecov",
    "codecov-commenter", "sonarcloud", "sonarqubecloud", "snyk-bot", "mergify",
    "imgbot", "allcontributors", "pre-commit-ci", "coderabbitai", "gemini-code-assist",
    "netlify", "vercel", "sourcery-ai", "deepsource-autofix", "github-advanced-security",
}


@dataclass
class Config:
    churn_days: int = 21
    ignore: list[str] = field(default_factory=lambda: list(DEFAULT_IGNORE))
    bots: set[str] = field(default_factory=set)
    # identity (login, git name or email; lowercase) -> the person it should be counted as
    join: dict[str, str] = field(default_factory=dict)
    path: Path | None = None

    def is_ignored(self, path: str) -> bool:
        """Case-insensitive; a pattern matches the full path, the file name, or any sub-path."""
        path = path.lower()
        base = path.rsplit("/", 1)[-1]
        return any(
            fnmatch.fnmatchcase(path, p) or fnmatch.fnmatchcase(base, p) or fnmatch.fnmatchcase(path, "*/" + p)
            for p in (p.lower() for p in self.ignore)
        )

    def is_bot(self, name: str | None, typename: str | None = None) -> bool:
        if typename == "Bot":
            return True
        if not name:
            return False
        lowered = name.lower()
        if lowered.endswith("[bot]") or lowered.endswith("-bot"):
            return True
        return lowered in DEFAULT_BOTS or lowered in {b.lower() for b in self.bots}

    def ignore_fingerprint(self) -> str:
        return hashlib.sha1(json.dumps(sorted(self.ignore)).encode()).hexdigest()

    # --- joining people ---------------------------------------------------------------------------

    def add_join(self, identity: str, into: str) -> None:
        self.join[identity.lower()] = into
        self._save_join()

    def remove_joins_into(self, person: str) -> list[str]:
        removed = [k for k, v in self.join.items() if v.lower() == person.lower()]
        for k in removed:
            del self.join[k]
        self._save_join()
        return removed

    def _save_join(self) -> None:
        """Rewrite only the [join] table, keeping the rest of the file (comments included) as is."""
        if self.path is None:
            return
        doc = tomlkit.parse(self.path.read_text()) if self.path.exists() else tomlkit.document()
        if "join" not in doc:
            doc.add(tomlkit.nl())
            doc.add("join", tomlkit.table())
            doc["join"].comment("identity (GitHub login, git name or email) = person it counts as")
        table = doc["join"]
        for key in [k for k in table if k.lower() not in self.join]:
            del table[key]
        for key, value in sorted(self.join.items()):
            table[key] = value
        self.path.write_text(tomlkit.dumps(doc))


def load_config(repo_root: Path) -> Config:
    path = repo_root / CONFIG_FILE
    cfg = Config(path=path)
    if not path.exists():
        return cfg
    data = tomlkit.parse(path.read_text()).unwrap()
    if "churn_days" in data:
        cfg.churn_days = int(data["churn_days"])
    if "ignore" in data:
        cfg.ignore = list(data["ignore"])
    cfg.ignore += list(data.get("extra_ignore", []))
    cfg.bots = set(data.get("bots", []))
    cfg.join = {k.lower(): v for k, v in data.get("join", {}).items()}
    return cfg
