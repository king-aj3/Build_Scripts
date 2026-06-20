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

    windows : --windows-jobs (default 1; shared VM, but 2 measured OK at 32GB)
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
                                macOS is SKIPPED by default (Actions billing) --
                                name it here to build it (linux,windows,macos).
    --linux-jobs N              Max concurrent Linux builds (default 2).
    --mac-jobs N                Max concurrent macOS builds (default: #projects).
    --windows-jobs N            Max concurrent Windows builds (default 1; the
                                shared VM handles 2 at ~32GB, measured). Raise
                                with care -- concurrent compiles spike RAM.
    --all --root DIR            Build every dir under DIR that has a build_hosts.toml.
    --build-all PATH            Path to build_all.py (default: alongside this script).
    --log-dir DIR               Per-job logs in parallel mode (default: <cwd>/build-logs).
    --dry-run                   Print the schedule; run nothing.

Everything after `--` is forwarded verbatim to build_all.py (and thence build.py).
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import os
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
    discover_projects, load_default_projects, read_windows_vm,
    split_csv as _split_csv,
    read_raw_projects as _read_raw_projects,
    resolve_stored as _resolve_stored,
    normalize_token as _normalize_token,
    write_projects as _write_projects,
)

SCHED_VERSION = "1.4.1"

# A host with no explicit lane cap is treated as serial (cap 1) -- the safe
# default for any unknown, possibly-shared build host.
_DEFAULT_LANE_CAP = 1

# Hosts a bare run (no --only) skips: macOS builds on GitHub Actions bill at 10x
# and the private-repo free quota is spent, so routine runs don't attempt it --
# ask for it explicitly with --only ...,macos.
_DEFAULT_SKIP_HOSTS = {"macos"}

# Exit code build_all.py uses to signal a host was SKIPPED (billing/quota): not
# built and not failed. Mapped to a "skip" Result below.
_EXIT_SKIPPED = 3

Result = namedtuple("Result", "project host status dur log")  # status: ok|fail|skip
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


def lane_cap(host: str, linux_jobs: int, mac_jobs: int, win_jobs: int) -> int:
    if host == "windows":
        return max(1, win_jobs)        # shared VM; default 1, measured OK at 2 (32GB)
    if host == "linux":
        return max(1, linux_jobs)
    if host == "macos":
        return max(1, mac_jobs)
    return _DEFAULT_LANE_CAP


# ─────────────────────────────────────────────────────────────────────────────
#  Windows build VM lifecycle (libvirt) -- start before / stop after windows jobs
# ─────────────────────────────────────────────────────────────────────────────
def _virsh(vm: dict, *args: str) -> subprocess.CompletedProcess:
    connect = vm.get("connect", "qemu:///system")          # virsh works w/o sudo
    return subprocess.run(["virsh", "-c", connect, *args],
                          capture_output=True, text=True, timeout=30)


def _vm_state(vm: dict) -> str:
    """libvirt domain state ('running' / 'shut off' / ...); '' on error."""
    try:
        return _virsh(vm, "domstate", vm["domain"]).stdout.strip()
    except Exception:
        return ""


def _vm_reachable(ssh_target: str) -> bool:
    """True if the VM answers SSH (key auth, no prompt)."""
    try:
        return subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             ssh_target, "echo ok"],
            capture_output=True, text=True, timeout=20).returncode == 0
    except Exception:
        return False


def _host_ram_gb() -> int:
    """Total host RAM in GB (0 if unknown)."""
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // (1024 * 1024)
    except Exception:
        pass
    return 0


