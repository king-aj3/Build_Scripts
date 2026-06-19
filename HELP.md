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
| `--force`             | (with `--init`) Overwrite, **preserving** curated values (entry, data_dirs, data_files, include_qt_plugins). (with a build) **Bypass the pre-build gate** and compile despite blocking issues. |
| `--reset`             | (with `--init`) Regenerate config **from scratch**, ignoring the existing file. No preservation. |

> `--init`/`--reset` set `include_qt_plugins` to `"sensible"`. That set already bundles the Qt `printsupport` family wherever Qt ships it (so `QtPrintSupport` printing keeps working) — naming it explicitly is redundant and FATALs on Qt 6.11+, which removed the standalone family. Never `"all"` (its qml plugins break the Linux build). A real QML app sets `"sensible,qml"`/`"all"` by hand; `--force` preserves that, `--reset` resets it. At build time a stale `,printsupport` carried over from a pre-1.8.7 config is stripped automatically.
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
| `--jobs N`      | Parallel C compile jobs. Default: RAM-aware cap (~1.5 GB per LTO job, 4 GB headroom, max 32) so high-core / modest-RAM machines do not OOM. An explicit value is honored as-is. |
| `--python PATH` | Force a specific interpreter (overrides auto-discovery).|

## Pre-build checks (automatic, since v1.11.0)

Every build runs two checks before Nuitka starts:

- **Repo-freshness report** — a read-only `git fetch` (bounded, non-fatal),
  then a line telling you if the tree is `N commits behind origin/<branch>`.
  Report-only: it never modifies your working tree. (Actually *pulling* is
  `build_all.py`'s job.)
- **Pre-build gate** — refuses to compile a binary that's already broken:
  a **missing entry point**, or a declared `data_files`/`data_dirs` path that
  isn't on disk. It exits *before* the compile so you don't waste 5–15 min.
  Version drift and unbundled-asset hints stay warnings. **`--force` bypasses
  the gate.**

## Common recipes

| Goal                              | Command                                                       |
| --------------------------------- | ------------------------------------------------------------- |
| Onboard a new project             | `python build.py . --init`                                    |
| Refresh config, keep my edits     | `python build.py . --init --force`                            |
| Rebuild config from scratch       | `python build.py . --init --reset`                            |
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

### Build fails with "Failed to add resources to file … the result is unusable"
The compile and link succeeded; the icon/version-resource embedding step at the
very end could not write to the freshly linked exe. Nuitka blames Windows
Defender, but on hosts running an EDR (e.g. **CylancePROTECT**) the real cause
is the EDR holding every new unsigned exe for ~60+ seconds — far longer than
Nuitka's stock retry window (5 × 1 s). Since v1.10.0 the script patches the
build env's Nuitka copy to retry 40 × 2 s during env setup (log line
"Patched Nuitka AV retry window"), which rides out the scan; a verified build
needed 34 attempts. If you still hit this, the env may predate v1.10.0 — re-run
the build (the patch applies on every env setup) — or the Nuitka version
changed its retry code (the script warns "layout changed" instead of patching);
then ask IT to exclude the project `build/` folders from the EDR.

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

**Does `--force --init` wipe my custom data_dirs / entry?**
No (v1.8.3+). `--init` preserves an existing build_config.toml's `[app].entry`
and `[nuitka] data_dirs`/`data_files`; auto-detection only fills what isn't
already set. Plain `--init` (no `--force`) still refuses to touch an existing
file. To intentionally start fresh, delete build_config.toml first, then
`--init`.

**Standalone app shows `{"error": "not found"}` or ships without its web UI?**
The app's asset folder lives nested inside a Python package (e.g.
`ajj3_brain/console/web`) and wasn't bundled. Nuitka follows a package's `.py` but
not its non-`.py` data. v1.8.8+ auto-detects these on `--init`/`--reset`; if you
built with an older build.py, re-run `--init --reset` (or add the dir to
`data_dirs` manually) and rebuild. Verify with: launch the binary, then
`find /tmp -ipath '*<app>*' -name index.html`.

---

## Cross-OS orchestrator (`build_all.py`)

Runs `build.py` on multiple OS hosts and collects per-OS binaries into
`<project>/dist/<os>-<arch>/`. Nuitka cannot cross-compile; this drives a
native build on each host instead.

```
python build_all.py <project_dir> [orchestrator-opts] [-- build.py-flags]
```

| Flag           | Effect                                                          |
| -------------- | --------------------------------------------------------------- |
| `--init`       | Generate a tailored `build_hosts.toml` (current OS enabled) in the project root, then exit. |
| `--force`      | With `--init`: overwrite an existing file (your SSH host details are preserved). |
| `--hosts PATH` | `build_hosts.toml` location (default: `<project>/build_hosts.toml`, then alongside `build_all.py`). |
| `--only A,B`   | Build only the named host sections (e.g. `--only linux,windows`).|
| `--no-pull`    | Skip `git pull`; build the working tree as-is.                  |
| `--dry-run`    | Print the commands that would run; build nothing.               |
| `-- ...`       | Everything after `--` is forwarded verbatim to `build.py` on every host. |

Host map (`build_hosts.toml`, per-project): each `[hosts.<name>]` is
`enabled`, `transport = "local"|"ssh"|"github"`, and an `arch` that forms the
output label `<name>-<arch>`. SSH hosts also need `ssh`, `repo`, `build_py`,
and optionally `python`, `key`, `port`. GitHub hosts (macOS arm64 via an
Actions runner — Intel macOS is not built) need `gh_repo = "OWNER/REPO"` plus
an authenticated `gh` CLI, and optionally `workflow`, `ref`, `artifact`. See
`examples/build_hosts.template.toml` and `examples/macos-build.yml`.

