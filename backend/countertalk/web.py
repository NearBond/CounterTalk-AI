"""Serves the built web app (web/dist) from the same origin as the API.

Request paths are never turned into filesystem paths (CODING_GUIDELINES §2.6). The built files
are listed, and a request can only pick one of them by its exact URL path. Anything else gets
index.html, so the app's own router can answer it; a missing file under /assets/ is a 404.
Dotfiles, symlinks and anything outside the folder are never listed. Unknown /api paths keep
the JSON 404.

The landing page (batch F6) is web/public/landing/, which Vite copies to dist/landing/ as it
is. "/" shows it to a visitor without the session cookie, and the app to one with it. Only the
cookie's presence is checked: a stale one gets the app, which sends a signed-out visitor to
/signin. The landing's own files are served by their exact paths, like any other built file.
"""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from starlette.exceptions import HTTPException
from starlette.routing import Match, Route
from starlette.types import Scope

from countertalk.auth import COOKIE

# Vite puts a content hash in every file name under assets/, so those can be kept for good;
# index.html must be fetched again, or a redeploy wouldn't reach open browsers.
IMMUTABLE = "public, max-age=31536000, immutable"
NO_CACHE = "no-cache"
LANDING = "/landing/index.html"


class _AppRoute(Route):
    """Matches every path except /api ones, which keep the API's JSON 404 and 405."""

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        path = scope.get("path", "")
        if path == "/api" or path.startswith("/api/"):
            return Match.NONE, {}
        return super().matches(scope)


def _built_files(dist: Path) -> dict[str, Path]:
    root = dist.resolve()
    files = {}
    for path in root.rglob("*"):
        parts = path.relative_to(root).parts
        if any(part.startswith(".") for part in parts) or path.is_symlink() or not path.is_file():
            continue
        if not path.resolve().is_relative_to(root):
            continue
        files["/" + "/".join(parts)] = path
    return files


class _Build:
    """The listed files, listed again whenever index.html changes: every build rewrites it."""

    def __init__(self, dist: Path) -> None:
        self.index = dist / "index.html"
        self.dist = dist
        self.stamp: int | None = None
        self.files: dict[str, Path] = {}

    def current(self) -> dict[str, Path]:
        try:
            stamp = self.index.stat().st_mtime_ns
        except FileNotFoundError:
            stamp = None
        if stamp != self.stamp:
            self.files = _built_files(self.dist) if stamp is not None else {}
            self.stamp = stamp
        return self.files


def mount_web_app(app: FastAPI, dist: Path) -> bool:
    """Adds the web app's route, last, if `dist` holds a build. Returns whether it did."""
    if not (dist / "index.html").is_file():
        return False
    build = _Build(dist)

    async def serve(request: Request) -> FileResponse:
        path = request.scope["path"]
        files = build.current()
        file = files.get(path)
        if file is not None and file.is_file() and path != "/index.html":
            cache = IMMUTABLE if path.startswith("/assets/") else NO_CACHE
            return FileResponse(file, headers={"Cache-Control": cache})
        if path.startswith("/assets/"):
            # A stale page asking for an old hashed file needs an error, not the app as HTML.
            raise HTTPException(status_code=404)
        index = files.get("/index.html")
        if index is None:
            raise HTTPException(status_code=404)
        if path != "/":
            return FileResponse(index, headers={"Cache-Control": NO_CACHE})
        # "/" depends on the cookie, so a cache must keep the two answers apart.
        shown = files.get(LANDING) if COOKIE not in request.cookies else None
        headers = {"Cache-Control": NO_CACHE, "Vary": "Cookie"}
        return FileResponse(shown or index, headers=headers)

    app.router.routes.append(_AppRoute("/{path:path}", serve, methods=["GET", "HEAD"]))
    return True
