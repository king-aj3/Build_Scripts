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
| `--compiler=auto`     | **Default.** MSVC if installed/installable, else MinGW64 + Python 3.12. |
| `--compiler=mingw64`  | Force MinGW64. Auto-installs Python 3.12 if needed.        |
| `--compiler=msvc`     | Force MSVC. Auto-detected via `vswhere`; aborts if missing.|
| `--compiler=clang`    | Use `clang.exe` if on PATH (needs MSVC SDK on Windows).    |

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
| `--jobs N`      | Parallel C compile jobs. Default: CPU count.            |
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

### Build fails with "MSVC out of memory" / LTCG heap exhaustion
The `pymupdf.mupdf` module compiles to ~2M lines of C and crashes MSVC's
LTCG. Add `nofollow_imports = ["pymupdf.mupdf"]` to your `build_config.toml`
**or** use the default `--compiler=mingw64`.

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

### Where's `build.log`?
`<project_dir>/build.log` — full timestamped trail of every step. First place
to look after a failure.
