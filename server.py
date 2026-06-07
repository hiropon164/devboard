"""
server.py - serve the project dashboard.

Stdlib only. Reads projects from PROJECTS_DIR and serves:
  GET  /                       -> dashboard UI (index.html)
  GET  /api/config             -> client-side config (IDE URLs etc.)
  GET  /api/projects           -> JSON list (cached, TTL). ?archived=1 to include .archive
  GET  /api/project?path=...    -> full detail (README, files, git, AI rules, note)
  GET  /api/download?path=...   -> zip of a project (&full=1 = include all)
  POST /api/rescan             -> force a fresh scan
  POST /api/upload?project=&relpath=  -> write one uploaded file (raw body)
  POST /api/upload-zip?name=    -> upload+extract a zipped project (raw body)
  POST /api/delete?path=...     -> move a project to PROJECTS_DIR/.trash
  POST /api/archive?path=...    -> move a project to PROJECTS_DIR/.archive
  POST /api/unarchive?path=...  -> move an archived project back to PROJECTS_DIR
  POST /api/tags?path=...       -> body JSON {tags:[...], favorite:bool}
  POST /api/note?path=...       -> body = raw markdown text

Config via environment variables:
  PROJECTS_DIR       directory holding project folders   (default /data/projects)
  PORT               listen port                          (default 8080)
  SCAN_DEPTH         recursion depth when discovering     (default 4)
  CACHE_TTL          seconds before list auto-rescans     (default 30)
  MAX_UPLOAD_MB      max size per uploaded file/zip       (default 500)
  IDE_REMOTE_HOST    e.g. user@host for remote IDE URLs   (default empty = disabled)
  IDE_HOST_PROJECTS  host path equivalent of PROJECTS_DIR (default empty)
"""

import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import scanner

BASE_DIR = Path(__file__).resolve().parent
PROJECTS_DIR = Path(os.environ.get("PROJECTS_DIR", "/data/projects")).resolve()
PORT = int(os.environ.get("PORT", "8080"))
SCAN_DEPTH = int(os.environ.get("SCAN_DEPTH", "4"))
CACHE_TTL = int(os.environ.get("CACHE_TTL", "30"))
MAX_UPLOAD = int(os.environ.get("MAX_UPLOAD_MB", "500")) * 1024 * 1024
TRASH_DIR = PROJECTS_DIR / ".trash"
ARCHIVE_DIR = PROJECTS_DIR / scanner.ARCHIVE_DIRNAME

IDE_REMOTE_HOST = os.environ.get("IDE_REMOTE_HOST", "").strip()
IDE_HOST_PROJECTS = os.environ.get("IDE_HOST_PROJECTS", "").strip().rstrip("/")

_cache = {"active": None, "with_archived": None, "ts": 0.0}


def get_projects(force=False, include_archived=False):
    now = time.time()
    key = "with_archived" if include_archived else "active"
    if force or _cache[key] is None or (now - _cache["ts"]) > CACHE_TTL:
        _cache["active"] = scanner.scan(PROJECTS_DIR, SCAN_DEPTH, include_archived=False)
        _cache["with_archived"] = scanner.scan(PROJECTS_DIR, SCAN_DEPTH, include_archived=True)
        _cache["ts"] = now
    return _cache[key]


def invalidate():
    _cache["active"] = None
    _cache["with_archived"] = None
    _cache["ts"] = 0.0


def safe_filename(name):
    """Strip anything unsafe for a single path segment / download filename."""
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name or "")
    return name.strip("._") or ""


def safe_project_path(raw, allow_archived=False):
    """Resolve a requested path and ensure it lives inside PROJECTS_DIR.

    Excludes .trash always. Excludes .archive unless allow_archived=True.
    """
    if not raw:
        return None
    target = Path(raw).resolve()
    try:
        target.relative_to(PROJECTS_DIR)
    except ValueError:
        return None
    if not target.is_dir():
        return None
    if TRASH_DIR in target.parents or target == TRASH_DIR:
        return None
    in_archive = (ARCHIVE_DIR in target.parents or target == ARCHIVE_DIR)
    if in_archive and not allow_archived:
        return None
    return target


