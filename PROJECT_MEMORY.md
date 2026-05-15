# Project Memory

Persistent design decisions and rationale. Read this before making changes
that touch the architecture.

---

## Why a single common script instead of per-project scripts

The three predecessor scripts (`build_Prompt.py`, `build_TLZN.py`,
`build_Thrift.py`) had drifted apart: each project re-implemented Python
discovery, venv setup, compiler checks, and Nuitka invocation. Bug fixes
made in one didn't propagate. PyCharm External Tools makes the unified
approach cheap — `$ProjectFileDir$` macro tells the script which project is
active, so a single file in a separate location can serve all of them.

---

## Why TOML config and not auto-detection

Pure auto-detection would silently lose project-specific knowledge that
took real debugging effort to find:

- `PromptForge` needs `--nofollow-import-to=pymupdf.mupdf` because that
  module generates a 2M-line C file that exhausts MSVC's LTCG heap. No
  amount of project scanning can infer this.
- `Thrift` needs ~35 explicit `--include-module=...` flags because its UI
  loads tabs and parsers via dynamic `importlib` calls. Nuitka cannot
  statically detect them.
- `TLZN` needs `reportlab.platypus`, `reportlab.lib`, `reportlab.pdfgen`
  declared individually because `reportlab` uses lazy attribute imports.

TOML preserves all of this declaratively. pyproject.toml is read as a
fallback for `name` / `version` only — it doesn't have schema for Nuitka
flags, and stretching it would couple build config to packaging config.

---

## Why both `build_config.toml` AND `pyproject.toml` are supported

Standard Python convention says tool config goes under `[tool.<name>]` in
`pyproject.toml` (Black, Ruff, mypy, pytest all follow this). But two
constraints make a standalone `build_config.toml` also valuable:

1. **Not every project has a `pyproject.toml`.** Older projects (including
   two of the three predecessors) had only `requirements.txt`. Forcing a
   `pyproject.toml` migration just to build is annoying.
2. **Separation of concerns.** `pyproject.toml` is for packaging
   (uploading to PyPI, dependency declarations). Mixing it with Nuitka
   build flags couples two unrelated concerns. Some users prefer them
   separated; others prefer one source of truth.

So we read both. Priority: `build_config.toml` →
`pyproject.toml [tool.nuitka_builder]` → `pyproject.toml [project]` →
auto-detect. `build_config.toml` wins because it's the more specific,
intentionally-named file.

---

## Why `data_dirs` was added in 1.1.0

The original per-file `data_files` list required a TOML edit every time
the user added a new asset (a new QSS theme, a new icon variant). With
Thrift adding new platform parsers and Prompt adding new theme assets
periodically, this became real maintenance friction.

`data_dirs` bundles a whole directory at build time, so dropping a new
file into `assets/` requires no config change. Cost: slightly larger
bundles when the dir contains things you don't intend to ship (e.g.
`.psd` source files). Mitigations:

- Keep "source" files (`.psd`, `.ai`, raw recordings) outside the
  asset dir.
- Or list files explicitly via `data_files` for tight control.
- `--audit` flags unbundled files that look like assets so you spot
  drift early.

---

## Why `--audit` is read-only

A `--fix` mode that auto-adds suggested files would silently bloat
bundles when the user committed a stray file by mistake. Read-only
audit gives the user the agency to decide. The cost is one extra
manual step; the benefit is no surprise bundle size jumps.

---

## Why `--init` does only safe-to-detect things

The `--init` command auto-fills only what can be inferred reliably:

- **Filled in:** name/version (from `pyproject.toml`), entry point (file
  existence check), GUI plugin (requirements.txt substring match),
  asset dirs (well-known names + asset extensions), icons (well-known
  paths), top-level docs (allowlist of names).
- **Left blank for user:** `include_packages`, `include_modules`,
  `nofollow_imports`, `extra_flags`. These encode project-specific
  knowledge that auto-detection cannot recover without false positives.
  Scanning the code for dynamic imports would be unreliable — better to
  let Nuitka report the missing module after a build attempt and then add
  it deliberately.

`--init` is the *starting* point, not the *finishing* point. The
generated comments in the TOML guide the user to fill in the rest, and
`--audit` afterwards catches what was missed.

---

## Why `--compiler=auto` is the default (v1.4.0)

The previous default (`mingw64`) optimized for "smallest auto-installable
compiler", which was correct on Python ≤3.12 — MinGW64 is small,
auto-downloads via Nuitka, and produces good binaries. But on Python
3.13+, Nuitka blocks `--mingw64`, leaving users with three choices:

1. Stay on Python 3.12 (works, but locks the version)
2. Install MSVC manually (best results, but ~3 GB and not obvious)
3. Use the runtime MSVC fallback added in 1.2.2 (works, but only if MSVC
   happens to be installed)

The `auto` mode in 1.4.0 captures the actual best-practice flow:

- If MSVC is already installed → use it (best results, latest Python).
- If not → ask once. User can opt into a ~3 GB install for the best path
  forward, or accept the MinGW64 + Python 3.12 fallback for a small
  footprint.
- Either way, the build always succeeds with no further user action.

The non-interactive path (no TTY, no `--yes`) silently chooses MinGW64 +
Python 3.12 — safer than prompting and hanging a CI job.

