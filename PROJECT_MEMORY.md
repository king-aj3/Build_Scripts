# Project Memory

Persistent design decisions and rationale. Read this before making changes
that touch the architecture.

---

## build_all.py — cross-OS orchestrator (v1.0.0)

**Why a separate script, not a flag on build.py.** Nuitka cannot
cross-compile (it targets only the host OS), so multi-OS output is
fundamentally an *orchestration* concern — run the one build brain on N
hosts and gather results — not a *build* concern. Keeping it in
`build_all.py` leaves `build.py` exactly as-is (single responsibility:
build this project on this machine) and means the orchestrator can be
absent on machines that only ever build locally.

**Why SSH as the single remote transport.** Windows 10+, macOS, and Linux
all ship OpenSSH, so one mechanism reaches every host — no WinRM, no
per-OS agent. `transport = "local"` covers the current machine with zero
setup, which is the common single-host starting point.

**Why git pull on the remote before building.** Repos are the source of
truth on GitHub; each host clones them. Pulling before the build keeps the
three OS binaries built from the same commit without manual syncing.
`--no-pull` (or `[git] pull = false`) opts out for dirty-tree debugging.

**Why collect into `dist/<os>-<arch>/`.** `build.py` always writes
`dist/<name>`; three hosts would overwrite each other in a shared `dist/`.
Per-OS subfolders keep them separate and make the deliverable set obvious.
The collector treats every *configured host label* as reserved, so a
re-run never sweeps one host's output folder into another's — this is what
makes repeated runs idempotent.

**Why auto-generate build_hosts.toml (v1.1.0).** Requiring a hand-copied
template per project is exactly the maintenance friction the single-script
design exists to avoid. So the host map is generated like build.py's
build_config.toml: written on first run (current OS enabled), with explicit
--init / --force. --force preserves user-entered SSH host fields (ssh, repo,
build_py, python, key, port, enabled) — only the local-OS section and
build_script path are refreshed — because those remote details are
environment knowledge auto-detection cannot recover, the same reason
build.py --force preserves curated data_dirs.

**Why N hosts, not exactly 3.** A user may have only one OS today. The
orchestrator builds whatever is `enabled`; with one host it just builds
that OS, and additional hosts are enabled later with no code change. macOS
is the one OS that cannot be virtualised off Apple hardware — documented
escape is a GitHub Actions macOS runner (USER_GUIDE §10), since the repos
are already on GitHub.

**What must not change:** `build.py` stays the only thing that knows how to
build; `build_all.py` only schedules, transports, and collects. Do not
duplicate Nuitka/venv/compiler logic into the orchestrator — pass flags
through after `--` instead.

---

## v1.8.2 — RAM-aware default jobs (zstd onefile OOM fix)

A My_LLM onefile build failed at the final stage with
`zstandard ... ZstdError: not enough memory`. The C compile and link had
succeeded; only the zstd payload compression OOM'd. Root cause: the default
`jobs = multiprocessing.cpu_count()` returned a high core count, and with LTO
enabled that many concurrent link jobs left no RAM for zstd to allocate its
compression context. Verified by a manual run with `--jobs=4 --lto=no`, which
compressed fine (16.5 MB → 5.7 MB, 34.5%).

> **Correction (2026-06-13):** an earlier version of this note attributed the
> failure to "a Threadripper 3990X (64C/128T, 32 GB)" on Linux. That machine
> attribution was **wrong** — the OOM occurred on a **different Windows
> box**, not this Linux 3990X. The specific core count, RAM figure, and
> LTO-on-Linux framing in the original were unreliable and have been removed.
> What remains verified: the symptom (zstd OOM at the payload stage), the
> cause (default jobs from `cpu_count()` starving zstd of RAM), and the fix
> below. The 3990X itself has 64 GB and is unrelated to this incident.

Fix: new `_safe_jobs()` / `_total_ram_gb()` helpers cap the *default* job
count to `min(cpu, (RAM_GB - 4) / 1.5, 32)`. Rationale for the formula: LTO
links run ~1.5 GB each, keep 4 GB headroom, never exceed 32 (diminishing
returns + safety). Explicit `--jobs N` bypasses the cap entirely. LTO logic
and all names left unchanged. This is the general-build path only; the old
heavy-C `--jobs=1` serialize machinery was already removed in 1.8.x (heavy
modules are handled via `--nofollow-import-to`/bytecode).

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

- `PromptForge` uses `pymupdf`, which ships a ~2.2M-line generated C
  file (`mupdf.c`). MSVC cannot compile it; the build must use MinGW64.
  The build script detects pymupdf and routes to MinGW64 automatically
  (see the heavy-C history below) — but the *fact* that the project
  needs that handling is project knowledge worth recording.
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

## Why the build gates on broken entry/assets but only warns on drift (v1.11.0)

`--audit` is opt-in, so a forgotten run can still ship a broken binary. v1.11.0
adds an automatic **pre-build gate** (`preflight_gate`) that runs right before
Nuitka and *blocks* — but only on the two failures that make the artifact
genuinely worthless:

- **Missing entry point** — Nuitka can't compile a file that isn't there; the
  build would fail anyway, just 30 seconds of env-setup later.
- **Declared `data_files`/`data_dirs` not on disk** — the compile *succeeds* but
  silently ships a binary missing an asset. A restructure that moves a folder is
  the classic cause; far better to stop than to discover it at runtime.

