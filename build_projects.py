#!/usr/bin/env python3
"""
build_projects.py — Multi-project build scheduler on top of build_all.py.
=========================================================================

build_all.py builds ONE project across all its OS hosts. This script builds
SEVERAL projects at once, scheduling the (project x OS) jobs with a per-OS
concurrency cap so independent work overlaps while shared, RAM-limited hosts
stay serial.

WHY a scheduler and not just a loop: the Windows build host is a single shared
VM that OOMs if two Nuitka compiles run at once, so EVERY Windows build must be
serial -- even across different projects. Linux (this box) and macOS (GitHub
Actions) have no such limit, so their jobs run in parallel. Modelled as three
lanes, each with its own concurrency cap:

    windows : 1            (shared VM -- concurrent builds OOM)
    linux   : --linux-jobs (default 2; this box has the cores, LTO eats RAM)
    macos   : --mac-jobs   (default: #projects -- GitHub does the compiling)

Each job is just:

    python build_all.py <project> --only <host>   [-- <build.py flags>]

so every audit gate, git pull, and per-OS artifact path that build_all.py and
build.py already provide is inherited unchanged.

The project list comes from (in order): positional args > `--all` discovery >
the default list in `build_projects.toml`. So with NO args it builds the curated
default set, which you manage with `--list-projects` / `--add-project` /
`--remove-project` (no hand-editing the file).

USAGE
-----
    python build_projects.py                                  # the default list (build_projects.toml)
    python build_projects.py --list-projects                  # show the default list
    python build_projects.py --add-project Foo Bar            # add to the list (sibling dirs)
    python build_projects.py --remove-project Foo             # remove from the list
    python build_projects.py PROJ [PROJ ...]                  # build just these instead
    python build_projects.py --sequential                     # default list, one job at a time
    python build_projects.py --all --root ~/PycharmProjects   # discover projects instead
    python build_projects.py --only linux,macos               # subset of OSes
    python build_projects.py --linux-jobs 3                   # more Linux overlap
    python build_projects.py -- --standalone --clean          # flags -> build.py

OPTIONS
-------
    --list-projects             Print the default project set (with status) and exit.
    --add-project NAME ...      Add project(s) to build_projects.toml and exit.
    --remove-project NAME ...   Remove project(s) from build_projects.toml and exit.
    --config PATH               Default project-list TOML (default: build_projects.toml).
    --parallel / --sequential   Overlap jobs by lane (default) or strictly serial.
    --only A,B                  Restrict to these OS hosts (linux,windows,macos).
    --linux-jobs N              Max concurrent Linux builds (default 2).
    --mac-jobs N                Max concurrent macOS builds (default: #projects).
                                Windows is ALWAYS 1 (shared VM, OOM).
    --all --root DIR            Build every dir under DIR that has a build_hosts.toml.
    --build-all PATH            Path to build_all.py (default: alongside this script).
    --log-dir DIR               Per-job logs in parallel mode (default: <cwd>/build-logs).
    --dry-run                   Print the schedule; run nothing.

Everything after `--` is forwarded verbatim to build_all.py (and thence build.py).
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import platform
import subprocess
import sys
import threading
import time
from collections import namedtuple
from pathlib import Path

try:
    import tomllib as _toml          # 3.11+
except ImportError:                  # pragma: no cover
    try:
        import tomli as _toml        # type: ignore
    except ImportError:
        _toml = None

# Shared project-selection + build_projects.toml CRUD (aliased to the original
# private names so call sites below are unchanged). Same dir as this script.
from projutil import (
    discover_projects, load_default_projects,
    split_csv as _split_csv,
    read_raw_projects as _read_raw_projects,
    resolve_stored as _resolve_stored,
    normalize_token as _normalize_token,
    write_projects as _write_projects,
)

SCHED_VERSION = "1.2.1"

# A host with no explicit lane cap is treated as serial (cap 1) -- the safe
# default for any unknown, possibly-shared build host.
_DEFAULT_LANE_CAP = 1

Result = namedtuple("Result", "project host ok dur log")
_PRINT_LOCK = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
#  Tiny logger (ASCII only, matches build_all.py / build.py house style)
# ─────────────────────────────────────────────────────────────────────────────
def say(msg: str = "") -> None:
    print(msg, flush=True)

def banner(msg: str) -> None:
    line = "=" * 70
    say(f"\n{line}\n  {msg}\n{line}")

def step(msg: str) -> None:
    say(f"[build_projects] {msg}")

def warn(msg: str) -> None:
    say(f"[build_projects] WARNING: {msg}")

def die(msg: str, code: int = 2) -> None:
    sys.stderr.write(f"[build_projects] ERROR: {msg}\n")
    sys.exit(code)


def _fmt(dur: float) -> str:
    mm, ss = divmod(int(dur), 60)
    return f"{mm:d}m {ss:02d}s"


def local_os_name() -> str:
    p = sys.platform
    return "windows" if p == "win32" else "macos" if p == "darwin" else "linux"


# ─────────────────────────────────────────────────────────────────────────────
#  Project / host discovery
# ─────────────────────────────────────────────────────────────────────────────
def enabled_hosts(project_dir: Path) -> list[str]:
    """Enabled [hosts.*] section names for a project, read from build_hosts.toml.

    If the project has no build_hosts.toml yet, build_all.py would auto-generate
    one with only the local OS enabled -- so we schedule a single local job.
    """
    hosts_file = project_dir / "build_hosts.toml"
    if not hosts_file.is_file():
        warn(f"{project_dir.name}: no build_hosts.toml -- scheduling local "
             f"'{local_os_name()}' only (build_all.py will auto-generate it).")
        return [local_os_name()]
    if _toml is None:
        die("No TOML reader. Use Python 3.11+ or `pip install tomli`.")
    with open(hosts_file, "rb") as fh:
        cfg = _toml.load(fh)
    hosts = cfg.get("hosts", {})
    enabled = [name for name, h in hosts.items() if h.get("enabled", False)]
    if not enabled:
        warn(f"{project_dir.name}: build_hosts.toml has no enabled hosts; skipping.")
    return enabled


# ─────────────────────────────────────────────────────────────────────────────
#  Project-list management (--list-projects / --add-project / --remove-project)
#  Selection + build_projects.toml CRUD now live in projutil.py (imported above).
# ─────────────────────────────────────────────────────────────────────────────
def cmd_list_projects(config_path: Path) -> None:
    config_dir = config_path.resolve().parent
    raw = _read_raw_projects(config_path)
    banner(f"Default projects  ({config_path})")
    if not raw:
        say("  (none — add some with --add-project NAME)")
        return
    width = max(len(e) for e in raw)
    for e in raw:
        abs_path = _resolve_stored(e, config_dir)
        if not abs_path.is_dir():
            status = "MISSING DIR"
        elif not (abs_path / "build_hosts.toml").is_file():
            status = "no build_hosts.toml (run build_all.py --init)"
        else:
            status = "ok"
        say(f"  {e:<{width}}  ->  {abs_path}   [{status}]")
    say(f"\n  {len(raw)} project(s).")


def cmd_add_projects(config_path: Path, tokens: list[str]) -> None:
    config_dir = config_path.resolve().parent
    raw = _read_raw_projects(config_path)
    have = {_resolve_stored(e, config_dir) for e in raw}
    for tok in tokens:
        abs_path, stored = _normalize_token(tok, config_dir)
        if not abs_path.is_dir():
            die(f"not a directory: '{tok}' -> {abs_path}")
        if abs_path in have:
            say(f"  (already present) {stored}")
            continue
        if not (abs_path / "build_hosts.toml").is_file():
            warn(f"{stored}: no build_hosts.toml yet — run "
                 f"`build_all.py {stored} --init` before building. Added anyway.")
        raw.append(stored)
        have.add(abs_path)
        step(f"added: {stored}")
    _write_projects(config_path, raw)
    step(f"{len(raw)} project(s) now in {config_path.name}")


def cmd_remove_projects(config_path: Path, tokens: list[str]) -> None:
    config_dir = config_path.resolve().parent
    raw = _read_raw_projects(config_path)
    targets = {_normalize_token(t, config_dir)[0] for t in tokens}
    matched, kept = set(), []
    for e in raw:
        ap = _resolve_stored(e, config_dir)
        if ap in targets:
            matched.add(ap)
            step(f"removed: {e}")
        else:
            kept.append(e)
    for t, tok in zip((_normalize_token(t, config_dir)[0] for t in tokens), tokens):
        if t not in matched:
            warn(f"not in the list: {tok}")
    _write_projects(config_path, kept)
    step(f"{len(kept)} project(s) now in {config_path.name}")


def lane_cap(host: str, linux_jobs: int, mac_jobs: int) -> int:
    if host == "windows":
        return 1                       # hard: shared VM, concurrent builds OOM
    if host == "linux":
        return max(1, linux_jobs)
    if host == "macos":
        return max(1, mac_jobs)
    return _DEFAULT_LANE_CAP


# ─────────────────────────────────────────────────────────────────────────────
#  One (project, host) job  ->  build_all.py <project> --only <host>
# ─────────────────────────────────────────────────────────────────────────────
def run_job(project_dir: Path, host: str, build_all_py: Path,
            passthrough: list[str], log_dir: Path, capture: bool,
            dry: bool) -> Result:
    label = f"{project_dir.name}/{host}"
    cmd = [sys.executable, str(build_all_py), str(project_dir), "--only", host]
    if passthrough:
        cmd += ["--"] + passthrough
    logf = log_dir / f"{project_dir.name}-{host}.log"

    with _PRINT_LOCK:
        step(f"START  {label}")
        say("    $ " + " ".join(cmd) + (f"   (log: {logf})" if capture else ""))

    if dry:
        return Result(project_dir.name, host, True, 0.0, logf)

    t0 = time.monotonic()
    if capture:
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(logf, "w", encoding="utf-8") as fh:
            rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT).returncode
    else:
        rc = subprocess.run(cmd).returncode          # stream live to console
    dur = time.monotonic() - t0
    ok = rc == 0

    with _PRINT_LOCK:
        msg = f"{'DONE ' if ok else 'FAIL '} {label}  ({_fmt(dur)})"
        if not ok and capture:
            msg += f"  -> {logf}"
        step(msg)
    return Result(project_dir.name, host, ok, dur, logf if capture else None)


# ─────────────────────────────────────────────────────────────────────────────
#  Scheduling
# ─────────────────────────────────────────────────────────────────────────────
def schedule_sequential(jobs, build_all_py, passthrough, log_dir, dry):
    """Strictly one job at a time; stream each build live to the console."""
    return [run_job(p, h, build_all_py, passthrough, log_dir,
                    capture=False, dry=dry) for p, h in jobs]


def schedule_parallel(jobs, caps, build_all_py, passthrough, log_dir, dry):
    """One thread-pool lane per host; lane size = that host's concurrency cap.

    Output is captured per-job (a tangle of N live builds is unreadable), and a
    concise START/DONE/FAIL line is printed under a lock as each job moves.
    """
    hosts = sorted({h for _, h in jobs})
    lanes = {h: cf.ThreadPoolExecutor(max_workers=caps.get(h, _DEFAULT_LANE_CAP),
                                      thread_name_prefix=f"lane-{h}")
             for h in hosts}
    futs = {}
    try:
        for p, h in jobs:
            futs[lanes[h].submit(run_job, p, h, build_all_py, passthrough,
                                 log_dir, True, dry)] = (p, h)
        results = []
        for fut in cf.as_completed(futs):
            results.append(fut.result())
    finally:
        # cancel_futures drops queued-but-unstarted jobs so a Ctrl-C actually
        # stops the run instead of draining each lane's queue (esp. the cap-1
        # Windows lane); wait=False returns at once -- the in-flight child has
        # already taken the terminal's SIGINT.
        for ex in lanes.values():
            ex.shutdown(wait=False, cancel_futures=True)
    return results


# ─────────────────────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    # Split scheduler args from build_all.py passthrough at the first bare '--'.
    argv = sys.argv[1:]
    passthrough: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, passthrough = argv[:i], argv[i + 1:]

    ap = argparse.ArgumentParser(
        prog="build_projects.py",
        description="Build several projects across their OS hosts, scheduling "
                    "(project x OS) jobs with a per-OS concurrency cap.")
    ap.add_argument("projects", nargs="*", help="Project dirs to build")
    ap.add_argument("--all", action="store_true",
                    help="Discover projects under --root (dirs with build_hosts.toml)")
    ap.add_argument("--root", default=".",
                    help="Root for --all discovery (default: current dir)")
    ap.add_argument("--config", metavar="PATH",
                    help="Default project-list TOML used when no projects are "
                         "given (default: build_projects.toml alongside this script)")
    mgmt = ap.add_mutually_exclusive_group()
    mgmt.add_argument("--list-projects", action="store_true",
                      help="List the default project set and exit")
    mgmt.add_argument("--add-project", nargs="+", metavar="NAME",
                      help="Add project(s) to the default list and exit "
                           "(a bare NAME is a sibling dir; a path also works)")
    mgmt.add_argument("--remove-project", nargs="+", metavar="NAME",
                      help="Remove project(s) from the default list and exit")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--parallel", dest="parallel", action="store_true",
                      default=True, help="Overlap jobs by lane (default)")
    mode.add_argument("--sequential", dest="parallel", action="store_false",
                      help="Run strictly one job at a time")
    ap.add_argument("--only", metavar="A,B",
                    help="Restrict to these OS hosts (e.g. linux,macos)")
    ap.add_argument("--linux-jobs", type=int, default=2,
                    help="Max concurrent Linux builds (default 2)")
    ap.add_argument("--mac-jobs", type=int, default=None,
                    help="Max concurrent macOS builds (default: #projects)")
    ap.add_argument("--build-all", metavar="PATH",
                    help="Path to build_all.py (default: alongside this script)")
    ap.add_argument("--log-dir", metavar="DIR", default="build-logs",
                    help="Per-job logs in parallel mode (default: ./build-logs)")
    ap.add_argument("--dry-run", action="store_true", help="Print schedule; build nothing")
    args = ap.parse_args(argv)

    config_path = (Path(args.config).expanduser() if args.config
                   else Path(__file__).resolve().parent / "build_projects.toml")

    # Project-list management commands: mutate/read the list and exit (no build).
    if args.list_projects:
        cmd_list_projects(config_path); sys.exit(0)
    if args.add_project:
        cmd_add_projects(config_path, _split_csv(args.add_project)); sys.exit(0)
    if args.remove_project:
        cmd_remove_projects(config_path, _split_csv(args.remove_project)); sys.exit(0)

    # Resolve build_all.py.
    build_all_py = (Path(args.build_all).expanduser() if args.build_all
                    else Path(__file__).resolve().parent / "build_all.py")
    if not build_all_py.is_file():
        die(f"build_all.py not found at: {build_all_py}  (use --build-all PATH)")

    # Resolve the project list: explicit args > --all discovery > config default.
    if args.all:
        source = f"--all discovery under {args.root}"
        projects = discover_projects(Path(args.root).expanduser().resolve())
        if not projects:
            die(f"--all found no projects with a build_hosts.toml under {args.root}")
    elif args.projects:
        source = "command-line args"
        projects = []
        for p in args.projects:
            pd = Path(p).expanduser().resolve()
            if not pd.is_dir():
                die(f"project dir not found: {pd}")
            projects.append(pd)
        projects = list(dict.fromkeys(projects))   # dedupe same dir given twice
    else:
        source = config_path.name
        projects = load_default_projects(config_path)
        if not projects:
            die(f"no projects given. Pass project dirs, use --all --root DIR, or "
                f"list them in {config_path} (projects = [...]).")
        missing = [p for p in projects if not p.is_dir()]
        if missing:
            die(f"{config_path.name} lists missing dir(s): "
                + ", ".join(str(m) for m in missing))

    only = {s.strip() for s in args.only.split(",")} if args.only else None
    mac_jobs = args.mac_jobs if args.mac_jobs is not None else len(projects)
    caps = {"windows": lane_cap("windows", args.linux_jobs, mac_jobs),
            "linux":   lane_cap("linux",   args.linux_jobs, mac_jobs),
            "macos":   lane_cap("macos",   args.linux_jobs, mac_jobs)}

    # Build the (project, host) job list.
    jobs: list[tuple[Path, str]] = []
    for pd in projects:
        for host in enabled_hosts(pd):
            if only and host not in only:
                continue
            jobs.append((pd, host))
    if not jobs:
        die("no jobs to run (check --only and each project's enabled hosts).")

    log_dir = Path(args.log_dir).expanduser().resolve()

    # Plan banner.
    banner(f"build_projects v{SCHED_VERSION}  |  {len(projects)} project(s), "
           f"{len(jobs)} job(s)")
    say(f"  mode      : {'parallel' if args.parallel else 'sequential'}")
    say(f"  projects  : from {source}")
    say(f"  build_all : {build_all_py}")
    say(f"  lane caps : windows={caps['windows']} (shared VM)  "
        f"linux={caps['linux']}  macos={caps['macos']}")
    if args.parallel:
        say(f"  logs      : {log_dir}/<project>-<host>.log")
    say(f"  passthru  : {' '.join(passthrough) or '(none)'}")
    say("  schedule  :")
    for pd in projects:
        hs = [h for (p, h) in jobs if p == pd]
        say(f"    - {pd.name:<20} {', '.join(hs)}")

    # Run.
    t0 = time.monotonic()
    try:
        if args.parallel:
            results = schedule_parallel(jobs, caps, build_all_py, passthrough,
                                        log_dir, args.dry_run)
        else:
            results = schedule_sequential(jobs, build_all_py, passthrough,
                                          log_dir, args.dry_run)
    except KeyboardInterrupt:
        with _PRINT_LOCK:
            warn("interrupted -- queued jobs cancelled; "
                 "in-flight build(s) stopped (see logs)")
        sys.exit(130)
    total = time.monotonic() - t0

    # Summary.
    banner("SUMMARY")
    width = max((len(f"{r.project}/{r.host}") for r in results), default=0)
    for pd in projects:
        for r in [r for r in results if r.project == pd.name]:
            tag = "OK  " if r.ok else "FAIL"
            extra = f"   -> {r.log}" if (not r.ok and r.log) else ""
            say(f"  {tag}  {f'{r.project}/{r.host}':<{width}}  ({_fmt(r.dur)}){extra}")
    failed = [r for r in results if not r.ok]
    say("")
    say(f"  {len(results) - len(failed)}/{len(results)} job(s) OK   "
        f"total wall-clock {_fmt(total)}")
    if failed:
        say(f"  FAILED: " + ", ".join(f"{r.project}/{r.host}" for r in failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
