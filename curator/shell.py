"""The reader app's shell (web/), as served: every file but the page and the
service worker under a name carrying its content hash (`js/app.<h>.js`), so a
page can only ever load files of its own version, whatever a cache holds
(#20). Built once at start-up from the files on disk, which keep their plain
names.

A module's hash covers its imports' hashed names (a Merkle tree: change
render.js and everything importing it gets a new name too). The version id
covers every served byte; it is written into sw.js, so each new shell is a
byte-different worker that installs that version's files as one, and it is
version.json's `shell`.

The plain names are served too (no-cache, with hashed imports), for pages
and workers from before hashing that still ask for them.

    python -m curator.shell WEB OUT   writes the served files to OUT (the e2e)
"""

import hashlib
import json
import mimetypes
import posixpath
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

HASHED = ("styles.css", "manifest.webmanifest", "icons/*", "js/*.js")  # what gets a hashed name
IMMUTABLE = "public, max-age=31536000, immutable"
IMPORT = re.compile(r"""(\b(?:from|import)\s*\(?\s*)(['"])(\.{1,2}/[^'"]+)\2""")
INDEX_REF = re.compile(r"""(\b(?:href|src)=")([^"]+)(")""")
MANIFEST_REF = re.compile(r"""("src"\s*:\s*")([^"]+)(")""")
TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".html": "text/html; charset=utf-8",
         ".webmanifest": "application/manifest+json", ".svg": "image/svg+xml"}


class ShellError(Exception):
    """An import cycle, or an import of a file that isn't in the shell."""


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()[:10]


def hashed_name(name: str, body: bytes) -> str:
    p = PurePosixPath(name)
    return str(p.with_name(f"{p.stem}.{digest(body)}{p.suffix}"))


def content_type(name: str) -> str:
    suffix = PurePosixPath(name).suffix
    return TYPES.get(suffix) or mimetypes.guess_type(name)[0] or "application/octet-stream"


@dataclass
class Shell:
    version: str = ""
    names: dict[str, str] = field(default_factory=dict)  # plain name → hashed name
    plain: dict[str, bytes] = field(default_factory=dict)  # plain name → served bytes (index.html and sw.js too)
    hashed: dict[str, bytes] = field(default_factory=dict)  # hashed name → served bytes


def build(web: Path) -> Shell:
    files = {str(p.relative_to(web).as_posix()): p.read_bytes()
             for pattern in HASHED for p in sorted(web.glob(pattern)) if p.is_file()}
    names: dict[str, str] = {}
    served: dict[str, bytes] = {}

    # Modules first, imports before importers.
    def module(name: str, path: tuple[str, ...]) -> str:
        if name in names:
            return names[name]
        if name in path:
            raise ShellError(f"import cycle: {' → '.join((*path, name))}")
        if name not in files:
            raise ShellError(f"{path[-1]} imports {name}, which isn't in the shell")

        def hashed_import(m: re.Match) -> str:
            target = str(PurePosixPath(name).parent / m[3])
            target = str(PurePosixPath(*_normalise(target)))
            rel = posixpath.relpath(module(target, (*path, name)), posixpath.dirname(name))
            return f"{m[1]}{m[2]}{rel if rel.startswith('../') else './' + rel}{m[2]}"

        body = IMPORT.sub(hashed_import, files[name].decode()).encode()
        served[name] = body
        names[name] = hashed_name(name, body)
        return names[name]

    for name in files:
        if name.endswith(".js"):
            module(name, ())
    for name, body in files.items():
        if name not in names and name != "manifest.webmanifest":
            served[name], names[name] = body, hashed_name(name, body)
    if "manifest.webmanifest" in files:
        body = MANIFEST_REF.sub(lambda m: m[1] + names.get(m[2], m[2]) + m[3], files["manifest.webmanifest"].decode()).encode()
        served["manifest.webmanifest"], names["manifest.webmanifest"] = body, hashed_name("manifest.webmanifest", body)

    index = INDEX_REF.sub(lambda m: m[1] + names.get(m[2], m[2]) + m[3], (web / "index.html").read_text()).encode()
    worker = (web / "sw.js").read_bytes()
    # The worker's own bytes too: a sw.js-only change must not reuse the live version's cache name (#22).
    version = digest(json.dumps([sorted(names.items()), digest(index), digest(worker)]).encode())
    head = (f"const VERSION = {json.dumps(version)};\n"
            f"const FILES = {json.dumps(sorted(names.values()))};\n")
    sw = head.encode() + worker
    return Shell(version=version, names=names, plain={**served, "index.html": index, "sw.js": sw},
                 hashed={names[n]: served[n] for n in names})


def _normalise(path: str) -> list[str]:
    parts: list[str] = []
    for part in path.split("/"):
        if part == "..":
            if not parts:
                raise ShellError(f"{path} leaves the shell")
            parts.pop()
        elif part not in ("", "."):
            parts.append(part)
    return parts


def response(request: Request, body: bytes, name: str, cache: str) -> Response:
    """The file, or a 304 if the browser has it already (the etag is the body's hash)."""
    etag = f'"{digest(body)}"'
    headers = {"Cache-Control": cache, "ETag": etag}
    if etag in request.headers.get("if-none-match", ""):
        return Response(status_code=304, headers=headers)
    return Response(b"" if request.method == "HEAD" else body, media_type=content_type(name), headers=headers)


class App:
    """Serves a Shell: the page and the worker always revalidated, hashed
    files forever, plain names revalidated (the transition), nothing else."""

    def __init__(self, shell: Shell):
        self.shell = shell

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        name = request.url.path.removeprefix("/") or "index.html"
        if request.method not in ("GET", "HEAD"):
            resp = Response(status_code=405)
        elif name in self.shell.hashed:
            resp = response(request, self.shell.hashed[name], name, IMMUTABLE)
        elif name in self.shell.plain:
            resp = response(request, self.shell.plain[name], name, "no-cache")
        else:
            resp = Response("Not Found", status_code=404)
        await resp(scope, receive, send)


def write(shell: Shell, out: Path) -> None:
    for name, body in {**shell.plain, **shell.hashed}.items():
        (out / name).parent.mkdir(parents=True, exist_ok=True)
        (out / name).write_bytes(body)


if __name__ == "__main__":
    built = build(Path(sys.argv[1]))
    write(built, Path(sys.argv[2]))
    print(built.version)
