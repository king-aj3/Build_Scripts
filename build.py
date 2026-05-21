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

SCRIPT_VERSION    = "1.7.2"
COMPATIBLE_PYTHON = [(3, 14), (3, 13), (3, 12), (3, 11), (3, 10)]
MIN_PYTHON        = (3, 10)
MAX_PYTHON        = (3, 14)
NUITKA_EXPERIMENTAL = {(3, 14)}   # update when Nuitka stabilises a version

CONFIG_FILENAME   = "build_config.toml"
REQUIREMENTS_NAME = "requirements.txt"

# Standard locations the script will auto-detect
_ENTRY_CANDIDATES = ["main.py", "app.py", "__main__.py", "run.py"]
_ICON_WIN  = ["assets/icon.ico", "resources/icon.ico", "icon.ico"]
_ICON_MAC  = ["assets/icon.icns", "resources/icon.icns", "icon.icns"]


# ═════════════════════════════════════════════════════════════════════════════
#  HEAVY-C MODULE REGISTRY
# ═════════════════════════════════════════════════════════════════════════════
#
# Packages (by import name OR pip distribution name) that ship huge generated
# C/C++ *source* which Nuitka recompiles into one enormous translation unit.
# MSVC's compiler dies on these:
#   - C1002   "compiler is out of heap space in pass 2"
#   - C1060   "compiler is out of heap space" (LTCG)
#
# IMPORTANT - this is NOT "any large package". opencv-python, tensorflow,
# torch, scipy, pandas, etc. ship *prebuilt* extension modules (.pyd / .so) in
# their wheels. Nuitka copies those as-is and never recompiles them, so they
# DO NOT cause C1002. Only packages shipping compilable C source belong here.
#
# pymupdf is the canonical (and rare) case: it ships `mupdf.c`, a SWIG-
# generated ~2.2M-line file. Nuitka recompiles it; MSVC's pass-2 heap blows up.
#
# What the build script does when it finds one of these in a project:
#   It routes the build to MinGW64 (--compiler=auto -> mingw64). GCC/MinGW64
#   compiles the giant translation unit without trouble where MSVC cannot.
#   The module IS compiled and bundled normally - the build is just slow.
#
#   It does NOT use --nofollow-import-to. That was tried (v1.6.0) and was
#   wrong: --nofollow-import-to *excludes* the module from a standalone build,
#   producing an .exe that crashes at startup with ImportError. See
#   PROJECT_MEMORY.md "Heavy-C modules: the design and how it changed".
#
# Detection matches case-insensitively against import names and pip
# distribution names (with -/_ normalisation).
#
# To add a new offender: add its import name (and pip distribution name if
# different). A false positive only costs a slower MinGW64 build.
HEAVY_C_MODULES: set = {
    "pymupdf",   # ships SWIG-generated mupdf.c, ~2.2M lines
    "fitz",      # legacy import alias for pymupdf
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


def detect_heavy_c_modules(project_dir: Path, cfg: "Config") -> list:
    """Scan the project for HEAVY_C_MODULES (packages shipping huge C source).

    Sources scanned (cheap, ~10ms total):
      1.  requirements.txt
      2.  pyproject.toml [project].dependencies and optional-dependencies
      3.  Top-level `import` / `from X import ...` statements in the entry file

    Returns a sorted list of matched package names. Empty = nothing found.
    """
    import re
    hits: set = set()
    # Normalised lookup: "pymupdf" / "fitz"
    canon = {m.lower().replace("-", "_") for m in HEAVY_C_MODULES}

    def _check(token: str):
        t = token.strip().lower()
        # Strip extras + version specifiers: "foo[extra]>=1.0; python_version<'3.13'"
        for sep in ("[", "<", ">", "=", "!", "~", ";", " ", "\t"):
            if sep in t:
                t = t.split(sep, 1)[0]
        t = t.strip().replace("-", "_")
        if t and t in canon:
            hits.add(t)

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

    return sorted(hits)


def _win_memstatus():
    """Return a populated MEMORYSTATUSEX (Windows) or None."""
    if not IS_WIN:
        return None
    try:
        class _MEMSTAT(ctypes.Structure):
            _fields_ = [("dwLength",                ctypes.c_ulong),
                        ("dwMemoryLoad",            ctypes.c_ulong),
                        ("ullTotalPhys",            ctypes.c_ulonglong),
                        ("ullAvailPhys",            ctypes.c_ulonglong),
                        ("ullTotalPageFile",        ctypes.c_ulonglong),
                        ("ullAvailPageFile",        ctypes.c_ulonglong),
                        ("ullTotalVirtual",         ctypes.c_ulonglong),
                        ("ullAvailVirtual",         ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = _MEMSTAT()
        m.dwLength = ctypes.sizeof(_MEMSTAT)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            return m
    except Exception:
        pass
    return None


def get_total_ram_gb() -> "float | None":
    """Total physical RAM in GiB, or None if it cannot be determined.

    Stdlib only - no psutil dependency.
      Windows : GlobalMemoryStatusEx via ctypes
      Linux   : os.sysconf SC_PHYS_PAGES * SC_PAGE_SIZE
      macOS   : same sysconf keys where available
    """
    try:
        if IS_WIN:
            m = _win_memstatus()
            if m is not None:
                return m.ullTotalPhys / (1024 ** 3)
        else:
            return (os.sysconf("SC_PAGE_SIZE")
                    * os.sysconf("SC_PHYS_PAGES")) / (1024 ** 3)
    except Exception:
        pass
    return None


def get_commit_limit_gb() -> "float | None":
    """Windows commit limit (physical RAM + pagefile) in GiB, else None.

    This is the ceiling on how much memory all processes can reserve.
    `cc1.exe: out of memory` happens when the C compiler cannot commit
    within this limit - so for a heavy-C build it matters more than RAM
    alone. ullTotalPageFile from GlobalMemoryStatusEx *is* the system
    commit limit (despite the name). Non-Windows: None.
    """
    m = _win_memstatus()
    if m is not None:
        return m.ullTotalPageFile / (1024 ** 3)
    return None


# A single cc1.exe compiling pymupdf's mupdf.c at -O3 can need well over
# 10 GB. If the Windows commit limit is below this, the build is likely to
# fail with 'cc1.exe: out of memory' - warn before the (multi-hour) build.
HEAVY_C_MIN_COMMIT_GB = 40.0


def _tune_heavy_c_build(jobs_explicit: "int | None", lto_cfg: str) -> tuple:
    """Auto-tune (jobs, lto) for a heavy-C build.

    Heavy-C builds compile one pathological translation unit (pymupdf's
    mupdf.c, ~2.2M lines) whose cc1.exe needs many GB. Running it
    concurrently with other large units multiplies peak memory and causes
    'cc1.exe: out of memory'.

    Decision: heavy-C builds default to --jobs=1 - each huge unit then
    compiles alone with the whole machine's memory available. Compile time
    is not a concern for these (rare, release-only) builds. A RAM-derived
    job count was tried (v1.7.1) and was too optimistic; serializing is the
    only reliable choice.

    Returns (jobs, lto, ram_gb). Explicit user choices always win:
    --jobs N is honoured as-is; an explicit lto = "yes"/"no" overrides.
    """
    ram = get_total_ram_gb()

    # ── Jobs: serialize heavy-C builds ────────────────────────────────
    jobs = jobs_explicit if jobs_explicit is not None else 1

    # ── LTO ───────────────────────────────────────────────────────────
    if lto_cfg in ("yes", "no"):
        lto = lto_cfg                              # explicit config wins
    elif ram is None:
        lto = "no"                                 # unknown -> safe
    else:
        # LTO's link stage is very memory-hungry on a heavy-C program.
        # Keep it only when RAM clearly affords it. Runtime gain for a GUI
        # app is negligible, so 'off' below the threshold costs nothing real.
        lto = "yes" if ram >= 32.0 else "no"

    return jobs, lto, ram


def _resolve_compiler_auto(auto_yes: bool = False, heavy_c: list | None = None) -> str:
    """
    Resolve --compiler=auto to a concrete compiler on Windows.

    Tier 0: heavy-C modules present  -> "mingw64" (MSVC cannot compile them;
            GCC/MinGW64 handles the giant translation unit)
    Tier 1: MSVC already installed   -> "msvc"
    Tier 2: MSVC missing, user agrees to install + install OK -> "msvc"
    Tier 3: anything else            -> "mingw64" (will use Python <3.13)
    """
    if not IS_WIN:
        return ""   # Linux/macOS: no compiler flag, system default is used

    # Tier 0 - heavy-C modules need MinGW64. MSVC dies with C1002 on the
    # giant translation unit, and excluding the module breaks the standalone
    # build. MinGW64/GCC compiles it cleanly (slowly). No way around it.
    if heavy_c:
        warn(f"Heavy-C modules detected: {', '.join(heavy_c)}")
        info("MSVC cannot compile these (C1002); selecting --compiler=mingw64.")
        info("The build will be slow - GCC must compile a multi-million-line")
        info("translation unit - but it produces a working standalone exe.")
        return "mingw64"

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


def _install_packages(project_dir: Path, venv_py: Path, cfg: Config):
    """Install pip + Nuitka + requirements.txt + cfg.extra_requirements."""
    info("Upgrading pip/setuptools/wheel...")
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
        cmd.append(f"--include-qt-plugins={cfg.include_qt_plugins}")

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
        lto = "no" if (IS_WIN and compiler == "msvc") else "yes"
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
    # Packages that ship huge C source MSVC cannot compile (C1002). The build
    # is routed to MinGW64, which compiles them - slowly but correctly. The
    # module IS compiled and bundled; we do NOT exclude it (that breaks the
    # standalone exe).
    heavy_c = detect_heavy_c_modules(project_dir, cfg)
    jobs_explicit = jobs   # None unless the user passed --jobs N

    # ── Resolve the compiler ──────────────────────────────────────────────
    # Precedence: --force-msvc > --compiler=auto resolution > explicit value.
    # --compiler=auto routes heavy-C projects to MinGW64 (see
    # _resolve_compiler_auto Tier 0).
    if force_msvc and IS_WIN:
        if compiler not in ("auto", "msvc"):
            warn(f"--force-msvc overrides --compiler={compiler}.")
        compiler = "msvc"
    elif force_msvc and not IS_WIN:
        warn("--force-msvc is Windows-only; ignoring on this platform.")
        if compiler == "auto":
            compiler = _resolve_compiler_auto(auto_yes=auto_yes, heavy_c=heavy_c)
    elif compiler == "auto":
        compiler = _resolve_compiler_auto(auto_yes=auto_yes, heavy_c=heavy_c)

    # Heavy-C + MSVC is a guaranteed failure - warn loudly.
    if heavy_c and IS_WIN and compiler == "msvc":
        warn(f"Heavy-C modules present ({', '.join(heavy_c)}) and compiler is "
             f"MSVC.")
        warn("MSVC cannot compile these - the build will fail with C1002.")
        warn("Use --compiler=mingw64 (or --compiler=auto) for this project.")

    # ── Resolve parallel jobs + LTO ───────────────────────────────────────
    # Heavy-C builds compile one pathological translation unit; they default
    # to --jobs=1 (serialize) so it compiles alone. LTO is RAM-gated.
    # Normal builds keep CPU-count jobs and config LTO.
    ram = None
    commit = None
    if heavy_c:
        jobs, cfg.lto, ram = _tune_heavy_c_build(jobs_explicit, cfg.lto)
        commit = get_commit_limit_gb()
    elif jobs is None:
        jobs = multiprocessing.cpu_count()

    mode = "One-File" if onefile else "Standalone Folder"
    banner(f"{cfg.name} v{cfg.version} - Nuitka Build ({mode})")
    say(f"  Project   : {project_dir}")
    say(f"  Platform  : {OS_NAME} {OS_ARCH}")
    say(f"  Compiler  : {compiler if IS_WIN else 'system default'}")
    if heavy_c:
        ram_str = f"{ram:.0f} GB" if ram else "undetected"
        say(f"  Heavy-C   : {', '.join(heavy_c)}")
        say(f"              compiled normally on {compiler} (slow, works)")
        say(f"  RAM       : {ram_str}  ->  --jobs={jobs}, lto={cfg.lto}"
            + ("  (--jobs override honoured)" if jobs_explicit is not None
               else ""))
        if commit is not None:
            say(f"  Commit lim: {commit:.0f} GB (RAM + pagefile)")
    say(f"  Jobs      : {jobs}")
    say(f"  Entry     : {cfg.entry}\n")

    if heavy_c and ram is None:
        warn("Could not detect system RAM - using conservative "
             f"--jobs={jobs}, lto={cfg.lto}.")

    # Pre-flight: a heavy-C build needs a large Windows commit limit.
    # cc1.exe compiling pymupdf's mupdf.c can need 10-18 GB; if the commit
    # limit (RAM + pagefile) is too low the build fails with 'out of
    # memory' - and that failure costs ~2 hours. Warn BEFORE building.
    if heavy_c and commit is not None and commit < HEAVY_C_MIN_COMMIT_GB:
        warn(f"Windows commit limit is {commit:.0f} GB - low for a heavy-C "
             f"build (pymupdf).")
        warn(f"cc1.exe compiling mupdf.c may need >10 GB; recommend a commit "
             f"limit of >= {HEAVY_C_MIN_COMMIT_GB:.0f} GB.")
        warn("Increase the Windows pagefile before building:")
        warn("  System Properties -> Advanced -> Performance Settings ->")
        warn("  Advanced -> Virtual memory Change -> Custom size (e.g.")
        warn("  49152 MB), then reboot. Otherwise this build may OOM after")
        warn("  ~2 hours. Proceeding anyway in 10 seconds...")
        try:
            time.sleep(10)
        except KeyboardInterrupt:
            error("Aborted by user.")
            return False

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
        if IS_WIN:
            if heavy_c and compiler == "msvc":
                say("        Heavy-C module on MSVC -> C1002 is expected.")
                say("        Rebuild with --compiler=mingw64 (or --compiler=auto).")
            if compiler == "mingw64":
                say("        'cc1.exe: out of memory'? Too many parallel jobs")
                say("        for this RAM. Retry with --jobs 1, or free RAM.")
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


def init_config(project_dir: Path, force: bool = False,
                target: str = "build_config") -> bool:
    """
    Generate a starter config from project introspection.

    target = "build_config" -> writes <project>/build_config.toml
    target = "pyproject"    -> appends [tool.nuitka_builder.*] to pyproject.toml
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

    # Asset directory detection
    asset_names = ("assets", "resources", "data", "themes", "static", "icons")
    asset_exts  = {".qss", ".svg", ".png", ".jpg", ".ico", ".icns", ".json",
                   ".yaml", ".yml", ".css", ".html", ".ttf", ".otf"}
    detected_dirs = []
    for n in asset_names:
        d = project_dir / n
        if d.is_dir() and any(f.is_file() and f.suffix.lower() in asset_exts
                              for f in d.rglob("*")):
            detected_dirs.append(n)

    # Top-level docs
    doc_names = ("README.md", "README.txt", "LICENSE", "LICENSE.txt",
                 "LICENSE.md", "CHANGELOG.md")
    detected_files = [f.name for f in project_dir.iterdir()
                      if f.is_file() and f.name in doc_names]

    say(f"  Detected name        : {name}")
    say(f"  Detected version     : {version}")
    say(f"  Detected entry       : {entry}")
    say(f"  Detected plugins     : {plugins or '(none)'}")
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
            L.append('include_qt_plugins = "all"')
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
    if detected_dirs:
        L.append(f"# Auto-detected from project: {', '.join(detected_dirs)}")
        L.append(f"data_dirs        = {_toml_array(detected_dirs)}")
    else:
        L.append("data_dirs        = []")
    L.append("")
    if detected_files:
        L.append("data_files       = [")
        for f in detected_files:
            L.append(f'    ["{f}", "{f}"],')
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
        if target_path.exists() and not force:
            error(f"{CONFIG_FILENAME} already exists at {target_path}.")
            say("  Use --force to overwrite, or edit the existing file manually.")
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
        info(f"Detected: {', '.join(heavy_c)}")
        say("        On Windows the build is routed to MinGW64 (MSVC cannot")
        say("        compile these). The build is slow but produces a working")
        say("        standalone exe. --compiler=auto handles this.")
    else:
        info("None detected.")

    say("")
    if issues == 0:
        info("Audit complete - no issues.")
    else:
        warn(f"Audit complete - {issues} issue(s) need attention.")
    say("")
    return issues == 0


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
        say(f"  Heavy-C modules  : {', '.join(heavy_c)}")
        say(f"                     -> build routes to MinGW64 (slow, but works)")
    else:
        say(f"  Heavy-C modules  : (none detected)")
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
                        help="Generate build_config.toml from project introspection")
    parser.add_argument("--target",     choices=["build_config", "pyproject"],
                        default="build_config",
                        help="Where --init writes (default: build_config.toml)")
    parser.add_argument("--force",      action="store_true",
                        help="Overwrite existing config (use with --init)")
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

    # --init runs before load_config so it can create the config from scratch.
    if args.init:
        ok = init_config(project_dir, force=args.force, target=args.target)
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
