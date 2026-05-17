# Build_Scripts

A single Nuitka-based build script that compiles **any** PyCharm project to a
native executable, invoked from PyCharm's External Tools.

Consolidates and replaces the three per-project build scripts
(`build_Prompt.py`, `build_TLZN.py`, `build_Thrift.py`).

---

## What it does

- Reads `build_config.toml` from the target project root, OR
  `[tool.nuitka_builder]` from the project's existing `pyproject.toml`.
- Falls back to `pyproject.toml [project]` for standard metadata.
- Creates an isolated `build_env/` venv inside that project.
- Installs `requirements.txt` + Nuitka.
- Runs Nuitka with the right flags for the current OS.
- Drops the executable in `<project>/dist/`.

All build artefacts (`build_env/`, `build/`, `dist/`, `build.log`) land in the
**target project**, not in this `Build_Scripts` project.

---

## Defaults

| Setting           | Default               | Override                                |
| ----------------- | --------------------- | --------------------------------------- |
| Build mode        | `--onefile`           | `--standalone`                          |
| Compiler (Win)    | `--compiler=auto`     | `--compiler=msvc\|mingw64\|clang`       |
| Heavy-C projects  | routed to MinGW64     | (none — MSVC cannot build them)         |
| Python            | Highest stable 3.10–3.14 | `--python /path/to/python`           |
| Parallel jobs     | CPU count             | `--jobs N`                              |

### Heavy-C module handling

A few packages ship huge **C source** that Nuitka recompiles into one
enormous translation unit. The canonical case is `pymupdf` — its
`mupdf.c` is ~2.2M lines. MSVC's compiler heap fails on it (`C1002`);
GCC/MinGW64 compiles it fine (slowly).

The build script scans `requirements.txt`, `pyproject.toml`, and the
entry-point's imports for these. When found, `--compiler=auto` **routes
the build to MinGW64**. The module is compiled and bundled normally —
the result is a fully standalone exe. A pymupdf build takes ~2 hours.

This is *not* "any large package". opencv-python, tensorflow, torch,
scipy, pandas, etc. ship prebuilt wheels; Nuitka never recompiles them,
so they never triggered `C1002`. Only source-shipping packages
(pymupdf) are in the registry.

The script does **not** exclude heavy-C modules via
`--nofollow-import-to` — that would drop them from the bundle and the
exe would crash at startup. See `HEAVY_C_MODULES` in `build.py` to
register a new offender, and `PROJECT_MEMORY.md` for the full history.

---

## Quick start

### 1. Generate a config for your project

```bash
python <Build_Scripts>/build.py /path/to/project --init
```

This introspects the project (name, version, entry point, GUI plugin,
asset directories, icon) and writes a tailored `build_config.toml` to the
project root. Add `--target=pyproject` to append into an existing
`pyproject.toml` instead.

For existing projects, you can also copy one of the pre-made configs in
`examples/`:

- `examples/build_config.PromptForge.toml` — for PromptForge
- `examples/build_config.TLZN.toml` — for TLZN_Calculator
- `examples/build_config.Thrift.toml` — for Thrift Reseller Tracker
- `examples/build_config.template.toml` — blank template, all options documented

### 2. Set up the PyCharm External Tool (one-time, per IDE)

See `USER_GUIDE.md` → "PyCharm External Tools setup".

### 3. Build

From PyCharm: **Tools → External Tools → Nuitka Build**.

From CLI:
```bash
python <Build_Scripts>/build.py "/path/to/project"
python <Build_Scripts>/build.py "/path/to/project" --clean --standalone
python <Build_Scripts>/build.py "/path/to/project" --audit
```

---

## Documentation

| File                 | Purpose                                                |
| -------------------- | ------------------------------------------------------ |
| `README.md`          | This file — overview                                   |
| `HELP.md`            | CLI flag reference + troubleshooting                   |
| `USER_GUIDE.md`      | PyCharm setup + walkthrough + config schema reference  |
| `ABOUT.md`           | Version, credits, lineage                              |
| `PROJECT_MEMORY.md`  | Design rationale and historical decisions              |

---

## Requirements

- **Host**: Python 3.11+ (uses stdlib `tomllib`). On 3.10, install `tomli`.
- **Target project**: Python 3.10–3.14 installed somewhere on the system.
- **Windows**: MinGW64 (auto-downloaded) **or** Visual Studio Build Tools 2019+.
- **macOS**: Xcode CLI Tools (`xcode-select --install`).
- **Linux**: `build-essential` and `patchelf`.
