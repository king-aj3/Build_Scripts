#!/usr/bin/env python3
"""
build.py — Common Nuitka build script for PyCharm External Tools.
==================================================================

Single script that builds ANY PyCharm project (one --project-dir argument).
Reads <project>/build_config.toml for per-project settings; falls back to
pyproject.toml for name/version and auto-detects entry/icon when possible.

USAGE (PyCharm External Tool)
-----------------------------
    Program:           <python you want to run build.py with>
    Arguments:         "<Build_Scripts>/build.py" "$ProjectFileDir$" --onefile
    Working directory: $ProjectFileDir$

USAGE (CLI)
-----------
    python build.py [PROJECT_DIR]                # default: --onefile, --compiler=mingw64 on Win
    python build.py [PROJECT_DIR] --standalone   # folder build instead of onefile
    python build.py [PROJECT_DIR] --compiler=msvc
    python build.py [PROJECT_DIR] --clean --onefile
    python build.py [PROJECT_DIR] --clean --clean-env
    python build.py [PROJECT_DIR] --info
    python build.py [PROJECT_DIR] --test
    python build.py [PROJECT_DIR] --ci

DEFAULTS
--------
    --onefile             ON  (override with --standalone)
    --compiler=mingw64    ON on Windows (override with --compiler=msvc|clang)
    Python                Highest stable installed Python in 3.10-3.14

ARTIFACTS land in PROJECT_DIR (not in Build_Scripts dir):
    build_env/   venv used to run Nuitka
    build/       Nuitka intermediate output
    dist/        Final deliverable
    build.log    Verbose log
"""
from __future__ import annotations

import argparse
import ctypes
import multiprocessing
import os
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from datetime import datetime
from pathlib import Path

# TOML reader (stdlib in 3.11+, fallback to tomli for 3.10)
try:
    import tomllib as _toml          # 3.11+
    _TOML_MODE = "rb"
except ImportError:
    try:
        import tomli as _toml        # type: ignore
        _TOML_MODE = "rb"
    except ImportError:
        _toml = None
        _TOML_MODE = None


# ═════════════════════════════════════════════════════════════════════════════
#  CONSTANTS
# ═════════════════════════════════════════════════════════════════════════════

SCRIPT_VERSION    = "1.11.0"
COMPATIBLE_PYTHON = [(3, 14), (3, 13), (3, 12), (3, 11), (3, 10)]
MIN_PYTHON        = (3, 10)
MAX_PYTHON        = (3, 14)
NUITKA_EXPERIMENTAL = {(3, 14)}   # update when Nuitka stabilises a version

# MinGW64 fallback note: Nuitka 4.x BLOCKS --mingw64 on Python 3.13+. Whenever the
# mingw64 compiler is specified or selected, the valid and safe fallback
# interpreter is Python 3.12 (the newest Python MinGW64 still supports). The
# script enforces this via prefer_below=(3,13) in setup_build_env / get_best_python
# and will auto-install 3.12 when only a newer Python is present.
MINGW64_SAFE_PYTHON = (3, 12)

CONFIG_FILENAME   = "build_config.toml"
REQUIREMENTS_NAME = "requirements.txt"

# Standard locations the script will auto-detect
_ENTRY_CANDIDATES = ["main.py", "app.py", "__main__.py", "run.py"]
_ICON_WIN  = ["assets/icon.ico", "resources/icon.ico", "icon.ico"]
_ICON_MAC  = ["assets/icon.icns", "resources/icon.icns", "icon.icns"]


# ═════════════════════════════════════════════════════════════════════════════
#  HEAVY-C MODULE REGISTRY  (v1.8.0)
# ═════════════════════════════════════════════════════════════════════════════
#
# Packages that ship huge generated C *source* which Nuitka would recompile
# into one enormous translation unit. The pymupdf case is canonical: its
# `mupdf.py` is a SWIG-generated wrapper that Nuitka translates to ~2.2M lines
# of C; both MSVC and GCC die compiling it ("C1002" / "cc1.exe: out of
# memory"). pymupdf's *actual* native code lives in a prebuilt .pyd that
# Nuitka bundles as-is via its dll-files plugin - so only the wrapper .py is
# the problem.
#
# The script passes `--noinclude-custom-mode=<target>:bytecode` to Nuitka.
# That tells Nuitka to ship the target submodule as plain Python bytecode
# (.pyc) instead of compiling it to C. CPython interprets it at runtime; the
# prebuilt .pyd loads through it normally. No giant C, no OOM. Works on both
# MSVC and MinGW64. A heavy-C build now completes in ~25-30 min.
#
# Why earlier approaches were dropped:
#   - --nofollow-import-to (v1.6.0): excludes the module entirely; runtime
#     ImportError in standalone mode. Wrong for standalone builds.
#   - Route to MinGW64 + actually compile mupdf.c (v1.5/1.7.0-1.7.2): the
#     compile itself OOMs cc1.exe at the same ~10 MB allocation even on
#     32 GB RAM + 32 GB pagefile (64 GB commit). Proven empirically across
#     two machines and three job configurations - a per-process address-space
#     ceiling, not a system-tuning problem.
#
# Format: { detect_name : bytecode_target_submodule }
# Detection matches case-insensitively against import names and pip
# distribution names (with -/_ normalisation).
HEAVY_C_MODULES: dict = {
    "pymupdf": "pymupdf.mupdf",   # SWIG wrapper - 2.2M lines of C if compiled
    "fitz":    "pymupdf.mupdf",   # legacy import alias for pymupdf
}


# ═════════════════════════════════════════════════════════════════════════════
#  PACKAGE-DATA REGISTRY
# ═════════════════════════════════════════════════════════════════════════════
#
# Packages whose Python code Nuitka compiles fine, but which ship NON-Python
# data files (fonts, templates, .ttf, .svg) inside the package directory.
# Nuitka does NOT bundle those automatically. Symptom: the exe runs without
# raising any exception but silently produces wrong output (empty barcode
# bars, missing labels, no text glyphs, etc.). No traceback in console mode
# because no exception is raised - the package just returns empty results.
#
# Classic example (and the case this registry was added for):
#   python-barcode ships .ttf font files inside `barcode/fonts/`. Without
#   them, the library still runs but renders barcodes without the human-
#   readable text underneath - and in some renderers, fails silently and
#   produces nothing at all for non-default label sizes.
#
# Listing a package here injects `--include-package-data=<output_name>` so
# Nuitka bundles every non-Python file inside that package. Detection is
# the same mechanism as HEAVY_C_MODULES (requirements.txt + pyproject.toml +
# entry-file top-level imports).
#
# Format: { detect_name : nuitka_output_name }
# The detect_name is matched case-insensitively (with -/_ normalisation);
# the output_name is passed verbatim to Nuitka and IS case-sensitive (PIL is
# literally named "PIL" on disk).
#
# IMPORTANT - this is NOT for arbitrary large-data packages. matplotlib,
# scipy, etc. have dedicated Nuitka plugins that already handle their data
# correctly - blindly using --include-package-data on them inflates the
# bundle by tens to hundreds of MB. Only list known small-data, non-plugin-
# covered packages here.
PACKAGE_DATA_MODULES: dict = {
    "barcode":      "barcode",   # python-barcode ships .ttf fonts (the real fix)
    "python_barcode": "barcode", # pip distribution name -> import name
    "pil":          "PIL",       # safety - PIL is case-sensitive on disk
    "pillow":       "PIL",       # pip distribution name -> import name
    "qrcode":       "qrcode",    # safety; small data, costs nothing
}


# ═════════════════════════════════════════════════════════════════════════════
#  PLATFORM DETECTION  (sys.platform-based; never calls platform.system())
# ═════════════════════════════════════════════════════════════════════════════

def _detect_os():
    p = sys.platform
    if p == "win32":
        arch = (os.environ.get("PROCESSOR_ARCHITEW6432")
                or os.environ.get("PROCESSOR_ARCHITECTURE") or "AMD64")
        return "Windows", arch
    if p == "darwin":
        try:    return "Darwin", os.uname().machine
        except: return "Darwin", "unknown"
    if p.startswith("linux"):
        try:    return "Linux", os.uname().machine
        except: return "Linux", "unknown"
    return p, "unknown"

OS_NAME, OS_ARCH = _detect_os()
IS_WIN = OS_NAME == "Windows"
IS_MAC = OS_NAME == "Darwin"
IS_LIN = OS_NAME == "Linux"


