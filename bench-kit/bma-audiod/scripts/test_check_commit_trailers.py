#!/usr/bin/env python3
"""Tests for scripts/check_commit_trailers.py. The REAL script runs as a subprocess against a REAL scratch git repository, so what is
tested is the actual command line: its stdout and exit code. Every 'flagged' commit has a 'clean' twin.

    python -I test_check_commit_trailers.py     (exit 0 = all pass)"""

import os, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "check_commit_trailers.py")
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
n_checks, fails = 0, []


def check(name, ok, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  [" + detail + "]"))
    if not ok:
        fails.append(name)


def git(repo, *args):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, env=ENV, encoding="utf-8")
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


def commit(repo, msg):
    git(repo, "commit", "--allow-empty", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


def run(*args):
    r = subprocess.run([sys.executable, "-I", TOOL, *args], capture_output=True, text=True, encoding="utf-8")
    return r.returncode, r.stdout, r.stderr


BOT = "Co-" + "authored-by: Cla" + "ude Sonnet 5.5 <noreply@anthropic.com>"
GEN = "\U0001f916 Gener" + "ated with [Claude Code](https://claude.com/claude-code)"
with tempfile.TemporaryDirectory() as d:
    git(d, "init", "-q")
    old = commit(d, "old\n\n" + BOT)
    clean = commit(d, "clean commit")
    human = commit(d, "pair work\n\nCo-authored-by: Pat Example <pat@example.invalid>")
    prose = commit(d, "tool output generated with numpy 2.1")
    rc, out, err = run("--repo", d, "--base", old)
    check(
        "clean range: exit 0 and the exact summary line", (rc, out, err) == (0, f"0 AI attribution trailer(s) in {old[:12]}..HEAD\n", ""), repr((rc, out, err))
    )
    bad1 = commit(d, "feature\n\nbody\n\n" + BOT)
    rc, out, err = run("--repo", d, "--base", old)
    check(
        "a Co-authored-by AI trailer after the base: exit 1, hash and line named",
        (rc, out) == (1, f"{bad1[:12]} {BOT}\n1 AI attribution trailer(s) in {old[:12]}..HEAD\n"),
        repr((rc, out)),
    )
    bad2 = commit(d, "another\n\n" + GEN)
    rc, out, err = run("--repo", d, "--base", old)
    check(
        "a Generated-with AI line is the second violation, newest first",
        (rc, out) == (1, f"{bad2[:12]} {GEN}\n{bad1[:12]} {BOT}\n2 AI attribution trailer(s) in {old[:12]}..HEAD\n"),
        repr((rc, out)),
    )
    rc, out, err = run("--repo", d, "--base", bad2)
    check(
        "positive twin: base at the newest bad commit leaves a clean range",
        (rc, out) == (0, f"0 AI attribution trailer(s) in {bad2[:12]}..HEAD\n"),
        repr((rc, out)),
    )
    rc, out, err = run("--repo", d, "--base", clean, "--head", prose)
    check(
        "human co-author and prose 'generated with numpy' are not flagged (twins), exit 0",
        (rc, out) == (0, f"0 AI attribution trailer(s) in {clean[:12]}..{prose}\n"),
        repr((rc, out)),
    )
    rc, out, err = run("--repo", d, "--base", "0" * 40)
    check(
        "an unknown base is a usage error: exit 2, nothing on stdout, git's message on stderr",
        rc == 2 and out == "" and err.startswith("check_commit_trailers: "),
        repr((rc, out, err)),
    )
    rc, out, err = run("--repo", d, "--base", old, "--head", clean)
    check(
        "--head limits the range: the violations above it are not seen",
        (rc, out) == (0, f"0 AI attribution trailer(s) in {old[:12]}..{clean}\n"),
        repr((rc, out)),
    )

print(f"\n{n_checks} checks, {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