def vm_resize(vm: dict, lanes: int) -> None:
    """Size the VM's vCPU + RAM to the windows lane count BEFORE cold-boot, so K
    concurrent builds each get a full core budget with no oversubscription. vCPU
    and topology are set atomically (Win10 caps at 2 sockets); RAM is capped to
    leave headroom for the host + Linux builds. Best-effort: on any virt-xml
    error it warns and leaves the existing size."""
    cpb = int(vm.get("cores_per_build", 16))
    mpb = int(vm.get("mem_per_build_gb", 16))
    vcpu = max(2, cpb * lanes)
    if vcpu % 2:
        vcpu += 1                                    # even -> clean 2-socket split
    host_threads = os.cpu_count() or vcpu
    if vcpu > host_threads:
        vcpu = host_threads - (host_threads % 2)
    mem_gb = mpb * lanes
    host_ram = _host_ram_gb()
    if host_ram:
        cap = max(8, host_ram - 20)                  # ~20 GB for host + linux builds
        if mem_gb > cap:
            warn(f"windows VM RAM {mem_gb}GB for {lanes} lane(s) exceeds the host "
                 f"budget (~{cap}GB of {host_ram}GB); capping to {cap}GB.")
            mem_gb = cap
    domain, connect = vm["domain"], vm.get("connect", "qemu:///system")
    cores, mib = vcpu // 2, mem_gb * 1024
    step(f"sizing VM '{domain}' -> {vcpu} vCPU (2x{cores}) / {mem_gb}GB "
         f"for {lanes} windows lane(s)")
    r1 = subprocess.run(["virt-xml", "--connect", connect, domain, "--edit",
                         "--vcpus", f"{vcpu},maxvcpus={vcpu},sockets=2,"
                                    f"cores={cores},threads=1"],
                        capture_output=True, text=True)
    r2 = subprocess.run(["virt-xml", "--connect", connect, domain, "--edit",
                         "--memory", f"{mib},currentMemory={mib}"],
                        capture_output=True, text=True)
    if r1.returncode != 0 or r2.returncode != 0:
        warn(f"VM resize did not fully apply (vcpu rc={r1.returncode}, mem "
             f"rc={r2.returncode}); starting with existing size. "
             f"{(r1.stderr or '').strip()} {(r2.stderr or '').strip()}")


def vm_ensure_up(vm: dict, lanes: int) -> bool:
    """Make sure the Windows VM is running and SSH-reachable before windows jobs.

    Returns True ONLY if we started it (caller then owns shutting it down); a VM
    that was already running is left alone. Aborts the run if it can't come up."""
    domain = vm["domain"]
    if _vm_state(vm) == "running":
        step(f"windows VM '{domain}' already running -- will leave it up "
             f"(not resizing a running VM)")
        return False
    if vm.get("size_to_jobs", True):
        vm_resize(vm, lanes)
    step(f"windows VM '{domain}' not running -- starting it")
    if _virsh(vm, "start", domain).returncode != 0:
        die(f"could not start windows VM '{domain}' (virsh start failed). "
            f"Fix the VM, or re-run with --no-manage-vm / --only linux.")
    ssh_target = vm.get("ssh")
    timeout = int(vm.get("boot_timeout", 180))
    step(f"waiting up to {timeout}s for '{domain}' to answer SSH ({ssh_target})")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ssh_target and _vm_reachable(ssh_target):
            step(f"windows VM '{domain}' is up")
            return True
        time.sleep(5)
    die(f"windows VM '{domain}' did not answer SSH within {timeout}s -- aborting "
        f"(re-run with --no-manage-vm once it's up, or --only linux).")


def vm_shutdown(vm: dict) -> None:
    """Gracefully shut the Windows VM down and wait until off. Never forces."""
    domain = vm["domain"]
    timeout = int(vm.get("shutdown_timeout", 120))
    step(f"shutting down windows VM '{domain}' (graceful, up to {timeout}s)")
    _virsh(vm, "shutdown", domain)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _vm_state(vm) == "shut off":
            step(f"windows VM '{domain}' is shut off")
            return
        time.sleep(5)
    warn(f"windows VM '{domain}' did not power off within {timeout}s; left as-is "
         f"(not forcing). Check: virsh domstate {domain}")


