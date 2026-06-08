# USER GUIDE - Common Build Script

## 1. One-time setup

### 1.1 Place the Build_Scripts project

Put this folder anywhere convenient, e.g.:

- Windows: `C:\Dev\Build_Scripts\`
- Linux/macOS: `~/dev/Build_Scripts/`

The path will be referenced from PyCharm — pick somewhere stable.

### 1.2 (Optional) Use a dedicated Python for the builder

The builder itself only needs Python 3.11+ (for `tomllib`). The Python it
*uses* to build your project is auto-discovered separately. To pin the
builder to a specific Python:

```bash
cd <Build_Scripts>
python3.13 -m venv .venv
# Linux/macOS
.venv/bin/python -m pip install tomli   # only if host < 3.11
# Windows
.venv\Scripts\python -m pip install tomli
```

Then point PyCharm's External Tool at `.venv/bin/python` (Linux/macOS) or
`.venv\Scripts\python.exe` (Windows).

### 1.3 Configure PyCharm External Tools

**Settings → Tools → External Tools → +**

| Field              | Value                                                              |
| ------------------ | ------------------------------------------------------------------ |
| Name               | `Nuitka Build` (or anything you like)                              |
| Description        | `Compile current project via Nuitka`                               |
| Program            | `<Build_Scripts>/.venv/bin/python` (or system `python` / `python3`)|
| Arguments          | `"<Build_Scripts>/build.py" "$ProjectFileDir$"`                    |
| Working directory  | `$ProjectFileDir$`                                                 |

**On Windows**, the Program field is typically:
```
C:\Dev\Build_Scripts\.venv\Scripts\python.exe
```
Arguments:
```
"C:\Dev\Build_Scripts\build.py" "$ProjectFileDir$"
```

You can create extra tools with different argument suffixes:

| Tool name              | Extra argument         |
| ---------------------- | ---------------------- |
| `Nuitka Build`         | *(nothing)*            |
| `Nuitka Init`          | `--init`               |
| `Nuitka Audit`         | `--audit`              |
| `Nuitka Build Clean`   | `--clean --clean-env`  |
| `Nuitka Build Folder`  | `--standalone`         |
| `Nuitka Info`          | `--info`               |

---

## 2. Onboard a project

### Step 1 — Generate config with `--init`

From the new project directory:

```bash
python <Build_Scripts>/build.py . --init
```

This introspects the project and writes a `build_config.toml` with:
- Name/version pulled from `pyproject.toml` `[project]` if present
- Entry point auto-detected (`main.py`, `app.py`, etc.)
- GUI plugin detected from `requirements.txt` (PySide6, PyQt6, etc.)
- `assets/`, `resources/`, `themes/` etc. auto-listed under `data_dirs`
- Asset dirs nested inside packages (e.g. `ajj3_brain/console/web`) also auto-listed —
  Nuitka won't bundle non-`.py` data inside a package on its own, so a console/web
  UI shipped under a package is detected and added (top-level `config/`, `docs/`
  are left untouched)
- Top-level docs (`README.md`, `LICENSE.txt`, etc.) listed under `data_files`
- Icon path detected from common locations (`assets/icon.ico` etc.)

**Variants:**

```bash
# Append [tool.nuitka_builder] to existing pyproject.toml instead
python build.py . --init --target=pyproject