# ═════════════════════════════════════════════════════════════════════════════
#  RAM-AWARE PARALLELISM  (v1.8.2)
#  cpu_count() alone (e.g. 128 on a 3990X) + LTO links exhausted RAM and made
#  zstd onefile compression fail with "not enough memory". Cap jobs by RAM.
# ═════════════════════════════════════════════════════════════════════════════
def _total_ram_gb():
    """Best-effort total physical RAM in GB; None if undetectable."""
    try:
        if IS_LIN:
            with open("/proc/meminfo") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        return int(line.split()[1]) / (1024 * 1024)
        if IS_WIN:
            class _MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            ms = _MS(); ms.dwLength = ctypes.sizeof(_MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
            return ms.ullTotalPhys / (1024 ** 3)
        # macOS / generic POSIX
        return (os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")) / (1024 ** 3)
    except Exception:
        return None


def _safe_jobs(requested):
    """RAM-aware parallel C-job count. An explicit --jobs value is honored
    as-is; otherwise jobs are capped so LTO links (~1.5 GB each) leave ~4 GB
    headroom. Prevents the zstd 'not enough memory' onefile crash on
    high-core / modest-RAM machines."""
    cpu = multiprocessing.cpu_count()
    if requested is not None:
        return max(1, requested)
    ram = _total_ram_gb()
    if not ram:
        return min(cpu, 8)               # unknown RAM: stay conservative
    budget = int((ram - 4) / 1.5)        # ~4 GB headroom, ~1.5 GB per LTO job
    return max(1, min(cpu, budget, 32))  # never below 1, never silly-high


# ═════════════════════════════════════════════════════════════════════════════
#  WINDOWS CONSOLE HARDENING  (UTF-8, QuickEdit off, Ctrl+C handler)
# ═════════════════════════════════════════════════════════════════════════════

_CTRL_HANDLER_REF = None  # keep alive

def _harden_windows_console():
    if not IS_WIN:
        return
    try:
        k32 = ctypes.windll.kernel32
        # UTF-8 codepage
        k32.SetConsoleOutputCP(65001)
        k32.SetConsoleCP(65001)
    except Exception:
        pass
    try:
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        STD_INPUT_HANDLE = -10
        ENABLE_QUICK_EDIT     = 0x0040
        ENABLE_EXTENDED_FLAGS = 0x0080
        ENABLE_INSERT_MODE    = 0x0020
        h = k32.GetStdHandle(STD_INPUT_HANDLE)
        if h not in (0, -1):
            mode = wintypes.DWORD()
            if k32.GetConsoleMode(h, ctypes.byref(mode)):
                new = (mode.value | ENABLE_EXTENDED_FLAGS) & ~ENABLE_QUICK_EDIT & ~ENABLE_INSERT_MODE
                k32.SetConsoleMode(h, new)
    except Exception:
        pass
    try:
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        HANDLER_TYPE = ctypes.CFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        def handler(_ctrl_type):
            try:
                sys.stderr.write("\n[build.py] Ctrl+C - exiting.\n"); sys.stderr.flush()
            except Exception:
                pass
            k32.ExitProcess(130)
            return True

        global _CTRL_HANDLER_REF
        _CTRL_HANDLER_REF = HANDLER_TYPE(handler)
        k32.SetConsoleCtrlHandler(_CTRL_HANDLER_REF, True)
    except Exception:
        pass

_harden_windows_console()


# ═════════════════════════════════════════════════════════════════════════════
#  LOGGING  (console + build.log)
# ═════════════════════════════════════════════════════════════════════════════

_LOG_FH       = None
_LOG_LOCK     = threading.Lock()
_STDOUT_LOCK  = threading.Lock()

def _open_log(project_dir: Path):
    global _LOG_FH
    try:
        _LOG_FH = open(project_dir / "build.log", "w", encoding="utf-8", buffering=1)
        _LOG_FH.write(f"# build.py v{SCRIPT_VERSION} log - pid={os.getpid()} - "
                      f"started {datetime.now().isoformat()}\n")
        _LOG_FH.write(f"# project={project_dir}\n")
        _LOG_FH.flush()
    except Exception as e:
        sys.stderr.write(f"[build.py] could not open build.log: {e}\n")

def _log(msg: str):
    if _LOG_FH is None:
        return
    with _LOG_LOCK:
        try:
            _LOG_FH.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
            _LOG_FH.flush()
        except Exception:
            pass

def say(text: str = "", end: str = "\n"):
    """Write to stdout via os.write to bypass Python I/O buffering."""
    msg = (str(text) + end).encode("utf-8", errors="replace")
    with _STDOUT_LOCK:
        try:
            off = 0
            while off < len(msg):
                n = os.write(1, msg[off:])
                if n <= 0: break
                off += n
        except OSError:
            try: print(text, end=end, flush=True)
            except Exception: pass
    _log(text)

def banner(t: str): say(f"\n{'=' * 60}\n  {t}\n{'=' * 60}")
def step  (t: str): say(f"\n--  {t}")
def info  (t: str): say(f"  [OK] {t}")
def warn  (t: str): say(f"  [!]  {t}")
def error (t: str): say(f"  [X]  {t}")


# ═════════════════════════════════════════════════════════════════════════════
#  CONFIG LOADING  (build_config.toml + pyproject.toml + auto-detect)
# ═════════════════════════════════════════════════════════════════════════════

class Config:
    """Resolved per-project build configuration."""
    def __init__(self):
        self.name:                str   = "App"
        self.version:             str   = "0.0.0"
        self.description:         str   = ""
        self.author:              str   = ""
        self.entry:               str   = "main.py"
        self.plugins:             list  = []
        self.include_qt_plugins:  str   = ""
        self.include_packages:    list  = []
        self.include_package_data:list  = []
        self.include_modules:     list  = []
        self.nofollow_imports:    list  = []
        self.extra_flags:         list  = []
        self.data_files:          list  = []   # list of [src, dst]
        self.data_dirs:           list  = []   # list of str or [src, dst]
        self.icon_win:            str   = ""
        self.icon_mac:            str   = ""
        self.linux_desktop:       str   = ""
        self.lto:                 str   = "auto"  # auto|yes|no
        self.extra_requirements:  list  = []     # always-install in addition to requirements.txt


def _read_toml(path: Path) -> dict:
    if _toml is None:
        warn("tomllib/tomli unavailable - install tomli (`pip install tomli`) or use Python 3.11+")
        return {}
    try:
        with open(path, _TOML_MODE) as fh:
            return _toml.load(fh)
    except Exception as e:
        warn(f"Could not parse {path.name}: {e}")
        return {}


def _autodetect_entry(project_dir: Path) -> str:
    for name in _ENTRY_CANDIDATES:
        if (project_dir / name).is_file():
            return name
    return "main.py"


def _autodetect_icon(project_dir: Path, candidates: list) -> str:
    for rel in candidates:
        if (project_dir / rel).is_file():
            return rel
    return ""


def _pick(key, *sources, default=None):
    """Return first non-empty value for `key` across sources (priority order)."""
    for s in sources:
        if isinstance(s, dict) and key in s:
            v = s[key]
            if v not in (None, "", [], {}):
                return v
    return default


def load_config(project_dir: Path) -> Config:
    """
    Resolve config from two possible sources, in this priority order:

        1.  <project>/build_config.toml                (full schema)
        2.  <project>/pyproject.toml [tool.nuitka_builder]   (full schema)
        3.  <project>/pyproject.toml [project]         (standard fields only)
        4.  Auto-detect (entry point, icon, PySide6 plugin from requirements.txt)

    Both build_config.toml AND pyproject.toml may exist; build_config.toml
    wins on conflicts.
    """
    cfg = Config()
    bc_path = project_dir / CONFIG_FILENAME
    pp_path = project_dir / "pyproject.toml"

    bc       = _read_toml(bc_path) if bc_path.is_file() else {}
    pp       = _read_toml(pp_path) if pp_path.is_file() else {}
    pp_tool  = pp.get("tool", {}).get("nuitka_builder", {}) if isinstance(pp, dict) else {}
    pp_proj  = pp.get("project", {}) if isinstance(pp, dict) else {}

    # Single-source-of-truth guard: build_config.toml WINS over (shadows)
    # pyproject [tool.nuitka_builder]. Having BOTH is the #1 cause of "my build
    # silently dropped files" — warn loudly so a stray --init artifact can't
    # quietly override the intended pyproject config.
    if bc and pp_tool:
        warn("BOTH build_config.toml AND pyproject.toml [tool.nuitka_builder] exist.")
        warn("  -> build_config.toml WINS and is SHADOWING your pyproject config.")
        warn("  -> Standardize on ONE: delete build_config.toml (pyproject is the")
        warn("     recommended single source) or drop the [tool.nuitka_builder] table.")

    if not bc and not pp_tool and not pp_proj:
        warn(f"No {CONFIG_FILENAME} or pyproject.toml found - relying on auto-detect.")

    # ── [app] ────────────────────────────────────────────────────────────
    bc_app, pp_app = bc.get("app", {}), pp_tool.get("app", {})

    cfg.name        = _pick("name",        bc_app, pp_app, pp_proj) or project_dir.name
    cfg.version     = str(_pick("version", bc_app, pp_app, pp_proj) or "0.0.0").lstrip("v")
    cfg.description = _pick("description", bc_app, pp_app, pp_proj) or ""

    pp_authors = pp_proj.get("authors") or []
    pp_author  = (pp_authors[0].get("name", "") if pp_authors else "")
    cfg.author = _pick("author", bc_app, pp_app) or pp_author or "Unknown"
    cfg.entry  = _pick("entry",  bc_app, pp_app) or _autodetect_entry(project_dir)

    # ── [build] ──────────────────────────────────────────────────────────
    bc_b, pp_b = bc.get("build", {}), pp_tool.get("build", {})
    cfg.lto                = _pick("lto",                bc_b, pp_b, default="auto")
    cfg.extra_requirements = _pick("extra_requirements", bc_b, pp_b, default=[]) or []

    # ── [nuitka] ─────────────────────────────────────────────────────────
    bc_n, pp_n = bc.get("nuitka", {}), pp_tool.get("nuitka", {})
    cfg.plugins              = _pick("plugins",              bc_n, pp_n, default=[]) or []
    cfg.include_qt_plugins   = _pick("include_qt_plugins",   bc_n, pp_n, default="") or ""
    cfg.include_packages     = _pick("include_packages",     bc_n, pp_n, default=[]) or []
    cfg.include_package_data = _pick("include_package_data", bc_n, pp_n, default=[]) or []
    cfg.include_modules      = _pick("include_modules",      bc_n, pp_n, default=[]) or []
    cfg.nofollow_imports     = _pick("nofollow_imports",     bc_n, pp_n, default=[]) or []
    cfg.extra_flags          = _pick("extra_flags",          bc_n, pp_n, default=[]) or []
    cfg.data_files           = _pick("data_files",           bc_n, pp_n, default=[]) or []
    cfg.data_dirs            = _pick("data_dirs",            bc_n, pp_n, default=[]) or []

    # ── [icons] ──────────────────────────────────────────────────────────
    bc_i, pp_i = bc.get("icons", {}), pp_tool.get("icons", {})
    cfg.icon_win      = _pick("windows", bc_i, pp_i) or _autodetect_icon(project_dir, _ICON_WIN)
    cfg.icon_mac      = _pick("macos",   bc_i, pp_i) or _autodetect_icon(project_dir, _ICON_MAC)
    cfg.linux_desktop = _pick("linux",   bc_i, pp_i) or ""

    # Auto-detect PySide6 plugin from requirements.txt if still empty
    if not cfg.plugins:
        req = project_dir / REQUIREMENTS_NAME
        if req.is_file():
            try:
                txt = req.read_text(encoding="utf-8", errors="ignore").lower()
                if "pyside6" in txt:
                    cfg.plugins = ["pyside6"]
            except Exception:
                pass

    return cfg


# ═════════════════════════════════════════════════════════════════════════════
#  PYTHON DISCOVERY
# ═════════════════════════════════════════════════════════════════════════════

def find_pythons():
    """Return [(version_tuple, path), ...] sorted highest first."""
    found: dict = {}
    candidates: list = []

    if IS_WIN:
        for ma, mi in COMPATIBLE_PYTHON:
            candidates.append((f"py -{ma}.{mi}", "py-launcher"))
        for ma, mi in COMPATIBLE_PYTHON:
            for base in [
                Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python" / f"Python{ma}{mi}",
                Path(os.environ.get("LOCALAPPDATA", "")) / f"Python{ma}{mi}",
                Path(os.environ.get("ProgramFiles", "")) / f"Python{ma}{mi}",
                Path("C:/Python") / f"Python{ma}{mi}",
            ]:
                exe = base / "python.exe"
                if exe.is_file():
                    candidates.append((str(exe), "direct"))
        candidates += [("python", "generic"), ("python3", "generic")]
    else:
        for ma, mi in COMPATIBLE_PYTHON:
            candidates.append((f"python{ma}.{mi}", "versioned"))
        candidates += [("python3", "generic"), ("python", "generic")]
        if IS_MAC:
            for ma, mi in COMPATIBLE_PYTHON:
                for brew in (f"/opt/homebrew/bin/python{ma}.{mi}",
                             f"/usr/local/bin/python{ma}.{mi}"):
                    if Path(brew).is_file():
                        candidates.append((brew, "homebrew"))

    for exe, _src in candidates:
        try:
            parts = exe.split() if exe.startswith("py ") else [exe]
            v = subprocess.run(parts + ["--version"], capture_output=True,
                               text=True, timeout=5)
            if v.returncode != 0:
                continue
            if parts[0] == "py":
                p = subprocess.run(parts + ["-c", "import sys; print(sys.executable)"],
                                   capture_output=True, text=True, timeout=5)
                actual = p.stdout.strip() if p.returncode == 0 else None
            else:
                actual = exe
            ver_str = v.stdout.strip().split()[-1]
            ma_s, mi_s, *_ = ver_str.split(".")
            ver = (int(ma_s), int(mi_s))
            if MIN_PYTHON <= ver <= MAX_PYTHON and ver not in found:
                if actual and not os.path.isabs(actual):
                    actual = shutil.which(actual) or actual
                if actual:
                    found[ver] = Path(actual)
        except Exception:
            continue
    return sorted(found.items(), key=lambda x: x[0], reverse=True)


def get_best_python(prefer_below: tuple | None = None):
    """
    Pick the best Python.

    Order of preference:
      1. If prefer_below=(M, N): newest STABLE Python < (M, N).
         (Used for `--compiler=mingw64` which Nuitka 4.x blocks on 3.13+.)
      2. Newest STABLE Python (any version).
      3. Newest EXPERIMENTAL Python (with warning).
    Returns (version_tuple, Path) or (None, None) if no compatible Python.
    """
    available = find_pythons()
    if not available:
        return None, None
    stable     = [(v, p) for v, p in available if v not in NUITKA_EXPERIMENTAL]
    experiment = [(v, p) for v, p in available if v in NUITKA_EXPERIMENTAL]

    # 1. Compiler-constrained preference
    if prefer_below and stable:
        constrained = [(v, p) for v, p in stable if v < prefer_below]
        if constrained:
            return constrained[0]   # newest stable below the cap

    # 2. Newest stable
    if stable:
        ver, path = stable[0]
        if experiment and experiment[0][0] > ver:
            sk = experiment[0][0]
            warn(f"Python {sk[0]}.{sk[1]} skipped - Nuitka experimental only.")
            info(f"Using Python {ver[0]}.{ver[1]} for stable build.")
        return ver, path

    # 3. Newest experimental (warn the user)
    ver, path = experiment[0]
    warn(f"Only Python {ver[0]}.{ver[1]} available - Nuitka experimental.")
    warn("Install Python 3.13 for a stable build.")
    return ver, path


def _msvc_available() -> bool:
    """
    Best-effort: True iff Nuitka has a reasonable chance of finding MSVC.
    Checks vswhere (preferred) and cl.exe on PATH.
    """
    if not IS_WIN:
        return False
    if shutil.which("cl"):
        return True
    for env_var in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(env_var, "")
        if not base:
            continue
        vswhere = Path(base) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
        if vswhere.is_file():
            try:
                r = subprocess.run(
                    [str(vswhere), "-latest", "-products", "*",
                     "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                     "-property", "installationPath"],
                    capture_output=True, text=True, timeout=10)
                if r.returncode == 0 and r.stdout.strip():
                    return True
            except Exception:
                pass
    return False


def _install_msvc() -> bool:
    """Auto-install MSVC Build Tools (VCTools workload, ~3 GB) via winget.
    Returns True iff vswhere reports MSVC after the install completes.
    """
    if not IS_WIN:
        return False
    if not shutil.which("winget"):
        error("winget is not on PATH - cannot auto-install MSVC.")
        say("  Manual install: https://visualstudio.microsoft.com/downloads/")
        return False

    step("Installing MSVC Build Tools (VCTools workload, ~3 GB) via winget...")
    info("This typically takes 10-20 minutes. Do not close this window.")
    cmd = [
        "winget", "install",
        "--id", "Microsoft.VisualStudio.2022.BuildTools",
        "-e",
        "--accept-package-agreements",
        "--accept-source-agreements",
        "--override",
        "--quiet --wait "
        "--add Microsoft.VisualStudio.Workload.VCTools "
        "--includeRecommended",
    ]
    try:
        r = subprocess.run(cmd, timeout=1800)   # 30 min cap
    except subprocess.TimeoutExpired:
        error("MSVC install timed out after 30 minutes.")
        return False
    except Exception as e:
        error(f"MSVC install raised: {e}")
        return False

    if r.returncode == 0 and _msvc_available():
        info("MSVC installation complete.")
        return True
    if r.returncode != 0:
        error(f"MSVC install exited with status {r.returncode}.")
    else:
        error("MSVC install reported success but vswhere can't find it.")
    return False


def detect_heavy_c_modules(project_dir: Path, cfg: "Config") -> dict:
    """Scan the project for HEAVY_C_MODULES (packages shipping huge C source).

    Returns a dict {matched_name: bytecode_target}. Empty = nothing found.
    Scanning uses _scan_project_for_packages() - see that helper for the
    sources scanned.
    """
    canon = {k.lower().replace("-", "_"): v for k, v in HEAVY_C_MODULES.items()}
    return _scan_project_for_packages(project_dir, cfg, canon)


def detect_package_data_modules(project_dir: Path, cfg: "Config") -> dict:
    """Scan the project for PACKAGE_DATA_MODULES (packages shipping data files
    like .ttf fonts that Nuitka does not auto-bundle).

    Returns a dict {matched_name: nuitka_output_name}. Empty = nothing found.
    Multiple matched names may point to the same output name (e.g. both
    'pillow' and 'pil' -> 'PIL'); the caller should dedupe on the value.
    """
    canon = {k.lower().replace("-", "_"): v for k, v in PACKAGE_DATA_MODULES.items()}
    return _scan_project_for_packages(project_dir, cfg, canon)


def _scan_project_for_packages(project_dir: Path, cfg: "Config",
                               canon: dict) -> dict:
    """Generic project-dependency scanner.

    Looks for any of `canon`'s keys (normalised lowercase, '-'->'_') in:
      1. requirements.txt
      2. pyproject.toml [project].dependencies and optional-dependencies
      3. Top-level `import` / `from X import ...` of the entry file

    Returns {matched_key: canon[matched_key]} for everything found.
    Cheap (~10ms); no AST, no execution.
    """
    import re
    hits: dict = {}

    def _check(token: str):
        t = token.strip().lower()
        # Strip extras + version specifiers: "foo[extra]>=1.0; python_version<'3.13'"
        for sep in ("[", "<", ">", "=", "!", "~", ";", " ", "\t"):
            if sep in t:
                t = t.split(sep, 1)[0]
        t = t.strip().replace("-", "_")
        if t and t in canon:
            hits[t] = canon[t]

    # 1. requirements.txt
    req = project_dir / REQUIREMENTS_NAME
    if req.is_file():
        try:
            for line in req.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.split("#", 1)[0].strip()
                if line and not line.startswith("-"):
                    _check(line)
        except Exception:
            pass

    # 2. pyproject.toml [project].dependencies + optional-dependencies
    pp_path = project_dir / "pyproject.toml"
    if pp_path.is_file():
        pp = _read_toml(pp_path)
        proj = pp.get("project", {}) if isinstance(pp, dict) else {}
        for dep in proj.get("dependencies", []) or []:
            _check(str(dep))
        for _grp, deps in (proj.get("optional-dependencies", {}) or {}).items():
            for dep in deps or []:
                _check(str(dep))

    # 3. Top-level imports in the entry file (regex; no AST, no execution)
    entry = project_dir / cfg.entry
    if entry.is_file():
        try:
            txt = entry.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(
                r"^\s*(?:from|import)\s+([A-Za-z_][A-Za-z0-9_]*)",
                txt, re.MULTILINE,
            ):
                _check(m.group(1))
        except Exception:
            pass

    return hits



def _resolve_compiler_auto(auto_yes: bool = False) -> str:
    """
    Resolve --compiler=auto to a concrete compiler on Windows.

    Tier 1: MSVC already installed   -> "msvc"
    Tier 2: MSVC missing, user agrees to install + install OK -> "msvc"
    Tier 3: anything else            -> "mingw64" (will use Python <3.13)

    Note: heavy-C modules (pymupdf) do not influence compiler choice.
    They are shipped as bytecode via --noinclude-custom-mode, so the
    giant translation unit is never generated and any compiler is fine.
    """
    if not IS_WIN:
        return ""   # Linux/macOS: no compiler flag, system default is used

    if _msvc_available():
        info("MSVC detected via vswhere - using --compiler=msvc.")
        return "msvc"

    say("")
    banner("MSVC Build Tools not found")
    say("  Auto mode prefers MSVC for best build quality with Python 3.13.")
    say("  Fallback path: MinGW64 + Python 3.12 (~180 MB total).")
    say("")

    do_install = auto_yes
    if not do_install:
        if not sys.stdin.isatty():
            warn("Non-interactive terminal - skipping install prompt.")
            info("Using --compiler=mingw64 (Python 3.12 path).")
            info("To auto-install MSVC non-interactively, pass --yes.")
            return "mingw64"
        try:
            ans = input("  Install MSVC Build Tools (~3 GB, 10-20 min)? "
                        "[y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        do_install = ans in ("y", "yes")

    if not do_install:
        info("Using --compiler=mingw64 (Python 3.12 path).")
        return "mingw64"

    if _install_msvc():
        info("Switching to --compiler=msvc.")
        return "msvc"

    warn("MSVC install failed - falling back to --compiler=mingw64 + Python 3.12.")
    return "mingw64"


def auto_install_python(target: str = "3.13"):
    """Try to install Python <target> via system package manager.
    Returns Path to the installed Python (or matching one), or None on failure.
    """
    step(f"Attempting to install Python {target} via system package manager...")
    try:
        if IS_WIN:
            subprocess.run(["winget", "install", "--id", f"Python.Python.{target}",
                            "-e", "--silent", "--accept-package-agreements",
                            "--accept-source-agreements"], check=True)
        elif IS_MAC:
            subprocess.run(["brew", "install", f"python@{target}"], check=True)
        elif IS_LIN:
            if shutil.which("apt"):
                pkg = f"python{target}"
                subprocess.run(["sudo", "apt", "install", "-y",
                                pkg, f"{pkg}-venv", f"{pkg}-dev"], check=True)
            elif shutil.which("dnf"):
                subprocess.run(["sudo", "dnf", "install", "-y", f"python{target}"], check=True)
            elif shutil.which("pacman"):
                subprocess.run(["sudo", "pacman", "-Sy", "--noconfirm", "python"], check=True)
            else:
                error("No supported Linux package manager."); return None
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        error(f"Auto-install failed: {e}"); return None

    # Prefer the exact version we tried to install
    try:
        target_tuple = tuple(int(x) for x in target.split(".")[:2])
    except Exception:
        target_tuple = None
    for v, p in find_pythons():
        if v == target_tuple:
            return p
    _, p = get_best_python()
    return p


# ═════════════════════════════════════════════════════════════════════════════
#  COMPILER CHECK
# ═════════════════════════════════════════════════════════════════════════════

def check_compiler(compiler: str):
    """Verify a usable C compiler is present (and patchelf on Linux)."""
    step(f"Checking C compiler ({compiler})...")
    if IS_WIN:
        # mingw64 mode: Nuitka can auto-download it on demand
        if compiler == "mingw64":
            info("MinGW64 will be auto-downloaded by Nuitka if missing.")
            return
        if compiler == "msvc":
            if _msvc_available():
                info("MSVC detected (via cl.exe or vswhere)."); return
            error("MSVC not found.")
            say("  Install with: winget install Microsoft.VisualStudio.2022.BuildTools")
            say("  (select 'Desktop development with C++' workload)")
            say("  Or switch to MinGW64: --compiler=mingw64 (needs Python <3.13).")
            sys.exit(1)
        if compiler == "clang":
            if shutil.which("clang"):
                info("clang found."); return
            error("clang not found on PATH.")
            sys.exit(1)
    elif IS_MAC:
        if shutil.which("clang") or shutil.which("gcc"):
            info("Compiler found."); return
        error("Xcode CLI tools not installed. Run: xcode-select --install")
        sys.exit(1)
    elif IS_LIN:
        if not (shutil.which("gcc") or shutil.which("cc")):
            error("gcc not found. apt install build-essential")
            sys.exit(1)
        if not shutil.which("patchelf"):
            error("patchelf missing. apt install patchelf")
            sys.exit(1)
        info("gcc + patchelf found.")


# ═════════════════════════════════════════════════════════════════════════════
#  BUILD ENVIRONMENT
# ═════════════════════════════════════════════════════════════════════════════

def venv_python(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts/python.exe" if IS_WIN else "bin/python")


def _pip(venv_py: Path, *args):
    """Always invoke pip via 'python -m pip' for cross-version safety."""
    cmd = [str(venv_py), "-m", "pip", "install", "--disable-pip-version-check", *args]
    r = subprocess.run(cmd)
    if r.returncode != 0:
        error("pip install failed - see output above.")
        sys.exit(r.returncode)


def _pip_try(venv_py: Path, *args) -> bool:
    """Non-fatal pip install: returns True on success, False on failure.
    Used where we want to attempt a self-repair before giving up."""
    cmd = [str(venv_py), "-m", "pip", "install", "--disable-pip-version-check", *args]
    return subprocess.run(cmd).returncode == 0


def _env_healthy(venv_py: Path) -> bool:
    """Integrity check for a reusable build_env.

    Detects the 'interrupted pip install' failure mode: the venv Python
    still runs, but pip's own package body was gutted. (pip deletes its
    old files before writing the new ones during a self-upgrade; a kill
    mid-write leaves an importable-looking pip whose _internal package
    is empty, so every 'python -m pip ...' then dies with
    ModuleNotFoundError. The same fingerprint appears across any package
    whose reinstall was interrupted.)  A False here means the env is
    corrupt and should be recreated rather than reused."""
    try:
        r = subprocess.run(
            [str(venv_py), "-m", "pip", "--version"],
            capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def setup_build_env(project_dir: Path, cfg: Config, forced_python=None,
                    force_recreate: bool = False, compiler: str = "") -> Path | None:
    """Create or reuse build_env; install Nuitka and requirements.

    `compiler` is used to bias Python selection — MinGW64 needs Python <3.13.
    """
    venv_dir = project_dir / "build_env"
    venv_py  = venv_python(venv_dir)
    step("Preparing build environment...")

    # Compiler-aware Python preference: MinGW64 doesn't work on Python 3.13+
    prefer_below = (3, 13) if (IS_WIN and compiler == "mingw64") else None

    # Integrity gate: an interrupted pip run can leave build_env with a
    # runnable Python but gutted packages (pip/_internal emptied,
    # dist-info orphaned). Such an env passes the version check below but
    # then fails cryptically mid-build. Detect and wipe it up front so we
    # recreate cleanly instead of half-trusting it.
    if (venv_dir.exists() and not force_recreate and venv_py.is_file()
            and not _env_healthy(venv_py)):
        warn("Build env corrupted (pip not runnable) - recreating from scratch.")
        shutil.rmtree(venv_dir, ignore_errors=True)

    # Reuse?
    if venv_dir.exists() and not force_recreate and venv_py.is_file():
        try:
            r = subprocess.run([str(venv_py), "--version"], capture_output=True,
                               text=True, timeout=5)
            ver_str = r.stdout.strip().split()[-1]
            ma_s, mi_s, *_ = ver_str.split(".")
            ver = (int(ma_s), int(mi_s))
            if MIN_PYTHON <= ver <= MAX_PYTHON:
                # Compiler-incompatible: existing venv Python conflicts with chosen compiler
                if prefer_below and ver >= prefer_below:
                    constrained = [v for v, _ in find_pythons()
                                   if v not in NUITKA_EXPERIMENTAL and v < prefer_below]
                    if constrained:
                        warn(f"Existing build env uses Python {ver[0]}.{ver[1]}, "
                             f"but {compiler} needs Python <{prefer_below[0]}.{prefer_below[1]}.")
                        info(f"Rebuilding venv with Python {constrained[0][0]}.{constrained[0][1]}.")
                        shutil.rmtree(venv_dir)
                    else:
                        warn(f"Existing build env uses Python {ver[0]}.{ver[1]} - "
                             f"{compiler} needs <{prefer_below[0]}.{prefer_below[1]}, "
                             f"but no such Python is installed.")
                        info(f"Reusing venv; compiler will auto-fall-back at compile time.")
                        _install_packages(project_dir, venv_py, cfg)
                        return venv_py
                # Rebuild if it's experimental and a stable exists
                elif ver in NUITKA_EXPERIMENTAL:
                    stable = [v for v, _ in find_pythons() if v not in NUITKA_EXPERIMENTAL]
                    if stable:
                        warn(f"Build env uses Python {ver[0]}.{ver[1]} (experimental); rebuilding stable.")
                        shutil.rmtree(venv_dir)
                    else:
                        info(f"Reusing build env (Python {ver_str}).")
                        _install_packages(project_dir, venv_py, cfg)
                        return venv_py
                else:
                    info(f"Reusing build env (Python {ver_str}).")
                    _install_packages(project_dir, venv_py, cfg)
                    return venv_py
        except Exception:
            pass
        warn("Build env invalid - recreating.")
        shutil.rmtree(venv_dir, ignore_errors=True)

    # Pick interpreter
    if forced_python:
        python_exe = Path(forced_python)
        if not python_exe.is_file():
            error(f"Forced Python not found: {forced_python}")
            return None
    else:
        ver, python_exe = get_best_python(prefer_below=prefer_below)
        if ver is None:
            warn("No compatible Python found - attempting auto-install.")
            python_exe = auto_install_python()
            if python_exe is None:
                error("Install Python 3.13 from https://python.org and retry.")
                return None
        elif prefer_below and ver >= prefer_below:
            target = f"{prefer_below[0]}.{prefer_below[1] - 1}"
            warn(f"Compiler '{compiler}' needs Python <{prefer_below[0]}.{prefer_below[1]}, "
                 f"but only Python {ver[0]}.{ver[1]} is installed.")
            info(f"Auto-installing Python {target} for MinGW64 compatibility...")
            new_py = auto_install_python(target)
            if new_py:
                # Verify the new Python actually meets the constraint
                new_ver = _python_version(new_py)
                if new_ver and new_ver < prefer_below:
                    python_exe = new_py
                    info(f"Using newly installed Python {new_ver[0]}.{new_ver[1]}.")
                else:
                    warn(f"Auto-installed Python doesn't meet the <"
                         f"{prefer_below[0]}.{prefer_below[1]} constraint.")
                    info("Build will fall back to MSVC at compile time (if available).")
            else:
                warn(f"Could not auto-install Python {target}.")
                info("Build will fall back to MSVC at compile time (if available).")

    info(f"Creating venv with {python_exe}")
    r = subprocess.run([str(python_exe), "-m", "venv", str(venv_dir)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        error(f"venv creation failed: {r.stderr}")
        return None
    if not venv_py.is_file():
        error(f"venv missing Python at {venv_py}")
        return None

    _install_packages(project_dir, venv_py, cfg)
    return venv_py


def _patch_nuitka_av_retries(venv_py: Path):
    """Widen Nuitka's Windows retry window so EDR doesn't kill the build.

    CylancePROTECT (and similar EDR) holds freshly linked exes for ~60+ s,
    which makes Nuitka's resource-embedding step fail every time with
    "Failed to add resources to file ... the result is unusable" — its stock
    retry window (decoratorRetries attempts=5, sleep_time=1) is far too
    short. A verified Thrift_Reseller build needed 34 attempts. Patch the
    env's Nuitka copy to 40 x 2 s. Idempotent; runs on every env setup so a
    --clean-env rebuild or Nuitka upgrade is re-patched automatically.
    """
    if os.name != "nt":
        return
    r = subprocess.run(
        [str(venv_py), "-c",
         "import nuitka.utils.Utils as u; print(u.__file__)"],
        capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        warn("Could not locate nuitka.utils.Utils - AV retry patch skipped.")
        return
    utils_py = Path(r.stdout.strip().splitlines()[-1])
    try:
        src = utils_py.read_text(encoding="utf-8")
    except OSError as e:
        warn(f"AV retry patch skipped ({e})")
        return
    if "attempts=40," in src:
        return  # already patched
    stock = "    attempts=5,\n    sleep_time=1,"
    patched = (
        "    # Patched by build.py: EDR (e.g. CylancePROTECT) holds freshly\n"
        "    # linked exes longer than the stock 5x1s window - 40x2s rides\n"
        "    # out the scan so resource embedding stops failing.\n"
        "    attempts=40,\n"
        "    sleep_time=2,"
    )
    if stock not in src:
        warn("Nuitka decoratorRetries layout changed - AV retry patch NOT "
             "applied (builds may fail under EDR; see HELP.md).")
        return
    try:
        utils_py.write_text(src.replace(stock, patched, 1),
                            encoding="utf-8", newline="")
    except OSError as e:
        warn(f"AV retry patch failed to write ({e})")
        return
    info("Patched Nuitka AV retry window (5x1s -> 40x2s) for EDR/Cylance.")


def _install_packages(project_dir: Path, venv_py: Path, cfg: Config):
    """Install pip + Nuitka + requirements.txt + cfg.extra_requirements."""
    info("Upgrading pip/setuptools/wheel...")
    if not _pip_try(venv_py, "--upgrade", "pip", "setuptools", "wheel"):
        # A prior interrupted upgrade can leave pip itself broken. Restore
        # it from the bundled wheel via ensurepip, then retry once before
        # giving up. Turns a hard, cryptic failure into a self-heal.
        warn("pip upgrade failed - repairing pip via ensurepip and retrying...")
        subprocess.run([str(venv_py), "-m", "ensurepip", "--upgrade"], check=False)
        _pip(venv_py, "--upgrade", "pip", "setuptools", "wheel")

    # Nuitka core
    r = subprocess.run([str(venv_py), "-m", "nuitka", "--version"],
                       capture_output=True, text=True)
    if r.returncode == 0:
        info(f"Nuitka present: {r.stdout.strip().splitlines()[0]}")
    else:
        info("Installing Nuitka + runtime deps...")
        _pip(venv_py, "nuitka", "ordered-set", "zstandard")

    # requirements.txt
    req = project_dir / REQUIREMENTS_NAME
    if req.is_file():
        info("Installing requirements.txt...")
        _pip(venv_py, "-r", str(req))
    else:
        warn("No requirements.txt found - skipping.")

    # extra_requirements from TOML
    if cfg.extra_requirements:
        info(f"Installing extra_requirements ({len(cfg.extra_requirements)})...")
        _pip(venv_py, *cfg.extra_requirements)

    # Strip PyQt6 if PySide6 is in use (they conflict in Nuitka)
    if "pyside6" in [p.lower() for p in cfg.plugins]:
        if subprocess.run([str(venv_py), "-c", "import PyQt6"],
                          capture_output=True).returncode == 0:
            info("Removing PyQt6 (conflicts with PySide6)...")
            subprocess.run([str(venv_py), "-m", "pip", "uninstall", "-y",
                            "PyQt6", "PyQt6-Qt6", "PyQt6-sip"],
                           capture_output=True)

    # EDR on this host holds fresh exes past Nuitka's stock retry window
    _patch_nuitka_av_retries(venv_py)
    info("Build environment ready.")


# ═════════════════════════════════════════════════════════════════════════════
#  NUITKA COMMAND
# ═════════════════════════════════════════════════════════════════════════════

def _nuitka_env() -> dict:
    env = os.environ.copy()
    env["CLCACHE_DISABLE"] = "1"
    return env


def _python_version(py: Path) -> tuple | None:
    """Return (major, minor) for the given Python exe; None on failure."""
    try:
        r = subprocess.run(
            [str(py), "-c",
             "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
            capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            ma, mi = r.stdout.strip().split(".")
            return (int(ma), int(mi))
    except Exception:
        pass
    return None


def build_nuitka_command(project_dir: Path, venv_py: Path, cfg: Config,
                         onefile: bool, compiler: str, jobs: int) -> list:
    build_dir = project_dir / "build"

    # ── Nuitka constraint: --mingw64 is BLOCKED on Python 3.13+ ───────────
    # Auto-fallback to MSVC keeps the build succeeding without user action.
    # If MSVC isn't installed, abort with actionable instructions.
    if IS_WIN and compiler == "mingw64":
        venv_ver = _python_version(venv_py)
        if venv_ver and venv_ver >= (3, 13):
            warn(f"MinGW64 unsupported on Python {venv_ver[0]}.{venv_ver[1]} "
                 f"(Nuitka 4.x constraint).")
            if _msvc_available():
                info("MSVC detected via vswhere - auto-fallback to --compiler=msvc.")
                compiler = "msvc"
            else:
                error("Neither MinGW64 (blocked by Nuitka) nor MSVC is usable.")
                say("")
                say("  Pick ONE of these to fix:")
                say("")
                say("  OPTION 1 - install Python 3.12 to keep MinGW64 (smaller download):")
                say("    winget install Python.Python.3.12")
                say(f"    python build.py <project> --clean-env")
                say("")
                say("  OPTION 2 - install Visual Studio Build Tools (larger, ~6 GB):")
                say("    winget install Microsoft.VisualStudio.2022.BuildTools")
                say("    (select 'Desktop development with C++' workload during install)")
                say(f"    python build.py <project>")
                say("")
                sys.exit(1)

    cmd = [
        str(venv_py), "-m", "nuitka",
        f"--output-dir={build_dir}",
        f"--output-filename={cfg.name}",
    ]

    # ── Output mode ──────────────────────────────────────────────────────
    if onefile:
        cmd += [
            "--onefile",
            "--onefile-tempdir-spec=" +
            (f"{{TEMP}}/{cfg.name}" if IS_WIN else f"/tmp/{cfg.name}"),
        ]
    else:
        cmd.append("--standalone")

    # ── Plugins ──────────────────────────────────────────────────────────
    for plugin in cfg.plugins:
        cmd.append(f"--enable-plugin={plugin}")
    if cfg.include_qt_plugins:
        # Normalize the Qt plugin set before handing it to Nuitka (v1.8.7).
        # Qt 6.11 removed the standalone "printsupport" plugin family (folded
        # into the platform plugin). Nuitka's "sensible" set already pulls in
        # printsupport wherever it still exists - it is gated on hasPluginFamily
        # - so naming it explicitly is redundant on every Qt version AND a hard
        # FATAL on 6.11+ ("no Qt plugin family 'printsupport'"). Strip it here so
        # older configs still carrying "sensible,printsupport" keep building with
        # no per-project edit and no printing regression.
        _families = [p.strip() for p in cfg.include_qt_plugins.split(",")
                     if p.strip()]
        if "printsupport" in _families:
            _families = [f for f in _families if f != "printsupport"]
            warn("Dropping redundant Qt plugin family 'printsupport' from "
                 "include_qt_plugins ('sensible' already bundles it; an "
                 "explicit name FATALs on Qt 6.11+).")
        if _families:
            cmd.append(f"--include-qt-plugins={','.join(_families)}")

    # ── Packages / modules / data ────────────────────────────────────────
    for pkg in cfg.include_packages:
        cmd.append(f"--include-package={pkg}")
    for pkg in cfg.include_package_data:
        cmd.append(f"--include-package-data={pkg}")
    for mod in cfg.include_modules:
        cmd.append(f"--include-module={mod}")
    # nofollow: only modules the user explicitly declared in build_config.toml.
    # The script does NOT auto-add nofollow for heavy-C modules - excluding a
    # module breaks a standalone build (ImportError at runtime). Heavy-C
    # modules are handled by routing to MinGW64, which compiles them.
    for imp in cfg.nofollow_imports:
        if imp:
            cmd.append(f"--nofollow-import-to={imp}")

    for entry in cfg.data_files:
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            src, dst = entry
            src_path = (project_dir / src).resolve()
            if src_path.exists():
                cmd.append(f"--include-data-files={src_path}={dst}")
            else:
                warn(f"Data file not found, skipping: {src}")

    # data_dirs: each entry is either "src" (dst=src) or ["src", "dst"]
    for entry in cfg.data_dirs:
        if isinstance(entry, str):
            src, dst = entry, entry
        elif isinstance(entry, (list, tuple)) and len(entry) == 2:
            src, dst = entry
        else:
            warn(f"Malformed data_dirs entry, skipping: {entry}")
            continue
        src_path = (project_dir / src).resolve()
        if src_path.is_dir():
            cmd.append(f"--include-data-dir={src_path}={dst}")
        else:
            warn(f"Data dir not found, skipping: {src}")

    # ── LTO ──────────────────────────────────────────────────────────────
    if cfg.lto == "yes":
        lto = "yes"
    elif cfg.lto == "no":
        lto = "no"
    else:  # auto
        # LTO holds the whole program in memory during the final link.
        # On Windows+MSVC it can exhaust LTCG; keep it off there. Heavy-C
        # modules are now handled by --nofollow-import-to (the giant TU is
        # never compiled), so LTO is no longer the heavy-module concern it
        # was - but staying conservative on Win+MSVC costs little.
        lto = "no" if ((IS_WIN and compiler == "msvc") or IS_MAC) else "yes"
    cmd.append(f"--lto={lto}")

    cmd += [
        f"--jobs={jobs}",
        "--assume-yes-for-downloads",
        "--python-flag=no_site",
    ]

    # ── Metadata ─────────────────────────────────────────────────────────
    cmd += [
        f"--product-name={cfg.name}",
        f"--product-version={cfg.version}",
        f"--file-description={cfg.description or cfg.name}",
        f"--company-name={cfg.author or cfg.name}",
        f"--copyright=Copyright (C) {datetime.now().year} {cfg.author or cfg.name}",
    ]

    # ── Platform-specific ────────────────────────────────────────────────
    if IS_WIN:
        cmd.append("--windows-console-mode=disable")
        cmd.append(f"--file-version={cfg.version}")
        if compiler == "mingw64":
            cmd.append("--mingw64")
        elif compiler == "clang":
            cmd.append("--clang")
        else:  # msvc
            cmd.append("--msvc=latest")
            cmd.append("--nowarn-mnemonic=dll-files")
        if cfg.icon_win and (project_dir / cfg.icon_win).is_file():
            cmd.append(f"--windows-icon-from-ico={project_dir / cfg.icon_win}")
    elif IS_MAC:
        if not onefile:
            cmd += [
                "--macos-create-app-bundle",
                f"--macos-app-name={cfg.name}",
                f"--macos-app-version={cfg.version.rsplit('.', 1)[0]}",
            ]
            if cfg.icon_mac and (project_dir / cfg.icon_mac).is_file():
                cmd.append(f"--macos-app-icon={project_dir / cfg.icon_mac}")
    elif IS_LIN:
        if cfg.linux_desktop and (project_dir / cfg.linux_desktop).is_file():
            cmd.append(f"--include-data-files={project_dir / cfg.linux_desktop}="
                       f"{Path(cfg.linux_desktop).name}")

    # ── Extra raw flags ──────────────────────────────────────────────────
    cmd += list(cfg.extra_flags)

    # ── Entry point ──────────────────────────────────────────────────────
    cmd.append(str(project_dir / cfg.entry))
    return cmd


# ═════════════════════════════════════════════════════════════════════════════
#  BUILD EXECUTION + OUTPUT COLLECTION
# ═════════════════════════════════════════════════════════════════════════════

def run_build(project_dir: Path, cfg: Config, onefile: bool, compiler: str,
              jobs: int | None = None, forced_python=None, ci: bool = False,
              auto_yes: bool = False, force_msvc: bool = False) -> bool:
    # ── Heavy-C module detection ──────────────────────────────────────────
    # Packages that ship a huge SWIG-generated wrapper (pymupdf) - see the
    # HEAVY_C_MODULES block near the top of this file for the full story.
    heavy_c = detect_heavy_c_modules(project_dir, cfg)

    # ── Package-data module detection ─────────────────────────────────────
    # Packages that ship data files (fonts, templates) that Nuitka does not
    # auto-bundle - see PACKAGE_DATA_MODULES block for the full story.
    pkg_data = detect_package_data_modules(project_dir, cfg)

    # ── Resolve the compiler ──────────────────────────────────────────────
    # Precedence: --force-msvc > --compiler=auto resolution > explicit value.
    if force_msvc and IS_WIN:
        if compiler not in ("auto", "msvc"):
            warn(f"--force-msvc overrides --compiler={compiler}.")
        compiler = "msvc"
    elif force_msvc and not IS_WIN:
        warn("--force-msvc is Windows-only; ignoring on this platform.")
        if compiler == "auto":
            compiler = _resolve_compiler_auto(auto_yes=auto_yes)
    elif compiler == "auto":
        compiler = _resolve_compiler_auto(auto_yes=auto_yes)

    # ── Heavy-C: ship as Nuitka bytecode instead of compiling to C ────────
    # Inject --noinclude-custom-mode=<target>:bytecode for each heavy module.
    # Nuitka then bundles the target submodule as .pyc; CPython interprets it
    # at runtime; the prebuilt .pyd that pymupdf actually calls is bundled
    # by the dll-files plugin as usual. No giant C is generated, both MSVC
    # and MinGW64 handle the rest of the build at normal speed.
    bytecode_flags: list = []
    nofollow_conflicts: list = []
    if heavy_c:
        targets = sorted(set(heavy_c.values()))
        for t in targets:
            bytecode_flags.append(f"--noinclude-custom-mode={t}:bytecode")
            if t in set(cfg.nofollow_imports):
                nofollow_conflicts.append(t)
        cfg.extra_flags = list(cfg.extra_flags) + bytecode_flags

    # ── Package-data: bundle non-Python files inside known data-shipping pkgs
    # Inject --include-package-data=<name> for each. Dedupe on the output name
    # since multiple aliases (e.g. pillow + pil) may map to the same package.
    package_data_flags: list = []
    if pkg_data:
        targets = sorted(set(pkg_data.values()))
        package_data_flags = [f"--include-package-data={t}" for t in targets]
        cfg.extra_flags = list(cfg.extra_flags) + package_data_flags

    jobs = _safe_jobs(jobs)

    mode = "One-File" if onefile else "Standalone Folder"
    banner(f"{cfg.name} v{cfg.version} - Nuitka Build ({mode})")
    say(f"  Project   : {project_dir}")
    say(f"  Platform  : {OS_NAME} {OS_ARCH}")
    say(f"  Compiler  : {compiler if IS_WIN else 'system default'}")
    if heavy_c:
        say(f"  Heavy-C   : {', '.join(sorted(heavy_c))}")
        say(f"              shipped as bytecode (no C compile of wrapper)")
        say(f"              " + "  ".join(bytecode_flags))
    if pkg_data:
        say(f"  Pkg-data  : {', '.join(sorted(set(pkg_data.values())))}")
        say(f"              " + "  ".join(package_data_flags))
    say(f"  Jobs      : {jobs}")
    say(f"  Entry     : {cfg.entry}\n")

    if nofollow_conflicts:
        warn(f"Conflict: nofollow_imports declares "
             f"{', '.join(nofollow_conflicts)} but bytecode mode needs to "
             f"include them.")
        warn("Remove those entries from build_config.toml's nofollow_imports")
        warn("(they were the discarded v1.6.0 workaround).")

    check_compiler(compiler)

    if ci:
        venv_py = Path(sys.executable)
        info(f"CI mode - using current Python: {venv_py}")
        _install_packages(project_dir, venv_py, cfg)
    else:
        venv_py = setup_build_env(project_dir, cfg, forced_python=forced_python,
                                  compiler=compiler)
        if venv_py is None:
            error("Build aborted - no compatible Python.")
            return False

    r = subprocess.run([str(venv_py), "--version"], capture_output=True, text=True)
    info(f"Compiling with: {r.stdout.strip()}")

    cmd = build_nuitka_command(project_dir, venv_py, cfg, onefile, compiler, jobs)
    step("Nuitka command")
    say("  " + " \\\n    ".join(cmd[:4]))
    for arg in cmd[4:]:
        say(f"    {arg} \\")
    say("")
    info("Compiling (typically 5-15 min)...")
    say("-" * 60)

    # Stream Nuitka's stdout/stderr line-by-line so it lands in BOTH the
    # console and build.log. subprocess.run() inherited the console only,
    # which left build.log with no diagnostic detail after a failure.
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(project_dir), env=_nuitka_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            say(line.rstrip("\r\n"))
        proc.wait()
        returncode = proc.returncode
    except Exception as e:
        error(f"Failed to launch Nuitka: {e}")
        returncode = 1

    if returncode != 0:
        banner("BUILD FAILED")
        say("  Full compiler output captured above and in build.log.")
        say("  Try:  python build.py <project> --clean --clean-env --onefile")
        if heavy_c:
            say(f"        Heavy-C modules detected ({', '.join(sorted(heavy_c))})")
            say("        are shipped as bytecode (no C compile). If the build")
            say("        or runtime broke specifically because of this, the")
            say("        next fallback is the PyInstaller backend.")
        if IS_WIN and compiler == "mingw64":
            say("        Early header errors (corecrt.h)? Nuitka's GCC")
            say("        download may be corrupt - delete and re-download:")
            say('          %LOCALAPPDATA%\\Nuitka\\Nuitka\\Cache\\downloads\\gcc')
        return False

    step("Collecting output...")
    dist_dir  = project_dir / "dist"
    build_dir = project_dir / "build"
    dist_dir.mkdir(parents=True, exist_ok=True)
    exe_ext = ".exe" if IS_WIN else ""
    ok = (_collect_onefile(cfg.name, build_dir, dist_dir, exe_ext) if onefile
          else _collect_folder(cfg.name, build_dir, dist_dir))
    if not ok:
        banner("BUILD FAILED - executable not found after compile.")
        return False

    total = sum(p.stat().st_size for p in dist_dir.rglob("*") if p.is_file())
    banner("BUILD SUCCESSFUL")
    say(f"  Output: {dist_dir}")
    say(f"  Size  : {total / (1024 * 1024):.1f} MB\n")
    return True


def _collect_onefile(name: str, build_dir: Path, dist_dir: Path, exe_ext: str) -> bool:
    target = dist_dir / f"{name}{exe_ext}"
    for root, _dirs, files in os.walk(build_dir):
        for fname in files:
            if fname.endswith(exe_ext) and (
                name.lower() in fname.lower() or "main" in fname.lower()
            ) and ".onefile-build" not in root:
                src = Path(root) / fname
                # Skip helper exes shipped in the dist folder
                if src.parent.name.endswith(".dist"):
                    continue
                shutil.copy2(src, target)
                info(f"Executable: {target}")
                return True
    # Fallback: any *.exe at top of build_dir
    for f in build_dir.glob(f"*{exe_ext}"):
        shutil.copy2(f, target)
        info(f"Executable: {target}")
        return True
    error("Could not locate compiled executable.")
    return False


def _collect_folder(name: str, build_dir: Path, dist_dir: Path) -> bool:
    for item in build_dir.iterdir():
        if item.is_dir() and (
            name.lower() in item.name.lower() or "main" in item.name.lower()
        ) and item.name.endswith(".dist"):
            dst = dist_dir / name
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(item, dst)
            info(f"App folder: {dst}")
            return True
    error("Could not find standalone .dist folder.")
    return False


# ═════════════════════════════════════════════════════════════════════════════
#  SMOKE TEST, CLEAN, INFO
# ═════════════════════════════════════════════════════════════════════════════

def smoke_test(project_dir: Path, cfg: Config) -> bool:
    exe_ext = ".exe" if IS_WIN else ""
    dist = project_dir / "dist"
    exe = dist / cfg.name / f"{cfg.name}{exe_ext}"
    if not exe.exists():
        exe = dist / f"{cfg.name}{exe_ext}"
    if not exe.exists():
        warn("Executable not found - run a build first.")
        return False
    info(f"Launching: {exe}")
    info("Will auto-pass after 30s.")
    try:
        return subprocess.run([str(exe)], timeout=30).returncode == 0
    except subprocess.TimeoutExpired:
        info("Timed out - app is running, test passed.")
        return True


def clean(project_dir: Path, include_env: bool = False):
    step("Cleaning build artifacts...")
    for sub in ("build", "dist"):
        d = project_dir / sub
        if d.exists():
            shutil.rmtree(d); info(f"Removed {d}")
    # Nuitka cache folders
    for child in project_dir.iterdir():
        if child.is_dir() and any(child.name.endswith(s)
                                  for s in (".build", ".dist", ".onefile-build")):
            shutil.rmtree(child); info(f"Removed {child}")
    if include_env:
        env = project_dir / "build_env"
        if env.exists():
            shutil.rmtree(env); info(f"Removed {env}")
    info("Clean complete.")


def _toml_array(items: list) -> str:
    """Format a Python list of strings as a TOML inline array."""
    return "[" + ", ".join(f'"{x}"' for x in items) + "]"


def _detect_package_data_dirs(project_dir: Path, asset_exts: set) -> list:
    """Asset dirs nested INSIDE Python packages (e.g. ajj3_brain/console/web).

    Nuitka follows a package's .py files but does NOT bundle non-.py data
    living inside it, so a browser/console UI shipped under a package is
    silently dropped from the binary - the standalone app then 404s on its
    own assets. Walk only the importable package tree (dirs with __init__.py)
    and return POSIX-relative paths of every non-package subdir holding asset
    files. Top-level non-package dirs (config/, docs/) are NOT touched.
    """
    found: list = []

    def _has_asset(d: Path) -> bool:
        return any(p.is_file() and p.suffix.lower() in asset_exts
                   for p in d.rglob("*"))

    def _walk_package(pkg: Path):
        for child in sorted(pkg.iterdir()):
            if (not child.is_dir() or child.name.startswith(".")
                    or child.name == "__pycache__"):
                continue
            if (child / "__init__.py").is_file():
                _walk_package(child)               # deeper sub-package
            elif _has_asset(child):
                found.append(child.relative_to(project_dir).as_posix())

    for top in sorted(project_dir.iterdir()):
        if top.is_dir() and (top / "__init__.py").is_file():
            _walk_package(top)
    return found


def init_config(project_dir: Path, force: bool = False,
                target: str = "pyproject", reset: bool = False) -> bool:
    """
    Generate a starter config from project introspection.

    target = "build_config" -> writes <project>/build_config.toml
    target = "pyproject"    -> appends [tool.nuitka_builder.*] to pyproject.toml

    reset = True            -> ignore any existing config entirely and
                               regenerate from detection/defaults (no
                               preservation of entry/data_dirs/data_files/
                               include_qt_plugins). Implies overwrite.
    """
    banner(f"Generating config for {project_dir.name}")

    # ── Probe pyproject.toml for existing metadata ────────────────────────
    pp_path = project_dir / "pyproject.toml"
    pp = _read_toml(pp_path) if pp_path.is_file() else {}
    pp_proj = pp.get("project", {}) if isinstance(pp, dict) else {}
    pp_tool = pp.get("tool", {}).get("nuitka_builder", {}) if isinstance(pp, dict) else {}

    # ── Auto-detect everything safe to detect ─────────────────────────────
    name        = pp_proj.get("name") or project_dir.name
    version     = str(pp_proj.get("version", "0.1.0")).lstrip("v")
    description = pp_proj.get("description", "")
    authors     = pp_proj.get("authors") or []
    author      = (authors[0].get("name", "") if authors else "") or "Unknown"
    entry       = _autodetect_entry(project_dir)
    icon_win    = _autodetect_icon(project_dir, _ICON_WIN)
    icon_mac    = _autodetect_icon(project_dir, _ICON_MAC)

    # ── Preserve curated values from an existing build_config.toml ─────────
    # So `--force --init` REFRESHES detection without discarding hand-added
    # entries (custom data_dirs like config/ or console/web, an explicit entry,
    # or [src,dst] data_files). Auto-detection only fills what isn't already set.
    # `--reset` skips this entirely: nothing is read from the old file, so the
    # config is rebuilt purely from detection + current defaults.
    _existing = {}
    if not reset:
        if target == "build_config":
            _bc_path = project_dir / CONFIG_FILENAME
            if _bc_path.is_file():
                _existing = _read_toml(_bc_path)
        elif target == "pyproject":
            # Preserve curated values from an existing [tool.nuitka_builder] so
            # --init --force RE-DETECTS without discarding hand-added data_dirs.
            _existing = pp_tool if isinstance(pp_tool, dict) else {}
    if reset:
        warn("--reset: ignoring existing config; entry, data_dirs, data_files "
             "and include_qt_plugins are regenerated from detection/defaults.")
    _ex_app = _existing.get("app", {})    if isinstance(_existing, dict) else {}
    _ex_nk  = _existing.get("nuitka", {}) if isinstance(_existing, dict) else {}
    if _ex_app.get("entry"):
        entry = _ex_app["entry"]
    _preserved_dirs  = _ex_nk.get("data_dirs")  or None
    _preserved_files = _ex_nk.get("data_files") or None
    _preserved_qt    = _ex_nk.get("include_qt_plugins") or None
    # Self-heal a pre-1.8.7 preserved value: drop the stale "printsupport" token
    # so even --force (which preserves curated values) rewrites the file clean.
    # Qt 6.11 removed that family; build-time normalization strips it regardless.
    if _preserved_qt and "printsupport" in _preserved_qt:
        _kept = [p.strip() for p in _preserved_qt.split(",")
                 if p.strip() and p.strip() != "printsupport"]
        _preserved_qt = ",".join(_kept) or None

    # GUI plugin detection from requirements.txt
    plugins = []
    req_file = project_dir / REQUIREMENTS_NAME
    if req_file.is_file():
        try:
            txt = req_file.read_text(encoding="utf-8", errors="ignore").lower()
            if   "pyside6" in txt: plugins.append("pyside6")
            elif "pyqt6"   in txt: plugins.append("pyqt6")
            elif "pyqt5"   in txt: plugins.append("pyqt5")
            elif "tkinter" in txt: plugins.append("tk-inter")
        except Exception:
            pass

    # Qt plugin set: always Nuitka's "sensible" group (v1.8.7). It already
    # bundles the "printsupport" family wherever Qt still ships it (gated on
    # hasPluginFamily), so QtPrintSupport printing keeps working without naming
    # it. Do NOT append ",printsupport": Qt 6.11 removed that standalone family,
    # and an explicit-but-absent family is a FATAL ("no Qt plugin family
    # 'printsupport'"). "all" is likewise avoided - it drags in the QML tree that
    # breaks Linux builds. A real QML app sets "sensible,qml"/"all" by hand.
    qt_default = "sensible"

    # Asset directory detection
    asset_names = ("assets", "resources", "data", "themes", "static", "icons")
    asset_exts  = {".qss", ".svg", ".png", ".jpg", ".ico", ".icns", ".json",
                   ".yaml", ".yml", ".css", ".html", ".js", ".ttf", ".otf"}
    detected_dirs = []
    for n in asset_names:
        d = project_dir / n
        if d.is_dir() and any(f.is_file() and f.suffix.lower() in asset_exts
                              for f in d.rglob("*")):
            detected_dirs.append(n)
    # Also bundle asset dirs nested inside packages (e.g. ajj3_brain/console/web)
    # that Nuitka would otherwise leave out of the binary.
    for rel in _detect_package_data_dirs(project_dir, asset_exts):
        if rel not in detected_dirs:
            detected_dirs.append(rel)

    # Top-level docs
    doc_names = ("README.md", "README.txt", "LICENSE", "LICENSE.txt",
                 "LICENSE.md", "CHANGELOG.md")
    detected_files = [f.name for f in project_dir.iterdir()
                      if f.is_file() and f.name in doc_names]

    say(f"  Detected name        : {name}")
    say(f"  Detected version     : {version}")
    say(f"  Detected entry       : {entry}")
    say(f"  Detected plugins     : {plugins or '(none)'}")
    if any(p in plugins for p in ("pyside6", "pyqt6", "pyqt5")):
        say(f"  Qt plugin set        : {qt_default}")
    say(f"  Detected asset dirs  : {detected_dirs or '(none)'}")
    say(f"  Detected doc files   : {detected_files or '(none)'}")
    say(f"  Detected icon (Win)  : {icon_win or '(none)'}")
    say(f"  Detected icon (mac)  : {icon_mac or '(none)'}")
    say("")

    # ── Render TOML body (shared between both targets) ────────────────────
    if target == "pyproject":
        prefix = "tool.nuitka_builder."
    else:
        prefix = ""

    L = []
    L.append(f"[{prefix}app]")
    L.append(f'name        = "{name}"')
    L.append(f'version     = "{version}"')
    L.append(f'description = "{description}"')
    L.append(f'author      = "{author}"')
    L.append(f'entry       = "{entry}"')
    L.append("")
    L.append(f"[{prefix}build]")
    L.append('# "yes" | "no" | "auto" (auto = off on Windows+MSVC, on elsewhere)')
    L.append('lto = "auto"')
    L.append("")
    L.append(f"[{prefix}nuitka]")
    if plugins:
        L.append(f"plugins            = {_toml_array(plugins)}")
        if "pyside6" in plugins or "pyqt6" in plugins or "pyqt5" in plugins:
            # Default to Nuitka's "sensible" set (NOT "all"). "all" drags in the
            # Qt qml plugin tree, which ships stray .cpp.o object files; Nuitka's
            # Linux rpath step then runs patchelf on them and aborts ("wrong ELF
            # type"). "sensible" already bundles the printsupport family wherever
            # Qt ships it, so printing works on every OS without naming it - Qt
            # 6.11 removed the standalone family and naming it would FATAL. A real
            # QML app can set "sensible,qml" or "all" by hand; --force keeps it.
            L.append(f'include_qt_plugins = "{_preserved_qt or qt_default}"')
    else:
        L.append('# plugins = ["pyside6"]   # uncomment if using Qt')
    L.append("")
    L.append("# Add dynamic-import safety nets here if Nuitka misses modules:")
    L.append("include_packages = []")
    L.append("include_modules  = []")
    L.append("")
    L.append("# nofollow_imports = exclude modules from the build entirely.")
    L.append("# WARNING: a nofollow'd module is NOT bundled - importing it in")
    L.append("# a standalone build raises ImportError at runtime. Use only for")
    L.append("# modules genuinely not needed at runtime. pymupdf is handled")
    L.append("# automatically (build routes to MinGW64); do NOT list it here.")
    L.append("nofollow_imports = []")
    L.append("")
    _dirs = _preserved_dirs if _preserved_dirs is not None else detected_dirs
    if _dirs:
        if _preserved_dirs is not None:
            L.append("# Preserved from your existing build_config.toml")
        else:
            L.append(f"# Auto-detected from project: {', '.join(detected_dirs)}")
        _parts = []
        for _d in _dirs:
            if isinstance(_d, (list, tuple)) and len(_d) == 2:
                _parts.append(f'["{_d[0]}", "{_d[1]}"]')
            else:
                _parts.append(f'"{_d}"')
        L.append("data_dirs        = [" + ", ".join(_parts) + "]")
    else:
        L.append("data_dirs        = []")
    L.append("")
    _files = _preserved_files if _preserved_files is not None else \
        [[f, f] for f in detected_files]
    if _files:
        L.append("data_files       = [")
        for _f in _files:
            if isinstance(_f, (list, tuple)) and len(_f) == 2:
                L.append(f'    ["{_f[0]}", "{_f[1]}"],')
            else:
                L.append(f'    ["{_f}", "{_f}"],')
        L.append("]")
    else:
        L.append("data_files       = []")
    L.append("")
    if icon_win or icon_mac:
        L.append(f"[{prefix}icons]")
        if icon_win: L.append(f'windows = "{icon_win}"')
        if icon_mac: L.append(f'macos   = "{icon_mac}"')
    else:
        L.append(f"# [{prefix}icons]")
        L.append('# windows = "assets/icon.ico"')
        L.append('# macos   = "assets/icon.icns"')
    L.append("")

    body = "\n".join(L)

    # ── Write to target ──────────────────────────────────────────────────
    if target == "pyproject":
        if not pp_path.is_file():
            error("pyproject.toml does not exist - can't append [tool.nuitka_builder].")
            say("  Either: 1) create pyproject.toml first, or")
            say("          2) re-run without --target=pyproject to create build_config.toml")
            return False
        if pp_tool:
            if not force:
                error("[tool.nuitka_builder] already exists in pyproject.toml.")
                say("  Use --force to overwrite (existing keys will be appended,")
                say("  TOML parser will reject duplicates - prefer manual merge).")
                return False
            warn("--force given; appending despite existing [tool.nuitka_builder].")
        try:
            existing = pp_path.read_text(encoding="utf-8")
        except Exception as e:
            error(f"Could not read pyproject.toml: {e}")
            return False
        # Ensure separation between existing content and our append
        sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
        header = (f"# ─────────────────────────────────────────────────────────\n"
                  f"# Generated by build.py v{SCRIPT_VERSION} --init "
                  f"on {datetime.now().strftime('%Y-%m-%d')}\n"
                  f"# Review and edit as needed. "
                  f"Validate with: python build.py . --audit\n"
                  f"# ─────────────────────────────────────────────────────────\n")
        try:
            pp_path.write_text(existing + sep + header + body, encoding="utf-8")
        except Exception as e:
            error(f"Could not write pyproject.toml: {e}")
            return False
        info(f"Appended [tool.nuitka_builder.*] to: {pp_path}")
    else:
        target_path = project_dir / CONFIG_FILENAME
        if target_path.exists() and not (force or reset):
            error(f"{CONFIG_FILENAME} already exists at {target_path}.")
            say("  Use --force to overwrite (curated values preserved), or")
            say("  --reset to regenerate from scratch, or edit the file manually.")
            return False
        header = (f"# build_config.toml - {name}\n"
                  f"# Generated by build.py v{SCRIPT_VERSION} --init "
                  f"on {datetime.now().strftime('%Y-%m-%d')}.\n"
                  f"# Review and edit as needed. "
                  f"Validate with: python build.py . --audit\n\n")
        try:
            target_path.write_text(header + body, encoding="utf-8")
        except Exception as e:
            error(f"Could not write {target_path}: {e}")
            return False
        info(f"Generated: {target_path}")

    say("")
    say("  Next steps:")
    say(f"    1. Open the generated config and fill in any blanks.")
    say(f"    2. python build.py {project_dir} --audit    # validate")
    say(f"    3. python build.py {project_dir}            # build")
    say("")
    return True


def audit(project_dir: Path, cfg: Config) -> bool:
    """Read-only validation: declared-but-missing, unbundled candidates, version drift."""
    banner(f"Build config audit - {cfg.name}")
    issues = 0

    # 1. data_files: declared sources exist on disk?
    say("\n  [data_files]")
    if not cfg.data_files:
        say("    (none declared)")
    for entry in cfg.data_files:
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            src = project_dir / entry[0]
            if src.is_file():
                info(f"OK    {entry[0]}")
            else:
                warn(f"MISS  {entry[0]} (declared but not on disk)")
                issues += 1
        else:
            warn(f"BAD   malformed entry: {entry}")
            issues += 1

    # 2. data_dirs: declared dirs exist on disk?
    say("\n  [data_dirs]")
    if not cfg.data_dirs:
        say("    (none declared)")
    for entry in cfg.data_dirs:
        path_str = entry if isinstance(entry, str) else (entry[0] if entry else "")
        if not path_str:
            warn(f"BAD   malformed entry: {entry}"); issues += 1; continue
        src = project_dir / path_str
        if src.is_dir():
            count = sum(1 for _ in src.rglob("*") if _.is_file())
            info(f"OK    {path_str} ({count} files)")
        else:
            warn(f"MISS  {path_str} (declared but not on disk)")
            issues += 1

    # 3. Suggest likely-asset files/dirs that aren't bundled
    say("\n  [unbundled candidates]")
    declared_files = {str((project_dir / e[0]).resolve())
                      for e in cfg.data_files
                      if isinstance(e, (list, tuple)) and len(e) == 2}
    declared_dirs = set()
    for e in cfg.data_dirs:
        p = e if isinstance(e, str) else (e[0] if e else "")
        if p:
            declared_dirs.add(str((project_dir / p).resolve()))

    def _is_inside_declared_dir(path: Path) -> bool:
        s = str(path.resolve())
        return any(s.startswith(d + os.sep) or s == d for d in declared_dirs)

    asset_dir_names = ("assets", "resources", "data", "themes", "icons", "static")
    asset_exts = {".qss", ".svg", ".png", ".jpg", ".jpeg", ".ico", ".icns",
                  ".json", ".yaml", ".yml", ".css", ".html", ".sql",
                  ".ttf", ".otf", ".woff", ".woff2", ".webp", ".gif",
                  ".desktop"}
    suggestions = []

    # Suggest top-level asset dirs that aren't declared
    for name in asset_dir_names:
        d = project_dir / name
        if d.is_dir() and str(d.resolve()) not in declared_dirs:
            has_asset = any(f.is_file() and f.suffix.lower() in asset_exts
                            for f in d.rglob("*"))
            if has_asset:
                suggestions.append(f'dir   data_dirs += ["{name}"]')

    # Suggest top-level docs/configs that might be wanted
    for f in project_dir.iterdir():
        if (f.is_file() and f.suffix.lower() in {".md", ".txt", ".cfg", ".ini"}
            and f.name not in {"build.log", "build_config.toml"}
            and str(f.resolve()) not in declared_files):
            suggestions.append(f'file  data_files += [["{f.name}", "{f.name}"]]')

    # Scattered assets not under a declared dir
    extra = []
    for ext in asset_exts:
        for f in project_dir.rglob(f"*{ext}"):
            if (f.is_file() and "build_env" not in f.parts
                and "build" not in f.parts and "dist" not in f.parts
                and not _is_inside_declared_dir(f)
                and str(f.resolve()) not in declared_files):
                rel = f.relative_to(project_dir).as_posix()
                # Skip if already counted as a top-level dir suggestion
                if rel.split("/", 1)[0] not in asset_dir_names:
                    extra.append(rel)
    if extra:
        extra = sorted(set(extra))
        for rel in extra[:10]:
            suggestions.append(f'file  data_files += [["{rel}", "{rel}"]]')
        if len(extra) > 10:
            suggestions.append(f"      ... and {len(extra) - 10} more")

    if suggestions:
        warn(f"{len(suggestions)} unbundled item(s) found:")
        for s in suggestions:
            say(f"      {s}")
    else:
        info("No unbundled candidates found.")

    # 4. Version drift between pyproject.toml and resolved config
    pp_path = project_dir / "pyproject.toml"
    if pp_path.is_file():
        pp = _read_toml(pp_path)
        pp_ver = pp.get("project", {}).get("version") if isinstance(pp, dict) else None
        if pp_ver and str(pp_ver).lstrip("v") != cfg.version:
            say("\n  [version drift]")
            warn(f"pyproject.toml says : {pp_ver}")
            warn(f"resolved config says: {cfg.version}")
            issues += 1

    # 5. Entry point exists?
    say("\n  [entry point]")
    entry = project_dir / cfg.entry
    if entry.is_file():
        info(f"OK    {cfg.entry}")
    else:
        warn(f"MISS  {cfg.entry} (entry point does not exist)")
        issues += 1

    # 6. Heavy-C modules (informational; not an issue)
    say("\n  [heavy-C modules]")
    heavy_c = detect_heavy_c_modules(project_dir, cfg)
    if heavy_c:
        info(f"Detected: {', '.join(sorted(heavy_c))}")
        targets = sorted(set(heavy_c.values()))
        say(f"        Will be shipped as bytecode (no C compile):")
        for t in targets:
            say(f"          --noinclude-custom-mode={t}:bytecode")
        say("        No C-compile of the giant SWIG wrapper; MSVC handles")
        say("        the rest of the build normally.")
    else:
        info("None detected.")

    # 7. Package-data modules (informational; auto-handled)
    say("\n  [package-data modules]")
    pkg_data = detect_package_data_modules(project_dir, cfg)
    if pkg_data:
        targets = sorted(set(pkg_data.values()))
        info(f"Detected: {', '.join(targets)}")
        say(f"        These packages ship non-Python data files (fonts, etc.)")
        say(f"        that Nuitka does not auto-bundle. Will auto-add:")
        for t in targets:
            say(f"          --include-package-data={t}")
    else:
        info("None detected.")

    say("")
    if issues == 0:
        info("Audit complete - no issues.")
    else:
        warn(f"Audit complete - {issues} issue(s) need attention.")
    say("")
    return issues == 0


def preflight_warn(project_dir: Path, cfg: Config) -> None:
    """Concise pre-build sanity check -- runs automatically before EVERY build.

    Surfaces likely bundling gaps so a code/asset RESTRUCTURE can't silently
    ship a broken binary, WITHOUT you having to remember to run --init or
    --audit. Never blocks the build; it only warns. Full detail + copy-paste
    fixes: `build.py <project> --audit`.
    """
    asset_exts = {".qss", ".svg", ".png", ".jpg", ".jpeg", ".ico", ".icns",
                  ".json", ".yaml", ".yml", ".css", ".html", ".js", ".sql",
                  ".ttf", ".otf", ".woff", ".woff2", ".webp", ".gif", ".desktop"}
    declared = set()
    for e in cfg.data_dirs:
        p = e if isinstance(e, str) else (e[0] if e else "")
        if p:
            declared.add(str((project_dir / p).resolve()))

    problems = []
    # (a) declared paths that no longer exist (moved/renamed in a restructure)
    for e in cfg.data_dirs:
        p = e if isinstance(e, str) else (e[0] if e else "")
        if p and not (project_dir / p).is_dir():
            problems.append(f"declared data_dir is gone: {p}")
    for e in cfg.data_files:
        if (isinstance(e, (list, tuple)) and len(e) == 2
                and not (project_dir / e[0]).is_file()):
            problems.append(f"declared data_file is gone: {e[0]}")
    # (b) top-level asset dirs not bundled
    for name in ("assets", "resources", "data", "themes", "icons", "static"):
        d = project_dir / name
        if (d.is_dir() and str(d.resolve()) not in declared
                and any(f.is_file() and f.suffix.lower() in asset_exts
                        for f in d.rglob("*"))):
            problems.append(f"asset dir not bundled: {name}/")
    # (c) package-nested asset dirs not bundled (e.g. ajj3_brain/console/web)
    for rel in _detect_package_data_dirs(project_dir, asset_exts):
        if str((project_dir / rel).resolve()) not in declared:
            problems.append(f"package asset dir not bundled: {rel}")

    if problems:
        warn("Preflight: possible bundling gaps (did a restructure move things?) --")
        for p in problems[:8]:
            say(f"        - {p}")
        if len(problems) > 8:
            say(f"        ... and {len(problems) - 8} more")
        say("        These may be MISSING from the binary. Declare them in pyproject")
        say("        [tool.nuitka_builder] data_dirs/data_files, or run --audit for")
        say("        copy-paste fixes. (Warning only -- the build continues.)")


def report_repo_freshness(project_dir: Path) -> None:
    """Report-only Git freshness check -- NEVER modifies the working tree.

    If project_dir is a Git repo with an upstream, do a read-only `git fetch`
    (updates remote-tracking refs, not your files) and report how many commits
    HEAD is behind/ahead. Bounded + non-fatal: a slow/offline/auth-less remote
    just skips the report. Actually *pulling* stays build_all.py's job; this
    only warns you when you're about to build a stale tree.
    """
    if not (project_dir / ".git").exists():
        return

    def _git(*a, timeout=15):
        try:
            return subprocess.run(["git", "-C", str(project_dir), *a],
                                  capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None

    up = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", timeout=5)
    if not up or up.returncode != 0 or not up.stdout.strip():
        return                              # detached HEAD or no upstream -- skip
    upstream = up.stdout.strip()
    _git("fetch", "--quiet", timeout=15)    # read-only; non-fatal if it fails
    counts = _git("rev-list", "--left-right", "--count", f"HEAD...{upstream}", timeout=5)
    if not counts or counts.returncode != 0:
        return
    try:
        ahead, behind = (int(x) for x in counts.stdout.split())
    except ValueError:
        return

    step("Repo freshness")
    if behind == 0 and ahead == 0:
        info(f"Up to date with {upstream}.")
    elif behind:
        warn(f"{behind} commit(s) BEHIND {upstream}"
             + (f" (and {ahead} ahead)" if ahead else "")
             + " -- consider `git pull` (build_all.py pulls automatically).")
    else:
        info(f"{ahead} commit(s) ahead of {upstream}; not behind.")


def preflight_gate(project_dir: Path, cfg: Config, force: bool) -> bool:
    """Hard pre-build gate: refuse to compile a binary that's already broken.

    BLOCKS on (a) a missing entry point -- Nuitka can't compile a file that is
    not there -- and (b) declared data_files/data_dirs not on disk, which would
    silently ship an incomplete binary. Returns True to proceed. Version drift
    and unbundled-asset suggestions are NOT blocking (--audit / preflight_warn
    surface those). `force` (--force) overrides the gate and builds anyway.
    """
    blockers: list[str] = []

    if not (project_dir / cfg.entry).is_file():
        blockers.append(f"entry point missing: {cfg.entry}")

    for e in cfg.data_files:
        if isinstance(e, (list, tuple)) and len(e) == 2:
            if not (project_dir / e[0]).is_file():
                blockers.append(f"declared data_file not on disk: {e[0]}")
        else:
            blockers.append(f"malformed data_files entry: {e!r}")

    for e in cfg.data_dirs:
        p = e if isinstance(e, str) else (e[0] if e else "")
        if not p:
            blockers.append(f"malformed data_dirs entry: {e!r}")
        elif not (project_dir / p).is_dir():
            blockers.append(f"declared data_dir not on disk: {p}")

    if not blockers:
        return True

    banner("Pre-build gate: blocking issues")
    for b in blockers:
        error(b)
    say("")
    if force:
        warn(f"{len(blockers)} blocking issue(s) -- proceeding anyway (--force).")
        return True
    error(f"Refusing to build: {len(blockers)} blocking issue(s).")
    say("        Fix them, run `--audit` for copy-paste fixes, or pass --force "
        "to override.")
    return False


def show_info(project_dir: Path, cfg: Config):
    banner("Build script info")
    say(f"  build.py version : {SCRIPT_VERSION}")
    say(f"  Platform         : {OS_NAME} {OS_ARCH}")
    say(f"  Project dir      : {project_dir}")
    bc = project_dir / CONFIG_FILENAME
    pp = project_dir / "pyproject.toml"
    pp_has_tool = False
    if pp.is_file():
        d = _read_toml(pp)
        pp_has_tool = bool(d.get("tool", {}).get("nuitka_builder"))
    say(f"  build_config.toml: {'(found)' if bc.is_file() else '(missing)'}")
    say(f"  pyproject.toml   : {'(has [tool.nuitka_builder])' if pp_has_tool else '(no [tool.nuitka_builder])' if pp.is_file() else '(missing)'}")
    say(f"  App name         : {cfg.name}")
    say(f"  Version          : {cfg.version}")
    say(f"  Entry point      : {cfg.entry}")
    say(f"  Plugins          : {', '.join(cfg.plugins) or '(none)'}")
    say(f"  Includes (pkgs)  : {len(cfg.include_packages)}")
    say(f"  Includes (mods)  : {len(cfg.include_modules)}")
    say(f"  Data files       : {len(cfg.data_files)}")
    say(f"  Data dirs        : {len(cfg.data_dirs)}")
    heavy_c = detect_heavy_c_modules(project_dir, cfg)
    if heavy_c:
        targets = sorted(set(heavy_c.values()))
        say(f"  Heavy-C modules  : {', '.join(sorted(heavy_c))}")
        say(f"                     -> bytecode mode: {', '.join(targets)}")
    else:
        say(f"  Heavy-C modules  : (none detected)")
    pkg_data = detect_package_data_modules(project_dir, cfg)
    if pkg_data:
        targets = sorted(set(pkg_data.values()))
        say(f"  Pkg-data modules : {', '.join(targets)}")
        say(f"                     -> --include-package-data: {', '.join(targets)}")
    else:
        say(f"  Pkg-data modules : (none detected)")
    say("")
    cur = sys.version_info
    ok = MIN_PYTHON <= (cur.major, cur.minor) <= MAX_PYTHON
    say(f"  Host Python      : {cur.major}.{cur.minor}.{cur.micro} "
        f"({'compatible' if ok else 'INCOMPATIBLE'})")
    say(f"                     {sys.executable}")
    say("")
    available = find_pythons()
    if available:
        say(f"  Discovered Pythons ({MIN_PYTHON[0]}.{MIN_PYTHON[1]}-{MAX_PYTHON[0]}.{MAX_PYTHON[1]}):")
        for ver, path in available:
            tag = ""
            if ver == available[0][0]:
                tag = "  <- best"
            if ver in NUITKA_EXPERIMENTAL:
                tag += "  (Nuitka-experimental)"
            say(f"    Python {ver[0]}.{ver[1]:>2}  {path}{tag}")
    else:
        warn("No compatible Python found on PATH.")
    say("")
    venv_dir = project_dir / "build_env"
    if venv_dir.exists():
        vp = venv_python(venv_dir)
        if vp.is_file():
            try:
                r = subprocess.run([str(vp), "--version"], capture_output=True,
                                   text=True, timeout=5)
                say(f"  Build env        : {r.stdout.strip()}  ({venv_dir})")
            except Exception:
                say(f"  Build env        : broken ({venv_dir})")
    else:
        say(f"  Build env        : not created yet")
    say("")


# ═════════════════════════════════════════════════════════════════════════════
#  CLI
# ═════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description=f"Common Nuitka build script v{SCRIPT_VERSION}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Defaults:  --onefile,  --compiler=auto (Windows: MSVC if available,
            else asks to install Build Tools; declines fall back to MinGW64).
            Examples:
              python build.py "$ProjectFileDir$" --init   # NEW: onboard a project
              python build.py "$ProjectFileDir$"          # build (default: onefile)
              python build.py /path/to/project --standalone
              python build.py . --compiler=msvc           # force MSVC
              python build.py . --compiler=mingw64        # force MinGW64
              python build.py . --yes                     # auto-accept install prompts
              python build.py . --clean --clean-env
              python build.py . --info
              python build.py . --audit
              python build.py . --test
              python build.py . --ci
        """),
    )
    parser.add_argument("project_dir", nargs="?", default=".",
                        help="Project root (default: current dir or $ProjectFileDir$)")
    # Build mode (mutually exclusive)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--onefile",    action="store_true", help="(default) Single-file exe")
    mode.add_argument("--standalone", action="store_true", help="Folder build instead")
    # Compiler
    parser.add_argument("--compiler", choices=["auto", "mingw64", "msvc", "clang"],
                        default="auto",
                        help="C compiler (Windows). Default 'auto': MinGW64 for "
                             "heavy-C projects (pymupdf), else MSVC if "
                             "installed/installable, else MinGW64 + Python 3.12.")
    parser.add_argument("--force-msvc", action="store_true",
                        help="Force MSVC on Windows regardless of --compiler. "
                             "Will fail on heavy-C projects (pymupdf) with "
                             "C1002 - use only on projects without them.")
    # Operations
    parser.add_argument("--clean",      action="store_true", help="Remove build/dist first")
    parser.add_argument("--clean-env",  action="store_true", help="Also remove build_env")
    parser.add_argument("--setup-only", action="store_true", help="Create env, don't compile")
    parser.add_argument("--test",       action="store_true", help="Smoke-test after build")
    parser.add_argument("--info",       action="store_true", help="Show config + Python survey")
    parser.add_argument("--audit",      action="store_true",
                        help="Validate config vs. project files (read-only, suggestions)")
    parser.add_argument("--init",       action="store_true",
                        help="Onboard a NEW project: generate pyproject "
                             "[tool.nuitka_builder]. NOT needed for routine "
                             "builds (a clean build just reads existing config).")
    parser.add_argument("--target",     choices=["build_config", "pyproject"],
                        default="pyproject",
                        help="Where --init writes (default: pyproject.toml "
                             "[tool.nuitka_builder] -- the recommended single source)")
    parser.add_argument("--force",      action="store_true",
                        help="With --init: overwrite existing config (curated "
                             "entry, data_dirs, data_files, include_qt_plugins "
                             "are preserved). With a build: bypass the pre-build "
                             "gate and compile despite blocking issues.")
    parser.add_argument("--reset",      action="store_true",
                        help="With --init: regenerate config FROM SCRATCH, "
                             "ignoring the existing file entirely (no "
                             "preservation). Implies overwrite.")
    parser.add_argument("--yes", "-y",   action="store_true",
                        help="Auto-accept install prompts (e.g., MSVC Build Tools)")
    parser.add_argument("--ci",         action="store_true", help="Use current Python, no venv")
    # Tuning
    parser.add_argument("--jobs",   type=int, default=None,
                        help="Parallel C jobs. Default: CPU count for normal "
                             "builds; for heavy-C builds (pymupdf) auto-tuned "
                             "from system RAM. An explicit value is honoured "
                             "as-is.")
    parser.add_argument("--python", metavar="PATH",
                        help="Force a specific Python interpreter")
    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve()
    if not project_dir.is_dir():
        sys.stderr.write(f"[build.py] project dir not found: {project_dir}\n")
        sys.exit(2)

    _open_log(project_dir)
    say(f"[build.py v{SCRIPT_VERSION}] starting (pid={os.getpid()}, python="
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro})")
    say(f"  Project dir: {project_dir}")

    # --init / --reset run before load_config so they can create config from scratch.
    if args.init or args.reset:
        # Guard: if a config ALREADY exists, --init is rarely what you want.
        # Restructuring CODE never needs re-init -- a clean build just reads the
        # existing config. --init/--reset exist to ONBOARD or deliberately
        # RE-DETECT. This stops the habit of running --init/--force/--reset for
        # a "clean build" (which only spawns/overwrites config files).
        _bc_exists = (project_dir / CONFIG_FILENAME).is_file()
        _pp = _read_toml(project_dir / "pyproject.toml") \
            if (project_dir / "pyproject.toml").is_file() else {}
        _pp_nk = bool(_pp.get("tool", {}).get("nuitka_builder")) \
            if isinstance(_pp, dict) else False
        if (_bc_exists or _pp_nk) and not (args.force or args.reset):
            banner("This project already has a build config")
            if _pp_nk:     say("  - pyproject.toml [tool.nuitka_builder]   (recommended single source)")
            if _bc_exists: say("  - build_config.toml")
            say("")
            say("  You do NOT need --init to get a clean build after restructuring.")
            say("  Rebuild this:        python build.py <project> --clean --onefile")
            say("  Check for gaps:      python build.py <project> --audit")
            say("  Re-detect anyway:    add --force (preserves curated data_dirs/files)")
            say("")
            sys.exit(0)
        ok = init_config(project_dir, force=args.force, target=args.target,
                         reset=args.reset)
        sys.exit(0 if ok else 1)

    cfg = load_config(project_dir)

    # Resolve onefile default (True unless --standalone)
    onefile = not args.standalone

    if args.info:
        show_info(project_dir, cfg); return

    if args.audit:
        sys.exit(0 if audit(project_dir, cfg) else 1)

    if args.clean or args.clean_env:
        clean(project_dir, include_env=args.clean_env)
        # Fall through to setup-only or build. Build IS the default action,
        # so --clean alone means "clean then build" - not "clean and exit".
        # To just clean without building, delete build/, dist/, build_env/
        # manually, or use --clean --setup-only to clean + warm the venv.

    if args.setup_only:
        vp = setup_build_env(project_dir, cfg, forced_python=args.python,
                             force_recreate=args.clean_env, compiler=args.compiler)
        if vp:
            banner("Build env ready")
            say(f"  Python: {vp}")
        return

    report_repo_freshness(project_dir)        # report-only: warn if tree is stale
    preflight_warn(project_dir, cfg)          # soft warnings: bundling gaps
    if not preflight_gate(project_dir, cfg, force=args.force):
        sys.exit(1)                           # blocking issues -- abort before compile
    ok = run_build(project_dir, cfg,
                   onefile=onefile, compiler=args.compiler,
                   jobs=args.jobs, forced_python=args.python, ci=args.ci,
                   auto_yes=args.yes, force_msvc=args.force_msvc)
    if ok and args.test:
        step("Smoke test...")
        smoke_test(project_dir, cfg)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