def safe_upload_target(project, relpath):
    """Build PROJECTS_DIR/<project>/<relpath>, guaranteeing it stays inside."""
    proj = safe_filename(project)
    if not proj:
        return None
    base = (PROJECTS_DIR / proj).resolve()
    parts = [p for p in (relpath or "").replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    target = base.joinpath(*parts).resolve() if parts else base
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target


def trash_project(target):
    """Move a project into PROJECTS_DIR/.trash (recoverable 'delete')."""
    TRASH_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = TRASH_DIR / f"{target.name}.{stamp}"
    i = 1
    while dest.exists():
        dest = TRASH_DIR / f"{target.name}.{stamp}.{i}"
        i += 1
    shutil.move(str(target), str(dest))
    return dest


def archive_project(target):
    """Move a project into PROJECTS_DIR/.archive (preserves name, no stamp)."""
    ARCHIVE_DIR.mkdir(exist_ok=True)
    dest = ARCHIVE_DIR / target.name
    if dest.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = ARCHIVE_DIR / f"{target.name}.{stamp}"
    shutil.move(str(target), str(dest))
    return dest


def unarchive_project(target):
    """Move an archived project back to PROJECTS_DIR top-level."""
    dest = PROJECTS_DIR / target.name
    if dest.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = PROJECTS_DIR / f"{target.name}.{stamp}"
    shutil.move(str(target), str(dest))
    return dest


def ide_host_path(project_path):
    """Translate a container-side project path to the host-side path for IDE URLs.

    Returns None if IDE_HOST_PROJECTS is not configured.
    """
    if not IDE_HOST_PROJECTS:
        return None
    try:
        rel = Path(project_path).resolve().relative_to(PROJECTS_DIR)
    except ValueError:
        return None
    return IDE_HOST_PROJECTS + "/" + rel.as_posix() if str(rel) != "." else IDE_HOST_PROJECTS


def _is_zip_junk(name):
    """macOS の zip 残骸（__MACOSX/・.DS_Store・._AppleDouble）を除外する。"""
    parts = name.replace("\\", "/").split("/")
    if "__MACOSX" in parts:
        return True
    base = parts[-1]
    return base == ".DS_Store" or base.startswith("._")


def extract_zip_safely(zip_path, name_hint=""):
    """Extract a project zip into PROJECTS_DIR, guarding against zip-slip."""
    base_root = PROJECTS_DIR.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist()
                 if n and not n.startswith("/") and not _is_zip_junk(n)]
        tops = {n.replace("\\", "/").split("/")[0] for n in names}
        if len(tops) == 1:                      # zip already wraps a folder
            base = PROJECTS_DIR
        else:                                   # loose files -> wrap them
            proj = safe_filename(name_hint) or "uploaded-" + time.strftime("%Y%m%d-%H%M%S")
            base = PROJECTS_DIR / proj
        for m in zf.infolist():
            if m.is_dir() or _is_zip_junk(m.filename):
                continue
            parts = [p for p in m.filename.replace("\\", "/").split("/") if p not in ("", ".")]
            if not parts or any(p == ".." for p in parts):
                continue
            dest = base.joinpath(*parts).resolve()
            try:
                dest.relative_to(base_root)
            except ValueError:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(m) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
    return base


def build_zip(path, include_all=False):
    """Zip a project into a temp file; returns its path. Caller must delete it.

    By default, regenerable dependency/build dirs (node_modules, .venv, dist,
    target, …) are excluded to keep archives small, but .git history is kept.
    Pass include_all=True for a byte-for-byte archive.
    """
    exclude = set() if include_all else (scanner.SKIP_DIRS - {".git"})
    arc_root = path.name
    tmp = tempfile.NamedTemporaryFile(prefix="devboard-", suffix=".zip", delete=False)
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d not in exclude]
            root_p = Path(root)
            for f in files:
                fp = root_p / f
                if fp.is_symlink():
                    continue
                try:
                    arc = Path(arc_root) / fp.relative_to(path)
                    zf.write(fp, arcname=str(arc))
                except (OSError, ValueError):
                    continue
    return tmp.name


