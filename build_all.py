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
  * transport = "github" -> dispatch a GitHub Actions workflow (gh CLI),
                            wait for it, download the artifact. Used for
                            macOS (Apple Silicon / arm64) without a Mac.

All artifacts land in   <project>/dist/<os>-<arch>/   so they never collide.
After the run, every successful LINUX host's output is also packaged as
<project>/dist/<project>-<os>-<arch>.tar.gz automatically.
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
    --init          Generate a tailored build_hosts.toml (current OS enabled)
                    in the project root, then exit. Auto-runs on a normal
                    build too, if no build_hosts.toml exists yet.
    --force         With --init: overwrite an existing build_hosts.toml.
                    Remote-host (SSH) details you added are preserved.
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
import platform
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

try:
    import tomllib as _toml          # 3.11+
except ImportError:                  # pragma: no cover
    try:
        import tomli as _toml        # type: ignore
    except ImportError:
        _toml = None

ORCH_VERSION = "1.2.4"

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
def _find_hosts_file(project_dir: Path, explicit: str | None) -> Path | None:
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(project_dir / "build_hosts.toml")
    candidates.append(Path(__file__).resolve().parent / "build_hosts.toml")
    for c in candidates:
        if c.is_file():
            return c
    return None


def load_hosts(project_dir: Path, explicit: str | None) -> tuple[dict, Path]:
    if _toml is None:
        die("No TOML reader. Use Python 3.11+ or `pip install tomli`.")
    found = _find_hosts_file(project_dir, explicit)
    if not found:
        die("No build_hosts.toml found (and auto-generate did not run).")
    with open(found, "rb") as fh:
        return _toml.load(fh), found


def resolve_local_build_py(cfg: dict) -> Path:
    bs = cfg.get("build_script")
    p = Path(bs).expanduser() if bs else Path(__file__).resolve().parent / "build.py"
    if not p.is_file():
        die(f"build.py not found at: {p}\n  Set `build_script` in build_hosts.toml.")
    return p


# ─────────────────────────────────────────────────────────────────────────────
#  build_hosts.toml generation  (mirrors build.py --init / --force)
# ─────────────────────────────────────────────────────────────────────────────
def detect_local_os_arch() -> tuple[str, str]:
    """(section_name, arch) for THIS machine, e.g. ('linux','x86_64')."""
    p = sys.platform
    name = "windows" if p == "win32" else "macos" if p == "darwin" else "linux"
    m = (platform.machine() or "").lower()
    arch = {"amd64": "amd64", "x86_64": "x86_64",
            "aarch64": "arm64", "arm64": "arm64"}.get(m, m or _DEFAULT_ARCH[name])
    if name == "windows" and arch == "x86_64":
        arch = "amd64"          # template convention
    return name, arch


def _host_block(name: str, local_os: str, local_arch: str, ex: dict) -> str:
    """Render one [hosts.<name>] block, preserving curated values in `ex`."""
    g = lambda k, d="": ex.get(name, {}).get(k, d)
    if name == local_os:
        arch = g("arch") or local_arch
        return textwrap.dedent(f"""\
            # This machine. transport = "local" => builds right here, no SSH.
            [hosts.{name}]
            enabled   = true
            transport = "local"
            arch      = "{arch}"
            """)
    # Remote SSH host — keep whatever the user already filled in.
    arch = g("arch") or _DEFAULT_ARCH[name]
    enabled = "true" if g("enabled") is True else "false"
    note = ""
    if name == "macos":
        note = ("# macOS needs Apple hardware (no legal VM elsewhere). No Mac?\n"
                "# Set transport = \"github\" + gh_repo = \"OWNER/REPO\" to build\n"
                "# on a GitHub Actions arm64 runner — see USER_GUIDE.md \u00a710.\n")
    def field(k: str, default_stub: str) -> str:
        v = g(k)
        return f'{k:<9} = "{v}"' if v else default_stub
    if name == "macos" and not g("ssh"):
        # No Mac on the network — default the stub to the github transport.
        return (note + textwrap.dedent(f"""\
            [hosts.{name}]
            enabled   = {enabled}
            transport = "github"
            """)
            + field("gh_repo",  '# gh_repo = "OWNER/REPO"        # the PROJECT repo on GitHub') + "\n"
            + field("workflow", '# workflow= "macos-build.yml"   # default') + "\n"
            + field("ref",      '# ref     = "main"              # default') + "\n"
            + f'arch      = "{arch}"\n')
    return (note + textwrap.dedent(f"""\
        [hosts.{name}]
        enabled   = {enabled}
        transport = "ssh"
        """)
        + field("ssh",      '# ssh     = "builder@HOST"      # user@host or ~/.ssh/config alias') + "\n"
        + field("repo",     '# repo    = "PATH/TO/REPO"      # cloned repo ON that host') + "\n"
        + field("build_py", '# build_py= "PATH/TO/build.py"  # build.py ON that host') + "\n"
        + field("python",   '# python  = "python3"           # interpreter ON that host') + "\n"
        + (f'key       = "{g("key")}"\n' if g("key") else "")
        + (f'port      = {g("port")}\n' if g("port") else "")
        + f'arch      = "{arch}"\n')


