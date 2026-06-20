#!/usr/bin/env python3
"""
projutil.py — Shared project-selection helpers for the PyCharm build toolset.
=============================================================================

Factored out of build_projects.py so that build_projects.py AND sync_projects.py
agree on EXACTLY what a "project" is: a bare NAME is a sibling dir, relative
entries resolve against the config dir, and build_projects.toml stores a simple
`projects = [...]` array. One implementation, no drift. stdlib-only.

Public API:
    discover_projects(root)            -> list[Path]   (dirs with build_hosts.toml)
    load_default_projects(config_path) -> list[Path]   (resolved build_projects.toml set)
    read_raw_projects(config_path)     -> list[str]    (verbatim stored entries)
    read_windows_vm(config_path)       -> dict         ([windows_vm] table, or {})
    resolve_stored(entry, config_dir)  -> Path
    normalize_token(token, config_dir) -> (abs_path, stored_form)
    write_projects(config_path, entries)               (rewrite the array, keep comments)
    split_csv(items)                   -> list[str]    (flatten 'a,b' c -> a b c)
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

try:
    import tomllib as _toml          # 3.11+
except ImportError:                  # pragma: no cover
    try:
        import tomli as _toml        # type: ignore
    except ImportError:
        _toml = None


def _die(msg: str, code: int = 2):
    sys.stderr.write(f"[projutil] ERROR: {msg}\n")
    sys.exit(code)


def discover_projects(root: Path) -> list[Path]:
    """Every immediate child dir of `root` that contains a build_hosts.toml."""
    found = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "build_hosts.toml").is_file():
            found.append(child.resolve())
    return found


def load_default_projects(config_path: Path) -> list[Path]:
    """Read the default project list from build_projects.toml (`projects = [...]`).

    Relative paths resolve against the config file's directory; absolute paths
    are used as-is. Returns [] if the file is absent.
    """
    if not config_path.is_file():
        return []
    if _toml is None:
        _die("No TOML reader. Use Python 3.11+ or `pip install tomli`.")
    with open(config_path, "rb") as fh:
        cfg = _toml.load(fh)
    base = config_path.resolve().parent
    out = []
    for entry in cfg.get("projects", []):
        p = Path(entry).expanduser()
        out.append((p if p.is_absolute() else base / p).resolve())
    return out


def split_csv(items: list[str]) -> list[str]:
    """Flatten ['a,b', 'c'] -> ['a','b','c'] so `--add-project a,b c` works."""
    return [x.strip() for tok in items for x in tok.split(",") if x.strip()]


def read_raw_projects(config_path: Path) -> list[str]:
    """The `projects` array verbatim (stored strings, not resolved paths)."""
    if not config_path.is_file():
        return []
    if _toml is None:
        _die("No TOML reader. Use Python 3.11+ or `pip install tomli`.")
    with open(config_path, "rb") as fh:
        return [str(e) for e in _toml.load(fh).get("projects", [])]


def read_windows_vm(config_path: Path) -> dict:
    """The [windows_vm] table (auto start/stop the Windows build VM), or {}."""
    if not config_path.is_file():
        return {}
    if _toml is None:
        _die("No TOML reader. Use Python 3.11+ or `pip install tomli`.")
    with open(config_path, "rb") as fh:
        return dict(_toml.load(fh).get("windows_vm", {}))


def resolve_stored(entry: str, config_dir: Path) -> Path:
    """How a stored entry resolves (relative entries are vs. the config dir)."""
    p = Path(entry).expanduser()
    return (p if p.is_absolute() else config_dir / p).resolve()


def normalize_token(token: str, config_dir: Path) -> tuple[Path, str]:
    """A CLI token -> (abs_path, stored_form). A bare NAME is treated as a
    sibling project dir (the workspace layout: projects live beside the config
    dir's parent); a path is resolved against the CWD. The stored form is made
    relative to the config dir when possible, matching the existing entries."""
    token = token.strip()
    p = Path(token).expanduser()
    if ("/" in token) or (os.sep in token) or token.startswith(".") or p.is_absolute():
        abs_path = (p if p.is_absolute() else Path.cwd() / p).resolve()
    else:
        abs_path = (config_dir.parent / token).resolve()   # bare name = sibling
    try:
        stored = Path(os.path.relpath(abs_path, config_dir)).as_posix()
    except ValueError:                                     # different drive (Windows)
        stored = str(abs_path)
    return abs_path, stored


def write_projects(config_path: Path, entries: list[str]) -> None:
    """Rewrite the `projects = [...]` array, preserving the file's comments.

    stdlib has no TOML writer, but the array is simple: swap just that block and
    leave the header untouched. A function replacement avoids re.sub treating
    backslashes in stored paths as group references."""
    block = "projects = [\n" + "".join(f'    "{e}",\n' for e in entries) + "]"
    if config_path.is_file():
        text = config_path.read_text(encoding="utf-8")
        if re.search(r"(?ms)^projects\s*=\s*\[.*?\]", text):
            text = re.sub(r"(?ms)^projects\s*=\s*\[.*?\]", lambda _m: block, text, count=1)
        else:
            text = text.rstrip() + "\n\n" + block + "\n"
    else:
        text = ("# build_projects.toml — default project list for build_projects.py\n\n"
                + block + "\n")
    config_path.write_text(text, encoding="utf-8")
