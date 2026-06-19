# About

**Project:** Build_Scripts — Common Nuitka Build System
**Script version:** 1.11.1  (build.py)
**Orchestrator:** build_all.py v1.2.5
**Multi-project scheduler:** build_projects.py v1.2.0
**Multi-repo sync:** sync_projects.py v1.0.0  (shared: gitutil.py v1.0.0, projutil.py)
**Date:** 2026-06-19
**License:** Internal use

## What's new in 1.11.1 (build.py)

- **`report_repo_freshness` now uses the shared `gitutil.py`** instead of its own
  inline `git` logic (DRY). Output is byte-identical. On a host where `gitutil`
  isn't importable (e.g. a remote build host that hasn't synced it yet), the
  freshness report is silently skipped — it's informational, never essential.
  `build_all.py`'s local pull intentionally stays inline (its `run()`/dry-run
  wrapper + no-GCM-on-local-pull are deliberate; routing it through `gitutil`
  would change behavior for a 1-line gain).

## What's new — sync_projects.py v1.0.0 (multi-repo git sync)

- **Compare local repos to GitHub and (safely) update them, in one command** —
  instead of one-at-a-time in PyCharm. Selection: default = the
  `build_projects.toml` set; `--project a,b`; `--all` (every git repo under the
  workspace, including projects not in the build list).
- **Safe by default.** No verb = a read-only `fetch` + per-repo status table
  (branch, ahead/behind, dirty, untracked, shallow/lfs/submodule flags); zero
  working-tree mutation. The only mutating verb is `--pull`, which is
  **fast-forward-only**, **refuses a dirty tree**, **skips ahead/diverged/
  detached/shallow** repos with a reason, and **confirms per-repo** (showing the
  incoming commits) unless `--yes`. No push/commit/merge in v1; no `--force`
  anywhere. `--dry-run` previews; `--non-interactive` for CI.
- **Shared modules.** New `gitutil.py` (one git subprocess chokepoint, read-only
  queries + FF-only pull, GCM-hardened network ops, no force/reset surface) and
  `projutil.py` (project selection + `build_projects.toml` CRUD, now shared with
  `build_projects.py` so the two tools agree on what a "project" is). `build.py`
  / `build_all.py` are intended to wire into `gitutil` next (deferred).

## What's new in build_projects.py v1.2.0

- **Manage the default project list from the CLI** — no hand-editing
  `build_projects.toml`. `--list-projects` shows the set (with per-project
  status), `--add-project NAME ...` and `--remove-project NAME ...` edit it
  (a bare NAME is a sibling dir; a path works too; duplicates resolved by real
  path). The file's comment header is preserved; edits round-trip cleanly.

## What's new in build_projects.py v1.1.0

- **Default project list (`build_projects.toml`).** Run `build_projects.py`
  with no args to build a curated set; **adding a future project is a one-line
  edit** (`projects = [...]`, paths relative to the file). Precedence:
  positional args → `--all` discovery → this default list. `--config PATH`
  points at an alternate list.

## What's new in 1.11.0 (build.py)

- **Pre-build gate.** Before compiling, the build now refuses to run if it
  would produce a guaranteed-broken binary: a **missing entry point** or a
  declared `data_files`/`data_dirs` path that isn't on disk. It exits before
  Nuitka starts, so you don't burn a 5–15 min compile on a build that can't
  work. Version drift and unbundled-asset suggestions stay **warnings** (not
  blocking). Pass `--force` to build anyway. (The existing warn-only
  `preflight_warn` still runs first; the gate is the hard stop.)
- **Repo-freshness report.** A build now does a read-only `git fetch` (bounded,
  non-fatal) and tells you if the working tree is `N commits behind origin/…`
  before it starts — so you don't ship a stale build. It is **report-only**:
  it never modifies your tree. Actually pulling stays `build_all.py`'s job.

## What's new — build_projects.py v1.0.0 → v1.1.0 (multi-project scheduler)

- **Build several projects at once, scheduled by OS lane.** Sits on top of
  `build_all.py`: each `(project × OS)` job runs as
  `build_all.py <project> --only <host>`, so every audit gate, git pull, and
  per-OS artifact path is inherited unchanged.
