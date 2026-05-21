# About

**Project:** Build_Scripts — Common Nuitka Build System
**Script version:** 1.7.2
**Date:** 2026-05-17
**License:** Internal use

## What's new in 1.7.2

**Heavy-C builds now serialize to `--jobs=1`.** v1.7.1's RAM-derived job
count was too optimistic — on a 16 GB machine it picked `--jobs=2`, and
two large `cc1.exe` processes (pymupdf's `mupdf.c` and `pymupdf.c`)
compiling concurrently still exhausted memory (`cc1.exe: out of memory`).

The job-count formula is removed. Heavy-C builds default to `--jobs=1`:
the pathological translation unit then compiles alone with the whole
machine's memory. Compile time is not a concern for these rare,
release-only builds. An explicit `--jobs N` still overrides; LTO stays
RAM-gated (on only at ≥ 32 GB).

**Windows commit-limit pre-flight check.** `cc1.exe: out of memory` on
Windows is a *commit limit* failure — commit limit = physical RAM +
pagefile. `cc1` compiling `mupdf.c` at `-O3` can need 10–18 GB. The
script now reads the commit limit (`GlobalMemoryStatusEx`) and, for a
heavy-C build, **warns before the build starts** if it's below 40 GB,
with instructions to enlarge the Windows pagefile — so a too-small
pagefile is caught in seconds instead of after a ~2-hour compile.

The build banner now shows detected RAM and commit limit.

## What's new in 1.7.1

**RAM-aware auto-tuning for heavy-C builds.** A pymupdf build on
MinGW64 failed with `cc1.exe: out of memory` — GCC ran out of RAM
compiling the giant `mupdf` translation unit, because `--jobs=4` ran
four C-compiler processes at once (one of them the multi-GB `mupdf`
unit) and LTO inflated per-process memory further.

The build script now detects total system RAM (stdlib only — Windows
`GlobalMemoryStatusEx`, Linux/macOS `sysconf`) and, for heavy-C builds,
auto-tunes:

- **Parallel jobs** — budgeted at ~4 GB per job against 70% of total
  RAM, capped at the CPU count. Examples on a 4-core box: 8 GB → 1 job,
  16 GB → 2, 32 GB → 4.
- **LTO** — kept on only when RAM clearly affords its memory-hungry link
  stage (≥ 32 GB); off below that. For a GUI app the runtime difference
  is negligible, so this trades nothing meaningful for a build that
  finishes.

Explicit choices always win: `--jobs N` is honoured as-is, and an
explicit `lto = "yes"/"no"` in `build_config.toml` overrides the
auto-decision. Normal (non-heavy-C) builds are unaffected — full CPU
count, config LTO.

The BUILD FAILED footer now distinguishes a `cc1.exe: out of memory`
(too many jobs for the RAM) from early header errors (corrupt GCC
download).

## What's new in 1.7.0

**Heavy-C handling corrected again — auto-nofollow removed.** v1.6.0's
`--nofollow-import-to` approach was wrong: a nofollow'd module is
*excluded* from a standalone build, so the resulting `.exe` crashed at
startup with an ImportError (the app simply never opened). Nuitka's own
documentation confirms nofollow causes a runtime ImportError in
standalone mode.

The corrected behaviour:

- **No auto-nofollow.** The script never auto-adds `--nofollow-import-to`.
  Heavy-C modules are *compiled and bundled normally*.
- **Heavy-C projects route to MinGW64.** `--compiler=auto` now selects
  MinGW64 when a heavy-C module (pymupdf) is detected. GCC/MinGW64
  compiles the ~2.2M-line `mupdf.c` translation unit where MSVC's
  pass-2 heap fails (`C1002`). The build is slow (~2 hrs for a pymupdf
  project) but produces a genuinely standalone exe.
- **Heavy-C + MSVC** now emits a hard warning — that combination cannot
  succeed.
- `--no-auto-nofollow` flag removed (obsolete).
- `HEAVY_C_MODULES` is now a simple set of package names.
- The "MinGW64 cannot work — Nuitka 4.1 can't drive GCC 15" claim from
  v1.6.0 was **wrong**. The GCC-15 failure was a *corrupt download*.
  Clearing Nuitka's GCC cache and re-downloading produced a clean GCC
  that compiled the project successfully. The BUILD FAILED footer now
  tells the user to clear `%LOCALAPPDATA%\Nuitka\Nuitka\Cache\downloads\gcc`
  if a MinGW64 build fails early with header errors.

### Build-time expectation

A pymupdf project on MinGW64 takes roughly 2–2.5 hours to build,
because GCC must compile the multi-million-line `mupdf` translation
unit. This is inherent to compiling pymupdf with Nuitka. Release builds
are infrequent, so this is accepted; `lto = "no"` in `build_config.toml`
trims some time if needed.

## What's new in 1.6.0

**Heavy-module strategy redesigned.** v1.5.0's approach — detect heavy
modules and switch the compiler to MinGW64 — was based on a wrong
assumption: that MinGW64 is a reliable fallback. It is not. Nuitka 4.1
auto-downloads the newest winlibs GCC (currently 15.2.0) and **cannot
drive GCC 15** — even MinGW's own CRT headers fail to parse
(`corecrt.h: expected ';' before 'typedef'`). So routing heavy-module
builds to MinGW64 sent them into a broken compiler.

The redesign:

- **`HEAVY_MODULES` (set) → `HEAVY_C_MODULES` (dict).** Maps a package
  to the submodule that ships huge compilable C *source*. Currently just
  `pymupdf` / `fitz` → `pymupdf.mupdf`.
- **Corrected the criterion.** v1.5.0's list included opencv-python,
  tensorflow, torch, scipy, pandas, lxml, etc. — but those ship
  *prebuilt* `.pyd`/`.so` wheels. Nuitka copies them as-is and never
  recompiles them, so they never caused `C1002`. Only packages shipping
  compilable C source (pymupdf) belong here. The list is now accurate
  rather than conservative.
- **New action: auto-inject `--nofollow-import-to`.** When a heavy-C
  module is detected, the script adds `--nofollow-import-to=<submodule>`
  so Nuitka does not recompile the giant translation unit. The prebuilt
  `.pyd` is bundled instead. **MSVC stays selected** — no compiler
  switch, no MinGW64, no GCC-15 problem. This is the workaround
  PromptForge already used, now automatic for every project.
- **`--no-auto-nofollow`** disables the injection (Nuitka will then
  recompile the module; MSVC may fail with `C1002`).
- **Compiler resolution simplified.** `--compiler=auto` is again just
  "MSVC if available, else MinGW64". Heavy modules no longer influence
  it. `--force-msvc` remains but is rarely needed now.
- **Detection deduplicates against `nofollow_imports`.** A target the
  user already declared in `build_config.toml` is not injected twice.
- LTO `auto` no longer keys on heavy modules (the giant TU is skipped
  outright now); it stays off only on Windows+MSVC, as before.

### MinGW64 status

`--compiler=mingw64` still exists but, with Nuitka 4.1, will fail if
Nuitka downloads GCC 15.x. To use MinGW64, put a GCC 13/14 toolchain on
PATH so Nuitka uses that instead of downloading 15.x. For most Windows
projects MSVC + auto-nofollow is now the recommended path.

## What's new in 1.5.2

- **LTO auto-disabled for heavy-module builds.** Link-time optimization
  holds the whole program in memory during the final link; with heavy
  modules (pymupdf, opencv, ...) this exhausts the linker on *both*
  toolchains — MSVC LTCG (`C1060` / `LNK1102`) and MinGW64 `ld` alike.
  `lto="auto"` now resolves to `no` whenever heavy modules are detected,
  on any OS/compiler (previously only Windows+MSVC). An explicit
  `lto="yes"` is still honoured but now warns when heavy modules are
  present. This fixes MinGW64 builds that compiled for ~20 minutes and
  then died at the link stage.
- **Parallel C jobs capped at 2 for heavy-module builds.** Each huge
  translation unit can use 2-4 GB of RAM during compilation; 4+ in
  parallel can OOM the machine mid-compile. When `--jobs` is not given
  explicitly, heavy-module builds now cap at 2. Pass `--jobs N` to
  override (e.g. on a high-RAM machine).
- **Nuitka output now captured in `build.log`.** The compile step ran
  Nuitka as a subprocess whose stdout/stderr went only to the console;
  `build.log` recorded the command but nothing about *why* a build
  failed. Output is now streamed line-by-line to both the console and
  `build.log`, so post-mortem diagnosis no longer needs the console
  scrollback.

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
