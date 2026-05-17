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

### Step 2 — Review the generated config

Open `build_config.toml` and:

- Fill in `description` and `author` if `--init` left them blank
- Add anything Nuitka's static analysis won't see — dynamic imports
  (`importlib`, `__import__`, plugin loaders) — to `include_modules` or
  `include_packages`
- **Leave `nofollow_imports` empty unless you know exactly what you're
  doing.** A nofollow'd module is *excluded from the build* — importing
  it in a standalone exe raises ImportError at runtime. `pymupdf` is
  handled automatically (the build routes to MinGW64, see §7); do **not**
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
| `include_qt_plugins`   | `--include-qt-plugins=X`                | `"all"`                                |
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

## 7. Heavy-C module handling (Windows)

A few Python packages ship huge **C source** that Nuitka recompiles into
one enormous translation unit, exhausting MSVC's compiler heap:

- `C1002` — "compiler is out of heap space in pass 2"
- `C1060` — "compiler is out of heap space" (LTCG)

This is **not** "any large package". opencv-python, tensorflow, torch,
scipy, pandas, lxml, etc. ship *prebuilt* `.pyd` / `.so` wheels — Nuitka
copies those as-is and never recompiles them, so they never cause
`C1002`. Only packages that ship compilable C source qualify.

`pymupdf` is the canonical (and currently the only common) case: its
`mupdf.c` is a SWIG-generated file of ~2.2M lines.

The build script keeps a `HEAVY_C_MODULES` registry in `build.py` — a
set of package names:

```python
HEAVY_C_MODULES = {
    "pymupdf",   # ships SWIG-generated mupdf.c, ~2.2M lines
    "fitz",      # legacy import alias for pymupdf
}
```

Before each build the script scans:

1. `requirements.txt`
2. `pyproject.toml` `[project].dependencies` and `optional-dependencies`
3. Top-level `import` / `from X import …` statements in the entry file

If a match is found, `--compiler=auto` **routes the build to MinGW64**.
GCC/MinGW64 compiles the giant translation unit fine where MSVC cannot.
The module is compiled and bundled normally — the result is a fully
standalone exe. (`--compiler=msvc` on such a project will fail with
`C1002`; the script warns you if you try.)

> **Why not exclude the module instead?** An earlier version
> (v1.6.0) auto-added `--nofollow-import-to=pymupdf.mupdf` to keep MSVC
> viable. That was wrong: a nofollow'd module is *excluded from the
> build*, so the standalone exe crashed at startup with ImportError. A
> standalone build must actually compile every module it needs.

### Build time and memory

A pymupdf project on MinGW64 takes roughly **2–2.5 hours** — GCC
compiling the multi-million-line `mupdf` unit is inherently slow.

That unit also needs a lot of RAM. The script auto-tunes for it: it
detects total system RAM and picks a safe `--jobs` count (≈4 GB
budgeted per parallel job) and decides LTO (kept only at ≥ 32 GB, where
its memory-hungry link stage is affordable). You'll see a line like:

```
  RAM       : 16 GB  ->  auto-tuned --jobs=2, lto=no
```

Override either if you know better: `--jobs N` on the command line, or
`lto = "yes"/"no"` in `build_config.toml`. If a build dies with
`cc1.exe: out of memory`, the machine is short on RAM — close other
apps or force `--jobs 1`.

### See what was detected

```bash
python build.py . --info     # shows "Heavy-C modules: ..." line
python build.py . --audit    # has a [heavy-C modules] section
```

### Add a new entry when you hit a fresh C1002

1. From the failing `module.<name>.c` path, identify the package.
2. Open `build.py`, find `HEAVY_C_MODULES = {…}`.
3. Add its import name (and the pip distribution name too if different).
4. Rebuild — `--compiler=auto` now routes it to MinGW64.

### If a MinGW64 build fails early (corrupt GCC)

If a MinGW64 build dies almost immediately with C header errors (e.g.
`corecrt.h: expected ';' before 'typedef'`), Nuitka's downloaded GCC is
corrupt. Delete it and rebuild — Nuitka re-downloads a clean copy:

```
rmdir /s /q "%LOCALAPPDATA%\Nuitka\Nuitka\Cache\downloads\gcc"
python build.py . --clean --clean-env --compiler=mingw64
```

---

## 8. CI / GitHub Actions

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
