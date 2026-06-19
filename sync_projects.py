#!/usr/bin/env python3
"""
sync_projects.py — Multi-repo git status + safe fast-forward update.
====================================================================

Compare each local project to its GitHub origin and (optionally) bring it up to
date — across many repos at once, so you don't do it one-at-a-time in PyCharm.

SAFE BY DEFAULT: with NO verb it only fetches and prints a per-repo status table
(zero working-tree mutation). The only mutating verb is `--pull`, which is
fast-forward-ONLY (git refuses anything that isn't a clean fast-forward) and:
  * never touches a DIRTY tree (refuses, tells you to commit/stash first),
  * confirms per-repo (showing the incoming commits) unless you pass --yes,
  * skips ahead/diverged/detached/shallow repos with an explanation.
Push, commit, and non-FF merge are intentionally NOT in v1 (see NEXT_SESSION).

Selection:  default = the build_projects.toml set (shared with build_projects.py)
            --project a,b  = just those (bare name = sibling dir, or a path)
            --all          = every git repo under the workspace (incl. off-list ones)

stdlib-only; reuses gitutil.py (git layer) and projutil.py (project selection).
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import gitutil
import projutil

SYNC_VERSION = "1.0.0"


# ─────────────────────────────────────────────────────────────────────────────
#  Logger ([sync] house style)
# ─────────────────────────────────────────────────────────────────────────────
def say(msg: str = "") -> None:
    print(msg, flush=True)

def banner(msg: str) -> None:
    line = "=" * 70
    say(f"\n{line}\n  {msg}\n{line}")

def step(msg: str) -> None:
    say(f"[sync] {msg}")

def warn(msg: str) -> None:
    say(f"[sync] WARNING: {msg}")

def die(msg: str, code: int = 2):
    sys.stderr.write(f"[sync] ERROR: {msg}\n")
    sys.exit(code)


STATE_LABEL = {
    "up_to_date": "up-to-date", "behind": "BEHIND", "ahead": "ahead",
    "diverged": "DIVERGED", "detached": "detached", "no_upstream": "no-upstream",
    "no_remote": "no-remote", "not_repo": "not-a-repo", "unknown": "unknown",
}


# ─────────────────────────────────────────────────────────────────────────────
#  Selection
# ─────────────────────────────────────────────────────────────────────────────
def discover_git_repos(root: Path) -> list[Path]:
    """Every immediate child dir of `root` that is a git repo (has .git)."""
    return [c.resolve() for c in sorted(root.iterdir())
            if c.is_dir() and (c / ".git").exists()]


def _dedupe(paths: list[Path]) -> list[Path]:
    seen, out = set(), []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def resolve_selection(args, config_path: Path) -> tuple[list[Path], str]:
    config_dir = config_path.resolve().parent
    root = (Path(args.root).expanduser().resolve() if args.root
            else config_dir.parent)
    if args.project:
        repos = []
        for tok in projutil.split_csv(args.project):
            abs_path, _ = projutil.normalize_token(tok, config_dir)
            if not abs_path.is_dir():
                die(f"not a directory: '{tok}' -> {abs_path}")
            repos.append(abs_path)
        return _dedupe(repos), "command-line --project"
    if args.all:
        repos = discover_git_repos(root)
        if not repos:
            die(f"--all found no git repos under {root}")
        return repos, f"--all ({root})"
    repos = projutil.load_default_projects(config_path)
    if not repos:
        die(f"no projects in {config_path.name}; use --project NAME or --all")
    return _dedupe(repos), config_path.name


# ─────────────────────────────────────────────────────────────────────────────
#  Status rendering
# ─────────────────────────────────────────────────────────────────────────────
def _flags(st) -> str:
    f = []
    if gitutil.is_dirty(st):
        f.append(f"dirty({st.staged}s/{st.unstaged}u)")
    if st.untracked:
        f.append(f"{st.untracked} untracked")
    if st.shallow:
        f.append("shallow")
    if st.lfs:
        f.append("lfs")
    if st.submodules:
        f.append("submodules")
    return ", ".join(f)


def render_status(statuses, show_diff: bool) -> None:
    banner(f"Status — {len(statuses)} repo(s)")
    w = max((len(s.name) for s in statuses), default=4)
    for s in statuses:
        if s.state == "behind":
            ab = f"behind {s.behind}"
        elif s.state == "ahead":
            ab = f"ahead {s.ahead}"
        elif s.state == "diverged":
            ab = f"+{s.ahead}/-{s.behind}"
        else:
            ab = ""
        flags = _flags(s)
        extra = f"   {flags}" if flags else ""
        note = f"   ({s.note})" if s.note and s.state in (
            "no_remote", "no_upstream", "detached", "not_repo", "unknown") else ""
        say(f"  {s.name:<{w}}  {(s.branch or '-'):<8}  "
            f"{STATE_LABEL.get(s.state, s.state):<11} {ab:<10}{extra}{note}")
        if show_diff and s.state == "behind" and s.upstream:
            for line in gitutil.incoming_log(s.path, s.upstream).splitlines():
                say(f"        | {line}")

    c = Counter(s.state for s in statuses)
    dirty = sum(1 for s in statuses if gitutil.is_dirty(s))
    ffable = sum(1 for s in statuses
                 if s.state == "behind" and not gitutil.is_dirty(s) and not s.shallow)
    say(f"\n  {c.get('up_to_date', 0)} up-to-date · {c.get('behind', 0)} behind "
        f"({ffable} ff-pullable) · {c.get('ahead', 0)} ahead · "
        f"{c.get('diverged', 0)} diverged · {dirty} dirty")
    if c.get("up_to_date", 0) == len(statuses):
        say("  All up to date.")


# ─────────────────────────────────────────────────────────────────────────────
#  Pull (fast-forward only, gated)
# ─────────────────────────────────────────────────────────────────────────────
def _confirm(prompt: str) -> bool:
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def do_pull(statuses, args) -> int:
    """Fast-forward-only pull of clean+behind repos. Returns failure count."""
    pulled = skipped = failed = 0
    for s in statuses:
        tag = f"{s.name}/{s.branch or '?'}"
        if s.state == "up_to_date":
            continue                                   # nothing to do
        if s.state != "behind":
            warn(f"{tag}: {s.note or STATE_LABEL.get(s.state, s.state)} — skipping "
                 f"(v1 does fast-forward pull only; push/merge are not built yet).")
            skipped += 1
            continue
        if gitutil.is_dirty(s):
            warn(f"{tag}: {s.staged} staged / {s.unstaged} unstaged change(s) — "
                 f"refusing to touch a dirty tree. Commit or stash first (PyCharm), "
                 f"or run with --diff to see them.")
            skipped += 1
            continue
        if s.shallow:
            warn(f"{tag}: shallow clone — skipping (unshallow first).")
            skipped += 1
            continue

        step(f"{tag}: BEHIND by {s.behind} — fast-forward would bring in:")
        for line in gitutil.incoming_log(s.path, s.upstream).splitlines():
            say(f"      {line}")
        if s.submodules:
            say("      (note: has submodules — they are NOT auto-updated)")

        if args.dry_run:
            say("  (dry-run) would fast-forward; not pulling.")
            skipped += 1
            continue
        go = args.yes or (not args.non_interactive and _confirm(f"  fast-forward {tag}?"))
        if not go:
            say("  skipped." + ("" if not args.non_interactive else
                                " (--non-interactive without --yes)"))
            skipped += 1
            continue
        ok, out = gitutil.pull_ff_only(s.path)
        if ok:
            step(f"  {tag}: pulled ✓")
            pulled += 1
        else:
            tail = out.splitlines()[-1] if out else "(no output)"
            warn(f"  {tag}: pull FAILED — {tail}")
            failed += 1

    banner("PULL SUMMARY")
    say(f"  {pulled} pulled · {skipped} skipped · {failed} failed")
    return failed


# ─────────────────────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(
        prog="sync_projects.py",
        description="Multi-repo git status + safe fast-forward update. No verb = "
                    "read-only status; --pull fast-forwards clean+behind repos.")
    # Selection
    ap.add_argument("--project", nargs="+", metavar="NAME",
                    help="Only these repos (bare name = sibling dir, or a path; CSV ok)")
    ap.add_argument("--all", action="store_true",
                    help="Every git repo under --root (includes projects NOT in the build list)")
    ap.add_argument("--root", metavar="DIR",
                    help="Workspace root for --all (default: parent of build_projects.toml)")
    ap.add_argument("--config", metavar="PATH",
                    help="build_projects.toml location (default: alongside this script)")
    # Verbs
    ap.add_argument("--pull", action="store_true",
                    help="Fast-forward-only pull of clean+behind repos (the one mutating verb)")
    ap.add_argument("--diff", action="store_true",
                    help="In status, also print the incoming commit log for behind repos")
    ap.add_argument("--no-fetch", action="store_true",
                    help="Don't fetch; classify against already-cached remote refs")
    # Safety / automation
    ap.add_argument("--dry-run", action="store_true",
                    help="Preview only; never modify any working tree")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="Auto-confirm fast-forward pulls (no per-repo prompt)")
    ap.add_argument("--non-interactive", action="store_true",
                    help="Never prompt (CI); pulls happen only if --yes is also given")
    args = ap.parse_args()

    config_path = (Path(args.config).expanduser() if args.config
                   else Path(__file__).resolve().parent / "build_projects.toml")
    repos, source = resolve_selection(args, config_path)

    banner(f"sync_projects v{SYNC_VERSION}  |  {len(repos)} repo(s) from {source}")
    do_fetch = not args.no_fetch
    say(f"  fetch : {do_fetch}{'  (skipped — using cached refs)' if not do_fetch else ''}")
    say(f"  mode  : {'pull (fast-forward only)' if args.pull else 'status (read-only)'}"
        + ("   [dry-run]" if args.dry_run else ""))

    statuses = [gitutil.status(r, do_fetch=do_fetch) for r in repos]
    render_status(statuses, show_diff=args.diff)

    rc = 0
    if args.pull:
        rc = do_pull(statuses, args)
    sys.exit(1 if rc else 0)


if __name__ == "__main__":
    main()