---



## Historical: why MinGW64 was the original Windows default

Superseded by `--compiler=auto` in 1.4.0. Kept here for context on why
MinGW64 was chosen originally.

MSVC bit two of the three predecessor projects:

1. **Heap exhaustion on huge generated C.** MSVC's LTCG (link-time code
   generation) chokes on multi-million-line auto-generated C from pymupdf,
   matplotlib, and similar. MinGW64's `ld.lld` handles it.
2. **Install pain.** Visual Studio Build Tools is multi-GB and requires
   selecting the right workload manually. MinGW64 is auto-downloaded by
   Nuitka itself on first build (with `--assume-yes-for-downloads`).

MSVC remains supported via `--compiler=msvc` for users who prefer it. The
script also auto-falls-back `lto=auto` to `lto=no` when MSVC is selected.

### Caveat: Nuitka 4.x blocks MinGW64 on Python 3.13+

Nuitka 4.x refuses `--mingw64` on Python 3.13 and later (CPython API
changes the toolchain doesn't track). v1.3.0 handles this with a
two-layer approach:

1. **Compiler-aware Python selection.** When MinGW64 is the chosen
   compiler, `get_best_python(prefer_below=(3,13))` picks the newest
   installed Python below 3.13. If the user has 3.10/3.11/3.12
   alongside 3.13, the venv uses 3.12 and MinGW64 works normally.
2. **Runtime MSVC fallback.** If only Python 3.13+ is installed, the
   venv has to use 3.13. At Nuitka-invocation time, the script then
   switches `--mingw64` → `--msvc=latest` (if VS Build Tools is
   detected via `vswhere`). If neither is usable, the script aborts
   with explicit `winget install` instructions for both fixes.

The compiler-aware selection is the primary mechanism — the runtime
fallback exists only for the rare case of a 3.13-only machine.

---

## Why artefacts go in the project directory, not Build_Scripts

`build_env/`, `build/`, `dist/`, `build.log` are project-scoped:

- Multiple projects can build in parallel without venv collisions.
- `dist/` is the natural download/distribute location.
- Cleanup (`git clean`, IDE-managed) treats them correctly.
- The `Build_Scripts` directory stays read-only and version-controlled.

---

## Onefile vs standalone — why onefile is the default

User explicitly requested it, and it's also the more common deliverable
target. Standalone (`--standalone`) remains a one-flag override for
debugging — a folder build is easier to inspect when troubleshooting
"why isn't this file being bundled".

---

## Why `tomllib` over PyYAML / JSON / INI

- Native to Python 3.11+ stdlib (no extra dependency on modern Pythons).
- More readable than JSON (comments, multiline strings).
- Less ambiguous than YAML (no indentation traps, no implicit type coercion).
- Better than INI for nested config (Nuitka flags have hierarchy).

Fallback to `tomli` for Python 3.10 is documented but assumed rare —
official policy is "use Python 3.11+ to run the builder".

---

## Win32 console hardening — why include it

Inherited from `build_Thrift.py`. Three concrete problems it solves on
Windows:

1. **Ctrl+C in cmd.exe** can leave a half-built venv if interrupted between
   pip's `download` and `install` phases. The custom handler calls
   `ExitProcess(130)` which terminates without running atexit cleanup.
2. **QuickEdit mode** lets a stray mouse click pause subprocess output
   indefinitely. The output looks frozen; users hit Ctrl+C; see (1).
3. **Codepage 437** (cmd.exe default) renders any non-ASCII as garbage.
   `SetConsoleOutputCP(65001)` switches to UTF-8.

Costs ~50 lines, harmless on non-Windows (early-returns), and prevents real
user-visible failures.

---

## Auto-install Python — when it triggers

Only when `find_pythons()` returns empty AND the user did not pass
`--python`. Uses winget / brew / apt / dnf / pacman as available. Asks for
sudo on Linux (will fail in non-interactive contexts; that's intentional —
CI should pre-install Python via `setup-python` action).

---

## What to NOT change without thinking

1. **The `nofollow_imports` mechanism.** Removing it will break PromptForge
   on MSVC. Keep it even if you switch defaults.
2. **The PyQt6 auto-uninstall** when PySide6 is the configured plugin.
   Nuitka picks up whichever it imports first; uninstalling PyQt6 from the
   build env is the only reliable fix.
3. **Project-local artefact paths.** Cross-project parallel builds depend
   on these being unique-per-project.
4. **The `--onefile` / `--standalone` mutual exclusion.** They produce
   different outputs and downstream tooling distinguishes them.

---

## Open items / future work

- **Code signing.** Nuitka outputs unsigned binaries. A `[codesign]` TOML
  section with platform-specific signing config (cert thumbprint on
  Windows, developer ID on macOS) would close this gap.
- **Cached MinGW64.** First MinGW64 download happens per-venv. A
  `~/.nuitka-mingw64/` shared cache (via env var) would speed up
  fresh-machine builds.
- **Wheels-first install.** `pip install` could use `--only-binary=:all:`
  to fail fast when a dep needs compilation. Currently silent and slow.
- **Output directory override.** `--output-dir` flag could redirect `dist/`
  somewhere else (useful for CI publishing).
