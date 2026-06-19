# Build_Scripts

**What it is.** Shared Nuitka build tooling used across ALL the user's PyCharm projects. A single `build.py` compiles *any* project to a native executable (invoked from PyCharm External Tools or CLI); `build_all.py` orchestrates the same `build.py` across Windows/Linux/macOS hosts. `sync_projects.py` keeps the local repos in step with GitHub. Config-driven via `build_config.toml` or `pyproject [tool.nuitka_builder]`, else pure auto-detection.

**Status.** Active. build.py v1.11.1, build_all.py v1.2.2, build_projects.py v1.2.0, sync_projects.py v1.0.0 (per ABOUT.md, 2026-06-19).

## Stack & layout
- Python 3.11+ host (stdlib `tomllib`; `tomli` fallback on 3.10). No third-party deps — no requirements.txt.
- Entry points: `build.py` (per-machine build brain), `build_all.py` (cross-OS orchestrator for ONE project), `build_projects.py` (schedules MANY projects × OSes; calls `build_all.py --only <host>` per job), and `sync_projects.py` (multi-repo git status + safe fast-forward update).
- Shared modules (stdlib-only, imported by the scripts above — same dir): `gitutil.py` (the safe git layer — one subprocess chokepoint, read-only queries, FF-only pull; no force/reset surface), `projutil.py` (project selection + `build_projects.toml` CRUD, shared by build_projects.py and sync_projects.py).
- `examples/` — per-project `build_config.*.toml`, `build_config.template.toml`, `macos-build.yml` (GitHub Actions arm64).
- `build_hosts.template.toml` (repo root) — host-map template for `build_all.py`.
- `build_projects.toml` (repo root) — default project list for `build_projects.py` AND the default sync set for `sync_projects.py` (add a project = one line, or `build_projects.py --add-project`).
- Build/dist: artifacts (`build_env/`, `build/`, `dist/`, `build.log`) land in the **target** project, never here. Default `--onefile`; per-OS outputs in `dist/<os>-<arch>/`.

## How to run / build / test
```
# Generate a config for a target project (introspects it)
python build.py /path/to/project --init           # add --target=pyproject to write into pyproject.toml

# Build one project (default --onefile)
python build.py "/path/to/project"
python build.py "/path/to/project" --standalone --clean
python build.py "/path/to/project" --audit        # dry-run audit of resolved config
python build.py "/path/to/project" --info         # show config + Python survey
python build.py "/path/to/project" --test         # smoke-test after build

# Cross-OS build (reads build_hosts.toml from project root)
python build_all.py "/path/to/project"
python build_all.py "/path/to/project" --only linux
python build_all.py "/path/to/project" -- --standalone --clean   # flags after -- pass to build.py

# Build SEVERAL projects across all OSes at once (per-OS lanes: win=1, linux=2, mac=all)
python build_projects.py                          # the default set in build_projects.toml
python build_projects.py --only linux --dry-run   # preview a Linux-only run
python build_projects.py ../ajj3-brain            # specific project(s) instead of the default list
python build_projects.py --list-projects          # show the default set
python build_projects.py --add-project NewProj    # add to the default set (or --remove-project)

# Keep local repos in step with GitHub (safe: no verb = read-only status)
python sync_projects.py                            # status of the build-list set (fetch + table)
python sync_projects.py --all                      # status of EVERY git repo under the workspace
python sync_projects.py --pull --dry-run           # preview fast-forwards; mutate nothing
python sync_projects.py --pull                     # ff-only pull of clean+behind repos (per-repo confirm)
```
No unit-test suite in this repo; `build.py --test`/`--audit` against a real project is the smoke check. `sync_projects.py` is validated against throwaway scratch repos (behind/dirty/diverged cases).

## Conventions (match existing code)
- `from __future__ import annotations`; stdlib only; module-level version constants (`SCRIPT_VERSION`, `ORCH_VERSION`).
- Heavy-C / package-data handling lives in module-level registries in build.py — extend the registry, don't special-case inline.
- Config precedence: `build_config.toml` → `pyproject [tool.nuitka_builder]` → `pyproject [project]` → auto-detect. Keep this order.

## Fragile / do-NOT-touch
- Heavy-C bytecode mode (`--noinclude-custom-mode=...:bytecode`, e.g. pymupdf) is flagged "largely untested" by Nuitka upstream; PyInstaller backend is the documented fallback. Don't casually rework it.
- MinGW64 is BLOCKED on Python 3.13+ by Nuitka 4.x — script forces `MINGW64_SAFE_PYTHON = 3.12`. Preserve that guard.

## How I want you to work on this project
- Walk me through consequences before any destructive or behavior-changing edit: what the code does today, what changes, your confidence, and what could break — then wait for my yes/no. Batch only clearly-safe sweeps.
- Edit files directly on disk (I reload in PyCharm); don't hand me paste-in patches unless I ask.
- Prefer completing a half-built feature over deleting it, when that's a real option.
- After each change, run the smoke/compile check and report a clean state before moving on.
- I'm cautious about breaking working/production behavior — when in doubt, ask.

## Git
- Branch: master; remote: origin → https://github.com/king-aj3/Build_Scripts.git
- Commit/push only when I ask.

## Pointers
- `HELP.md` (CLI flags + troubleshooting), `USER_GUIDE.md` (PyCharm setup + config schema), `PROJECT_MEMORY.md` (design rationale + open items), `ABOUT.md` (versions/lineage).
