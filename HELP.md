# HELP - Common Build Script

## Synopsis

```
python build.py [PROJECT_DIR] [options]
```

If `PROJECT_DIR` is omitted the current working directory is used.
PyCharm's `$ProjectFileDir$` macro is the recommended value.

## Build-mode flags (mutually exclusive)

| Flag           | Effect                                  |
| -------------- | --------------------------------------- |
| *(none)*       | Default: `--onefile`                    |
| `--onefile`    | Single-file executable                  |
| `--standalone` | Folder containing exe + dependencies    |

## Compiler flags (Windows only — Linux/macOS use system default)

| Flag                  | Notes                                                      |
| --------------------- | ---------------------------------------------------------- |
| `--compiler=auto`     | **Default.** MinGW64 for heavy-C projects (pymupdf); else MSVC if installed/installable; else MinGW64 + Python 3.12. |
| `--compiler=mingw64`  | Force MinGW64. Nuitka auto-downloads GCC. Required for pymupdf projects. |
| `--compiler=msvc`     | Force MSVC. Auto-detected via `vswhere`; aborts if missing. Fails on pymupdf projects (`C1002`). |
| `--compiler=clang`    | Use `clang.exe` if on PATH (needs MSVC SDK on Windows).    |
| `--force-msvc`        | Force MSVC regardless of `--compiler`. Do not use on pymupdf projects. |

## Operations

| Flag                  | Effect                                                       |
| --------------------- | ------------------------------------------------------------ |
| `--init`              | Generate `build_config.toml` from project introspection.     |
| `--target=pyproject`  | (with `--init`) Append to `pyproject.toml` instead.          |
| `--force`             | (with `--init`) Overwrite existing config.                   |
| `--clean`             | Remove `build/` and `dist/` before building.                 |
| `--clean-env`         | Also remove `build_env/` (use with `--clean` for full reset).|
| `--setup-only`        | Create the venv and install deps. Skip compilation.          |
| `--test`              | Launch the built exe (auto-passes after 30s).                |
| `--info`              | Show config + Python survey. No build.                       |
| `--audit`             | Validate config vs. project files. Suggests unbundled assets.|
| `--yes` / `-y`        | Auto-accept install prompts (MSVC Build Tools, Python).      |
| `--ci`                | Skip venv. Use current Python (for GitHub Actions).          |

## Tuning

| Flag            | Effect                                                  |
| --------------- | ------------------------------------------------------- |
| `--jobs N`      | Parallel C compile jobs. Default: CPU count for normal builds; `1` for heavy-C builds (pymupdf) so the giant unit compiles alone. An explicit value is honored as-is. |
| `--python PATH` | Force a specific interpreter (overrides auto-discovery).|

## Common recipes

| Goal                              | Command                                                       |
| --------------------------------- | ------------------------------------------------------------- |
| Onboard a new project             | `python build.py . --init`                                    |
| Onboard into existing pyproject   | `python build.py . --init --target=pyproject`                 |
| Quick rebuild                     | `python build.py .`                                           |
| Full clean rebuild                | `python build.py . --clean --clean-env`                       |
| Folder build (debug-friendly)     | `python build.py . --standalone`                              |
| Build then smoke-test             | `python build.py . --test`                                    |
| Use MSVC instead of MinGW         | `python build.py . --compiler=msvc`                           |
| Inspect config only               | `python build.py . --info`                                    |
| Check for config drift / orphans  | `python build.py . --audit`                                   |
| GitHub Actions                    | `python build.py . --ci`                                      |

## Troubleshooting

### "FATAL: cannot use '--mingw64' on Python version 3.13 or higher"
Nuitka 4.x blocks MinGW64 on Python 3.13+. As of v1.3.0 the build script
prefers Python ≤3.12 when MinGW64 is the chosen compiler, so this rarely
surfaces. If only Python 3.13+ is installed, the script:
- Falls back to MSVC if VS Build Tools is detected.
- Aborts with install instructions otherwise.

To keep MinGW64: `winget install Python.Python.3.12`, then re-run with
`--clean-env` to rebuild the venv against 3.12.

To switch to MSVC permanently: `winget install Microsoft.VisualStudio.2022.BuildTools`
(select "Desktop development with C++" workload), then re-run with
`--compiler=msvc`.

### Build fails with "MinGW64 not found"
With `--compiler=mingw64` (the default), Nuitka downloads MinGW64 on first
build. If the download fails, switch to MSVC: `--compiler=msvc`. Or install
MinGW64 via MSYS2: `winget install MSYS2.MSYS2`.