# ─────────────────────────────────────────────────────────────────────────────
#  One (project, host) job  ->  build_all.py <project> --only <host>
# ─────────────────────────────────────────────────────────────────────────────
def run_job(project_dir: Path, host: str, build_all_py: Path,
            passthrough: list[str], log_dir: Path, capture: bool,
            dry: bool, win_jobs: int | None = None) -> Result:
    label = f"{project_dir.name}/{host}"
    cmd = [sys.executable, str(build_all_py), str(project_dir), "--only", host]
    extra = list(passthrough)
    if host == "windows" and win_jobs and "--jobs" not in extra:
        extra += ["--jobs", str(win_jobs)]      # cap Nuitka jobs to this lane's cores
    if extra:
        cmd += ["--"] + extra
    logf = log_dir / f"{project_dir.name}-{host}.log"

    with _PRINT_LOCK:
        step(f"START  {label}")
        say("    $ " + " ".join(cmd) + (f"   (log: {logf})" if capture else ""))

    if dry:
        return Result(project_dir.name, host, "ok", 0.0, logf)

    t0 = time.monotonic()
    if capture:
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(logf, "w", encoding="utf-8") as fh:
            rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT).returncode
    else:
        rc = subprocess.run(cmd).returncode          # stream live to console
    dur = time.monotonic() - t0
    status = ("ok" if rc == 0
              else "skip" if rc == _EXIT_SKIPPED   # build_all: billing/quota skip
              else "fail")

    with _PRINT_LOCK:
        tag = {"ok": "DONE ", "skip": "SKIP ", "fail": "FAIL "}[status]
        msg = f"{tag} {label}  ({_fmt(dur)})"
        if status == "fail" and capture:
            msg += f"  -> {logf}"
        elif status == "skip":
            msg += "  (macOS Actions billing/quota -- not built)"
        step(msg)
    return Result(project_dir.name, host, status, dur, logf if capture else None)


# ─────────────────────────────────────────────────────────────────────────────
#  Scheduling
# ─────────────────────────────────────────────────────────────────────────────
def schedule_sequential(jobs, build_all_py, passthrough, log_dir, dry, win_jobs=None):
    """Strictly one job at a time; stream each build live to the console."""
    return [run_job(p, h, build_all_py, passthrough, log_dir,
                    capture=False, dry=dry, win_jobs=win_jobs) for p, h in jobs]


