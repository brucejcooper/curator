"""The never-collect list, applied on your Mac before anything is stored or
sent. It lives on your Mac, not in the repo (it names sensitive sites):
~/Library/Application Support/curator-agent/never-collect.yaml, started from
agent/never-collect.example.yaml the first time. Its domains and patterns go
to the curator with each upload, so history it now forbids is purged there
too."""

import os
import shutil
from pathlib import Path
from urllib.parse import urlsplit

import yaml

EXAMPLE = Path(__file__).resolve().parent.parent / "never-collect.example.yaml"
LIST = Path(os.environ.get("CURATOR_NEVER_COLLECT",
                           Path.home() / "Library/Application Support/curator-agent/never-collect.yaml"))


def local_list() -> Path:
    """Your list, created from the example the first time."""
    if not LIST.exists():
        LIST.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(EXAMPLE, LIST)
    return LIST


class NeverCollect:
    def __init__(self, path: Path | None = None):
        rules = yaml.safe_load((path or local_list()).read_text()) or {}
        domains = list(rules.get("domains") or [])
        for listed in (rules.get("categories") or {}).values():
            domains.extend(listed or [])
        self.domains = {d.lower().lstrip(".") for d in domains}
        self.patterns = [p.lower() for p in rules.get("url_patterns") or []]

    @classmethod
    def from_rules(cls, domains: list[str], patterns: list[str]) -> "NeverCollect":
        """The rules as the curator holds them (from the agent's uploads)."""
        self = cls.__new__(cls)
        self.domains = {d.lower().lstrip(".") for d in domains}
        self.patterns = [p.lower() for p in patterns]
        return self

    def rules(self) -> dict:
        return {"domains": sorted(self.domains), "patterns": self.patterns}

    def blocks_host(self, host: str) -> bool:
        host = host.lower().rstrip(".")
        parts = host.split(".")
        return any(".".join(parts[i:]) in self.domains for i in range(len(parts)))

    def blocks(self, url: str) -> bool:
        try:
            parts = urlsplit(url)
        except ValueError:
            return True
        if parts.scheme not in ("http", "https"):
            return True
        if self.blocks_host(parts.hostname or ""):
            return True
        rest = (parts.path + "?" + parts.query).lower()
        return any(p in rest for p in self.patterns)


def add_domain(domain: str, path: Path | None = None) -> None:
    """Append to `domains:` without disturbing the rest of the file."""
    path = path or local_list()
    text = path.read_text()
    rules = yaml.safe_load(text) or {}
    current = list(rules.get("domains") or [])
    if domain in current:
        return
    current.append(domain)
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("domains:"):
            comment = line[line.index("#"):] if "#" in line else ""
            lines[i] = f"domains: [{', '.join(current)}]" + (f"        {comment}" if comment else "")
            break
    else:
        lines.append(f"domains: [{domain}]")
    path.write_text("\n".join(lines) + "\n")