# Overwrite an existing config
python build.py . --init --force
```

Skip this step if you'd rather copy `examples/build_config.template.toml`
and fill it in by hand.

### `--force` vs `--reset`

Both rewrite `build_config.toml`, but differently:

- `--init --force` **merges**: it regenerates from detection but keeps
  curated values you set — `entry`, `data_dirs`, `data_files`,
  `include_qt_plugins`. Use this to refresh without losing hand edits.
- `--init --reset` **starts over**: it ignores the existing file entirely
  and regenerates from detection + current defaults (so a stale
  `include_qt_plugins = "all"` becomes `"sensible"` again). It will
  drop hand-added `data_files`/`data_dirs` and reset `entry` to the
  autodetected one, so re-add those afterward if needed.

### Step 2 — Review the generated config

Open `build_config.toml` and:

- Fill in `description` and `author` if `--init` left them blank
- Add anything Nuitka's static analysis won't see — dynamic imports
  (`importlib`, `__import__`, plugin loaders) — to `include_modules` or
  `include_packages`
- **Leave `nofollow_imports` empty unless you know exactly what you're
  doing.** A nofollow'd module is *excluded from the build* — importing
  it in a standalone exe raises ImportError at runtime. `pymupdf` is
  handled automatically (shipped as bytecode, see §7); do **not**
  list it here.

```toml
[nuitka]
include_packages     = ["reportlab", "openpyxl"]
include_modules      = ["ui.dynamic_loader"]
nofollow_imports     = []   # leave empty unless excluding a runtime-unused module
```

### Step 3 — Bundle data files

Anything you `open()` or load at runtime that isn't a `.py` file goes here.

**Preferred — bundle whole directories** (no edits when files are added later):

```toml
[nuitka]
data_dirs = ["assets", "resources"]
```

**Or — list individual files** (more control, more maintenance):

```toml
data_files = [
    ["assets/style.qss",  "assets/style.qss"],
    ["README.md",         "README.md"],
]
```

Format: `[source_relative_to_project, destination_inside_bundle]`.

### Step 4 — Validate, then build

```bash
python build.py . --audit    # confirm nothing's missing or orphaned
```

Then build via PyCharm: **Tools → External Tools → Nuitka Build**, or
from CLI: `python build.py .`

Output lands in `<project>/dist/MyApp.exe` (or `.app` / no-extension on
Linux).

---

## 3. `build_config.toml` schema reference

### `[app]` — required-ish

| Key           | Type   | Default                          | Notes                          |
| ------------- | ------ | -------------------------------- | ------------------------------ |
| `name`        | string | pyproject `name` or folder name  | Executable / product name      |
| `version`     | string | pyproject `version` or `0.0.0`   | `v` prefix is stripped         |
| `description` | string | pyproject `description` or `""`  | Set on the binary metadata     |
| `author`      | string | pyproject author or `"Unknown"`  | Company / copyright string     |
| `entry`       | string | `main.py` (auto-detected)        | Script Nuitka compiles         |

### `[build]` — script behaviour

| Key                  | Type         | Default | Notes                                                 |
| -------------------- | ------------ | ------- | ----------------------------------------------------- |
| `lto`                | `yes\|no\|auto` | `auto`  | `auto` = off on Windows+MSVC, on elsewhere         |
| `extra_requirements` | string list  | `[]`    | Installed in addition to `requirements.txt`           |

### `[nuitka]` — Nuitka flag mappings

| Key                    | Nuitka equivalent                       | Example                                |
| ---------------------- | --------------------------------------- | -------------------------------------- |
| `plugins`              | `--enable-plugin=X` (per entry)         | `["pyside6"]`                          |
| `include_qt_plugins`   | `--include-qt-plugins=X`                | `"sensible"`                           |
| `include_packages`     | `--include-package=X` (per entry)       | `["reportlab", "openpyxl"]`            |
| `include_package_data` | `--include-package-data=X` (per entry)  | `["tiktoken", "fitz"]`                 |
| `include_modules`      | `--include-module=X` (per entry)        | `["ui.dynamic_loader"]`                |
| `nofollow_imports`     | `--nofollow-import-to=X` (per entry)    | `["tests"]` (excludes from build)      |
| `extra_flags`          | raw passthrough                         | `["--noinclude-pytest-mode=nofollow"]` |
| `data_dirs`            | `--include-data-dir=src=dst`            | `["assets", "resources"]` or `[["src", "dst"]]` |
| `data_files`           | `--include-data-files=src=dst`          | `[["assets/x.qss", "assets/x.qss"]]`   |

### `[icons]` — per-platform icon paths

| Key       | Used for                              |
| --------- | ------------------------------------- |
| `windows` | `--windows-icon-from-ico` (.ico)      |
| `macos`   | `--macos-app-icon` (.icns)            |
| `linux`   | Bundled `.desktop` file (no icon flag)|

---

## 4. Day-to-day workflow

### What auto-updates (no edit needed)
- **`requirements.txt`** — every build reruns `pip install -r requirements.txt`
- **Version from `pyproject.toml`** — pulled in when `[app].version` is absent
- **New `.py` modules in your codebase** — Nuitka follows imports statically
- **New files inside `data_dirs`** — whole dir is re-bundled each build

### When you must edit `build_config.toml`
- Bumping app version for a release (one line)
- Adding a new top-level asset directory not previously declared
- Adding a dynamic-import module Nuitka can't see
- Adding a "tricky" package needing `--include-package-data`

### Catching drift with `--audit`
Run `python build.py . --audit` periodically. It scans the project and reports:
- Declared files/dirs that no longer exist
- Likely-asset files in `assets/`, `resources/`, etc. that aren't bundled
- Version mismatch between `pyproject.toml` and resolved config
- Missing entry point

| Situation                          | Run                                         |
| ---------------------------------- | ------------------------------------------- |
| Quick rebuild after code change    | `Nuitka Build`                              |
| `requirements.txt` changed         | `Nuitka Build` (auto-picks up new deps)     |
| Want a folder build for debugging  | `Nuitka Build Folder`                       |
| Build is acting weird              | `Nuitka Build Clean` then `Nuitka Build`    |
| New project onboarding             | `Nuitka Info` to verify config is parsed    |
| After adding new assets            | `Nuitka Audit` to check for unbundled files |

---

## 5. Putting config in `pyproject.toml` instead

If your project already has a `pyproject.toml`, you can skip
`build_config.toml` entirely and put everything under `[tool.nuitka_builder]`:

```toml
# pyproject.toml
[project]
name        = "MyApp"
version     = "1.0.0"
description = "What it does"