Everything else `audit()` flags stays a **warning, not a block**: version drift
is cosmetic metadata, and unbundled-asset *suggestions* are guesses (the
read-only-audit reasoning above — don't act on them automatically). Blocking on
those would turn a nuisance into a wall. `--force` bypasses the gate for the
rare case you know better. This deliberately does NOT catch compile failures —
it's a static config/bundle check, not a build. The matching scheduler
(`build_projects.py`) gets the gate for free because every job is a `build.py`
invocation.

Why the gate reuses `--force` (already an `--init` flag) rather than a new
`--no-gate`: `--force` already means "I know what I'm doing, override the
guardrail," and `--init` and a build are different invocations, so there's no
collision. One fewer flag to remember.

---

## Why repo-freshness is report-only on direct `build.py` (v1.11.0)

`build.py` now does a read-only `git fetch` and reports `N commits behind
origin/<branch>` before building — but it never pulls. `build_all.py` already
owns pulling (it's the only layer that can pull the *remote* Windows/macOS
trees too), and during hand-testing an auto-pull on the local tree would
silently change what you're compiling out from under you. A fetch updates only
remote-tracking refs (not the working tree), is bounded + non-fatal (offline or
auth-less just skips), so it informs without surprising. Pulling stays a
deliberate act, not a build side effect.

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

## Heavy-C modules: the full design history (v1.5.0 → v1.7.0)

This section records a design that was wrong **twice** before landing.
The wrong versions are kept deliberately — the mistakes are instructive
and must not be reinstated.

### The problem

pymupdf ships `mupdf.c`, a SWIG-generated ~2.2M-line file. Nuitka
recompiles it into one translation unit. MSVC's pass-2 code generator
runs out of heap on it (`C1002`). MinGW64/GCC compiles it fine (it's a
64-bit toolchain without that limit) — just slowly.

### v1.5.0 (WRONG) — switch to MinGW64, overbroad list

Detected "heavy" modules and switched the compiler to MinGW64. Two
mistakes: (1) the list included opencv/torch/tensorflow/scipy/pandas —
but those ship *prebuilt* wheels Nuitka never recompiles, so they never
caused `C1002`; (2) the MinGW64 build then failed, and that failure was
misread (see v1.6.0).

### v1.6.0 (WRONG) — auto-nofollow + stay on MSVC

The v1.5.0 MinGW64 build failed with `corecrt.h` errors. This was
**misdiagnosed** as "Nuitka 4.1 cannot drive GCC 15.x". Based on that
false conclusion, v1.6.0 abandoned MinGW64 and instead auto-injected
`--nofollow-import-to=pymupdf.mupdf` to keep MSVC viable.

That was also wrong. `--nofollow-import-to` *excludes* a module from the
build. Nuitka's own docs state a nofollow'd module raises ImportError at
runtime in standalone mode. The v1.6.0 build "succeeded" but the exe
crashed silently on startup (console disabled → no visible error) — the
user reported "app does not open" and "says to install pymupdf".

### v1.7.0 (current) — route to MinGW64, compile normally

A cheap diagnostic settled it: deleting Nuitka's GCC download cache and
rebuilding produced a **clean GCC that compiled the whole project
successfully**. So the v1.5.0 `corecrt.h` failure was a **corrupt /
truncated GCC download** — not a Nuitka/GCC-15 incompatibility. The
v1.6.0 misdiagnosis invalidated the entire v1.6.0 redesign.

The correct, final design:

- `HEAVY_C_MODULES` is a **set** of package names (`pymupdf`, `fitz`).
- When detected, `--compiler=auto` **routes the build to MinGW64**.
  GCC compiles the giant translation unit; the module is compiled and
  bundled normally; the standalone exe works.
- **No `--nofollow-import-to` is ever auto-added.** Excluding a module
  needed at runtime breaks a standalone build, full stop.
- Heavy-C + MSVC is a guaranteed failure → the script warns loudly.
- The membership criterion: only packages that ship *compilable C
  source* (pymupdf). Prebuilt-wheel packages never belong here.

This is essentially v1.5.0's instinct (route to MinGW64) — which was
right all along. It was derailed for two versions by one corrupt
download.

### Cost accepted

A pymupdf build on MinGW64 takes ~2–2.5 hours (GCC compiling 2.2M lines).
This is inherent. Release builds are infrequent, so it is accepted.
`lto = "no"` trims some time. There is no faster way to compile pymupdf
with Nuitka; the only fundamentally faster alternative considered was a
PyInstaller backend (rejected by the user in favour of staying on
Nuitka — see decision log below).

### Lessons that must not be relitigated

1. **Never auto-add `--nofollow-import-to` for a module the program
   imports.** It is not "skip compilation" — it is "remove from bundle".
2. **A failed MinGW64 build is not proof MinGW64 is unusable.** Check
   for a corrupt GCC download first: delete
   `%LOCALAPPDATA%\Nuitka\Nuitka\Cache\downloads\gcc` and retry.
3. **Verify a tool's flag semantics before building a feature on them.**
   The v1.6.0 nofollow design shipped on an assumption; the docs said
   the opposite.

### v1.7.1 / v1.7.2 — job count and the Windows commit limit

The first v1.7.0 pymupdf build on MinGW64 reached the C compiler (good —
compiler routing was right) but died with `cc1.exe: out of memory` on
`module.pymupdf.mupdf.o`.

**v1.7.1** added RAM detection and derived a job count (~4 GB budgeted
per job, 70% of RAM). On a 16 GB machine that produced `--jobs=2`. It
failed again: `mupdf.c` and `pymupdf.c` — two large units — compiled
concurrently and together exhausted memory. The 4 GB/job budget was far
too low for `mupdf.c`, whose `cc1.exe` needs well into double-digit GB.

**v1.7.2** removed the formula. Heavy-C builds now default to `--jobs=1`:
the pathological unit compiles alone, so peak memory is one `cc1`, not
several. Compile time is not a concern for these rare release builds, so
serializing costs nothing that matters. Explicit `--jobs N` still
overrides.

The deeper point: `cc1.exe: out of memory` on Windows is a **commit
limit** failure, not a physical-RAM failure. The commit limit is
physical RAM + pagefile. A 16 GB machine with a default pagefile has a
commit limit far below what `mupdf.c` needs. v1.7.2 reads the commit
limit (`GlobalMemoryStatusEx.ullTotalPageFile`) and warns *before* the
~2-hour build if it is under `HEAVY_C_MIN_COMMIT_GB` (40 GB), with
instructions to enlarge the pagefile. The fix for the user is an OS
setting (bigger pagefile), not a build flag — the script's job is to
detect and surface that early rather than fail after two hours.

Lesson: do not try to be clever deriving parallelism for a build with a
single pathological translation unit. Serialize, and make the real
constraint (commit limit) visible.

### v1.7.3 — abandon compilation, ship as bytecode

v1.7.2's `--jobs=1` + commit-limit guardrails were proven insufficient.
Builds on the user's 16 GB machine OOM'd; user moved to a 32 GB machine
with a 32 GB pagefile (commit limit 64 GB, jobs=1, lto=no — ample
headroom by every model). The build *still* OOM'd, this time after only
6 minutes, with the exact same `allocating 10937968 bytes` failure that
had appeared on every previous attempt.

The byte count being identical across all machines and configurations is
diagnostic: `cc1.exe` is hitting a per-process address-space ceiling on
Windows, not a system-tuning limit. No amount of RAM or pagefile will
let Nuitka's bundled GCC 15.2 `cc1.exe` compile `pymupdf.mupdf.c` on
this user's setup.

The fight against the C compiler is over. v1.7.3 stops generating the C
in the first place. The script appends
`--noinclude-custom-mode=<target>:bytecode` to the Nuitka command for
heavy-C modules. Nuitka then ships `pymupdf.mupdf` as plain `.pyc`
bytecode; CPython interprets it at runtime; the prebuilt `.pyd` is
bundled by Nuitka's dll-files plugin as usual. No giant C is generated,
MSVC compiles the rest of the build at full speed, and a heavy-C build
finishes in ~15–30 minutes instead of failing after 2 hours.

Consequences:

- `HEAVY_C_MODULES` is a dict again `{detect_name: bytecode_target}`.
- `_resolve_compiler_auto` has no special heavy-C tier — MSVC is fine.
- The `_tune_heavy_c_build` machinery (RAM-aware jobs, commit-limit
  pre-flight, 10-second pause) is no longer invoked for heavy-C builds.

### v1.8.0 — bytecode approach confirmed, dead code removed

The v1.7.3 experiment produced a working **202 MB onefile in
~26 minutes** on a 32 GB Windows machine (MinGW64, `--lto=yes`,
`--jobs=16`); the application opened normally and pymupdf loaded at
runtime. The Nuitka maintainer's "submodules-in-packages" warning did
not manifest for pymupdf's case.

v1.8.0 promotes the approach to default and removes 101 lines of dead
code: `_tune_heavy_c_build()`, `get_total_ram_gb()`,
`get_commit_limit_gb()`, `_win_memstatus()`, and `HEAVY_C_MIN_COMMIT_GB`.
These were workarounds for compiling `mupdf.c` itself, which the
bytecode path sidesteps entirely. The "experiment" framing is removed
from banners, audit output, and the BUILD FAILED footer.

The PyInstaller backend remains a documented fallback (in open items)
if a future package proves resistant to bytecode mode, but is no longer
the imminent next step it was during the v1.7.x failures.

### v1.8.1 — package-data registry for silent-empty-output gotcha

After v1.8.0 locked in the bytecode-mode build, Thrift_Reseller surfaced
a different class of bundling bug: the exe ran without raising any
exception (`cmd.exe` with console mode enabled showed no traceback), but
non-Avery label types silently produced no barcode. PyCharm rendered all
label types correctly. Avery worked in the exe; non-Avery didn't.

Initial hypothesis (PIL plugins not bundled) was wrong — pointed out
correctly: QR codes use PIL too and work everywhere, so PIL was bundled
fine. Also: the project uses `python-barcode` directly, not
`reportlab.graphics.barcode`.

Real cause: **`python-barcode` ships `.ttf` font files inside its package
directory** (`barcode/fonts/`). Nuitka compiles the Python code fine but
does **not** auto-bundle non-Python files inside a package. The library
ran in the exe, didn't error, just returned empty/incomplete renderings
for code paths that needed the fonts.

Fix: `--include-package-data=barcode` forces Nuitka to bundle every
non-Python file inside the `barcode/` package. The user requested this
go into the common script rather than the project's `build_config.toml`,
parallel to how heavy-C is handled — so every project gets the fix
automatically.

v1.8.1 adds:

- `PACKAGE_DATA_MODULES` dict (alongside `HEAVY_C_MODULES`) mapping
  detect-name → Nuitka output name. Initial entries: `barcode`,
  `python_barcode` → `barcode`; `pil`, `pillow` → `PIL`; `qrcode` →
  `qrcode`.
- `detect_package_data_modules()` mirroring the heavy-C detector.
- A new shared helper `_scan_project_for_packages()` that both detectors
  use, so adding a third category later is cheap.
- Auto-injection of `--include-package-data=<name>` into the Nuitka
  command, with dedup on the output name (e.g. `pillow` + `pil` both
  collapse to a single `--include-package-data=PIL`).
- Banner, `--info`, and `--audit` show what was detected and what's
  being injected.

**Membership criterion (important):** only small-data, non-Nuitka-plugin-
covered packages belong here. matplotlib, scipy, numpy etc. have
dedicated Nuitka plugins that already handle their data correctly —
blindly `--include-package-data` on them inflates the bundle by tens to
hundreds of MB.

**Diagnostic of this class of bug:** if the exe runs, console mode shows
no traceback, but a feature produces empty/broken output that works in
PyCharm — suspect unbundled package data files.

### Why Nuitka output is streamed into build.log (v1.5.2, kept)

The compile step originally used `subprocess.run(cmd)` with inherited
stdio, so Nuitka's output — including the actual compiler error — went
only to the console; `build.log` jumped straight to "BUILD FAILED" with
no diagnostics. v1.5.2 switched to `Popen` with `stdout=PIPE,
stderr=STDOUT`, streaming each line through `say()` (which writes to both
console and log). This is what finally made the real `corecrt.h` /
corrupt-download cause visible. Retained. Cost: `\r` progress bars render
slightly oddly in the log.

---

## Why `--compiler=auto` is the default (v1.4.0, refined through v1.7.0)

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

## include_qt_plugins: "all" broke Linux; plain "sensible" is the cross-OS answer (v1.8.4/1.8.6/1.8.7)

The shared build_config.toml is used by ALL OS hosts (one file, git-synced),
so include_qt_plugins must be correct for every OS at once — "all" is not, it
breaks Linux. v1.8.6 auto-appended "printsupport" when QtPrintSupport was
imported, to keep the print plugin "all" used to provide. v1.8.7 REVERSED that:
Qt 6.11 removed the standalone "printsupport" plugin family, so an explicit name
is a FATAL there ("no Qt plugin family 'printsupport'") — and Nuitka's "sensible"
already includes printsupport gated on hasPluginFamily() (PySidePyQtPlugin.py
_getSensiblePlugins), so it is bundled wherever Qt ships it and skipped on 6.11+.
Net: plain "sensible" is correct on every OS and Qt version; a stale ",printsupport"
from old configs is stripped at build time and self-healed on --force/--reset.
Original detail below.



`--init` used to hardcode `include_qt_plugins = "all"`. That passed
`--include-qt-plugins=all` to Nuitka, which bundles the entire Qt plugin set
including the `qml/` tree. Some qml plugins (e.g. `Qt/labs/assetdownloader`)
ship pre-built `.cpp.o` object files. Nuitka's **Linux** standalone step runs
`patchelf --force-rpath --set-rpath` on every ELF file in the dist tree; an
ET_REL object file rejects rpath edits, so the build dies at the very end
with `patchelf: wrong ELF type` — after a full compile+link, the most
expensive place to fail. Windows has no patchelf/rpath step, so the identical
config built there fine. This bit every PySide6 project `--init` onboarded,
on Linux only.

Fix: default to `"sensible"` (Nuitka's recommended set: platforms,
imageformats, iconengines, styles, platformthemes, …) which excludes qml.
Widgets/printing apps need nothing from qml. A genuine QML app sets
`"sensible,qml"` or `"all"` by hand, and `--force --init` preserves it (added
to the curated-value preserve set alongside entry/data_dirs/data_files).
build_all.py is unaffected — it never runs `--init`, so it never rewrites
build_config.toml; committing the corrected value propagates to all OS hosts
via git pull.

## --force preserves, --reset regenerates (v1.8.5)

`--init --force` deliberately *merges*: it keeps curated keys (entry,
data_dirs, data_files, include_qt_plugins) from the existing file so a regen
doesn't destroy hand-tuning. The cost is that a wrong pre-existing value (the
classic: include_qt_plugins="all" from a pre-1.8.4 init) also survives a
--force — --force can't distinguish "user chose this" from "stale default".
`--reset` is the escape hatch: it ignores the existing file completely and
rebuilds from detection + current defaults, so stale values are cleared. It
necessarily drops hand-added data_files/data_dirs and resets entry to the
autodetected value, so it warns. Keep BOTH: --force for safe refresh, --reset
for a clean slate. build_all.py is unaffected (it never runs --init/--reset).