def schedule_parallel(jobs, caps, build_all_py, passthrough, log_dir, dry, win_jobs=None):
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
                                 log_dir, True, dry, win_jobs)] = (p, h)
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
                    help="Restrict to these OS hosts (e.g. linux,macos). macOS is "
                         "skipped by default (Actions billing); name it here to build it.")
    ap.add_argument("--linux-jobs", type=int, default=2,
                    help="Max concurrent Linux builds (default 2)")
    ap.add_argument("--mac-jobs", type=int, default=None,
                    help="Max concurrent macOS builds (default: #projects)")
    ap.add_argument("--windows-jobs", type=int, default=1,
                    help="Max concurrent Windows builds (default 1; the shared VM "
                         "handles 2 at ~32GB, measured). Raise with care -- OOM risk.")
    ap.add_argument("--build-all", metavar="PATH",
                    help="Path to build_all.py (default: alongside this script)")
    ap.add_argument("--log-dir", metavar="DIR", default="build-logs",
                    help="Per-job logs in parallel mode (default: ./build-logs)")
    ap.add_argument("--dry-run", action="store_true", help="Print schedule; build nothing")
    ap.add_argument("--no-manage-vm", action="store_true",
                    help="Don't auto start/stop the Windows VM (overrides "
                         "[windows_vm].manage in build_projects.toml)")
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
    caps = {"windows": lane_cap("windows", args.linux_jobs, mac_jobs, args.windows_jobs),
            "linux":   lane_cap("linux",   args.linux_jobs, mac_jobs, args.windows_jobs),
            "macos":   lane_cap("macos",   args.linux_jobs, mac_jobs, args.windows_jobs)}

    # Build the (project, host) job list. macOS is skipped unless asked for by
    # name (--only ...,macos): its GitHub Actions runs bill at 10x and the
    # private-repo free quota is spent, so a bare run shouldn't attempt it.
    skipped_default: set[str] = set()
    jobs: list[tuple[Path, str]] = []
    for pd in projects:
        for host in enabled_hosts(pd):
            if only is not None:
                if host not in only:
                    continue
            elif host in _DEFAULT_SKIP_HOSTS:
                skipped_default.add(host)
                continue
            jobs.append((pd, host))
    if not jobs:
        die("no jobs to run (check --only and each project's enabled hosts).")
    if skipped_default:
        warn(f"skipped by default (not requested via --only): "
             f"{', '.join(sorted(skipped_default))} -- macOS is on GitHub Actions "
             f"(billing/quota); add it with --only ...,macos when you want it.")

    # Windows VM lifecycle: start it before windows builds and stop it after (only
    # if a windows job is scheduled). Config in build_projects.toml [windows_vm].
    vm_cfg = read_windows_vm(config_path)
    windows_scheduled = any(h == "windows" for _, h in jobs)
    manage_vm = (bool(vm_cfg.get("manage")) and bool(vm_cfg.get("domain"))
                 and windows_scheduled and not args.no_manage_vm)
    size_to_jobs = manage_vm and bool(vm_cfg.get("size_to_jobs", True))
    win_jobs = int(vm_cfg.get("cores_per_build", 16)) if size_to_jobs else None

    log_dir = Path(args.log_dir).expanduser().resolve()

    # Plan banner.
    banner(f"build_projects v{SCHED_VERSION}  |  {len(projects)} project(s), "
           f"{len(jobs)} job(s)")
    say(f"  mode      : {'parallel' if args.parallel else 'sequential'}")
    say(f"  projects  : from {source}")
    say(f"  build_all : {build_all_py}")
    say(f"  lane caps : windows={caps['windows']} (shared VM)  "
        f"linux={caps['linux']}  macos={caps['macos']}")
    if manage_vm:
        say(f"  win VM    : auto start/stop '{vm_cfg['domain']}' (manage)")
        if size_to_jobs:
            say(f"  win sizing: {caps['windows']} lane(s) x "
                f"{vm_cfg.get('cores_per_build', 16)} vCPU / "
                f"{vm_cfg.get('mem_per_build_gb', 16)}GB; --jobs {win_jobs}/build")
    if args.parallel:
        say(f"  logs      : {log_dir}/<project>-<host>.log")
    say(f"  passthru  : {' '.join(passthrough) or '(none)'}")
    say("  schedule  :")
    for pd in projects:
        hs = [h for (p, h) in jobs if p == pd]
        say(f"    - {pd.name:<20} {', '.join(hs)}")

    # Bring the Windows VM up (start it if shut off) before any windows build.
    started_vm = (vm_ensure_up(vm_cfg, caps['windows'])
                  if (manage_vm and not args.dry_run) else False)

    # Run.
    t0 = time.monotonic()
    try:
        if args.parallel:
            results = schedule_parallel(jobs, caps, build_all_py, passthrough,
                                        log_dir, args.dry_run, win_jobs)
        else:
            results = schedule_sequential(jobs, build_all_py, passthrough,
                                          log_dir, args.dry_run, win_jobs)
    except KeyboardInterrupt:
        with _PRINT_LOCK:
            warn("interrupted -- queued jobs cancelled; "
                 "in-flight build(s) stopped (see logs)")
        if started_vm:
            vm_shutdown(vm_cfg)          # we started it -- clean up on abort
        sys.exit(130)
    total = time.monotonic() - t0

    # Summary.
    banner("SUMMARY")
    width = max((len(f"{r.project}/{r.host}") for r in results), default=0)
    for pd in projects:
        for r in [r for r in results if r.project == pd.name]:
            tag = {"ok": "OK  ", "skip": "SKIP", "fail": "FAIL"}.get(r.status, "FAIL")
            extra = f"   -> {r.log}" if (r.status == "fail" and r.log) else ""
            say(f"  {tag}  {f'{r.project}/{r.host}':<{width}}  ({_fmt(r.dur)}){extra}")
    failed  = [r for r in results if r.status == "fail"]
    skipped = [r for r in results if r.status == "skip"]
    ok_n    = sum(1 for r in results if r.status == "ok")
    say("")
    say(f"  {ok_n}/{len(results)} job(s) OK"
        + (f", {len(skipped)} skipped" if skipped else "")
        + f"   total wall-clock {_fmt(total)}")
    if skipped:
        say("  SKIPPED: " + ", ".join(f"{r.project}/{r.host}" for r in skipped))
    if failed:
        say("  FAILED: " + ", ".join(f"{r.project}/{r.host}" for r in failed))
    if started_vm:
        win_failed = any(r.host == "windows" and r.status == "fail" for r in results)
        if win_failed:
            warn(f"windows build(s) failed -- leaving VM '{vm_cfg['domain']}' UP "
                 f"for debugging (shut down: virsh shutdown {vm_cfg['domain']}).")
        else:
            vm_shutdown(vm_cfg)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