def generate_hosts_toml(project_dir: Path, build_py: Path, force: bool) -> bool:
    """Write a tailored build_hosts.toml. Refuses to clobber unless force."""
    dest = project_dir / "build_hosts.toml"
    ex: dict = {}
    if dest.is_file():
        if not force:
            error_existing(dest)
            return False
        if _toml is not None:
            with open(dest, "rb") as fh:
                data = _toml.load(fh)
            ex = data.get("hosts", {}) if isinstance(data, dict) else {}
        warn("--force given; regenerating (remote-host details preserved).")

    local_os, local_arch = detect_local_os_arch()
    order = [local_os] + [n for n in ("linux", "windows", "macos") if n != local_os]
    blocks = "\n".join(_host_block(n, local_os, local_arch, ex) for n in order)
    content = textwrap.dedent(f"""\
        # build_hosts.toml — generated by build_all.py for {project_dir.name}
        # ====================================================================
        # Cross-OS build host map. Nuitka cannot cross-compile, so each OS
        # binary is built on a host running that OS; outputs land in
        # dist/<os>-<arch>/. Your current OS ({local_os}) is enabled below.
        # Add SSH hosts for the others, then re-run. Regenerate any time with:
        #   python <Build_Scripts>/build_all.py "{project_dir}" --init --force
        # Full option reference: Build_Scripts/examples/build_hosts.template.toml

        build_script = "{build_py}"

        [git]
        pull = true

        """) + blocks
    dest.write_text(content, encoding="utf-8")
    step(f"wrote {dest}")
    say(f"  enabled host: {local_os} (local, {local_arch}); others are SSH stubs.")
    return True


def error_existing(dest: Path) -> None:
    sys.stderr.write(f"[build_all] ERROR: {dest.name} already exists.\n")
    say("  Use --init --force to regenerate (remote-host details preserved),")
    say("  or edit the existing file directly.")


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
    # Skip *.tar.gz: those are OUR auto-generated packages (_package_linux);
    # collecting a prior run's package would nest it into the new one and bloat
    # the deliverable a little more each build. Nuitka never outputs a .tar.gz.
    if dry:
        return True
    dist = project_dir / "dist"
    target = dist / label
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    moved = 0
    for entry in list(dist.iterdir()):
        if entry.name in reserved or entry == target or entry.name.endswith((".tar.gz", ".zip")):
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
        # Over a non-interactive ssh session, Git Credential Manager can't
        # reach /dev/tty to prompt, so a private-repo pull errors. Force
        # non-interactive auth so it fails fast (we continue with the existing
        # tree) instead of hanging or erroring noisily.
        pull_cmd = (f'git -C "{repo}" -c credential.interactive=false '
                    f'-c core.askPass= pull --ff-only')
        if run(sb + [pull_cmd], dry) != 0:
            warn("remote git pull failed (often GCM auth over ssh); "
                 "continuing with the existing remote tree. "
                 "Use --no-pull to skip this step.")

    flags = " ".join(passthrough)
    remote_cmd = f'{python} "{remote_build_py}" "{repo}" {flags}'.strip()
    step(f"building on {host['ssh']} -> dist/{label}/")
    if run(sb + [remote_cmd], dry) != 0:
        return False

    # Pull the artifact back into dist/<label>/.
    # rsync needs the binary on BOTH ends — a Windows host has neither rsync
    # nor a daemon, so probing only the local side (shutil.which) wrongly
    # picks rsync and the remote leg fails. Probe the REMOTE too; fall back
    # to scp (which rides the host's OpenSSH) when rsync isn't on both ends.
    target = project_dir / "dist" / label
    if not dry:
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
    remote_dist = f'{repo.rstrip("/")}/dist/'

    remote_has_rsync = False
    if shutil.which("rsync") and not dry:
        # `rsync --version` over ssh: rc 0 means it exists on the remote.
        probe = subprocess.run(sb + ["rsync --version"],
                               capture_output=True, text=True)
        remote_has_rsync = probe.returncode == 0
    elif shutil.which("rsync") and dry:
        remote_has_rsync = True  # assume yes for dry-run printing

    if remote_has_rsync:
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

    # scp fallback (Windows hosts, or rsync missing on either end)
    if not remote_has_rsync and shutil.which("rsync"):
        step("remote has no rsync; using scp to copy artifacts back")
    scp = ["scp", "-r"]
    if host.get("key"):
        scp += ["-i", str(Path(host["key"]).expanduser())]
    if host.get("port"):
        scp += ["-P", str(host["port"])]
    scp += [f'{host["ssh"]}:{remote_dist}*', str(target) + "/"]
    return run(scp, dry) == 0