## What to NOT change without thinking

1. **`nofollow_imports` is exclusion, not "skip compilation".** A
   nofollow'd module is removed from the build; importing it in a
   standalone exe raises ImportError at runtime. Never auto-add it for a
   module the program uses. (This is the v1.6.0 mistake — see history.)
2. **The `HEAVY_C_MODULES` membership criterion.** Only packages that
   ship compilable C *source* (pymupdf) belong. Do **not** add
   prebuilt-wheel packages (opencv, torch, tensorflow, scipy, pandas,
   ...) — Nuitka never recompiles prebuilt extension modules, so they
   never cause `C1002`. A wrong entry just forces a needlessly slow
   MinGW64 build.
3. **Heavy-C → MinGW64 routing.** Heavy-C projects must build on
   MinGW64; MSVC cannot compile the giant translation unit. Do not route
   them to MSVC, and do not try to "skip" the module to keep MSVC.
4. **The PyQt6 auto-uninstall** when PySide6 is the configured plugin.
   Nuitka picks up whichever it imports first; uninstalling PyQt6 from
   the build env is the only reliable fix.
5. **Project-local artefact paths.** Cross-project parallel builds depend
   on these being unique-per-project.
6. **The `--onefile` / `--standalone` mutual exclusion.** They produce
   different outputs and downstream tooling distinguishes them.
