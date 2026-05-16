# About

**Project:** Build_Scripts — Common Nuitka Build System
**Script version:** 1.5.1
**Date:** 2026-05-16
**License:** Internal use

## What's new in 1.5.1

- **Fix: `--force-msvc` had no effect with `--compiler=auto`** (the
  default). The compiler-resolution logic checked `--compiler=auto`
  first and called the auto-resolver, which selected MinGW64 for heavy
  modules without ever consulting `force_msvc`. `--force-msvc` only
  worked when `--compiler=msvc` was *also* passed explicitly. It now
  takes precedence over `--compiler=auto` and over the heavy-module
  guard, as documented. On non-Windows it is ignored with a warning.

## What's new in 1.5.0

- **Heavy-module guard.** Before each build, the script scans
  `requirements.txt`, `pyproject.toml` dependencies (including
  optional-dependencies), and the entry file's top-level imports for
  packages in the new `HEAVY_MODULES` registry (`pymupdf`, `fitz`,
  `opencv-python`, `cv2`, `tensorflow`, `torch`, `scipy`, `pandas`,
  `lxml`, `shapely`, `rasterio`, `cryptography`, `pyarrow`, plus the
  `-cpu` / `-gpu` variants where applicable). When any match is
  detected, the script forces `--compiler=mingw64` on Windows — even if
  the user explicitly passed `--compiler=msvc` — because MSVC's pass-2
  heap (and LTCG, and the linker) cannot handle the multi-million-line
  generated C from these modules (`C1002` / `C1060` / `LNK1102`).
- **`--force-msvc` escape hatch** for the rare case where the user
  knows the failure mode and wants to try MSVC anyway.
- **`--info` and `--audit`** both now report whether heavy modules were
  detected, so it's obvious which compiler the build will pick before
  it starts.
- **`--init`-generated configs** no longer suggest manually adding
  `pymupdf.mupdf` to `nofollow_imports` — the guard handles it. Comment
  in the generated TOML explains that `nofollow_imports` is now only
  needed for project-specific dynamic-import edge cases.
- Rationale: one of the three predecessor projects (PromptForge) needed
  `nofollow_imports = ["pymupdf.mupdf"]` to survive MSVC. Without the
  guard, every new project using pymupdf or its siblings would re-hit
  the same C1002 failure and require the same manual workaround. The
  guard centralises that knowledge in the common script so no project
  ever has to learn this lesson again.

## What's new in 1.4.0

- **New `--compiler=auto` (now the default)** — tiered Windows compiler
  resolution:
  1. MSVC already installed (vswhere) → use MSVC + Python 3.13.
  2. MSVC missing + interactive prompt → offer to install Build Tools
     via winget. Install succeeds → MSVC + Python 3.13.
  3. Install fails or user declines → MinGW64 + Python 3.12 (auto-installs
     Python 3.12 if not present).
- **`--yes` / `-y` flag** auto-accepts install prompts (for CI / unattended
  builds). Without it, non-interactive terminals fall straight to the
  MinGW64 + Python 3.12 path with no prompt.
- **MSVC auto-install via winget**: VCTools workload only (~3 GB instead
  of 6 GB full Build Tools). Command:
  `winget install Microsoft.VisualStudio.2022.BuildTools --override "...--add Microsoft.VisualStudio.Workload.VCTools..."`.
- **Python 3.12 auto-install when MinGW64 path is taken** and only 3.13+
  is present. Uses the existing `winget Python.Python.3.12` mechanism.

## What's new in 1.3.0

- **Compiler-aware Python selection.** When `--compiler=mingw64` (the
  Windows default) is chosen, the script now prefers the newest installed
  Python *below 3.13* for the build venv. This sidesteps Nuitka 4.x's
  block on MinGW64 + Python 3.13+, so users with Python 3.10–3.12
  alongside 3.13 get MinGW64 with no warnings and no fallback.
- **MSVC detection via `vswhere`.** Previously the script only checked
  `cl.exe` on PATH, which most VS installs don't add. Now uses
  `vswhere.exe` (always installed alongside VS) to detect MSVC
  reliably. Applies both to `--compiler=msvc` checks and the MinGW→MSVC
  fallback.
- **Hard abort with actionable instructions** when neither MinGW64
  (blocked by Nuitka) nor MSVC (not installed) is usable. The error
  now lists both fixes (install Python 3.12 OR install VS Build Tools)
  with the exact winget commands.
- **Existing venv detection.** If `build_env/` already contains a
  Python ≥3.13 but the user wants MinGW64, the script rebuilds the venv
  with a compatible Python automatically (if one is installed).

## What's new in 1.2.2

- Auto-fallback: when the venv Python is 3.13+ and the user requested
  `--compiler=mingw64` (the default on Windows), the script now silently
  switches to MSVC. Nuitka 4.x emits `FATAL: cannot use '--mingw64' on
  Python version 3.13 or higher` — without the fallback, the build would
  fail at the Nuitka invocation with no actionable guidance. The warning
  message points at Python 3.12 if the user wants to keep MinGW64.

## What's new in 1.2.1

- Fix: `--clean` alone now correctly proceeds to build. Previously the
  clean handler short-circuited because it checked for `args.onefile`
  being explicitly True, but `--onefile` is the default action (no flag
  required), so the script would clean and exit without building. To
  clean without building, use `--clean --setup-only` (cleans + warms the
  venv) or delete `build/`, `dist/`, `build_env/` manually.

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