def build_github(name: str, host: dict, project_dir: Path, label: str,
                 dry: bool) -> bool:
    """Dispatch a GitHub Actions workflow via `gh`, wait, download artifact."""
    if not shutil.which("gh"):
        warn(f"host '{name}' (github): `gh` CLI not found on PATH; skipping.")
        return False
    gh_repo = host.get("gh_repo")
    if not gh_repo:
        warn(f"host '{name}' (github) is missing required key 'gh_repo'; skipping.")
        return False
    workflow = host.get("workflow", "macos-build.yml")
    ref = host.get("ref", "main")
    artifact = host.get("artifact", label)

    step(f"dispatching {workflow} on {gh_repo} (ref {ref})")
    if run(["gh", "workflow", "run", workflow, "-R", gh_repo, "--ref", ref],
           dry) != 0:
        return False
    if dry:
        return True

    # Find the run we just started (newest run of this workflow).
    import json as _json
    import time as _time
    _time.sleep(5)  # give GitHub a moment to register the run
    out = subprocess.run(
        ["gh", "run", "list", "-R", gh_repo, "--workflow", workflow,
         "--limit", "1", "--json", "databaseId"],
        capture_output=True, text=True)
    try:
        run_id = str(_json.loads(out.stdout)[0]["databaseId"])
    except (ValueError, IndexError, KeyError):
        warn("could not determine the dispatched run id.")
        return False

    step(f"waiting on run {run_id} (this is the macOS compile — be patient)")
    if run(["gh", "run", "watch", run_id, "-R", gh_repo, "--exit-status"],
           dry) != 0:
        warn(f"workflow run failed — see: gh run view {run_id} -R {gh_repo} --log")
        return False

    target = project_dir / "dist" / label
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    step(f"downloading artifact '{artifact}' -> dist/{label}/")
    dl = ["gh", "run", "download", run_id, "-R", gh_repo,
          "-n", artifact, "-D", str(target)]
    for attempt in range(1, 5):
        if run(dl, dry) == 0:
            return True
        if attempt < 4:
            wait = attempt * 10
            warn(f"artifact download failed (attempt {attempt}/4) — "
                 f"the build succeeded, this is a transient blob-store hiccup; "
                 f"retrying in {wait}s.")
            import time as _t
            _t.sleep(wait)
    warn("artifact download still failing after 4 tries. The build is fine — "
         "grab it manually with:")
    say(f"    gh run download {run_id} -R {gh_repo} -n {artifact} "
        f"-D {target}")
    say(f"  or from: https://github.com/{gh_repo}/actions/runs/{run_id}")
    return False


# ─────────────────────────────────────────────────────────────────────────────
#  Linux / macOS packaging (automatic)
# ─────────────────────────────────────────────────────────────────────────────
def _package_linux(project_dir: Path,
                   results: list[tuple[str, str, bool, float]], dry: bool) -> None:
    """tar.gz every successful linux host's dist/<label>/ folder."""
    import tarfile
    for name, label, ok, _dur in results:
        if not ok or not label.startswith("linux"):
            continue
        src = project_dir / "dist" / label
        if not src.is_dir():
            continue
        tgz = project_dir / "dist" / f"{project_dir.name}-{label}.tar.gz"
        step(f"packaging dist/{label}/ -> {tgz.name}")
        if dry:
            continue
        if tgz.exists():
            tgz.unlink()
        with tarfile.open(tgz, "w:gz") as tf:
            for entry in sorted(src.iterdir()):
                tf.add(entry, arcname=entry.name)