7. **Streaming Nuitka output to `build.log`.** Without it, failures are
   undiagnosable from the log.

---

## sync_projects.py: why safe-by-default + FF-pull-only (v1.0.0)

A "sync all my repos" tool is the most dangerous in the set — it mutates working
trees across ~14 repos, so a bad default can destroy uncommitted work in a
project you weren't even thinking about. Design decisions and their reasons:

- **No verb = read-only.** With no flags it only fetches and prints a status
  table. An accidental or fat-fingered `sync_projects.py` literally cannot
  mutate a tree. This is also ~all the day-to-day value (the repos are usually
  clean and just need a status check).
- **`--pull` is fast-forward-ONLY.** `git pull --ff-only` refuses anything that
  isn't a clean fast-forward, so it can never create a merge commit or rewrite
  history. "Update local from GitHub" *is* a fast-forward when you haven't
  diverged — which is the normal case.
- **Never touch a dirty tree.** A behind+dirty repo is refused (commit/stash in
  PyCharm first). Untracked files do NOT count as dirty — a fast-forward is safe
  over them — but staged/unstaged tracked changes do.
- **ahead/diverged/detached/shallow → skip with a reason.** These need human
  judgement (push, merge, rebase, unshallow); the tool reports, never guesses.
- **No `--force`/reset/stash/commit surface exists in `gitutil.py` at all.** The
  data-loss vectors from the design survey are unreachable from any caller, not
  merely avoided by convention. The only mutating function is `pull_ff_only`.
- **v1 scope = status + ff-pull. Push/commit/non-FF-merge are PARKED (decided
  2026-06-19), not just deferred.** The owner confirmed pull/ff-only is all
  that's wanted — the dangerous verbs aren't worth the marginal benefit, so
  status + ff-pull is the intended FINAL shape unless that changes. Validated v1
  against /tmp scratch repos covering behind / behind+dirty / diverged.
- **Shared, not duplicated.** `gitutil.py` (git layer) and `projutil.py`
  (selection + TOML CRUD, extracted from build_projects.py with byte-identical
  behavior) are the chosen architecture so build.py/build_all.py can later wire
  into the same audited git code. Design from a 7-agent analysis; the code
  passed a 3-lens adversarial review (safety / correctness / integration).

---

## Open items / future work

