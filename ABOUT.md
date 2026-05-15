# About

**Project:** Build_Scripts — Common Nuitka Build System
**Script version:** 1.2.0
**Date:** 2026-05-15
**License:** Internal use

## What's new in 1.2.0

- `--init` command — auto-generates a tailored `build_config.toml` from
  project introspection. Detects entry point, GUI plugin (from
  `requirements.txt`), asset directories (`assets/`, `resources/`, etc.),
  top-level docs, and icon paths.
- `--target=pyproject` writes the config as a `[tool.nuitka_builder]`
  section appended to an existing `pyproject.toml` instead of creating
  a separate file.
- `--force` allows overwriting an existing config.

## What's new in 1.1.0

- `pyproject.toml [tool.nuitka_builder]` recognised as a config source
  alongside `build_config.toml`. Same schema, just nested under `tool.*`.
- `data_dirs` config key — bundle whole directories instead of listing
  files.
- `--audit` command — read-only check for declared-but-missing files,
  unbundled assets, version drift, and missing entry point.
- `--info` reports which config sources are present.

## What this is

A single PyCharm External Tool that compiles any of the user's Python
projects into a native executable via [Nuitka](https://nuitka.net). Replaces
three separate per-project `build.py` files with one driver that reads a
declarative `build_config.toml` from each project.

## Lineage

Consolidated from three predecessors, each contributing distinct features:

| Predecessor       | What it brought                                                  |
| ----------------- | ---------------------------------------------------------------- |
| `build_Prompt.py` | Concise CLI, `--nofollow-import-to=pymupdf.mupdf` workaround     |
| `build_TLZN.py`   | Python auto-discovery, `--info`, `--test`, `--ci`, auto-install  |
| `build_Thrift.py` | `build.log`, Win32 Ctrl+C / UTF-8 / QuickEdit hardening, ASCII I/O|

All three are now obsolete and can be removed from their respective projects
once a `build_config.toml` is in place.

## Design philosophy

- **Declarative over imperative.** Each project's identity lives in TOML, not
  Python — no risk of side effects, no need to read code to find the build
  config.
- **Project-local artefacts.** `build_env/`, `build/`, `dist/`, `build.log`
  all land in the target project, never in `Build_Scripts/`.
- **Fail loud, fail informative.** Every failure mode in the predecessors
  was kept and pointed at the right recovery action.
- **MinGW64-first on Windows.** MSVC is supported, but MinGW64 avoids the
  LTCG heap exhaustion and toolchain-install pain that bit two of the three
  predecessor scripts.

## What did NOT make the cut

- The `--all-platforms` cross-platform guide text from `build_TLZN.py` — same
  info now lives in `USER_GUIDE.md` under "CI / GitHub Actions".
- `build_Thrift.py`'s spinner threads and waypoint counter — replaced by a
  simpler timestamped log line per step.