[tool.nuitka_builder.app]
entry = "main.py"

[tool.nuitka_builder.build]
lto = "auto"

[tool.nuitka_builder.nuitka]
plugins   = ["pyside6"]
data_dirs = ["assets"]

[tool.nuitka_builder.icons]
windows = "assets/icon.ico"
```

Same schema, just nested. `[project].name` and `[project].version` are
re-used automatically — you don't need to repeat them under
`[tool.nuitka_builder.app]`.

**Priority when both files exist:** `build_config.toml` →
`pyproject.toml [tool.nuitka_builder]` → `pyproject.toml [project]` →
auto-detect.

---

## 7. Heavy-C module handling

A few Python packages — `pymupdf` is the canonical case — ship a giant
SWIG-generated Python wrapper (`mupdf.py`) that Nuitka would translate
into ~2.2M lines of C. Every C compiler tested (MSVC, MinGW64) runs out
of memory on it (`C1002` on MSVC, `cc1.exe: out of memory` on GCC),
regardless of RAM or pagefile — the failure is a per-process
address-space ceiling, not a system-tuning problem.

This is **not** "any large package". opencv-python, tensorflow, torch,
scipy, pandas, lxml, etc. ship *prebuilt* `.pyd` / `.so` wheels — Nuitka
copies those as-is and never recompiles them, so they never trigger this
failure.

**The approach:** stop compiling the wrapper to C. The script appends
`--noinclude-custom-mode=<target>:bytecode` to the Nuitka command. Nuitka
then ships that submodule as plain Python bytecode (`.pyc`); CPython
interprets it at runtime; the prebuilt native `.pyd` that pymupdf
actually calls into is bundled by Nuitka's dll-files plugin as before.
No giant C is generated, MSVC handles the rest of the build normally,
and total build time is ~15–30 min instead of ~2 hours.

The registry in `build.py` maps detect-name → bytecode-target:

```python
HEAVY_C_MODULES = {
    "pymupdf": "pymupdf.mupdf",   # SWIG wrapper -> ship as bytecode
    "fitz":    "pymupdf.mupdf",   # legacy import alias
}
```

Detection scans `requirements.txt`, `pyproject.toml`'s `dependencies` /
`optional-dependencies`, and the entry file's top-level imports.

### What this means for source protection

Your application code is still Nuitka-compiled to native machine code —
fully protected against casual reverse engineering. The only thing
shipped as bytecode is `pymupdf.mupdf`, which is the SWIG wrapper —
already open-source on PyPI, so nothing of *your* IP is exposed.

### If the bytecode path ever stops working

This approach is confirmed working for pymupdf as of v1.8.0. Nuitka's
maintainer has flagged `bytecode` mode as "largely untested" for
submodules inside packages, so a future package or Nuitka version could
break it — the failure mode would be the exe crashing at startup with
`ImportError` or `RuntimeError: Compiled function bytecode used`. If
that happens, the documented fallback is a PyInstaller backend (no C
compilation; recorded in PROJECT_MEMORY open items).

### See what was detected

```bash
python build.py . --info     # shows the bytecode targets
python build.py . --audit    # has a [heavy-C modules] section
```

### Add a new entry

1. From a failing `module.<name>.c` path, identify the package.
2. Open `build.py`, find `HEAVY_C_MODULES = {…}`.
3. Add `"<import_name>": "<submodule.to.bytecode>"` (and the pip
   distribution name too if it differs from the import name).
4. Rebuild — the bytecode flag is injected automatically.

### If a MinGW64 build fails early (corrupt GCC)

Unrelated to heavy-C, but worth noting: if a MinGW64 build dies almost
immediately with C header errors (e.g. `corecrt.h: expected ';' before
'typedef'`), Nuitka's downloaded GCC is corrupt. Delete and rebuild:

```
rmdir /s /q "%LOCALAPPDATA%\Nuitka\Nuitka\Cache\downloads\gcc"
python build.py . --clean --clean-env --compiler=mingw64
```

---

## 8. Package-data module handling

Some packages compile fine but ship **non-Python data files** (fonts,
templates, .ttf, .svg) inside the package directory. Nuitka does **not**
auto-bundle those. The library still runs in the exe, raises no
exception, prints nothing — but silently produces empty or broken output
(barcodes without bars, images without text, etc.).

The script keeps a registry in `build.py` mapping detect-name → Nuitka
output name:

```python
PACKAGE_DATA_MODULES = {
    "barcode":        "barcode",   # python-barcode ships .ttf fonts
    "python_barcode": "barcode",   # pip dist name -> import name
    "pil":            "PIL",       # case-sensitive on disk
    "pillow":         "PIL",       # pip dist name -> import name
    "qrcode":         "qrcode",
}
```

Detection scans the same sources as heavy-C (`requirements.txt`,
`pyproject.toml`, the entry file's top-level imports). When a match is
found the script appends `--include-package-data=<output_name>` to the
Nuitka command. The build banner shows what was auto-added:

```
  Pkg-data  : PIL, barcode, qrcode
              --include-package-data=PIL  --include-package-data=barcode  --include-package-data=qrcode