class Handler(BaseHTTPRequestHandler):
    server_version = "devboard/0.1.0"

    def _send(self, status, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _stream_file(self, filepath, filename, ctype="application/zip"):
        size = os.path.getsize(filepath)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        with open(filepath, "rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _spool_body(self):
        """Stream the request body to a temp file. Returns path or None if too big."""
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_UPLOAD:
            return None, length
        tmp = tempfile.NamedTemporaryFile(prefix="devboard-up-", delete=False)
        remaining = length
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                tmp.write(chunk)
                remaining -= len(chunk)
        finally:
            tmp.close()
        return tmp.name, length

    def _read_body(self, max_bytes=1_000_000):
        """Read the full request body into memory (small POSTs only)."""
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > max_bytes:
            return None
        return self.rfile.read(length)

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path

        if route == "/" or route == "/index.html":
            try:
                html = (BASE_DIR / "index.html").read_bytes()
            except OSError:
                return self._send(500, "index.html not found", "text/plain")
            return self._send(200, html, "text/html; charset=utf-8")

        if route == "/api/config":
            return self._json({
                "ide_remote_host": IDE_REMOTE_HOST,
                "ide_host_projects": IDE_HOST_PROJECTS,
                "ide_enabled": bool(IDE_REMOTE_HOST and IDE_HOST_PROJECTS),
            })

        if route == "/api/projects":
            qs = parse_qs(parsed.query)
            force = qs.get("force", ["0"])[0] == "1"
            include_archived = qs.get("archived", ["0"])[0] == "1"
            return self._json({
                "root": str(PROJECTS_DIR),
                "scanned_at": _cache["ts"],
                "projects": get_projects(force=force, include_archived=include_archived),
            })

        if route == "/api/project":
            raw = parse_qs(parsed.query).get("path", [""])[0]
            target = safe_project_path(raw, allow_archived=True)
            if not target:
                return self._json({"error": "invalid or unknown project path"}, 404)
            archived = ARCHIVE_DIR in target.parents
            rec = scanner.detail(target, archived=archived)
            rec["ide_host_path"] = ide_host_path(target)
            return self._json(rec)

        if route == "/api/download":
            qs = parse_qs(parsed.query)
            target = safe_project_path(qs.get("path", [""])[0], allow_archived=True)
            if not target:
                return self._json({"error": "invalid or unknown project path"}, 404)
            include_all = qs.get("full", ["0"])[0] == "1"
            zip_path = None
            try:
                zip_path = build_zip(target, include_all=include_all)
                fname = (safe_filename(target.name) or "project") + (".full" if include_all else "") + ".zip"
                self._stream_file(zip_path, fname)
            except (OSError, BrokenPipeError) as e:
                print("[devboard] download error:", e)
            finally:
                if zip_path and os.path.exists(zip_path):
                    os.remove(zip_path)
            return

        if route == "/health":
            return self._send(200, "ok", "text/plain")

        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        parsed = urlparse(self.path)
        route = parsed.path
        qs = parse_qs(parsed.query)

        if route == "/api/rescan":
            include_archived = qs.get("archived", ["0"])[0] == "1"
            return self._json({"projects": get_projects(force=True, include_archived=include_archived),
                               "scanned_at": _cache["ts"]})

        # write a single uploaded file (raw body) into PROJECTS_DIR/<project>/<relpath>
        if route == "/api/upload":
            target = safe_upload_target(qs.get("project", [""])[0], qs.get("relpath", [""])[0])
            if target is None:
                return self._json({"error": "invalid project or path"}, 400)
            tmp, length = self._spool_body()
            if tmp is None:
                return self._json({"error": f"empty or exceeds {MAX_UPLOAD} bytes"}, 413)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(tmp, str(target))
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
            invalidate()
            return self._json({"ok": True, "path": str(target)})

        # upload + extract a zipped project (raw body)
        if route == "/api/upload-zip":
            tmp, length = self._spool_body()
            if tmp is None:
                return self._json({"error": f"empty or exceeds {MAX_UPLOAD} bytes"}, 413)
            try:
                if not zipfile.is_zipfile(tmp):
                    return self._json({"error": "not a valid zip file"}, 400)
                dest = extract_zip_safely(tmp, qs.get("name", [""])[0])
            except Exception as e:
                return self._json({"error": f"extract failed: {e}"}, 500)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
            invalidate()
            return self._json({"ok": True, "path": str(dest)})

        # move a project to .trash (recoverable delete)
        if route == "/api/delete":
            target = safe_project_path(qs.get("path", [""])[0], allow_archived=True)
            if not target or target == PROJECTS_DIR:
                return self._json({"error": "invalid or protected path"}, 400)
            try:
                dest = trash_project(target)
            except OSError as e:
                return self._json({"error": f"delete failed: {e}"}, 500)
            invalidate()
            return self._json({"ok": True, "trashed_to": str(dest)})

        # move a project to .archive (still recoverable, no timestamp)
        if route == "/api/archive":
            target = safe_project_path(qs.get("path", [""])[0])
            if not target or target == PROJECTS_DIR:
                return self._json({"error": "invalid or already archived path"}, 400)
            try:
                dest = archive_project(target)
            except OSError as e:
                return self._json({"error": f"archive failed: {e}"}, 500)
            invalidate()
            return self._json({"ok": True, "archived_to": str(dest)})

        # move an archived project back
        if route == "/api/unarchive":
            target = safe_project_path(qs.get("path", [""])[0], allow_archived=True)
            if not target or ARCHIVE_DIR not in target.parents:
                return self._json({"error": "not an archived project"}, 400)
            try:
                dest = unarchive_project(target)
            except OSError as e:
                return self._json({"error": f"unarchive failed: {e}"}, 500)
            invalidate()
            return self._json({"ok": True, "restored_to": str(dest)})

        # set tags / favorite (body: {"tags": [...], "favorite": bool})
        if route == "/api/tags":
            target = safe_project_path(qs.get("path", [""])[0], allow_archived=True)
            if not target:
                return self._json({"error": "invalid or unknown project path"}, 404)
            body = self._read_body()
            if not body:
                return self._json({"error": "empty body"}, 400)
            try:
                payload = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return self._json({"error": "invalid JSON"}, 400)
            try:
                saved = scanner.write_tags(target, payload.get("tags", []), payload.get("favorite", False))
            except OSError as e:
                return self._json({"error": f"write failed: {e}"}, 500)
            invalidate()
            return self._json({"ok": True, **saved})

        # set note (body: raw markdown text)
        if route == "/api/note":
            target = safe_project_path(qs.get("path", [""])[0], allow_archived=True)
            if not target:
                return self._json({"error": "invalid or unknown project path"}, 404)
            body = self._read_body()
            text = body.decode("utf-8", errors="replace") if body else ""
            try:
                scanner.write_note(target, text)
            except OSError as e:
                return self._json({"error": f"write failed: {e}"}, 500)
            invalidate()
            return self._json({"ok": True, "length": len(text)})

        return self._send(404, "not found", "text/plain")

    def log_message(self, fmt, *args):
        print("[devboard]", self.address_string(), fmt % args)


def main():
    print(f"[devboard] projects dir : {PROJECTS_DIR}")
    print(f"[devboard] listening on  : http://0.0.0.0:{PORT}")
    if IDE_REMOTE_HOST and IDE_HOST_PROJECTS:
        print(f"[devboard] IDE URLs       : {IDE_REMOTE_HOST}:{IDE_HOST_PROJECTS}")
    if not PROJECTS_DIR.exists():
        print(f"[devboard] WARNING: {PROJECTS_DIR} does not exist yet.")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
