"""
scanner.py - discover and inspect code projects under a root directory.

Pure standard library. Used by the web server (server.py) but can also be
imported or run standalone for debugging:

    python3 scanner.py /data/projects
"""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# A directory is treated as a project root if it contains one of these.
PROJECT_MARKERS = [
    ".git", "package.json", "pyproject.toml", "setup.py", "requirements.txt",
    "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "build.gradle.kts",
    "Gemfile", "composer.json", "Makefile", "CMakeLists.txt",
]

LANG_BY_MANIFEST = {
    "package.json": "JavaScript/Node",
    "pyproject.toml": "Python",
    "setup.py": "Python",
    "requirements.txt": "Python",
    "Pipfile": "Python",
    "Cargo.toml": "Rust",
    "go.mod": "Go",
    "pom.xml": "Java",
    "build.gradle": "Java/Kotlin",
    "build.gradle.kts": "Kotlin",
    "Gemfile": "Ruby",
    "composer.json": "PHP",
    "CMakeLists.txt": "C/C++",
}

AI_MARKERS = {
    "claude-code": ["CLAUDE.md", ".claude"],
    "codex":       ["AGENTS.md", ".codex"],
    "cursor":      [".cursor", ".cursorrules"],
    "copilot":     [".github/copilot-instructions.md"],
    "aider":       [".aider.conf.yml", ".aider.chat.history.md"],
    "gemini-cli":  ["GEMINI.md", ".gemini"],
    "continue":    [".continue"],
}

# AI rule files whose *content* is worth showing (instructions/config the user wrote).
# Order matters: first hit per tool wins for "primary" rule file display.
AI_RULE_FILES = {
    "claude-code": ["CLAUDE.md"],
    "codex":       ["AGENTS.md"],
    "cursor":      [".cursorrules", ".cursor/rules.md"],
    "copilot":     [".github/copilot-instructions.md"],
    "aider":       [".aider.conf.yml"],
    "gemini-cli":  ["GEMINI.md"],
}

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "env", "__pycache__",
    "target", "dist", "build", ".next", ".nuxt", ".cache", "vendor",
    ".idea", ".vscode", ".gradle", "Pods", ".terraform",
}

README_NAMES = ("README.md", "README.rst", "README.txt", "README", "readme.md")

# コードマーカーが無くても、README や AI ルールがあればドキュメント系プロジェクトとして検出する
DOC_MARKERS = list(README_NAMES) + [
    "CLAUDE.md", "AGENTS.md", "GEMINI.md", ".cursorrules",
    ".github/copilot-instructions.md",
]

DEVBOARD_DIR = ".devboard"
DEVBOARD_TAGS = "tags.json"
DEVBOARD_NOTE = "note.md"
ARCHIVE_DIRNAME = ".archive"


def run_git(args, cwd):
    try:
        out = subprocess.run(
            ["git"] + args, cwd=str(cwd),
            capture_output=True, text=True, timeout=8,
        )
        return out.stdout.strip() if out.returncode == 0 else None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def detect_languages(path):
    langs = []
    for manifest, lang in LANG_BY_MANIFEST.items():
        if (path / manifest).exists() and lang not in langs:
            langs.append(lang)
    if (path / "tsconfig.json").exists():
        langs = ["TypeScript"] + [l for l in langs if l != "JavaScript/Node"]
    return langs or ["(unknown)"]


def detect_ai_tools(path):
    found = []
    for tool, markers in AI_MARKERS.items():
        if any((path / m).exists() for m in markers):
            found.append(tool)
    return found


def find_readme(path):
    for name in README_NAMES:
        f = path / name
        if f.exists():
            return f
    return None


def read_readme(path, limit=200_000):
    f = find_readme(path)
    if not f:
        return None, ""
    try:
        text = f.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return f.name, ""
    return f.name, text


def readme_summary(text, maxlen=90):
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("#").strip()
        if line and not line.startswith(("![", "[!", "<", "---", "===", "```")):
            return (line[:maxlen] + "…") if len(line) > maxlen else line
    return ""


