#!/usr/bin/env python3
"""
gitutil.py — Shared, SAFE git layer for the PyCharm build toolset.
==================================================================

One subprocess chokepoint (`_git`) and a small read-only API plus exactly one
gated write verb (fast-forward-only pull). Used by sync_projects.py today, and
intended for build.py's freshness check and build_all.py's pull to wire into.

SAFETY BY CONSTRUCTION:
  * No `--force`, no `reset`, no `checkout -f`, no stash, no rebase, no commit
    surface exists in this module — the survey's data-loss vectors are simply
    unreachable from any caller.
  * The only mutating call is `pull_ff_only()`, which git itself refuses unless
    the update is a true fast-forward (no merge commit, no history rewrite).
  * Every network call carries GCM hardening (`credential.interactive=false`,
    `core.askPass=`) so a missing/expired credential FAILS FAST instead of
    hanging on a prompt; a bounded timeout backstops any hang.
  * `_git` never raises — it returns None on OSError/timeout — so a flaky repo
    degrades to "unknown/skip", never a crash mid-sweep.

stdlib-only. Always `git -C <repo>`; never os.chdir().
"""
from __future__ import annotations

import subprocess
from collections import namedtuple
from pathlib import Path

GITUTIL_VERSION = "1.0.0"

# GCM hardening injected into every network op: fail fast instead of prompting
# on a non-interactive run (matches build_all.py's remote-pull pattern).
_GCM = ("-c", "credential.interactive=false", "-c", "core.askPass=")

# Per-op timeouts (seconds): cheap local queries are quick; network ops generous.
_T_QUERY, _T_FETCH, _T_PULL = 10, 45, 120

RepoStatus = namedtuple(
    "RepoStatus",
    "name path is_repo branch upstream default_branch ahead behind "
    "staged unstaged untracked detached shallow lfs submodules state fetched note")


# ─────────────────────────────────────────────────────────────────────────────
#  Subprocess chokepoint
# ─────────────────────────────────────────────────────────────────────────────
def _git(repo: Path, *args, timeout: int = _T_QUERY, network: bool = False):
    """Run `git -C <repo> [GCM] <args>`. Returns CompletedProcess, or None on
    OSError/timeout (never raises)."""
    cmd = ["git", "-C", str(repo)]
    if network:
        cmd += list(_GCM)
    cmd += [str(a) for a in args]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _out(cp):
    """Stripped stdout if the command succeeded, else None."""
    return cp.stdout.strip() if (cp and cp.returncode == 0) else None


# ─────────────────────────────────────────────────────────────────────────────
#  Read-only queries
# ─────────────────────────────────────────────────────────────────────────────
def is_repo(repo: Path) -> bool:
    return (Path(repo) / ".git").exists()


def current_branch(repo: Path):
    """Branch name, or the literal 'HEAD' when detached, or None on error."""
    return _out(_git(repo, "rev-parse", "--abbrev-ref", "HEAD", timeout=5))


def upstream_ref(repo: Path):
    """Upstream tracking ref (e.g. 'origin/master'), or None if unset/detached."""
    return _out(_git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name",
                     "@{u}", timeout=5))


def has_remote(repo: Path, name: str = "origin") -> bool:
    cp = _git(repo, "remote", timeout=5)
    return bool(cp and cp.returncode == 0 and name in cp.stdout.split())