- **[PARTIAL] sync_projects.py — wire build scripts into gitutil + add write
  verbs.** (1a) **[DONE 2026-06-19]** `build.py report_repo_freshness` now calls
  `gitutil.py` (byte-identical output; graceful skip if gitutil absent on a
  remote host). (1b) **[WON'T — by decision]** `build_all.py`'s local pull stays
  inline: it's one `git pull --ff-only` inside the `run()`/dry-run wrapper, and
  routing it through gitutil would add GCM hardening + change logging/error
  handling — a behavior change for a 1-line gain on a fragile file. Revisit only
  if build_all grows more git needs. (2) **[PARKED — by decision 2026-06-19]**
  the gated write verbs (`--push`/`--commit`/`--merge`) — the owner confirmed
  pull/ff-only is the intended final shape; don't build these unless asked. If
  un-parked: `--push` (clean+ahead, re-confirm branch, never `--force`,
  report+leave-untouched on auth failure), `--commit MSG` (tracked-only, message
  required), `--merge` (explicit per-repo confirm, never auto-resolve diverged);
  build each against a dirty/ahead/diverged scratch repo, LFS/shallow detect-and-
  warn, submodules not auto-updated.
- **[DONE 2026-06-13] Three-OS release via `build_all.py` — all 3 projects ×
  3 OSes building green.** macOS via github transport (arm64), Windows via SSH
  to the guest VM (native MSVC), Linux local + auto tar.gz. Verified builds:
  - **WealthBuilder:** macOS ✓, Windows .exe 48 MB / MSVC 14.5 / ~19m ✓.
  - **ajj3-brain:** macOS ✓ (~4m), Windows .exe 16 MB / ~3m ✓.
  - **Thrift_Reseller:** macOS ✓, Windows .exe 197.7 MB / ~31m ✓ — pymupdf
    bytecode mode (`--noinclude-custom-mode=pymupdf.mupdf:bytecode`) and the
    python-barcode DejaVuSansMono.ttf bundling both carried to native Windows
    MSVC cleanly. This was the key validation.
  Windows host setup that made it work is captured in the cross-os-build skill
  (sshd via wuauserv-reenable or winget, blank-password fix, admin
  authorized_keys + icacls, scp copy-back, GCM-pull caveat).
  Follow-ups (cosmetic, non-blocking):
  - Thrift has BOTH `build_config.toml` and `pyproject.toml
    [tool.nuitka_builder]`; build_config wins and shadows pyproject. Pick one.
  - ajj3-brain + Thrift fire an icon-only `assets/` preflight warning each
    build (false positive — dir is build-time icons only; Thrift's
    user_manual.pdf is correctly bundled). Optionally silence per-project.
  - Remote `git pull` over ssh fails on GCM (`--no-pull` to skip); VM builds
    its current working tree. Set up a PAT in the VM's git store if unattended
    private-repo pulls are wanted.
- **[DONE 2026-06-19] Parallel build-matrix mode (project × OS aware) —
  `build_projects.py` v1.0.0.** A scheduler layer above `build_all.py`: each
  `(project × OS)` job runs as `build_all.py <project> --only <host>`, so the
  audit gate, git pull, and per-OS artifact paths are inherited unchanged. Jobs
  are scheduled by OS lane (one thread-pool per host, sized to that host's cap)
  — exactly the "dispatch all macOS, run Linux with a small pool, serialize
  Windows" shape this item called for:
  - **macOS: fully parallel** (`--mac-jobs`, default #projects). Each GitHub
    runner is a separate cloud VM; local cost is just `gh run watch` polling,
    so N projects' macOS builds finish in ~one build's time.
  - **Linux: RAM-bounded** (`--linux-jobs`, default 2). One Threadripper; LTO
    multiplies peak RAM, so concurrency is capped + tunable, not core-bound.
  - **Windows: `--windows-jobs` (default 1).** One shared VM; default stays 1 for
    safety, but **2 concurrent compiles were MEASURED fine at 32GB** (2026-06-20:
    WB+Thrift together, peak ~12GB combined, ~20GB free) -- `--windows-jobs 2`
    runs two lanes for a ~30% Windows-lane speedup. No 2nd VM needed.
  `--sequential` runs one job at a time (streams live); `--parallel` (default)
  captures each to `build-logs/<project>-<host>.log`. The cross-process *lock*
  this item worried about turned out unnecessary: a single scheduler process
  owns all lanes, so the Windows cap-1 thread-pool IS the serialization. (Two
  separate `build_projects.py` runs launched at once could still collide on the
  VM — documented, not guarded.)
- **PyInstaller backend (fallback).** v1.8.0's bytecode-mode handling
  for pymupdf is confirmed working, so this is no longer the imminent
  next step. It remains the documented escape if a future package proves
  resistant to bytecode mode, or if Nuitka's bytecode-in-packages support
  ever regresses. Design: `backend = "pyinstaller"` per-project TOML key,
  same schema and PyCharm tool; ~10-min builds; weaker source protection
  (bytecode decompilable). Add only when needed.
- **Corrupt-GCC self-heal.** A MinGW64 build that fails early with C
  header errors is usually a corrupt Nuitka GCC download. The script
  currently tells the user to clear the cache manually; it could detect
  the signature and clear+retry automatically once.
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

## Why the env's Nuitka gets patched for EDR retries (v1.10.0)

CylancePROTECT on the Windows build host holds every freshly linked unsigned
exe for ~60+ s while it scans. Nuitka's resource-embedding step (icon +
version info, the LAST build stage) retries only 5 × 1 s
(`decoratorRetries` in `nuitka/utils/Utils.py`), so onefile builds failed
with "Failed to add resources … the result is unusable" after an otherwise
perfect compile — every time, because each relink produces a new file hash
that gets held again. Re-running the build can never fix it; only a longer
in-place retry window can (the verified Thrift_Reseller build needed 34
attempts ≈ 68 s).

Patching the env's installed Nuitka copy (idempotent string replace during
`_install_packages`, Windows only) was chosen over: an env var (Nuitka has
none for this), forking/pinning Nuitka (heavy), or hand-patching each
build_env (wiped by `--clean-env` / upgrades — that was the stopgap on
2026-06-10). If a future Nuitka changes the `attempts=5, sleep_time=1`
layout, the script warns ("layout changed") instead of failing, and the
patch needs a refresh.

## macOS via GitHub Actions: arm64 only, dispatched by build_all (v1.2.0 orch)

There is no legal macOS VM off Apple hardware, so the macOS host in
`build_hosts.toml` is either a real Mac over SSH or `transport = "github"`.
The github transport dispatches `.github/workflows/macos-build.yml` (in the
PROJECT repo) via the `gh` CLI, waits with `gh run watch --exit-status`, and
downloads the artifact into `dist/macos-arm64/`. Design decisions:

- **Intel (x86_64) macOS is deliberately not built.** The earlier two-job
  matrix (macos-13 Intel + macos-latest arm64) was removed by explicit user
  decision — do not reinstate an Intel job.
- **Manual-only trigger (`workflow_dispatch`).** The user wants the macOS
  build to happen only when they execute `build_all.py`, never on push —
  the orchestrator is the single button for all three OSes.
- **Build_Scripts reaches the runner as a second checkout** (its own GitHub
  repo, `path: .build_scripts`), not by copying build.py into each project —
  copies drift. Private repo access goes through the `BUILD_SCRIPTS_TOKEN`
  fine-grained PAT secret.
- **Linux tar.gz is automatic, not flag-gated.** `_package_linux()` runs
  after every build_all run and packages each successful linux label as
  `dist/<project>-<label>.tar.gz`. Windows (.exe) and macOS outputs are
  shipped as-is by user decision.

## Changelog
- 2026-06-20 — build_projects.py v1.4.1: **dynamic VM sizing DEFAULT OFF
  (size_to_jobs=false) -- a benchmark showed vCPU growth HURTS on this host.**
  Controlled experiment (WealthBuilder solo, --jobs 16, 32GB, cold): 16 vCPU(2x8)
  =12m48s, 32 vCPU(2x16)=16m13s, 32 vCPU(1x32)=15m52s -> **more vCPU = ~27% SLOWER
  for the same build** (L3/CCD locality on the 8-CCD 3990X: 16 threads stay in ~2
  CCDs; 32 scatter, more cross-CCD L3 misses); **socket layout irrelevant**;
  **cold-boot negligible** (≈ warm ~13m). And lane-2 on 16 vCPU: **jobs=8 (no
  oversub) 25m22 ≈ jobs=16 (oversub) 25m23** -> job-cap is neutral (Thrift is
  memory-bandwidth-bound, not CPU-bound). CONCLUSION: **16 vCPU is the sweet spot**;
  a fixed VM + lane parallelism is fastest (lane-2 ~25m, ~30% < sequential ~36m).
  v1.4.0's vCPU-growth premise was wrong for this hardware. Sizing code kept but
  DORMANT -- flip size_to_jobs=true only where more vCPU actually helps. VM reset to
  16 vCPU(2x8)/32GB. (Lesson stands: don't `virsh shutdown` a VM the owner is using
  to test cold-start.)
- 2026-06-20 — build_projects.py v1.4.0: **per-build job-cap + per-run dynamic VM
  sizing.** `--windows-jobs K` now, when it COLD-STARTS the VM, right-sizes it via
  `virt-xml` to `K*cores_per_build` vCPU (2-socket topology, set atomically with the
  vcpu count -- separate edits fail on Win10's topology check) and
  `K*mem_per_build_gb` RAM, and passes `--jobs cores_per_build` to each windows
  build (build.py honors an explicit --jobs as-is) -- so K builds each get a full
  core budget with NO oversubscription. Defaults cores_per_build=16, mem_per_build_gb
  =16 (K=2 → 32vcpu/32GB; K=1 → 16vcpu/16GB); RAM capped to host-20GB. A VM that is
  **already running is never resized or shut down** (only a VM we cold-start);
  virt-xml errors are non-fatal. Config [windows_vm].{size_to_jobs(default true),
  cores_per_build,mem_per_build_gb}. Rationale (measured): host=62GB RAM (the
  ceiling) / 128 threads (abundant); each build peaks ~6GB; CPU over-alloc is cheap
  → give each build full cores, scale vCPU with lanes, RAM-bounded → one right-sized
  VM beats many. VERIFIED: resize round-trips (host-passthrough preserved), feature
  fires resize on cold-start (→16vcpu/16GB, started, SSH up), --jobs passthrough
  (dry-run), compile. NOT re-run to build-completion (owner asked to leave the VM
  running); build-completion + post-build shutdown rely on the v1.3.0 lifecycle test.
  NOTE/LESSON: the dev's *test harnesses* (not the feature) repeatedly cold-cycled a
  VM the owner was keeping up -- don't `virsh shutdown` a running VM to test the
  cold-start path without asking.
- 2026-06-20 — build_projects.py v1.3.0: **auto start/stop the Windows build VM.**
  When a windows job is scheduled, the scheduler starts the libvirt VM
  (`win10_pro_x64_python`) if it's shut off — `virsh start` then polls SSH until
  reachable (aborts after `boot_timeout`) — and gracefully `virsh shutdown`s it
  after all windows binaries are built + copied. SAFETY: only shuts down a VM **it
  started** (an already-running VM is left up); on a windows-build FAILURE it
  leaves the VM up for debugging; cleans up on Ctrl-C; never force-`destroy`.
  Config in build_projects.toml `[windows_vm]` (domain/ssh/connect/timeouts,
  `manage=true`); `--no-manage-vm` skips; `--dry-run` shows the plan without
  acting. `virsh -c qemu:///system` runs without sudo (libvirt group). Helper
  `read_windows_vm` added to projutil.py. Verified end-to-end: already-running →
  left up; shut-off → started, built, shut off. See [[macos-actions-billing-blocked]].
  Follow-ons (not done): per-build Nuitka `--jobs` cap = vCPU÷lanes to avoid
  oversubscription; per-run dynamic VM sizing (set vCPU/RAM to `--windows-jobs`
  before cold-boot). Host measured: 3990X 128T / 62GB RAM (RAM is the ceiling);
  each heavy build peaks ~6GB / wants ~8 cores, so one right-sized VM beats many.
- 2026-06-20 — build_projects.py v1.2.3: **`--windows-jobs N` (default 1) — lane=2
  works on the single VM.** Earlier notes assumed two concurrent Windows compiles
  would OOM / need a 2nd VM (~52GB). MEASURED otherwise: WealthBuilder + Thrift
  windows built concurrently on the 16 vCPU / 32GB VM, **peak only ~12GB combined
  (min ~20GB free), both OK** — wall-clock ~25m vs ~36m serial (~30% faster). Added
  `--windows-jobs` (lane_cap gained a `win_jobs` arg); default 1 keeps current
  behavior. Corrected the "needs a 2nd VM" claim here + in NEXT_SESSION. Note: each
  build still requests jobs=16, so 2 lanes oversubscribe the 16 cores (hence ~30%,
  not 2×); capping per-build jobs could tune that later.
- 2026-06-20 — build_projects.py v1.2.2 + build_all.py v1.2.7: **macOS skipped by
  default + graceful Actions billing handling.** The product repos are PRIVATE, so
  macOS Actions runs bill at 10× and the free quota is spent; runs now fail in ~7s
  with *"job was not started ... spending limit"*. Two changes: (1) a bare
  `build_projects.py` builds only linux+windows (`_DEFAULT_SKIP_HOSTS={"macos"}`);
  macOS only when named via `--only ...,macos` (warns when it auto-skips). (2)
  build_all.py detects the billing annotation (`_is_billing_block`) and reports the
  macOS host as **SKIP** not FAIL — new tri-state: build_all exits `_EXIT_SKIPPED=3`
  when nothing built but a host was billing-skipped; build_projects maps rc==3 to a
  "skip" Result (the `Result.ok` bool became `Result.status` ∈ ok|fail|skip).
  Verified end-to-end: `ajj3-brain --only macos` → SKIP, exit 0. See
  [[macos-actions-billing-blocked]]. Long-term fix = a local macOS build target.
- 2026-06-20 — build_projects.py v1.2.1: **Ctrl-C now actually stops a run.** The
  parallel scheduler's `finally` called `executor.shutdown(wait=True)`, which does
  NOT cancel queued futures — so an interrupt drained each lane's queue instead of
  aborting (the cap-1 Windows lane kept launching the next build *after* the SIGINT,
  so it survived and ran to completion). Fix: `shutdown(wait=False, cancel_futures=
  True)` cancels unstarted jobs; in-flight children still take the terminal's SIGINT.
  `main()` now catches `KeyboardInterrupt`, prints a clean WARNING, and exits 130
  instead of dumping a traceback. (Found while diagnosing a real interrupted 9-job
  run where 4 jobs FAILed with `KeyboardInterrupt` but Thrift/windows kept building.)
- 2026-06-19 — build_all.py v1.2.6: package deliverables — **windows + macOS →
  `.zip`** (`_package_zip`; fixes the 0-byte Gumroad upload; macOS exec bit
  preserved in the zip), **linux → `.tar.gz` ONLY** (`_package_linux`; native
  Linux format, NOT zipped). (Iterated: 1.2.4 macОS-only zip, 1.2.5 zip-
  everything, then owner settled here.) Per project = linux `.tar.gz` + windows
  `.zip` + macOS `.zip`. Collector skips `*.zip` and `*.tar.gz` (no nesting).
- 2026-06-19 — build_all.py v1.2.4: auto-zip macOS binaries
  (`dist/<project>-macos-arm64.zip`) alongside the linux `.tar.gz`. A raw Mach-O
  uploaded to Gumroad/etc shows as **0 bytes**; zipping fixes it. The zip forces
  + preserves the executable bit (ZipInfo unix mode) so it runs after unzip even
  if a transport stripped `+x`. Collector skip extended to `*.zip` too (no
  nesting). Windows `.exe` left raw (uploads fine). Existing 3 macOS binaries
  zipped retroactively via the same function.
- 2026-06-19 — Windows VM sizing, MEASURED. Bumping the win10_pro_x64_python
  guest 4→16 vCPU (presented as 2 sockets × 8 cores — Win10 *client* caps at 2
  sockets, and it ONLY onlines hot-added vCPUs after a reboot; RAM hot-applies)
  cut Thrift's Windows build **36m53s → 21m02s (~43%, 1.75×)**. 4× cores ≠ 4×
  speed: ~57% of the build is parallel C-compile, ~43% is a serial floor (Nuitka
  analysis + link + onefile compression). **Sweet spot = 16 vCPU / 32 GB**:
  jobs = `min(cpu, (RAM−4)/1.5, 32)` = min(16,18,32)=16, so 16 cores are fully
  fed by 32 GB. 32 vCPU would need ~52 GB (most of the 62 GB host) for only ~4-5m
  more — not worth it. Extrapolated Windows lane ~63m → ~37-40m.
- 2026-06-19 — MILESTONE: first full cross-OS `build_projects.py` run, **9/9 green**.
  3 projects × {linux local, windows via SSH-to-VM (serial), macos via GitHub
  Actions (parallel)}. ~63m wall-clock vs ~148m fully serial (~2.3x from lane
  parallelism); the serial Windows lane is the critical path (one shared VM:
  ajj3 3m41 + WB 22m21 + Thrift 36m53 = ~63m). All 9 binaries verified in
  dist/<os-arch>/. Swap held at 189MiB/31GiB peak under the heaviest concurrent
  load. NOTE: a 2nd Windows build VM would let windows lane cap go to 2 and ~halve
  the critical path — the obvious next throughput win (the 32GiB swap already
  supports more concurrent VMs). [SUPERSEDED 2026-06-20: no 2nd VM needed — the
  single 32GB VM runs lane=2 fine (measured); see the 2026-06-20 changelog +
  `--windows-jobs`.]
- 2026-06-19 — build_all.py v1.2.3: artifact collector now skips `*.tar.gz`.
  Was sweeping the previous run's auto-package (`dist/<project>-<label>.tar.gz`)
  into `dist/<label>/`, which `_package_linux` then re-tarred — nesting the old
  package into the new one and bloating it every build. Found during the first
  real `build_projects.py --only linux` run (3/3 green, ~14m wall vs ~23m serial
  — lane-cap parallelism confirmed). Cleaned the nested tarballs + regenerated
  clean packages (ajj3-brain 36→18M, WealthBuilder 239→80M, Thrift 445→149M).
- 2026-06-19 — build.py v1.11.1: `report_repo_freshness` wired to gitutil.py
  (removes ~25 lines of inline _git/fetch/rev-list dup; output byte-identical;
  graceful `try/except ImportError` skip so a remote build host without gitutil
  still builds). build_all.py's local pull deliberately left inline (its
  run()/dry-run wrapper + intentional no-GCM-on-local-pull; routing through
  gitutil would change behavior for a 1-line gain — not worth it on a fragile
  production file).
- 2026-06-19 — sync_projects.py v1.0.0 (NEW) + gitutil.py v1.0.0 + projutil.py
  (NEW shared modules). Multi-repo git status + safe fast-forward update across
  the project repos. Safe by default (no verb = read-only fetch+status; only
  `--pull` mutates, and it is FF-only, refuses dirty trees, skips ahead/diverged/
  detached/shallow, confirms per-repo). gitutil.py is the shared git layer (one
  `_git` chokepoint, read-only API + `pull_ff_only`, GCM-hardened network ops,
  NO force/reset surface). projutil.py lifts build_projects.py's selection +
  TOML-CRUD helpers; build_projects.py now imports them (aliased, byte-identical
  behavior, regression-checked). Design chosen from a 7-agent analysis + a
  3-lens adversarial review; rationale below. v1 scope = status + ff-pull only
  (push/commit/merge deferred — all repos are clean/synced so those paths can't
  be exercised yet). build.py/build_all.py wire-into-gitutil is deferred.
- 2026-06-19 — build_projects.py v1.2.0: manage the default list from the CLI
  (`--list-projects` / `--add-project NAME ...` / `--remove-project NAME ...`)
  instead of hand-editing build_projects.toml. Bare name = sibling dir; dedup by
  resolved path; warns if a project lacks build_hosts.toml but adds it anyway.
  stdlib has no TOML writer, so the array is rewritten in place via regex while
  the comment header is preserved (round-trips byte-identical).
- 2026-06-19 — build_projects.py v1.1.0: default project list via
  `build_projects.toml` (`projects = [...]`). No-args run builds the curated set;
  adding a future project is a one-line edit (paths relative to the file).
  Precedence: positional args → `--all` discovery → config list. `--config PATH`
  overrides. The "easy way to grow the build set" without retyping paths.
- 2026-06-19 — build_projects.py v1.0.0 (NEW): multi-project scheduler above
  `build_all.py`. Builds N projects across their OS hosts, scheduling
  `(project × OS)` jobs by OS lane with per-host concurrency caps — windows=1
  (shared VM OOMs on concurrent compiles, serial even across projects),
  linux=`--linux-jobs` (default 2), macos=`--mac-jobs` (default #projects).
  `--parallel`/`--sequential`, `--only`, `--all --root`, `--dry-run`, `--`
  passthrough. Implements the long-standing "parallel build-matrix" roadmap
  item; no lock needed (one scheduler owns all lanes). `build_all.py` untouched.
- 2026-06-19 — build.py v1.11.0: pre-build gate + repo-freshness report.
  `preflight_gate()` blocks a build on a missing entry point or declared
  `data_files`/`data_dirs` not on disk (exits before the compile); version
  drift / unbundled hints stay warnings; `--force` bypasses. `report_repo_
  freshness()` does a read-only, bounded `git fetch` and reports `N behind
  origin/<branch>` — never modifies the tree (pulling stays build_all's job).
  Rationale recorded above ("Why the build gates…", "Why repo-freshness…").
- 2026-06-13 — build_all.py v1.2.2: Windows SSH host fixes. (1) Copy-back
  probed only the local side for rsync via `shutil.which`, so a Linux
  orchestrator always picked rsync even though Windows hosts have none — the
  transfer failed after a successful 19-min build. Now probes the remote over
  ssh (`rsync --version`) and falls back to scp when rsync isn't on both ends.
  (2) Remote `git pull` hardened with `credential.interactive=false` so GCM
  doesn't try to prompt `/dev/tty` over non-interactive ssh (was a non-fatal
  warning, now fails clean; `--no-pull` skips). WealthBuilder Windows .exe
  (48 MB, MSVC 14.5) was the first native SSH-VM build — proved the chain.
- 2026-06-12 — build_all.py v1.2.1: github transport retries the artifact
  download 4× (10/20/30s backoff) — a just-uploaded artifact can transiently
  fail to connect to Azure blob storage (`*.blob.core.windows.net`) even
  though the build succeeded; not a firewall issue. Prints manual download
  command + run URL on final failure.
- 2026-06-12 — build_all.py v1.2.0: new `transport = "github"`
  (`build_github()`: gh dispatch → watch → artifact download, keys gh_repo /
  workflow / ref / artifact) and automatic Linux tar.gz packaging
  (`_package_linux()`). `examples/macos-build.yml` added (arm64 only,
  workflow_dispatch only); template + all docs updated. Intel macOS removed.
- 2026-06-10 — v1.10.0 (build.py): env setup now patches the build env's
  Nuitka (`decoratorRetries` 5×1s → 40×2s) so EDR (CylancePROTECT) can't kill
  the resource-embedding step; idempotent, re-applies after --clean-env,
  warns-not-fails on unknown Nuitka layouts. HELP.md troubleshooting entry
  added; ABOUT.md version refreshed.
- 2026-06-04 — v1.8.8 (build.py): `--init`/`--reset` now auto-detect asset dirs
  nested INSIDE Python packages (e.g. `my_llm/console/web`) via
  `_detect_package_data_dirs()`, which walks only the importable package tree
  (dirs with `__init__.py`) and bundles any non-package subdir holding asset
  files. Nuitka follows a package's `.py` but never bundles its non-`.py` data,
  so a browser/console UI shipped under a package was silently dropped and the
  standalone app 404'd on its own `index.html` (worked on Windows only because
  that binary predated the regen). Top-level non-package dirs (`config/`,
  `docs/`) are intentionally left untouched. `.js` added to the asset-ext set.
- 2026-06-04 — v1.8.7 (build.py): drop explicit "printsupport" Qt plugin family
  (reverses 1.8.6). Qt 6.11 removed the standalone family, making an explicit
  name a FATAL; "sensible" already bundles it gated on hasPluginFamily(). --init/
  --reset now emit plain "sensible", --force self-heals an old preserved value,
  and every build strips a stale ",printsupport" so all projects build unedited.
- 2026-05-31 — v1.8.6 (build.py): --init/--reset auto-detect QtPrintSupport and
  emit include_qt_plugins="sensible,printsupport" (else "sensible"), so the print
  plugin "all" used to provide is retained cross-OS — moving off "all" can't
  regress Windows printing. Thrift example updated.
- 2026-05-31 — v1.8.5 (build.py): add `--reset` for `--init` — regenerate
  build_config.toml from scratch, ignoring the existing file (no preservation),
  with a warning. Complements `--force` (which preserves curated values). Fixes
  the gap where `--force` kept a stale include_qt_plugins="all". --reset implies
  overwrite and works with or without --init.
- 2026-05-31 — v1.8.4 (build.py): `--init` defaults include_qt_plugins to
  "sensible" instead of "all" (fixes Linux `patchelf: wrong ELF type` from the
  qml plugin tree's .cpp.o files; widgets apps never need qml). --force now
  preserves an existing include_qt_plugins value. Example configs + template +
  USER_GUIDE schema default updated.
- 2026-05-31 — build_all.py v1.1.0: auto-generate build_hosts.toml (current OS
  enabled as local host) on first run; explicit --init / --init --force mirror
  build.py, with SSH host details preserved across a --force regenerate.
- 2026-05-31 — build_all.py v1.0.0: new cross-OS orchestrator. Runs build.py
  on local + SSH hosts (git pull + remote build + artifact copy-back),
  collecting per-OS binaries into dist/<os>-<arch>/. Adds
  examples/build_hosts.template.toml. build.py unchanged; all its flags pass
  through after `--`. README/HELP/USER_GUIDE/ABOUT updated.
- 2026-05-31 — v1.8.3: `--init` now PRESERVES an existing build_config.toml's
  [app].entry and [nuitka].data_dirs / data_files when regenerating (incl. with
  --force). Previously --force --init overwrote them with auto-detected values,
  which silently dropped custom data dirs (e.g. config/, console/web) and reset
  entry to main.py. Auto-detection still fills any values not already set;
  fresh-project behavior unchanged. data_dirs/data_files render preserved
  [src,dst] pairs correctly.
