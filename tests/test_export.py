"""The profiles' git export: FORGEJO_TOKEN goes to git per command, in the
environment, and is never stored in the clone nor put in a command line
(#37). A clone from when it was in the remote URL is scrubbed."""

import base64
import subprocess

import pytest

from curator import pipeline

TOKEN = "sekrit"
REAL_RUN = subprocess.run  # the tests' own git, untouched by the spies below


def run(*args):
    return REAL_RUN(["git", *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def remote(tmp_path, monkeypatch, alex):
    """A bare repo with one commit, as the export repo; alex is exported."""
    bare = tmp_path / "remote.git"
    run("init", "--quiet", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    run("clone", "--quiet", str(bare), str(seed))
    (seed / "README.md").write_text("Profiles\n")
    run("-C", str(seed), "add", "-A")
    run("-c", "user.name=t", "-c", "user.email=t@t", "-C", str(seed), "commit", "--quiet", "-m", "readme")
    run("-C", str(seed), "push", "--quiet", "origin", "main")
    monkeypatch.setattr(pipeline, "DATA", tmp_path / "data")
    (tmp_path / "data").mkdir()
    monkeypatch.setenv("CURATOR_EXPORT_GIT_URL", bare.as_uri())
    monkeypatch.setenv("CURATOR_EXPORT_USERS", "alex")
    monkeypatch.setenv("FORGEJO_TOKEN", TOKEN)
    return bare


def stored(repo):
    """Every file under .git/ that holds the token."""
    return [str(p) for p in (repo / ".git").rglob("*") if p.is_file() and TOKEN.encode() in p.read_bytes()]


def exported(bare):
    return run("--git-dir", str(bare), "show", "main:alex/profile.md")


def test_a_fresh_clone_never_stores_the_token(db, remote):
    pipeline.export_profiles(db)
    repo = pipeline.DATA / "export"
    assert stored(repo) == []
    assert run("-C", str(repo), "remote", "get-url", "origin").strip() == remote.as_uri()
    assert exported(remote)


def test_an_old_clone_with_the_token_in_its_url_is_scrubbed(db, remote):
    repo = pipeline.DATA / "export"
    run("clone", "--quiet", str(remote), str(repo))
    run("-C", str(repo), "config", "user.name", "curator")
    run("-C", str(repo), "config", "user.email", "curator@localhost")
    run("-C", str(repo), "remote", "set-url", "origin", f"https://oauth2:{TOKEN}@git.example/bruce/profiles.git")
    assert stored(repo)  # as the old code left it
    pipeline.export_profiles(db)
    assert stored(repo) == []
    assert run("-C", str(repo), "remote", "get-url", "origin").strip() == remote.as_uri()
    assert exported(remote)


def test_the_token_goes_in_the_environment_never_in_argv(db, remote, monkeypatch):
    calls = []

    def spy(args, **kw):
        calls.append((args, kw.get("env")))
        return REAL_RUN(args, **kw)

    monkeypatch.setattr(pipeline.subprocess, "run", spy)
    pipeline.export_profiles(db)  # clone, then commit and push
    pipeline.export_profiles(db)  # set-url and pull
    header = "Authorization: Basic " + base64.b64encode(f"oauth2:{TOKEN}".encode()).decode()
    commands = {a[3] if a[1] == "-C" else a[1] for a, _ in calls}
    assert {"clone", "push", "pull"} <= commands
    for args, env in calls:
        assert not any(TOKEN in a or header.split()[-1] in a for a in args)
        assert env["GIT_CONFIG_COUNT"] == "1" and env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
        assert env["GIT_CONFIG_VALUE_0"] == header


def test_without_a_token_no_header(db, remote, monkeypatch):
    monkeypatch.delenv("FORGEJO_TOKEN")
    envs = []
    monkeypatch.setattr(pipeline.subprocess, "run", lambda args, **kw: envs.append(kw["env"]) or REAL_RUN(args, **kw))
    pipeline.export_profiles(db)
    assert envs and not any("GIT_CONFIG_VALUE_0" in e for e in envs)
    assert exported(remote)