def default_branch(repo: Path):
    """Remote default branch (origin/HEAD), e.g. 'main' or 'master'. Falls back
    to whichever of main/master exists on origin; None if neither."""
    r = _out(_git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", timeout=5))
    if r and "/" in r:
        return r.split("/", 1)[1]
    for b in ("main", "master"):
        cp = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{b}", timeout=5)
        if cp and cp.returncode == 0:
            return b
    return None


def is_shallow(repo: Path) -> bool:
    return _out(_git(repo, "rev-parse", "--is-shallow-repository", timeout=5)) == "true"


def has_lfs(repo: Path) -> bool:
    ga = Path(repo) / ".gitattributes"
    try:
        return ga.is_file() and "filter=lfs" in ga.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def has_submodules(repo: Path) -> bool:
    return (Path(repo) / ".gitmodules").is_file()


def porcelain_counts(repo: Path):
    """(staged, unstaged, untracked) file counts, or None on error."""
    cp = _git(repo, "status", "--porcelain", timeout=_T_QUERY)
    if not cp or cp.returncode != 0:
        return None
    staged = unstaged = untracked = 0
    for line in cp.stdout.splitlines():
        if not line:
            continue
        if line.startswith("??"):
            untracked += 1
            continue
        x, y = line[0], line[1]
        if x not in " ?":
            staged += 1
        if y not in " ?":
            unstaged += 1
    return staged, unstaged, untracked


def ahead_behind(repo: Path, upstream: str):
    """(ahead, behind) commit counts of HEAD vs upstream, or None on error."""
    r = _out(_git(repo, "rev-list", "--left-right", "--count",
                  f"HEAD...{upstream}", timeout=_T_QUERY))
    if r is None:
        return None
    try:
        ahead, behind = (int(x) for x in r.split())
        return ahead, behind
    except ValueError:
        return None


def incoming_log(repo: Path, upstream: str, n: int = 12) -> str:
    """One-line log of commits that a pull would bring in (HEAD..upstream)."""
    return _out(_git(repo, "log", "--oneline", "--no-decorate",
                     f"HEAD..{upstream}", "-n", str(n), timeout=_T_QUERY)) or ""


# ─────────────────────────────────────────────────────────────────────────────
#  Classification (the survey's state matrix, read-only)
# ─────────────────────────────────────────────────────────────────────────────
def is_dirty(st: RepoStatus) -> bool:
    """Tracked changes present (staged or unstaged). Untracked files do NOT
    count as dirty — a fast-forward never touches them."""
    return st.staged > 0 or st.unstaged > 0


def status(repo: Path, do_fetch: bool = True) -> RepoStatus:
    """Full read-only classification of one repo. Optionally fetches first (the
    only network touch — updates remote-tracking refs, never the working tree)."""
    repo = Path(repo)
    name = repo.name
    base = dict(name=name, path=repo, is_repo=True, branch=None, upstream=None,
                default_branch=None, ahead=0, behind=0, staged=0, unstaged=0,
                untracked=0, detached=False, shallow=False, lfs=False,
                submodules=False, state="unknown", fetched=False, note="")

    if not is_repo(repo):
        return RepoStatus(**{**base, "is_repo": False, "state": "not_repo",
                             "note": "not a git repo"})

    branch = current_branch(repo)
    detached = (branch == "HEAD")
    counts = porcelain_counts(repo) or (0, 0, 0)
    base.update(branch=branch, detached=detached, shallow=is_shallow(repo),
                lfs=has_lfs(repo), submodules=has_submodules(repo),
                default_branch=default_branch(repo),
                staged=counts[0], unstaged=counts[1], untracked=counts[2])

    if not has_remote(repo):
        return RepoStatus(**{**base, "state": "no_remote", "note": "no origin remote"})

    fetched = fetch(repo) if do_fetch else False
    base["fetched"] = fetched

    if detached:
        return RepoStatus(**{**base, "state": "detached", "note": "detached HEAD"})

    up = upstream_ref(repo)
    if not up:
        return RepoStatus(**{**base, "state": "no_upstream",
                             "note": "branch has no upstream"})
    base["upstream"] = up

    ab = ahead_behind(repo, up)
    if ab is None:
        return RepoStatus(**{**base, "state": "unknown",
                             "note": "could not compute ahead/behind"})
    ahead, behind = ab
    base.update(ahead=ahead, behind=behind)
    if ahead == 0 and behind == 0:
        state = "up_to_date"
    elif behind > 0 and ahead == 0:
        state = "behind"          # fast-forward possible
    elif ahead > 0 and behind == 0:
        state = "ahead"
    else:
        state = "diverged"
    note = "" if fetched or not do_fetch else "fetch failed; counts may be stale"
    return RepoStatus(**{**base, "state": state, "note": note})


# ─────────────────────────────────────────────────────────────────────────────
#  Network ops
# ─────────────────────────────────────────────────────────────────────────────
def fetch(repo: Path, timeout: int = _T_FETCH) -> bool:
    """Read-only `git fetch` (updates remote-tracking refs, never the worktree).
    GCM-hardened + bounded; returns True on success."""
    cp = _git(repo, "fetch", "--quiet", network=True, timeout=timeout)
    return bool(cp and cp.returncode == 0)


def pull_ff_only(repo: Path, timeout: int = _T_PULL):
    """THE only mutating verb: `git pull --ff-only`. Git itself refuses unless
    the update is a true fast-forward (no merge commit, no rewrite), so this
    cannot clobber local commits. The CALLER is still responsible for only
    invoking it on a clean, behind repo. Returns (ok: bool, output: str)."""
    cp = _git(repo, "pull", "--ff-only", network=True, timeout=timeout)
    if cp is None:
        return False, "(pull timed out or git not found)"
    return cp.returncode == 0, (cp.stdout + cp.stderr).strip()
