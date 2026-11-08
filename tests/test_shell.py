"""The reader app's shell as served (#20): every file under its content hash,
the page and worker always revalidated, one version id for the whole shell."""

import hashlib
import re
import shutil

import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from curator import server, shell


@pytest.fixture
def web(tmp_path):
    shutil.copytree(server.WEB, tmp_path / "web", ignore=shutil.ignore_patterns("node_modules", "test"))
    return tmp_path / "web"


def client_for(built):
    return TestClient(Starlette(routes=[Mount("/", shell.App(built))]))


def refs(text):
    """Every shell file a page or module names, as a path from the root."""
    page = re.findall(r'(?:href|src)="((?:js|icons)/[^"]+|styles[^"]*|manifest[^"]*)"', text)
    imports = ["js/" + m.removeprefix("./") for m in re.findall(r"""from '(\./[^']+)'""", text)]
    return page + imports


def test_every_reference_is_a_hashed_url_that_never_changes():
    client = client_for(shell.build(server.WEB))
    page = client.get("/")
    assert page.headers["cache-control"] == "no-cache"
    named = refs(page.text)
    assert {"js/boot.", "js/app.", "styles.", "manifest."} <= {re.sub(r"[0-9a-f]{10}\..*", "", n) for n in named}
    seen, todo = set(), list(named)
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        res = client.get(f"/{name}")
        assert res.status_code == 200, name
        assert res.headers["cache-control"] == "public, max-age=31536000, immutable"
        h = re.search(r"\.([0-9a-f]{10})\.\w+$", name)[1]
        assert hashlib.sha256(res.content).hexdigest()[:10] == h, name
        if name.endswith(".js"):
            todo += refs(res.text)
        if name.endswith(".webmanifest"):
            todo += re.findall(r'"src": "([^"]+)"', res.text)
    assert {"js/api", "js/render", "js/profile", "js/suggestions", "icons/icon-192"} <= {n.split(".")[0] for n in seen}
    # The plain names: revalidated, with hashed imports, for old pages and workers.
    plain = client.get("/js/app.js")
    assert plain.headers["cache-control"] == "no-cache"
    assert re.search(r"from '\./api\.[0-9a-f]{10}\.js'", plain.text)
    assert client.get("/js/app.js", headers={"If-None-Match": plain.headers["etag"]}).status_code == 304
    assert client.get("/js/app.0123456789.js").status_code == 404
    assert client.get("/README.md").status_code == 404


def test_a_change_renames_the_file_and_everything_importing_it(web):
    before = shell.build(web)
    (web / "js/render.js").write_text((web / "js/render.js").read_text() + "\n// changed\n")
    after = shell.build(web)
    changed = {n for n in before.names if before.names[n] != after.names[n]}
    assert changed == {"js/render.js", "js/profile.js", "js/suggestions.js", "js/app.js"}
    assert before.version != after.version
    assert shell.build(web).version == after.version  # the same files, the same id


def test_a_change_to_the_worker_alone_is_a_new_version(web):
    before = shell.build(web)
    (web / "sw.js").write_text((web / "sw.js").read_text() + "\n// changed\n")
    after = shell.build(web)
    assert before.names == after.names
    assert before.version != after.version  # a new cache name: a failed install can't delete the live one (#22)
    assert shell.build(web).version == after.version


def test_the_worker_carries_the_version_and_every_file():
    built = shell.build(server.WEB)
    sw = client_for(built).get("/sw.js")
    assert sw.headers["cache-control"] == "no-cache"
    assert f'const VERSION = "{built.version}";' in sw.text
    files = re.search(r"const FILES = (\[.*\]);", sw.text)[1]
    assert sorted(built.hashed) == sorted(re.findall(r'"([^"]+)"', files))
    assert all(f"js/{n}.js" in built.names for n in ("app", "api", "boot", "render", "profile", "suggestions"))


def test_an_import_of_nothing_or_a_cycle_fails_at_start_up(web):
    (web / "js/render.js").write_text("import { x } from './missing.js';\n")
    with pytest.raises(shell.ShellError, match="missing.js"):
        shell.build(web)
    (web / "js/render.js").write_text("import { x } from './profile.js';\n")
    with pytest.raises(shell.ShellError, match="cycle"):
        shell.build(web)


def test_version_json_names_the_shell(monkeypatch):
    monkeypatch.setattr(server, "VERSION", "abc1234")
    v = TestClient(server.app).get("/version.json").json()
    assert v["version"] == "abc1234" and v["shell"] == server.SHELL.version
