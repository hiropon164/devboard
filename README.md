# devboard

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12+-blue.svg)](#)

A lightweight, self-hosted **project console**. Point it at a folder of projects and
it auto-scans them, showing each one's contents, README, and Git status in your
browser. Runs in Docker on the **Python standard library + git only** — no extra
dependencies.

> 日本語版: [README.ja.md](README.ja.md)

## Table of Contents

- [Features](#features)
- [Screenshots](#screenshots)
- [Getting Started](#getting-started)
- [Installation](#installation)
- [Usage](#usage)
- [Configuration](#configuration)
- [How Detection Works](#how-detection-works)
- [Project Structure](#project-structure)
- [API](#api)
- [Contributing](#contributing)
- [Disclaimer](#disclaimer)
- [License](#license)

## Features

- Recursively scans a directory and **auto-detects projects**
  - Code markers: `.git` / `package.json` / `pyproject.toml` / `Cargo.toml` / `go.mod` / `Makefile`, …
  - Doc markers (detected even without code): `README*` / `CLAUDE.md` / `AGENTS.md` / `GEMINI.md` / `.cursorrules` / copilot-instructions
- Language and **AI-tool detection** (Claude Code, Codex, Cursor, Copilot, Gemini, Aider, Continue)
- **Git status**: branch, uncommitted changes, ahead/behind, last commit, remote
- Sort by last activity (last commit or folder mtime)
- README rendering and an **expandable file tree** — click a folder to fold/unfold its contents inline (nested up to 3 levels deep; deeper folders are flagged)
- **AI rule-file viewer** (`CLAUDE.md` / `.cursorrules` / `AGENTS.md` / `GEMINI.md` / copilot-instructions / `.aider.conf.yml`)
- Per-project **tags & favorites** (`<project>/.devboard/tags.json`) and **notes** (`<project>/.devboard/note.md`)
- **Drag & drop** folder / `.zip` upload (macOS `__MACOSX/`, `.DS_Store`, `._*` stripped automatically)
- Archive (`./projects/.archive/`) and delete (moved to `./projects/.trash/`, not erased)
- **"Open in VS Code / Cursor"** buttons (Remote-SSH URLs)
- Search / filter by name, language, AI tool, tag, status
- HTTP JSON API

## Screenshots

![devboard — a project selected with its README rendered](docs/screenshot.png)

After starting, open <http://localhost:8080>.

## Getting Started

### Prerequisites

- Docker and Docker Compose
- `git` is bundled in the image (no host install required)

## Installation

```bash
git clone https://github.com/your-username/devboard.git
cd devboard
docker compose up -d --build
```

Then open <http://localhost:8080>.

## Usage

- Drag & drop a folder or `.zip` onto the page, **or** place project folders directly
  into `./projects/` on the host.
- The list auto-rescans every `CACHE_TTL` seconds (default 30).

## Configuration

Set via environment variables in `docker-compose.yml`:

| Variable | Default | Description |
|---|---|---|
| `PROJECTS_DIR` | `/data/projects` | Scan target inside the container |
| `PORT` | `8080` | Listen port |
| `SCAN_DEPTH` | `4` | Recursion depth for project discovery |
| `CACHE_TTL` | `30` | Seconds before the list auto-rescans |
| `MAX_UPLOAD_MB` | `500` | Max size per uploaded file / zip |
| `IDE_REMOTE_HOST` | (empty) | e.g. `user@your-host`. Enables "Open in IDE" buttons |
| `IDE_HOST_PROJECTS` | (empty) | Absolute host path to the projects dir (for IDE URLs) |

Set `user: "1000:1000"` to your own `id -u`:`id -g` so uploaded files aren't owned by root.
Do not commit secrets such as API keys or access tokens.

## How Detection Works

A directory is treated as a project root if it contains **any** code marker or doc
marker (see Features). Once a project root is found, the scanner does not descend into
it, so nested files don't create duplicate entries. Common build/dependency dirs
(`.git`, `node_modules`, `.venv`, `dist`, `build`, …) are skipped.

## Project Structure

```text
.
├── server.py          # HTTP server + JSON API
├── scanner.py         # Project discovery & inspection
├── index.html         # Single-page UI
├── Dockerfile
├── docker-compose.yml
├── projects/          # Your projects live here (scanned; git-ignored)
├── README.md          # English README
└── README.ja.md       # Japanese README
```

## API

```bash
# List projects
curl -s http://localhost:8080/api/projects

# Project detail
curl -s 'http://localhost:8080/api/project?path=/data/projects/<name>'

# Add tags / mark favorite
curl -s -X POST 'http://localhost:8080/api/tags?path=/data/projects/<name>' \
  -d '{"tags":["wip","experimental"],"favorite":true}'
```

## Contributing

Contributions are welcome.

1. Fork the repository.
2. Create a feature branch: `git checkout -b feature/your-feature`
3. Commit your changes: `git commit -m "Add your feature"`
4. Push to the branch: `git push origin feature/your-feature`
5. Open a pull request.

## Disclaimer

This software is provided "as is", without warranty of any kind (see [LICENSE](LICENSE)). It can upload, archive, and delete project files (deletes are moved to `./projects/.trash`, not erased). **Use it at your own risk** — review actions (especially destructive ones) and keep backups. The author assumes no responsibility for any data loss or damage.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.

## Acknowledgements

- Built with the Python standard library and `git` — no third-party runtime dependencies.
