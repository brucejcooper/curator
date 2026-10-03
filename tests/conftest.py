"""Shared test setup: a throwaway database, a key for tokens at rest, and
sample readers (fictional, so nothing personal lives in the repo)."""

from pathlib import Path

import pytest
from cryptography.fernet import Fernet

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def secret_key(monkeypatch):
    monkeypatch.setenv("CURATOR_SECRET_KEY", Fernet.generate_key().decode())
    from curator import people

    people._fernet.cache_clear()


@pytest.fixture
def db(tmp_path, monkeypatch):
    from curator import publish, store

    monkeypatch.setattr(store, "DATA", tmp_path)
    monkeypatch.setattr(store, "DB", tmp_path / "t.sqlite")
    monkeypatch.setattr(publish, "FEEDS", tmp_path / "feeds")
    return store.connect()


def docs() -> tuple[str, str, str]:
    return tuple((FIXTURES / n).read_text() for n in ("profile.md", "sources.yaml", "questions.yaml"))


@pytest.fixture
def alex(db):
    """A reader, 'alex', with the sample profile."""
    from curator import people

    people.ensure_user(db, "alex", "Alex")
    people.add_version(db, "alex", "import", "sample", *docs())
    return people.profile(db, "alex")