```

### When to add a new entry

You'll know it when you see it: a feature in the exe silently produces
empty output, no exception, nothing in `cmd.exe` even with console
enabled. That's the smoking gun for unbundled data files.

1. Identify the package (often a font/template-using one).
2. Open `build.py`, find `PACKAGE_DATA_MODULES = {…}`.
3. Add `"<import_or_dist_name>": "<actual_package_name>"`. The
   right-hand-side is case-sensitive (e.g. `"PIL"` not `"pil"`).
4. Rebuild — flag injected automatically.

### What NOT to add

This is **not** a "include everything" knob. Big-data packages
(matplotlib, scipy, numpy, etc.) have dedicated Nuitka plugins that
already handle their data correctly. Blindly using
`--include-package-data` on them inflates the bundle by tens to hundreds
of MB. Only list known small-data, non-plugin-covered packages.

### See what was detected

```bash
python build.py . --info     # has a "Pkg-data modules" line
python build.py . --audit    # has a [package-data modules] section
```

---

## 9. CI / GitHub Actions

```yaml
- uses: actions/checkout@v4
- uses: actions/setup-python@v5
  with: { python-version: '3.13' }
- run: pip install tomli  # only if Python < 3.11
- run: python <Build_Scripts>/build.py . --ci --onefile
- uses: actions/upload-artifact@v4
  with:
    name: ${{ matrix.os }}
    path: dist/*
```

In CI mode the script uses the current Python directly (no venv), since the
runner is already an isolated environment.

---

## 10. Cross-OS builds with `build_all.py`

`build.py` builds for the OS it runs on. To produce Windows **and** Linux
**and** macOS binaries, `build_all.py` runs `build.py` natively on each
host and gathers the results into `<project>/dist/<os>-<arch>/`.

### Step 1 — Host map (auto-created)

You don't copy or maintain this by hand. `build_all.py` generates a tailored
`build_hosts.toml` in the project root on the first run — or explicitly:

```bash
python <Build_Scripts>/build_all.py /path/to/project --init
python <Build_Scripts>/build_all.py /path/to/project --init --force   # regenerate
```

The generated file enables your **current OS** as a local host; the other two
are SSH stubs. `--force` regenerates while **preserving any SSH host details
you've filled in** (same spirit as `build.py --init --force`). Each
`[hosts.<name>]` maps to an output folder `dist/<name>-<arch>/`.
`examples/build_hosts.template.toml` remains the full-option reference.

```toml
[hosts.linux]
enabled = true
transport = "local"     # builds on this machine — no SSH needed
arch = "x86_64"
```

### Step 2 — Build

```bash
python <Build_Scripts>/build_all.py /path/to/project            # all enabled hosts
python <Build_Scripts>/build_all.py /path/to/project --only linux
python <Build_Scripts>/build_all.py /path/to/project -- --standalone --clean
```

Anything after `--` is passed straight to `build.py` on every host.

### Step 3 — Add remote hosts over SSH (when you have them)

A Windows VM or a second box becomes a build host with `transport = "ssh"`:

```toml
[hosts.windows]
enabled   = true
transport = "ssh"
ssh       = "builder@192.168.1.50"
repo      = "C:/Users/builder/dev/MyApp"     # the cloned repo ON that host
build_py  = "C:/Dev/Build_Scripts/build.py"  # build.py ON that host
python    = "py -3.12"
arch      = "amd64"
```

One-time SSH prep on each remote host:

1. **Key-based login.** `ssh-copy-id builder@host` (or add your public key
   to the host). Verify: `ssh builder@host echo ok` returns instantly.
2. **Clone the repo** on that host at the `repo` path (your source of truth
   is GitHub — `build_all.py` runs `git pull` there before each build).
3. **Install build.py's prerequisites** on that host (a compatible Python
   plus the OS toolchain — see this guide's Requirements). Put `git` and the
   `python` you named on the host's PATH.
4. **Windows only:** enable OpenSSH Server (Settings → Optional Features →
   Add a feature → OpenSSH Server), then `Start-Service sshd`.

Then re-run `build_all.py` — the new host builds and its binary lands in
`dist/windows-amd64/`.

### macOS without a Mac

Apple's license only permits macOS in a VM on Apple hardware, so there is no
clean way to make a macOS binary on a Linux or Windows box. Two realistic
options:

- **A Mac** (even a low-end Mac mini) as an SSH host — same `[hosts.macos]`
  pattern as above.
- **A GitHub Actions macOS runner** — your repos are already on GitHub, so
  this needs no local hardware. Keep everything else local and let CI build
  *only* macOS:

```yaml
jobs:
  macos:
    runs-on: macos-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.13' }
      - run: python <Build_Scripts>/build.py . --ci --onefile
      - uses: actions/upload-artifact@v4
        with: { name: macos-arm64, path: dist/* }
```

Download the artifact and drop it into `dist/macos-arm64/` to complete the
three-OS set.