### Build fails with "MSVC out of memory" / C1002 / C1060
A package ships huge C *source* that Nuitka recompiled into one giant
translation unit, exhausting MSVC's heap. `pymupdf` (`mupdf.c`, ~2.2M
lines) is the usual cause. MSVC fundamentally cannot compile it.

Fix: build with MinGW64. `--compiler=auto` does this automatically when
it detects a heavy-C module; or pass `--compiler=mingw64` explicitly.
Do not use `--compiler=msvc` / `--force-msvc` on such projects.

If a *new* package triggers `C1002`, identify it from the failing
`module.<name>.c` path and add its import name to `HEAVY_C_MODULES` in
`build.py` — it will then route to MinGW64 automatically.

Note: a pymupdf build on MinGW64 takes ~2 hours (GCC compiling the
giant translation unit). That is expected.

### "patchelf not found" on Linux
```bash
sudo apt install patchelf                # Debian/Ubuntu
sudo dnf install patchelf                # Fedora/RHEL
```

### Build env was created with an experimental Python
The script auto-rebuilds the venv with a stable Python when one becomes
available. To force: `--clean-env`.

### TOML library missing on Python 3.10
```bash
pip install tomli
```
Or upgrade to Python 3.11+ which has `tomllib` in the standard library.

### My data files aren't bundled
Verify the paths in `[nuitka].data_files` exist relative to the project root.
The script prints `[!] Data file not found, skipping: <path>` for misses —
check `build.log`.

### PyQt6 / PySide6 conflict
If both are installed, Nuitka picks the wrong one. The build script
auto-uninstalls PyQt6 from `build_env/` when PySide6 is the configured
plugin. Set `plugins = ["pyqt6"]` in TOML to invert this.

### Heavy-C / pymupdf builds
The script appends `--noinclude-custom-mode=pymupdf.mupdf:bytecode` for
projects using pymupdf. Nuitka ships `mupdf.py` as plain bytecode instead
of compiling it to ~2.2M lines of C (which OOMs every C compiler on every
machine tested). The build runs at normal speed (~25-30 min) on either
MSVC or MinGW64.

If a built exe ever crashes at startup with `ImportError` or
`RuntimeError: Compiled function bytecode used`, the bytecode-mode path
has failed for that package — fallback is the PyInstaller backend
(see `PROJECT_MEMORY.md` open items).

### Exe produces empty output silently (no traceback)
Symptom: the exe runs, no exception is raised, console mode shows no
output, but a feature produces empty or broken results — empty images,
missing text on barcodes, blank graphics, etc.

Cause: a package ships non-Python data files (fonts, templates, tables)
that Nuitka did not auto-bundle. The library then runs without error
but returns empty results.

As of v1.8.1 known offenders (`barcode` / `python-barcode`, `PIL`,
`qrcode`) are detected and auto-handled via `--include-package-data`.
If a new package shows this behaviour:

1. Open `build.py`, find `PACKAGE_DATA_MODULES = {…}`.
2. Add the package: `"<import_or_dist_name>": "<actual_package_name>"`.
   The actual_package_name is case-sensitive (e.g. `"PIL"` not `"pil"`).
3. Rebuild — the `--include-package-data=<name>` flag is injected
   automatically.

Do **not** add large-data packages (matplotlib, scipy, …) — those have
dedicated Nuitka plugins. Blindly using `--include-package-data` on them
inflates the bundle massively.

### MinGW64 build fails early — CRT headers won't compile
Symptom: errors like `corecrt.h: expected ';' before 'typedef'` right at
the start of C compilation. Cause: **Nuitka's downloaded GCC is corrupt
or incomplete** (a truncated download). It is not a project problem and
not a Nuitka/GCC version incompatibility.
Fix: delete Nuitka's cached compiler so it re-downloads a clean copy:
```
rmdir /s /q "%LOCALAPPDATA%\Nuitka\Nuitka\Cache\downloads\gcc"
```
Then rebuild with `--clean --clean-env --compiler=mingw64`.

### Built exe says "install pymupdf" / app does not open
Cause: `pymupdf.mupdf` was excluded from the build via
`--nofollow-import-to` (an approach used in v1.6.0, removed in v1.7.0).
A nofollow'd module is not bundled, so the standalone exe crashes on
`import pymupdf` — and with `--windows-console-mode=disable` it crashes
silently (no window). Fix: ensure `pymupdf.mupdf` (or `pymupdf`) is NOT
in `nofollow_imports` in `build_config.toml`, and rebuild with
`--compiler=mingw64` (or `auto`). v1.7.0 compiles pymupdf normally.

### Where's `build.log`, and does it have the compiler error?
`<project_dir>/build.log`. As of v1.5.2 it captures the **full Nuitka
output**, including the C compiler / linker error that caused a failure.
First place to look after any failed build.