def get_git_info(path):
    info = {"is_repo": (path / ".git").exists()}
    if not info["is_repo"]:
        return info
    info["branch"] = run_git(["rev-parse", "--abbrev-ref", "HEAD"], path) or "?"
    info["remote"] = run_git(["config", "--get", "remote.origin.url"], path) or ""
    info["last_commit"] = run_git(["log", "-1", "--format=%cr"], path) or ""
    info["last_commit_iso"] = run_git(["log", "-1", "--format=%cI"], path) or ""
    info["last_subject"] = run_git(["log", "-1", "--format=%s"], path) or ""
    porcelain = run_git(["status", "--porcelain"], path)
    info["dirty"] = bool(porcelain) if porcelain is not None else False
    info["changes"] = len(porcelain.splitlines()) if porcelain else 0
    counts = run_git(["rev-list", "--left-right", "--count", "@{upstream}...HEAD"], path)
    if counts and "\t" in counts:
        behind, ahead = counts.split("\t")[:2]
        info["behind"], info["ahead"] = int(behind), int(ahead)
    else:
        info["behind"], info["ahead"] = 0, 0
    return info


def find_projects(root, max_depth=4):
    root = Path(root).expanduser().resolve()
    found = []

    def is_root(d):
        if any((d / m).exists() for m in PROJECT_MARKERS):
            return True
        # コード無しでも README / AI ルールがあれば検出（mdだけのドキュメント群など）
        return any((d / m).exists() for m in DOC_MARKERS)

    def walk(d, depth):
        if depth > max_depth:
            return
        if is_root(d):
            found.append(d)
            return
        try:
            children = sorted(d.iterdir())
        except (PermissionError, OSError):
            return
        for child in children:
            if (child.is_dir() and not child.is_symlink()
                    and child.name not in SKIP_DIRS
                    and not child.name.startswith(".")):
                walk(child, depth + 1)

    if not root.exists():
        return []
    if is_root(root):
        found.append(root)
    else:
        walk(root, 0)
    return found


def find_archived_projects(root):
    """List projects under PROJECTS_DIR/.archive (one level only)."""
    archive = Path(root).expanduser().resolve() / ARCHIVE_DIRNAME
    if not archive.exists():
        return []
    out = []
    try:
        for child in sorted(archive.iterdir()):
            if child.is_dir() and not child.is_symlink():
                out.append(child)
    except (PermissionError, OSError):
        pass
    return out


# ---------- devboard metadata (per-project sidecar) ----------

def _devboard_dir(path):
    return Path(path) / DEVBOARD_DIR


def read_meta(path):
    """Return {tags, favorite, note} from <project>/.devboard/."""
    d = _devboard_dir(path)
    tags, favorite, note = [], False, ""
    tags_f = d / DEVBOARD_TAGS
    if tags_f.exists():
        try:
            data = json.loads(tags_f.read_text(encoding="utf-8") or "{}")
            tags = [str(t) for t in data.get("tags", []) if isinstance(t, str)]
            favorite = bool(data.get("favorite", False))
        except (OSError, ValueError):
            pass
    note_f = d / DEVBOARD_NOTE
    if note_f.exists():
        try:
            note = note_f.read_text(encoding="utf-8", errors="replace")[:50_000]
        except OSError:
            pass
    return {"tags": tags, "favorite": favorite, "note": note}


def write_tags(path, tags, favorite):
    """Write tags/favorite to <project>/.devboard/tags.json. Returns saved meta."""
    d = _devboard_dir(path)
    d.mkdir(exist_ok=True)
    clean_tags = sorted({str(t).strip() for t in (tags or []) if str(t).strip()})
    payload = {"tags": clean_tags, "favorite": bool(favorite)}
    (d / DEVBOARD_TAGS).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


def write_note(path, text):
    """Write note to <project>/.devboard/note.md."""
    d = _devboard_dir(path)
    d.mkdir(exist_ok=True)
    body = (text or "")[:50_000]
    (d / DEVBOARD_NOTE).write_text(body, encoding="utf-8")
    return body


