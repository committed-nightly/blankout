from __future__ import annotations

import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest


@dataclass
class Repo:
    """A throwaway repository you can drop workflow files into."""

    root: Path

    def write(self, name: str, body: str) -> Path:
        path = self.root / ".github" / "workflows" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
        return path

    def action(self, directory: str, body: str) -> Path:
        path = self.root / directory / "action.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
        return path

    def __str__(self) -> str:
        return str(self.root)


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path)