- **Per-OS concurrency caps.** `windows = 1` (the shared build VM OOMs on
  concurrent compiles — serial even across projects), `linux = --linux-jobs`
  (default 2), `macos = --mac-jobs` (default: #projects; GitHub does the work).
- **`--parallel` (default) / `--sequential`.** Parallel overlaps lanes and
  captures each job to `build-logs/<project>-<host>.log`; sequential streams
  each build live. `--only`, `--all --root DIR` discovery, and `--dry-run`
  round it out. Full usage in HELP.md / USER_GUIDE.

## What's new in build_all.py 1.2.5

- **Every OS deliverable is now a `.zip`** — windows and linux joined the macOS
  zipping from 1.2.4, so customers get one consistent format (`dist/<project>-
  <label>.zip`). The unix exec bit is preserved for linux/macOS binaries.
- **Linux *also* keeps its `.tar.gz`** (the native Linux format) alongside the
  zip — linux ships both. Windows/macOS ship the zip only.

## What's new in build_all.py 1.2.4

- **macOS binaries are now auto-zipped** (`dist/<project>-macos-arm64.zip`),
  alongside the existing linux `.tar.gz`. A raw Mach-O executable uploaded to
  Gumroad and similar sites shows as **0 bytes** — zipping fixes that. The
  executable bit is forced on and preserved in the zip (the ZipInfo carries the
  unix mode), so the binary runs after the buyer unzips even if a transport
  stripped `+x`. The local-collector skip now also ignores `*.zip` (same
  no-nesting fix as `*.tar.gz`). Windows `.exe` is left as-is (uploads fine).

## What's new in build_all.py 1.2.3

- **Packaging no longer nests prior tarballs.** The local artifact collector
  swept *every* loose `dist/` entry into `dist/<label>/` — including the
  auto-generated `dist/<project>-<label>.tar.gz` from a previous run, which then
  got re-tarred into the new package, bloating it a little more each build. The
  collector now skips `*.tar.gz` (Nuitka never outputs one, so no real artifact
  is lost). Surfaced by the first real `build_projects.py --only linux` run
  (3/3 green); stale nested tarballs were cleaned and packages regenerated.

## What's new in build_all.py 1.2.2

- **Windows SSH copy-back fixed.** The artifact pull probed only the local
  side for rsync (`shutil.which`), so on a Linux orchestrator it always chose
  rsync — but a Windows host has no rsync, so the transfer failed after a
  successful build. It now probes the **remote** for rsync over ssh and falls
  back to **scp** (riding the host's OpenSSH) when rsync isn't on both ends.
  Windows `.exe` builds now copy back cleanly.
- **Remote `git pull` hardened against GCM-over-ssh.** Git Credential Manager
  can't reach `/dev/tty` to prompt over a non-interactive ssh session, so a
  private-repo pull errored. The pull now runs with
  `credential.interactive=false` so it fails fast (and we continue with the
  existing remote tree); `--no-pull` skips it entirely.

## What's new in the docs (2026-06-13)

- USER_GUIDE **§11 — First-time three-OS setup checklist**: a start-to-finish
  runbook (PAT creation + verification, per-repo secret, workflow file, host
  config) with a failure quick-reference table covering every issue hit
  during bring-up (wrong branch, missing slash, bad-credentials-vs-404,
  transient blob download).
- USER_GUIDE §10 Step 3: Windows SSH host expanded into a complete
  step-by-step (enable sshd, key login, prerequisites, repo clone).
- README **Roadmap** section added; PROJECT_MEMORY "Open items" now tracks
  three-OS bring-up status (macOS working; Windows SSH host = next).

## What's new in build_all.py 1.2.1

- **Resilient artifact download** — the github transport now retries the
  artifact download up to 4× (10/20/30s backoff) before giving up. A
  freshly uploaded artifact can briefly 404 on Azure blob storage; the
  build itself already succeeded, so a transient download hiccup no longer
  wastes the whole run. On final failure it prints the manual
  `gh run download` command and the run URL.

## What's new in build_all.py 1.2.0

- **`transport = "github"`** — builds the macOS binary on a GitHub Actions
  **Apple Silicon (arm64) runner** with no Mac on the network: dispatches
  `.github/workflows/macos-build.yml` via the `gh` CLI, waits, and downloads
  the artifact into `dist/macos-arm64/` within the same `build_all.py` run.
  The workflow is manual-only (`workflow_dispatch`) and **Intel (x86_64)
  macOS is intentionally not built**. Template: `examples/macos-build.yml`.
- **Automatic Linux packaging** — after every run, each successful linux
  host's `dist/<label>/` is tar.gz'd to `dist/<project>-<label>.tar.gz`.

## What's new in 1.10.0 (build.py)

**Env setup now patches Nuitka's anti-virus retry window (EDR fix).** On the
Windows build host, CylancePROTECT holds every freshly linked unsigned exe for
~60+ seconds, so Nuitka's final resource-embedding step (icon + version info)
failed every onefile build with "Failed to add resources to file … the result
is unusable" — after an otherwise perfect compile. Nuitka's stock retry window
is `decoratorRetries(attempts=5, sleep_time=1)` and has no configuration knob,
and re-running the build can never help (each relink is a new file hash that
gets held again). `_install_packages()` now rewrites the env's
`nuitka/utils/Utils.py` defaults to **40 × 2 s** (idempotent; logs "Patched
Nuitka AV retry window"; warns instead of failing if a future Nuitka changes
the code layout). The verified Thrift_Reseller build succeeded after 34
attempts (~68 s). Because the patch runs on every env setup, `--clean-env`
rebuilds and Nuitka upgrades re-patch themselves — the earlier hand-applied
build_env patches are superseded.

## What's new in 1.8.8 (build.py)

**`--init`/`--reset` now auto-detect asset directories nested inside Python
packages** (e.g. `ajj3_brain/console/web`). Nuitka follows a package's `.py` files
but does not bundle the non-`.py` data living inside it, so a browser/console UI
shipped under a package was silently left out of the standalone binary — the app
then returned `{"error": "not found"}` for its own `index.html`. The new
`_detect_package_data_dirs()` walks only the importable package tree (dirs with
`__init__.py`) and lists any non-package subdir that holds asset files under
`data_dirs`. Top-level non-package dirs (`config/`, `docs/`) are intentionally
left untouched, so nothing that built before changes. `.js` was added to the
detected asset extensions.

## What's new in 1.8.7 (build.py)

**Dropped the explicit `printsupport` Qt plugin family (reverses 1.8.6).** Qt
6.11 removed the standalone `printsupport` plugin family (folded into the
platform plugin), so naming it explicitly — as 1.8.6 did when a project imports
`QtPrintSupport` — is now a hard `FATAL: there is no Qt plugin family
'printsupport'`. Nuitka's `"sensible"` set already includes `printsupport`
*gated on `hasPluginFamily()`*, so it auto-bundles the print plugin wherever Qt
still ships it and skips it on 6.11+ — printing keeps working on every OS
without naming it. `--init`/`--reset` now emit plain `"sensible"`, `--force`
self-heals a stale value preserved from an old config, and **every build strips
a leftover `,printsupport`** — so all existing projects build again with no
per-project edit.

## What's new in 1.8.6 (build.py) — superseded by 1.8.7

**`--init`/`--reset` auto-detect `QtPrintSupport`.** When the project imports
`QtPrintSupport`, the generated `include_qt_plugins` becomes
`"sensible,printsupport"` (otherwise `"sensible"`). This guarantees the Qt
print plugin — which `"all"` used to bundle — stays in the build on **every**
OS, so switching off `"all"` cannot regress printing on Windows. Reported in
the `--init` summary as `Qt plugin set`.

## What's new in 1.8.5 (build.py)

**New `--reset` flag for `--init`.** `--force` intentionally *preserves*
curated values (entry, data_dirs, data_files, and now include_qt_plugins) so
a regen doesn't wipe hand edits — but that also means a stale value like
`include_qt_plugins = "all"` survives a `--force`. `--reset` is the
from-scratch counterpart: it ignores the existing config entirely and
regenerates from detection + current defaults, printing a warning that
curated values are not preserved. `--reset` implies overwrite and works with
or without an explicit `--init`.

## What's new in 1.8.4 (build.py)

**`--init` now defaults `include_qt_plugins` to `"sensible"`, not `"all"`.**
`"all"` bundles the Qt **qml** plugin tree, which ships stray `.cpp.o`
object files. On Linux, Nuitka's rpath step runs `patchelf --set-rpath` over
every ELF in the bundle and aborts on those objects
(`patchelf: wrong ELF type`) — so a config generated on/for Windows built
fine there but failed on Linux. Widgets apps never use qml, so `"sensible"`
(Nuitka's recommended default set) both fixes the Linux build and shrinks
the binary on every OS. A real QML app can set `"sensible,qml"` or `"all"`
by hand; `--force --init` now **preserves** an existing
`include_qt_plugins` value (joining `entry`, `data_dirs`, `data_files` in
the curated-value set). Example configs and the template updated to match.

## What's new — build_all.py v1.1.0

**`build_hosts.toml` is now auto-generated, with `--init` / `--force`.**
Mirrors `build.py`'s config handling so the host map isn't hand-copied or
maintained per project. A normal run with no host map auto-writes a tailored
one (current OS enabled as a local host); `--init` does it explicitly and
`--init --force` regenerates while **preserving any SSH host details** the
user added. The `examples/build_hosts.template.toml` stays as the
full-option reference.

## What's new — build_all.py v1.0.0 (cross-OS orchestrator)

**New companion script `build_all.py` for Windows + Linux + macOS
builds.** Nuitka cannot cross-compile — it only emits a binary for the OS
it runs on — so a three-OS deliverable means running the *same* `build.py`
natively on three hosts. `build_all.py` drives that from one command:

- Reads a per-project `build_hosts.toml` mapping each OS to a build host.
- `transport = "local"` builds on the current machine; `transport = "ssh"`
  runs `git pull` + the remote `build.py` over SSH and copies the artifact
  back (rsync, or scp fallback).
- Collects every host's output into `<project>/dist/<os>-<arch>/` so the
  three binaries never collide.
- All `build.py` flags pass through unchanged after `--`; `--only`,
  `--no-pull`, and `--dry-run` control the orchestration itself.

`build.py` is untouched. New files: `build_all.py`,
`examples/build_hosts.template.toml`. Docs (README/HELP/USER_GUIDE)
updated; `USER_GUIDE.md` §10 covers SSH host setup and the
"macOS without a Mac" GitHub Actions route.

## What's new in 1.8.2

**RAM-aware default job count (fixes zstd "not enough memory" onefile
crash).** The default was `multiprocessing.cpu_count()`, which on a
high-core-count machine returns a large number; with LTO on, that many
parallel link jobs exhausted available RAM, and the onefile build died at
the final zstd payload compression with `ZstdError: not enough memory` (the
C compile and link had already succeeded). Jobs are now capped by available
RAM: ~1.5 GB budgeted per LTO job, 4 GB headroom, hard ceiling of 32, read
at runtime via `_total_ram_gb()`. An explicit `--jobs N` is still honored
as-is, and LTO behavior is unchanged.

> Correction (2026-06-13): an earlier wording pinned this to "a Threadripper
> 3990X … 32 GB"; the incident was actually on a different Windows box. The
> specific machine details were wrong and have been removed — the mechanics
> (cap formula, runtime RAM read) are unaffected.

## What's new in 1.8.1

**Auto-bundle data files for known data-shipping packages.** A
Thrift_Reseller bug surfaced the issue: the standalone exe generated
barcodes only for the Avery label type and silently produced nothing
for other types. Root cause: `python-barcode` ships `.ttf` font files
inside the `barcode/` package directory. Nuitka compiles the Python
code fine but does **not** auto-bundle non-Python data files. The
library then ran in the exe without raising any exception — no
traceback in console mode — but produced empty/broken output for code
paths that relied on those fonts.

v1.8.1 adds a `PACKAGE_DATA_MODULES` registry parallel to
`HEAVY_C_MODULES`. When a listed package is detected in the project,
`--include-package-data=<name>` is appended automatically, telling
Nuitka to bundle every non-Python file inside that package directory.

Initial registry:

```python
PACKAGE_DATA_MODULES = {
    "barcode":        "barcode",   # python-barcode .ttf fonts (the fix)
    "python_barcode": "barcode",   # pip dist name -> import name
    "pil":            "PIL",       # safety; case-sensitive on disk
    "pillow":         "PIL",       # pip dist name -> import name
    "qrcode":         "qrcode",    # safety
}
```

Detection mechanism is shared with heavy-C detection (refactored into a
new `_scan_project_for_packages()` helper) so adding a third category
later is cheap. The build banner now shows a `Pkg-data` line listing
what was auto-added; `--info` and `--audit` show the same information.

**This is NOT a generic "always bundle everything" toggle.** Only
known small-data, non-Nuitka-plugin-covered packages belong in the
registry. matplotlib, scipy, etc. have dedicated Nuitka plugins that
already handle their data correctly — blindly using
`--include-package-data` on them inflates the bundle by tens to
hundreds of MB.

## What's new in 1.8.0

**Heavy-C bytecode-mode approach confirmed working — locked in.**
The v1.7.3 experiment produced a working 202 MB standalone exe in
~26 minutes on a 32 GB Windows machine (MinGW64, `--lto=yes`,
`--jobs=16`), with the application opening normally and `pymupdf`
loading at runtime. The Nuitka maintainer's "untested for
submodules-in-packages" warning did not manifest for pymupdf's case.

v1.8.0 promotes the approach from experimental to default:

- The pre-flight RAM detection and commit-limit pause from v1.7.1/1.7.2
  are removed (101 lines of dead code excised). They were workarounds
  for compiling `mupdf.c`, which v1.7.3 sidesteps entirely.
- `_tune_heavy_c_build()`, `get_total_ram_gb()`, `get_commit_limit_gb()`,
  `_win_memstatus()`, and `HEAVY_C_MIN_COMMIT_GB` are gone.
- The "experiment" framing is removed from banners, audit, and BUILD
  FAILED footer.
- `HEAVY_C_MODULES` dict and `--noinclude-custom-mode=:bytecode`
  injection are unchanged from v1.7.3.

The PyInstaller backend remains a documented fallback (in
PROJECT_MEMORY open items) if a future package proves resistant to
bytecode mode, but no longer flagged as imminent — Nuitka with the
bytecode workaround is the working solution.

### Build characteristics

- **Time**: ~25-30 minutes for a heavy-C onefile (pymupdf + PySide6 +
  matplotlib + opencv).
- **Size**: ~200 MB onefile, ~770 MB decompressed.
- **Compiler**: MSVC or MinGW64 — bytecode mode is compiler-agnostic.
- **Source protection**: your code is Nuitka-compiled to native machine
  code; only pymupdf's public SWIG wrapper ships as bytecode.

## What's new in 1.7.3 (experiment)

**Heavy-C modules now shipped as Python bytecode, not recompiled to C.**
Multiple v1.7.x builds proved that `cc1.exe` from Nuitka's bundled GCC
15.2 cannot compile `pymupdf.mupdf.c` regardless of physical RAM, pagefile,
or job count — the same `out of memory allocating 10937968 bytes` failure
happened on 16 GB and 32 GB machines, with `--jobs=1` and ample commit
limit. The failure is a per-process address-space ceiling, not a
system-tuning issue.

v1.7.3 stops fighting the compiler. When a heavy-C module is detected the
script appends `--noinclude-custom-mode=<target>:bytecode` to the Nuitka
command (e.g. `pymupdf.mupdf:bytecode`). Nuitka then ships that submodule
as plain `.pyc` bytecode; CPython interprets it at runtime; the prebuilt
native `.pyd` that pymupdf actually calls into is bundled by Nuitka's
dll-files plugin as usual. The giant `mupdf.c` is never generated.

Consequences:

- MSVC works again for heavy-C projects — no compiler routing.
- No `--jobs=1` cap, no LTO-off, no commit-limit pre-flight: the build
  is back to a normal-speed Nuitka build (~15-30 min instead of 2+ hrs).
- `HEAVY_C_MODULES` is a dict again `{detect_name: bytecode_target}`.
- Source-protection profile: user's app code is Nuitka-compiled (machine
  code, fully protected); only pymupdf's already-public SWIG wrapper
  is in bytecode form. Nothing of the user's IP is exposed.

**Caveat — this is an experiment.** Nuitka's maintainer has stated the
`bytecode` mode is "largely untested and unsupported" for submodules
inside packages, which is exactly pymupdf's case. The build may succeed
and the exe may run fine; or it may fail with a runtime ImportError or
RuntimeError. The cost of testing is one ~15-30 min build (much cheaper
than the prior ~2 hr attempts). If the experiment fails, the next move
is a PyInstaller backend.

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