def _package_macos(project_dir: Path,
                   results: list[tuple[str, str, bool, float]], dry: bool) -> None:
    """zip every successful macos host's dist/<label>/ folder.

    macOS onefile binaries MUST be zipped for distribution: uploaded raw, a
    Mach-O executable with no extension shows as 0 bytes on Gumroad and similar
    sites. The executable bit is forced on and preserved in the zip (the ZipInfo
    carries the unix mode), so the binary still runs after the buyer unzips even
    if a transport (e.g. a GitHub artifact download) stripped +x along the way.
    """
    import zipfile
    for name, label, ok, _dur in results:
        if not ok or not label.startswith("macos"):
            continue
        src = project_dir / "dist" / label
        if not src.is_dir():
            continue
        zpath = project_dir / "dist" / f"{project_dir.name}-{label}.zip"
        step(f"packaging dist/{label}/ -> {zpath.name}")
        if dry:
            continue
        if zpath.exists():
            zpath.unlink()
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
            for entry in sorted(src.iterdir()):
                if entry.is_file():
                    entry.chmod(entry.stat().st_mode | 0o755)   # runnable after unzip
                zf.write(entry, arcname=entry.name)             # write() preserves the unix mode


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
    ap.add_argument("--init", action="store_true",
                    help="Generate a tailored build_hosts.toml (current OS enabled) and exit")
    ap.add_argument("--force", action="store_true",
                    help="With --init: overwrite an existing build_hosts.toml (remote details preserved)")
    ap.add_argument("--only", metavar="A,B", help="Build only these host sections")
    ap.add_argument("--no-pull", action="store_true", help="Skip git pull")
    ap.add_argument("--dry-run", action="store_true", help="Print, do not build")
    args = ap.parse_args(argv)

    project_dir = Path(args.project_dir).resolve()
    if not project_dir.is_dir():
        die(f"project dir not found: {project_dir}")

    # --init: write a tailored build_hosts.toml and exit (mirrors build.py).
    if args.init:
        bp = resolve_local_build_py({})
        ok = generate_hosts_toml(project_dir, bp, force=args.force)
        sys.exit(0 if ok else 1)

    # Locate the host map; if absent, auto-generate one (current OS enabled)
    # so a fresh project "just builds" without hand-copying a template.
    if not _find_hosts_file(project_dir, args.hosts):
        step("no build_hosts.toml found — generating a default (current OS enabled).")
        generate_hosts_toml(project_dir, resolve_local_build_py({}), force=False)

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

    results: list[tuple[str, str, bool, float]] = []
    import time as _time
    for name, host in hosts.items():
        if not host.get("enabled", False):
            continue
        if only and name not in only:
            continue
        arch = host.get("arch", _DEFAULT_ARCH.get(name, name))
        label = f"{name}-{arch}"
        transport = host.get("transport", "ssh")
        banner(f"HOST: {name}  ({transport})  ->  dist/{label}/")
        _t0 = _time.monotonic()
        if transport == "local":
            ok = build_local(name, host, project_dir, build_py, label,
                             reserved, passthrough, pull, args.dry_run)
        elif transport == "ssh":
            ok = build_remote(name, host, project_dir, label,
                              passthrough, pull, args.dry_run)
        elif transport == "github":
            ok = build_github(name, host, project_dir, label, args.dry_run)
        else:
            warn(f"host '{name}': unknown transport '{transport}'; skipping.")
            ok = False
        results.append((name, label, ok, _time.monotonic() - _t0))

    _package_linux(project_dir, results, args.dry_run)
    _package_macos(project_dir, results, args.dry_run)

    banner("SUMMARY")
    if not results:
        say("  No hosts built (none enabled / matched --only).")
        sys.exit(1)
    for name, label, ok, dur in results:
        mm, ss = divmod(int(dur), 60)
        say(f"  {'OK  ' if ok else 'FAIL'}  {name:<10} -> dist/{label}/   ({mm:d}m {ss:02d}s)")
    failed = [r for r in results if not r[2]]
    say("")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
