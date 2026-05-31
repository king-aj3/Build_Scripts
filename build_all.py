#!/usr/bin/env python3
"""
build_all.py — Cross-OS build orchestrator for build.py.
=========================================================

Nuitka cannot cross-compile: it only ever produces a binary for the OS it
runs ON. So a "Windows + Linux + macOS" deliverable means running the SAME
build.py natively on three build hosts and collecting the results. This
script is that collector/driver.

It reads `build_hosts.toml`, then for every ENABLED host:
  * transport = "local"  -> runs build.py on THIS machine.
  * transport = "ssh"    -> ssh in, `git pull` the repo, run the remote
                            build.py, and copy the artifact back.

All artifacts land in   <project>/dist/<os>-<arch>/   so they never collide.
build.py itself is untouched; every build.py flag passes straight through.

USAGE
-----
    python build_all.py <project_dir> [orchestrator-opts] [-- build.py-flags]

    python build_all.py ~/proj/MyApp                       # all enabled hosts
    python build_all.py ~/proj/MyApp --only linux          # one host
    python build_all.py ~/proj/MyApp --only linux,windows  # subset
    python build_all.py ~/proj/MyApp --no-pull             # skip git pull
    python build_all.py ~/proj/MyApp -- --standalone --clean   # passthrough

ORCHESTRATOR OPTIONS
--------------------
    --hosts PATH    build_hosts.toml location (default: <project>/build_hosts.toml,
                    then alongside this script).
    --only A,B      Build only the named host sections.
    --no-pull       Do not `git pull` before building (use working tree as-is).
    --dry-run       Print what would run; do not build.

Everything after `--` is forwarded verbatim to build.py on every host.

CONFIG: see examples/build_hosts.template.toml
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import tomllib as _toml          # 3.11+
except ImportError:                  # pragma: no cover
    try:
        import tomli as _toml        # type: ignore
    except ImportError:
        _toml = None

ORCH_VERSION = "1.0.0"

# Default arch label per host section name, used when [hosts.X].arch is absent.
_DEFAULT_ARCH = {"linux": "x86_64", "windows": "amd64", "macos": "arm64"}


# ─────────────────────────────────────────────────────────────────────────────
#  Tiny logger (ASCII only, matches build.py house style)
# ─────────────────────────────────────────────────────────────────────────────
def say(msg: str = "") -> None:
    print(msg, flush=True)

def banner(msg: str) -> None:
    line = "=" * 70
    say(f"\n{line}\n  {msg}\n{line}")

def step(msg: str) -> None:
    say(f"[build_all] {msg}")

def warn(msg: str) -> None:
    say(f"[build_all] WARNING: {msg}")

def die(msg: str, code: int = 2) -> None:
    sys.stderr.write(f"[build_all] ERROR: {msg}\n")
    sys.exit(code)


# ─────────────────────────────────────────────────────────────────────────────
#  Config
# ─────────────────────────────────────────────────────────────────────────────
def load_hosts(project_dir: Path, explicit: str | None) -> tuple[dict, Path]:
    if _toml is None:
        die("No TOML reader. Use Python 3.11+ or `pip install tomli`.")
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(project_dir / "build_hosts.toml")
    candidates.append(Path(__file__).resolve().parent / "build_hosts.toml")
    for c in candidates:
        if c.is_file():
            with open(c, "rb") as fh:
                return _toml.load(fh), c
    die("No build_hosts.toml found. Looked in:\n    "
        + "\n    ".join(str(c) for c in candidates)
        + "\n  Copy examples/build_hosts.template.toml to your project root.")
    return {}, Path()  # unreachable


def resolve_local_build_py(cfg: dict) -> Path:
    bs = cfg.get("build_script")
    p = Path(bs).expanduser() if bs else Path(__file__).resolve().parent / "build.py"
    if not p.is_file():
        die(f"build.py not found at: {p}\n  Set `build_script` in build_hosts.toml.")
    return p


# ─────────────────────────────────────────────────────────────────────────────
#  Subprocess helpers
# ─────────────────────────────────────────────────────────────────────────────
def run(cmd: list[str], dry: bool) -> int:
    say("    $ " + " ".join(cmd))
    if dry:
        return 0
    return subprocess.run(cmd).returncode

def ssh_base(host: dict) -> list[str]:
    base = ["ssh"]
    key = host.get("key")
    if key:
        base += ["-i", str(Path(key).expanduser())]
    port = host.get("port")
    if port:
        base += ["-p", str(port)]
    base.append(host["ssh"])
    return base


# ─────────────────────────────────────────────────────────────────────────────
#  Build one host
# ─────────────────────────────────────────────────────────────────────────────
def build_local(name: str, host: dict, project_dir: Path, build_py: Path,
                label: str, reserved: set[str], passthrough: list[str],
                pull: bool, dry: bool) -> bool:
    repo = Path(host.get("repo", project_dir)).expanduser()
    if pull:
        step(f"git pull (local: {repo})")
        if (repo / ".git").exists() or dry:
            run(["git", "-C", str(repo), "pull", "--ff-only"], dry)
        else:
            warn(f"{repo} is not a git repo; skipping pull.")

    python = host.get("python") or sys.executable
    cmd = python.split() + [str(build_py), str(project_dir)] + passthrough
    step(f"building locally -> dist/{label}/")
    if run(cmd, dry) != 0:
        return False

    # Collect: any top-level dist entry that is NOT a host-label dir is the
    # fresh artifact build.py just produced (dist/<name> or dist/<name>/).
    if dry:
        return True
    dist = project_dir / "dist"
    target = dist / label
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    moved = 0
    for entry in list(dist.iterdir()):
        if entry.name in reserved or entry == target:
            continue
        shutil.move(str(entry), str(target / entry.name))
        moved += 1
    if moved == 0:
        warn("build reported success but no artifact found in dist/.")
        return False
    step(f"collected {moved} item(s) into dist/{label}/")
    return True


def build_remote(name: str, host: dict, project_dir: Path, label: str,
                 passthrough: list[str], pull: bool, dry: bool) -> bool:
    for req in ("ssh", "repo", "build_py"):
        if not host.get(req):
            warn(f"host '{name}' (ssh) is missing required key '{req}'; skipping.")
            return False
    repo = host["repo"]
    remote_build_py = host["build_py"]
    python = host.get("python", "python3")
    sb = ssh_base(host)

    if pull:
        step(f"git pull (remote {host['ssh']}: {repo})")
        if run(sb + [f'git -C "{repo}" pull --ff-only'], dry) != 0:
            warn("remote git pull failed; continuing with existing tree.")

    flags = " ".join(passthrough)
    remote_cmd = f'{python} "{remote_build_py}" "{repo}" {flags}'.strip()
    step(f"building on {host['ssh']} -> dist/{label}/")
    if run(sb + [remote_cmd], dry) != 0:
        return False

    # Pull the artifact back into dist/<label>/ (prefer rsync, fall back to scp).
    target = project_dir / "dist" / label
    if not dry:
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
    remote_dist = f'{repo.rstrip("/")}/dist/'
    if shutil.which("rsync"):
        rsh = "ssh"
        if host.get("key"):
            rsh += f" -i {Path(host['key']).expanduser()}"
        if host.get("port"):
            rsh += f" -p {host['port']}"
        cmd = ["rsync", "-az", "-e", rsh, "--exclude", "*/",  # skip nested label dirs
               f'{host["ssh"]}:{remote_dist}', str(target) + "/"]
        # NOTE: the --exclude above only skips dirs at depth>0; a folder build
        # (dist/<name>/) is wanted, so drop the exclude for standalone builds.
        if "--standalone" in passthrough:
            cmd.remove("--exclude"); cmd.remove("*/")
        return run(cmd, dry) == 0
    # scp fallback
    scp = ["scp", "-r"]
    if host.get("key"):
        scp += ["-i", str(Path(host["key"]).expanduser())]
    if host.get("port"):
        scp += ["-P", str(host["port"])]
    scp += [f'{host["ssh"]}:{remote_dist}*', str(target) + "/"]
    return run(scp, dry) == 0


# ─────────────────────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    # Split orchestrator args from build.py passthrough at the first bare '--'.
    argv = sys.argv[1:]
    passthrough: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, passthrough = argv[:i], argv[i + 1:]

    ap = argparse.ArgumentParser(
        prog="build_all.py",
        description="Run build.py natively on local + SSH hosts; collect "
                    "per-OS binaries into dist/<os>-<arch>/.")
    ap.add_argument("project_dir", nargs="?", default=".")
    ap.add_argument("--hosts", metavar="PATH", help="build_hosts.toml path")
    ap.add_argument("--only", metavar="A,B", help="Build only these host sections")
    ap.add_argument("--no-pull", action="store_true", help="Skip git pull")
    ap.add_argument("--dry-run", action="store_true", help="Print, do not build")
    args = ap.parse_args(argv)

    project_dir = Path(args.project_dir).resolve()
    if not project_dir.is_dir():
        die(f"project dir not found: {project_dir}")

    cfg, cfg_path = load_hosts(project_dir, args.hosts)
    build_py = resolve_local_build_py(cfg)
    hosts: dict = cfg.get("hosts", {})
    if not hosts:
        die(f"No [hosts.*] sections in {cfg_path}.")

    pull = not (args.no_pull or cfg.get("git", {}).get("pull") is False)
    only = {s.strip() for s in args.only.split(",")} if args.only else None

    # All configured labels are "reserved" so the local collector never moves
    # one host's output folder into another's.
    reserved = {f"{n}-{h.get('arch', _DEFAULT_ARCH.get(n, n))}"
                for n, h in hosts.items()}

    banner(f"build_all v{ORCH_VERSION}  |  project: {project_dir.name}")
    say(f"  config : {cfg_path}")
    say(f"  build  : {build_py}")
    say(f"  pull   : {pull}   passthrough: {' '.join(passthrough) or '(none)'}")

    results: list[tuple[str, str, bool]] = []
    for name, host in hosts.items():
        if not host.get("enabled", False):
            continue
        if only and name not in only:
            continue
        arch = host.get("arch", _DEFAULT_ARCH.get(name, name))
        label = f"{name}-{arch}"
        transport = host.get("transport", "ssh")
        banner(f"HOST: {name}  ({transport})  ->  dist/{label}/")
        if transport == "local":
            ok = build_local(name, host, project_dir, build_py, label,
                             reserved, passthrough, pull, args.dry_run)
        elif transport == "ssh":
            ok = build_remote(name, host, project_dir, label,
                              passthrough, pull, args.dry_run)
        else:
            warn(f"host '{name}': unknown transport '{transport}'; skipping.")
            ok = False
        results.append((name, label, ok))

    banner("SUMMARY")
    if not results:
        say("  No hosts built (none enabled / matched --only).")
        sys.exit(1)
    for name, label, ok in results:
        say(f"  {'OK  ' if ok else 'FAIL'}  {name:<10} -> dist/{label}/")
    failed = [r for r in results if not r[2]]
    say("")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