# ---------- AI rule file contents ----------

def read_ai_rules(path, per_file_limit=20_000):
    """Read the user-authored AI rule files (CLAUDE.md, .cursorrules, ...)."""
    out = []
    for tool, files in AI_RULE_FILES.items():
        for relname in files:
            f = path / relname
            if f.exists() and f.is_file():
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")[:per_file_limit]
                except OSError:
                    content = ""
                out.append({"tool": tool, "file": relname, "content": content})
                break  # one primary rule file per tool is enough
    return out


# ---------- summarize / detail ----------

def _last_activity(path, git_info):
    """ISO datetime of the most recent signal: git last commit or folder mtime."""
    candidates = []
    iso = git_info.get("last_commit_iso") if git_info else None
    if iso:
        candidates.append(iso)
    try:
        candidates.append(datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"))
    except OSError:
        pass
    return max(candidates) if candidates else ""


def summarize(path, archived=False):
    """Lightweight record for the list view."""
    git = get_git_info(path)
    _, readme_text = read_readme(path)
    meta = read_meta(path)
    mtime = ""
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        pass
    return {
        "name": path.name,
        "path": str(path),
        "languages": detect_languages(path),
        "ai_tools": detect_ai_tools(path),
        "summary": readme_summary(readme_text),
        "mtime": mtime,
        "git": git,
        "last_activity": _last_activity(path, git),
        "tags": meta["tags"],
        "favorite": meta["favorite"],
        "has_note": bool(meta["note"].strip()),
        "archived": archived,
    }


def scan(root, max_depth=4, include_archived=False):
    """List active projects; optionally append archived ones with archived=True."""
    projects = [summarize(p) for p in find_projects(root, max_depth)]
    if include_archived:
        projects += [summarize(p, archived=True) for p in find_archived_projects(root)]
    projects.sort(key=lambda p: p.get("last_activity") or p.get("mtime") or "", reverse=True)
    return projects


def list_files(path, max_depth=3, limit=2000):
    """Project contents as a nested tree (dirs first), up to max_depth levels deep.

    Each entry has {name, kind, size}. Directory entries also carry "children"
    (a list, possibly empty) and "truncated" (True when the directory's contents
    were not expanded because max_depth was reached, so the UI can flag it).
    SKIP_DIRS are listed but never descended into. ``limit`` caps the total
    number of entries across the whole tree to keep the payload bounded.
    """
    counter = [0]

    def walk(dir_path, depth):
        entries = []
        try:
            children = sorted(dir_path.iterdir())
        except (PermissionError, OSError):
            return entries
        for child in children:
            if counter[0] >= limit:
                break
            if child.name in SKIP_DIRS:
                entries.append({"name": child.name + "/", "kind": "skipped", "size": 0})
                counter[0] += 1
                continue
            is_dir = child.is_dir()
            try:
                size = child.stat().st_size if not is_dir else 0
            except OSError:
                size = 0
            entry = {
                "name": child.name + ("/" if is_dir else ""),
                "kind": "dir" if is_dir else "file",
                "size": size,
            }
            counter[0] += 1
            if is_dir:
                if depth < max_depth:
                    entry["children"] = walk(child, depth + 1)
                    entry["truncated"] = False
                else:
                    entry["children"] = []
                    entry["truncated"] = True
            entries.append(entry)
        entries.sort(key=lambda e: (e["kind"] != "dir", e["name"].lower()))
        return entries

    return walk(path, 1)


def detail(path, archived=False):
    """Full record for the detail panel: metadata + README + file list + AI rules + note."""
    path = Path(path)
    readme_name, readme_text = read_readme(path)
    meta = read_meta(path)
    rec = summarize(path, archived=archived)
    rec["readme_name"] = readme_name
    rec["readme"] = readme_text
    rec["files"] = list_files(path)
    rec["ai_rules"] = read_ai_rules(path)
    rec["note"] = meta["note"]
    return rec


if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    print(json.dumps(scan(root), ensure_ascii=False, indent=2))