After every run, each successful **linux** host's `dist/<label>/` is
automatically packaged to `dist/<project>-<label>.tar.gz` (overwritten on
re-runs). Windows/macOS outputs are left as-is.

### Recipes

| Goal                                   | Command                                                  |
| -------------------------------------- | -------------------------------------------------------- |
| Build every enabled host               | `python build_all.py /path/to/project`                   |
| Just my current OS                     | `python build_all.py /path/to/project --only linux`      |
| Clean standalone build on all hosts    | `python build_all.py /path/to/project -- --standalone --clean` |
| See what would run                     | `python build_all.py /path/to/project --dry-run`         |

### Troubleshooting

**No `build_hosts.toml` yet** — you don't need to copy one. The first normal
run auto-generates a tailored file (your current OS enabled as a local host);
or run `build_all.py <project> --init` explicitly. Re-generate with `--init
--force` (SSH host details are preserved). The template in
`examples/build_hosts.template.toml` is just the full-option reference.

**SSH host: "Permission denied" / hangs** — confirm key-based SSH works
manually first: `ssh builder@host echo ok`. On Windows hosts, enable
OpenSSH Server (Settings → Optional Features) and ensure `git` and your
`python` are on that host's PATH.

**Artifact not copied back from an SSH host** — `build_all.py` uses `rsync`
if present, else `scp`. Check the remote build actually produced
`<repo>/dist/`. Folder (`--standalone`) builds copy the whole `dist/<name>/`
directory; onefile copies the single file.

**GitHub host: "gh CLI not found" / auth errors** — install the GitHub CLI
and run `gh auth login` once (`gh auth status` to verify). The workflow file
must exist in the project repo at `.github/workflows/macos-build.yml`
(copy from `examples/macos-build.yml`); a failed run can be inspected with
`gh run view <id> -R OWNER/REPO --log`.

## Multi-project scheduler (`build_projects.py`)

Builds **several projects** across their OS hosts in one command. Each
`(project × OS)` job runs as `build_all.py <project> --only <host>`, so the
audit gate, git pull, freshness, and per-OS artifact paths are all inherited.
Jobs are scheduled by **OS lane**, each with its own concurrency cap, so
independent work overlaps while shared, RAM-limited hosts stay serial.

```
python build_projects.py [PROJ ...] [options] [-- build.py-flags]
```

**Project list** comes from (in order): positional args → `--all` discovery →
the default list in `build_projects.toml`. So `build_projects.py` with **no
args** builds the curated default set. **Manage that set with `--list-projects`
/ `--add-project` / `--remove-project`** — no hand-editing the file. A bare name
is taken as a sibling project dir; a project qualifies once it has its own
`build_hosts.toml`.

| Flag                       | Effect                                                       |
| -------------------------- | ------------------------------------------------------------ |
| `--list-projects`          | Print the default project set (with status) and exit.        |
| `--add-project NAME ...`    | Add project(s) to `build_projects.toml` and exit (bare name = sibling dir, or a path). |
| `--remove-project NAME ...` | Remove project(s) from `build_projects.toml` and exit.      |
| `--config PATH`            | Default project-list TOML (default: `build_projects.toml` beside the script). |
| `--parallel`             | **Default.** Overlap jobs by lane; capture each to `build-logs/<project>-<host>.log`. |
| `--sequential`           | Run strictly one job at a time, streaming each build live.   |
| `--only A,B`             | Restrict to these OS hosts (e.g. `--only linux,macos`).      |
| `--linux-jobs N`         | Max concurrent **Linux** builds (default 2).                 |
| `--mac-jobs N`           | Max concurrent **macOS** builds (default: # of projects).    |
| `--all --root DIR`       | Discover every dir under `DIR` that has a `build_hosts.toml`.|
| `--build-all PATH`       | Path to `build_all.py` (default: alongside this script).     |
| `--log-dir DIR`          | Per-job logs in parallel mode (default: `./build-logs`).     |
| `--dry-run`              | Print the schedule; build nothing.                           |
| `-- ...`                 | Everything after `--` is forwarded to `build_all.py` → `build.py`. |

**Why lanes.** `windows = 1` (the shared build VM OOMs on concurrent compiles —
serial **even across projects**), `linux = --linux-jobs` (this box has the
cores; LTO eats RAM, so it's capped + tunable), `macos = --mac-jobs` (GitHub
Actions does the compiling, so local cost is just polling).

### Recipes

| Goal                                   | Command                                                          |
| -------------------------------------- | ---------------------------------------------------------------- |
| Build the default project set, all OSes| `python build_projects.py`                                       |
| List the default project set           | `python build_projects.py --list-projects`                       |
| Add a project to the default set       | `python build_projects.py --add-project NewProj`                 |
| Remove a project from the default set  | `python build_projects.py --remove-project NewProj`              |
| Build specific projects instead        | `python build_projects.py ../ajj3-brain ../WealthBuilder`        |
| Discover & build everything            | `python build_projects.py --all --root ..`                       |
| Linux only (safe first real run)       | `python build_projects.py ../A ../B ../C --only linux`           |
| One at a time, live output             | `python build_projects.py ../A ../B --sequential`                |
| More Linux overlap                     | `python build_projects.py ../A ../B ../C --linux-jobs 3`         |
| Clean standalone across all            | `python build_projects.py ../A ../B -- --standalone --clean`     |
| Preview the schedule                   | `python build_projects.py ../A ../B --dry-run`                   |
