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
| Heavy-C projects  | bytecode mode (no C compile)  | (none — only sane option) |
| Python            | Highest stable 3.10–3.14 | `--python /path/to/python`           |
| Parallel jobs     | RAM-aware cap         | `--jobs N`                              |

### Heavy-C module handling

A few packages — `pymupdf` is the canonical case — ship a giant SWIG
wrapper that Nuitka would translate into ~2.2M lines of C. Every C
compiler runs out of memory on it (MSVC `C1002`, MinGW64 `cc1.exe:
out of memory`), regardless of RAM or pagefile.

The build script detects these in `requirements.txt`, `pyproject.toml`,
or the entry file's imports and appends
`--noinclude-custom-mode=<target>:bytecode` so Nuitka ships the offending
submodule as plain Python bytecode (`.pyc`) instead of compiling it.
CPython interprets it at runtime; the prebuilt native `.pyd` that
pymupdf actually calls is bundled by Nuitka's dll-files plugin. No giant
C, no OOM, ~30-min build instead of 2+ hours.

This is *not* "any large package". opencv-python, tensorflow, torch,
scipy, pandas, etc. ship prebuilt wheels and were never the problem.

**Source protection note:** your code stays Nuitka-compiled (machine
code). Only the SWIG wrapper — already public on PyPI — is in bytecode
form. Nothing of your IP is exposed.

**Fallback:** Nuitka's maintainer has flagged the `bytecode` mode as
"largely untested" for submodules in packages, so a future package could
break it. The documented fallback is a PyInstaller backend — see
`PROJECT_MEMORY.md` open items.

### Package-data module handling

Some packages (`python-barcode`, `PIL`, `qrcode`, …) ship non-Python
data files — fonts, templates — that Nuitka does not auto-bundle. The
library still runs in the exe but silently produces empty/broken output
(barcodes without text, images without glyphs). No traceback, no
exception.

The script keeps a `PACKAGE_DATA_MODULES` registry and auto-injects
`--include-package-data=<name>` for each one detected in the project,
so the data files are bundled. Add a new entry when you discover a new
silent-empty-output package. See `USER_GUIDE.md` §8 for details.

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
- `examples/build_hosts.template.toml` — host map for cross-OS `build_all.py`
- `examples/macos-build.yml` — GitHub Actions workflow for the macOS (arm64) host

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

## Cross-OS builds (Windows + Linux + macOS)

Nuitka **cannot cross-compile** — it only produces a binary for the OS it
runs on. To ship all three, the *same* `build.py` runs natively on a
Windows, a Linux, and a macOS host. `build_all.py` drives that:

```bash
python <Build_Scripts>/build_all.py "/path/to/project"
python <Build_Scripts>/build_all.py "/path/to/project" --only linux
python <Build_Scripts>/build_all.py "/path/to/project" -- --standalone --clean
```

It reads `build_hosts.toml` from the project root and, for every **enabled**
host, builds locally (`transport = "local"`), over SSH (`transport = "ssh"`
— `git pull`, remote `build.py`, copy artifact back), or on a GitHub
Actions Apple Silicon runner (`transport = "github"` — dispatch the
workflow via `gh`, wait, download the artifact; arm64 only, no Intel
macOS). Outputs are collected per-OS so they never collide:

```
<project>/dist/linux-x86_64/
<project>/dist/windows-amd64/
<project>/dist/macos-arm64/
<project>/dist/<project>-linux-x86_64.tar.gz   (auto-packaged)
```

`build.py` is unchanged — every one of its flags passes straight through
after `--`. You don't hand-maintain `build_hosts.toml`: it's auto-generated
in the project root on first run (current OS enabled), and regenerated with
`--init --force` (SSH host details preserved). With a single machine, just
your own OS builds now; flip the others on as you add a VM / SSH host. **macOS needs Apple hardware** (or a GitHub Actions macOS
runner) — see `USER_GUIDE.md` → "macOS without a Mac".

---

## Build many projects at once

`build_projects.py` runs `build_all.py` across **several projects × OSes** with a
per-OS concurrency lane, so independent work overlaps:

```bash
python <Build_Scripts>/build_projects.py                   # default set: Linux + Windows (macOS skipped)
python <Build_Scripts>/build_projects.py --only linux,windows,macos   # all three, incl. macOS
python <Build_Scripts>/build_projects.py --menu            # interactive picker, then build
python <Build_Scripts>/build_projects.py --windows-jobs 2  # 2 concurrent Windows builds
```

- **Lanes:** `windows = --windows-jobs` (default 1; one shared 16-vCPU VM is the
  L3-locality sweet spot), `linux = --linux-jobs` (default 2), `macos = --mac-jobs`
  (cloud — GitHub Actions does the compiling).
- **macOS is skipped by default** (private-repo Actions billing); add `--only ...,macos`
  to include it. A billing-blocked macOS run is reported as **SKIP**, not FAIL.
- **Windows VM auto start/stop** (libvirt): the run starts the VM if it's shut off and
  shuts it down afterward — only if it started it. See `HELP.md`.
- `Ctrl-C` aborts the whole run cleanly.

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

## Roadmap

- **[done] Three-OS release pipeline.** `build_all.py` drives Windows + Linux
  + macOS; all three projects (WealthBuilder, ajj3-brain, Thrift_Reseller)
  build green on all three OSes.
  - macOS via GitHub Actions (`transport = "github"`, arm64).
  - Windows native via SSH to the guest VM (`transport = "ssh"`, MSVC).
  - Linux local build + auto `.tar.gz` packaging.
- **[done] Parallel build-matrix mode** — `build_projects.py` schedules N projects ×
  OSes by per-OS lane: macOS in parallel (cloud runners), Linux RAM-bounded
  (`--linux-jobs`), Windows by `--windows-jobs` (default 1; the shared 16-vCPU VM is
  the L3-locality sweet spot, measured). Auto start/stops the Windows VM, skips macOS
  by default (Actions billing), aborts cleanly on Ctrl-C.
- **Code signing.** Unsigned binaries today; a `[codesign]` TOML section
  (Windows cert thumbprint, macOS developer ID) would close the gap.
- **Local macOS build host.** A small Apple-Silicon Mac (SSH host or self-hosted
  runner) for native arm64 — avoids the GitHub Actions billing wall.

---

## Requirements

- **Host**: Python 3.11+ (uses stdlib `tomllib`). On 3.10, install `tomli`.
- **Target project**: Python 3.10–3.14 installed somewhere on the system.
- **Windows**: MinGW64 (auto-downloaded) **or** Visual Studio Build Tools 2019+.
- **macOS**: Xcode CLI Tools (`xcode-select --install`).
- **Linux**: `build-essential` and `patchelf`.
