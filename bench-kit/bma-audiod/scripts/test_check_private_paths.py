#!/usr/bin/env python3
"""Tests for scripts/check_private_paths.py. The REAL guard runs as a subprocess against a REAL scratch git repository
(`git init` + `git add`), so what is tested is the actual command line: its stdout and exit code. Sample values are
assembled from pieces so that this file itself holds no private-looking value and passes the guard.

Every 'flagged' case has a 'clean' twin (the nearest value that must NOT be flagged), and the allowlist rules (exact
count, entry that matches nothing, missing reason) are tested in both directions.

    python -I test_check_private_paths.py     (exit 0 = all pass)"""

import os, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(HERE, "check_private_paths.py")

n_checks, fails = 0, []


def check(name, ok, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  [" + detail + "]"))
    if not ok:
        fails.append(name)


def ip(*o):
    return ".".join(str(x) for x in o)


BS = chr(92)
DRIVE_D = "D" + ":" + BS + "bma_data" + BS + "x"
DRIVE_F = "F" + ":/tmp_scratch/y"
WSL = "/mn" + "t/d/data"
HOME = "/ho" + "me/alice/work"
VENV = "~/." + "venvs/x"
MAC_PRIVATE = ":".join(["00", "90", "79", "10", "A6", "E6"])
MAC_DOC = "00-00-5E-00-53-01"
TOKEN = "." + "bma_dspd_" + "token"
DISTRO = "Ubuntu" + "Cap"
JOBDIR = ".clau" + "de" + BS + "jobs"


def git(repo, *a):
    subprocess.run(["git", "-C", repo] + list(a), check=True, capture_output=True)


def make_repo(files, add=True):
    d = tempfile.mkdtemp(prefix="guard_repo_")
    git(d, "init", "-q")
    for name, content in files.items():
        p = os.path.join(d, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(p, mode, **({} if mode == "wb" else {"newline": "\n", "encoding": "utf-8"})) as f:
            f.write(content)
    if add:
        git(d, "add", "-A")
    return d


def guard(repo, allow=None):
    """-> (exit code, stdout lines). `allow` = text of an allowlist file, or None for 'no allowlist file'."""
    with tempfile.TemporaryDirectory() as td:
        al = os.path.join(td, "allow.toml")
        if allow is not None:
            with open(al, "w", encoding="utf-8", newline="\n") as f:
                f.write(allow)
        r = subprocess.run(
            [sys.executable, "-I", GUARD, "--root", repo, "--allowlist", al, "--names", os.path.join(td, "none.txt")],
            capture_output=True,
            text=True,
            timeout=60,
        )
    return r.returncode, r.stdout.splitlines()


def one(content, name="a.txt"):
    return guard(make_repo({name: content}))


def flagged(label, content, category, match):
    code, out = one(content)
    exp = [f"a.txt:1: [{category}] {match}", "check_private_paths: 1 finding(s), 0 allowlist problem(s)"]
    check(f"flagged {label}: exit 1 and exactly the one finding", code == 1 and out == exp, f"{code} {out}")


def clean(label, content, name="a.txt"):
    code, out = one(content, name)
    check(f"clean {label}: exit 0 and 'clean'", code == 0 and out == ["check_private_paths: clean"], f"{code} {out}")


def run():
    # ---- one flagged sample per category, each with a clean twin
    flagged("drive path (backslash)", "x " + DRIVE_D + "\n", "drive-path", "D" + ":" + BS)
    flagged("drive path (slash)", "x " + DRIVE_F + "\n", "drive-path", "F" + ":/")
    clean("drive-like text that is no path (escape sequence and URL)", "re " + BS + "d:" + BS + "d and http://example.org/a\n")
    flagged("WSL mount", "cd " + WSL + "\n", "wsl-mount", "/mn" + "t/d/")
    clean("a path that merely contains 'mnt' later", "cd /srv/mnt-data/d/\n")
    flagged("home path", "cd " + HOME + "\n", "home-path", "/ho" + "me/alice/")
    flagged("venv under home", "run " + VENV + "\n", "home-path", "~/." + "venvs")
    clean("home path placeholder", "cd /ho" + "me/<user>/work\n")
    flagged("macOS-style user path", "cd /Us" + "ers/alice/work\n", "home-path", "/Us" + "ers/")
    clean("macOS-style user path placeholder", "cd /Us" + "ers/<user>/work\n")
    flagged("private 192.168 address", "ip " + ip(192, 168, 1, 5) + "\n", "lan-ip", ip(192, 168, 1, 5))
    flagged("private 10/8 address", "ip " + ip(10, 1, 2, 3) + "\n", "lan-ip", ip(10, 1, 2, 3))
    flagged("private 172.16/12 address", "ip " + ip(172, 16, 5, 4) + "\n", "lan-ip", ip(172, 16, 5, 4))
    flagged("CGNAT / tailnet 100.64/10 address", "ip " + ip(100, 73, 1, 2) + "\n", "lan-ip", ip(100, 73, 1, 2))
    clean("documentation addresses (RFC 5737)", "a " + ip(192, 0, 2, 5) + " b " + ip(198, 51, 100, 1) + " c " + ip(203, 0, 113, 9) + "\n")
    clean(
        "public-range neighbours of the private blocks",
        "a " + ip(172, 32, 0, 1) + " b " + ip(100, 63, 0, 1) + " c " + ip(11, 0, 0, 1) + " d " + ip(127, 0, 0, 1) + "\n",
    )
    clean("a version-like number with five parts", "v " + ip(10, 1, 2, 3) + ".4\n")
    flagged("link-local address", "ip " + ip(169, 254, 1, 2) + "\n", "link-local", ip(169, 254, 1, 2))
    flagged("hardware MAC", "mac " + MAC_PRIVATE + "\n", "mac", MAC_PRIVATE)
    clean("documentation MAC (RFC 7042)", "mac " + MAC_DOC + "\n")
    clean("multicast and all-zero MACs", "a 01:00:5e:00:00:fb b 00:00:00:00:00:00 c ff:ff:ff:ff:ff:ff\n")
    flagged("tailnet host name", "host box" + ".ts" + ".net\n", "hostname", ".ts" + ".net")
    flagged("Windows default host name", "host DESKTOP-" + "ABC123X\n", "hostname", "DESKTOP-" + "ABC123X")
    flagged("lab DNS name", "host photos" + ".lab\n", "hostname", "photos" + ".lab")
    clean("a local override file name", "copy bma.local.ps1 and geo_county.local.json\n")
    flagged("token file name", "read ~/" + TOKEN + "\n", "token-file", TOKEN)
    flagged("WSL distro name", "wsl -d " + DISTRO + "\n", "distro", DISTRO)
    flagged("JSON latitude", '{"la' + 't": 12.5, "lon": 0.0}\n', "coords", '"la' + 't": 12.5')
    clean("zero coordinates (the documented example)", '{"la' + 't": 0.0, "lon": 0.0}\n')
    flagged("job folder", "p " + JOBDIR + "/tmp\n", "job-dir", JOBDIR)
    clean("angle-bracket placeholders", "<repo>/<data-dir>/analysis and <board-ip>\n")

    # ---- positions: file name, line number, several findings, order
    code, out = guard(make_repo({"b/c.txt": "ok\n" + ip(192, 168, 1, 5) + " and " + ip(10, 1, 2, 3) + "\n", "a.txt": "ok\n"}))
    check(
        "findings carry path, line number and are listed in order",
        code == 1
        and out
        == [f"b/c.txt:2: [lan-ip] {ip(192, 168, 1, 5)}", f"b/c.txt:2: [lan-ip] {ip(10, 1, 2, 3)}", "check_private_paths: 2 finding(s), 0 allowlist problem(s)"],
        f"{code} {out}",
    )

    # ---- scope: what git would commit (tracked, or untracked and not ignored); gitignored local files are never scanned
    d = make_repo({".gitignore": "local.txt\n", "keep.txt": "ok\n"})
    for nm in ("local.txt", "new.txt"):
        with open(os.path.join(d, nm), "w") as f:
            f.write(ip(192, 168, 1, 5) + "\n")
    code, out = guard(d)
    check(
        "a gitignored local file is not scanned; an untracked, not ignored one is (it would be committed)",
        code == 1 and out == [f"new.txt:1: [lan-ip] {ip(192, 168, 1, 5)}", "check_private_paths: 1 finding(s), 0 allowlist problem(s)"],
        f"{code} {out}",
    )
    git(d, "add", "new.txt")
    code, out = guard(d)
    check("the same file once tracked is flagged the same way", code == 1 and out[0] == f"new.txt:1: [lan-ip] {ip(192, 168, 1, 5)}", f"{code} {out}")
    os.remove(os.path.join(d, "new.txt"))
    git(d, "rm", "-q", "--cached", "new.txt")
    code, out = guard(d)
    check(
        "with the offending file gone only the ignored local file remains and the guard is clean (positive twin)",
        code == 0 and out == ["check_private_paths: clean"],
        f"{code} {out}",
    )
    code, out = guard(make_repo({"blob.bin": b"\x00\x01" + ip(192, 168, 1, 5).encode() + b"\n"}))
    check("a binary file (NUL byte) is skipped", code == 0 and out == ["check_private_paths: clean"], f"{code} {out}")
    code, out = guard(make_repo({"blob.txt": b"\x01" + ip(192, 168, 1, 5).encode() + b"\n"}))
    check(
        "the same bytes without a NUL are text and flagged (positive twin)",
        code == 1 and out[0] == f"blob.txt:1: [lan-ip] {ip(192, 168, 1, 5)}",
        f"{code} {out}",
    )

    # ---- allowlist: exact counts, in both directions
    two = {"n.txt": ip(192, 168, 1, 5) + " " + ip(10, 1, 2, 3) + "\n"}
    ok_allow = '[[allow]]\npath = "n.txt"\ncategories = ["lan-ip"]\ncount = 2\nreason = "test"\n'
    code, out = guard(make_repo(two), ok_allow)
    check("allowlist with the exact count: clean", code == 0 and out == ["check_private_paths: clean"], f"{code} {out}")
    code, out = guard(make_repo(two), ok_allow.replace("count = 2", "count = 1"))
    check(
        "allowlist count too LOW (file now has more matches): fails with the exact message",
        code == 1 and out == ["allowlist: n.txt: count is 2, entry says 1", "check_private_paths: 0 finding(s), 1 allowlist problem(s)"],
        f"{code} {out}",
    )
    code, out = guard(make_repo(two), ok_allow.replace("count = 2", "count = 3"))
    check(
        "allowlist count too HIGH (file now has fewer matches): fails with the exact message (the list only shrinks)",
        code == 1 and out == ["allowlist: n.txt: count is 2, entry says 3", "check_private_paths: 0 finding(s), 1 allowlist problem(s)"],
        f"{code} {out}",
    )
    code, out = guard(make_repo(two), ok_allow.replace('"lan-ip"', '"mac"'))
    check(
        "allowlist for another category does not cover the finding",
        code == 1 and out[0] == f"n.txt:1: [lan-ip] {ip(192, 168, 1, 5)}" and "allowlist: n.txt: entry matches nothing (remove it)" in out,
        f"{code} {out}",
    )
    code, out = guard(make_repo(two), ok_allow.replace('["lan-ip"]', '["*"]'))
    check("allowlist category * covers every category", code == 0 and out == ["check_private_paths: clean"], f"{code} {out}")
    code, out = guard(make_repo({"n.txt": "nothing private\n"}), ok_allow.replace("count = 2", "count = 0"))
    check(
        "allowlist entry for a file with no findings: fails (remove it)",
        code == 1 and out == ["allowlist: n.txt: entry matches nothing (remove it)", "check_private_paths: 0 finding(s), 1 allowlist problem(s)"],
        f"{code} {out}",
    )
    skip = '[[allow]]\npath = "third/*"\nskip = true\nreason = "vendor strings"\n'
    code, out = guard(make_repo({"third/x.txt": ip(192, 168, 1, 5) + "\n"}), skip)
    check("a skip entry hides the file", code == 0 and out == ["check_private_paths: clean"], f"{code} {out}")
    code, out = guard(make_repo({"mine/x.txt": ip(192, 168, 1, 5) + "\n"}), skip)
    check(
        "the skip entry does not hide other paths (and, matching nothing, is reported)",
        code == 1 and out[0] == f"mine/x.txt:1: [lan-ip] {ip(192, 168, 1, 5)}" and "allowlist: third/*: entry matches nothing (remove it)" in out,
        f"{code} {out}",
    )
    r = subprocess.run(
        [
            sys.executable,
            "-I",
            GUARD,
            "--root",
            make_repo({"a.txt": "ok\n"}),
            "--allowlist",
            os.path.join(HERE, "no_such.toml"),
            "--names",
            os.path.join(HERE, "no_such.txt"),
        ],
        capture_output=True,
        text=True,
    )
    check(
        "a missing allowlist file is the same as an empty one",
        r.returncode == 0 and r.stdout.splitlines() == ["check_private_paths: clean"],
        f"{r.returncode} {r.stdout}",
    )
    with tempfile.TemporaryDirectory() as td:
        bad = os.path.join(td, "bad.toml")
        with open(bad, "w", newline="\n") as f:
            f.write('[[allow]]\npath = "n.txt"\ncategories = ["lan-ip"]\ncount = 1\n')
        r = subprocess.run(
            [sys.executable, "-I", GUARD, "--root", make_repo(two), "--allowlist", bad, "--names", os.path.join(td, "none.txt")], capture_output=True, text=True
        )
        check(
            "an allowlist entry without a reason is a usage error (exit 2)",
            r.returncode == 2 and "allowlist entry needs path and reason" in r.stderr,
            f"{r.returncode} {r.stderr}",
        )

    # ---- the optional private-names file
    with tempfile.TemporaryDirectory() as td:
        names = os.path.join(td, "names.txt")
        with open(names, "w", newline="\n") as f:
            f.write("# comment\nalice\n")
        for content, want in (("owner is Alice here\n", 1), ("owner is bob here\n", 0)):
            r = subprocess.run(
                [sys.executable, "-I", GUARD, "--root", make_repo({"n.txt": content}), "--allowlist", os.path.join(td, "none.toml"), "--names", names],
                capture_output=True,
                text=True,
            )
            exp = ["n.txt:1: [name] Alice", "check_private_paths: 1 finding(s), 0 allowlist problem(s)"] if want else ["check_private_paths: clean"]
            check(
                f"names file: {'a listed name is flagged (case-insensitive)' if want else 'an unlisted name is clean (negative twin)'}",
                r.returncode == (1 if want else 0) and r.stdout.splitlines() == exp,
                f"{r.returncode} {r.stdout}",
            )

    # ---- a tracked symlink is scanned as its own path with its target string as content, never followed
    def link(d, name, target):
        """-> 'symlink', or 'file' where the platform cannot create one (git there stores the target string as a regular file)."""
        full = os.path.join(d, name)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        try:
            os.symlink(target, full)
            return "symlink"
        except (OSError, NotImplementedError):
            with open(full, "w", newline="\n") as f:
                f.write(target)
            return "file"

    def allow_one(path, count):
        return "[[allow]]\npath = " + repr(path) + "\ncategories = ['home-path']\ncount = " + str(count) + "\nreason = 'test'\n"

    PRIV = ip(192, 168, 1, 5)
    d = make_repo({"real.txt": "ip " + PRIV + "\n"}, add=False)
    kind = link(d, "link.txt", "real.txt")
    git(d, "add", "-A")
    code, out = guard(d)
    check(
        f"[{kind}] a link to a flagged tracked file adds no finding of its own: exactly the target is reported",
        code == 1 and out == [f"real.txt:1: [lan-ip] {PRIV}", "check_private_paths: 1 finding(s), 0 allowlist problem(s)"],
        f"{code} {out}",
    )
    d = make_repo({"ok.txt": "fine\n"}, add=False)
    kind = link(d, "dir/link.txt", HOME)
    git(d, "add", "-A")
    code, out = guard(d)
    check(
        f"[{kind}] a link whose target string is private (target untracked, outside the repo) is flagged under the link's own path",
        code == 1 and out == ["dir/link.txt:1: [home-path] " + HOME[:-4], "check_private_paths: 1 finding(s), 0 allowlist problem(s)"],
        f"{code} {out}",
    )
    code, out = guard(d, allow=allow_one("dir/link.txt", 1))
    check(f"[{kind}] the allowlist applies to the link's own path (exact count 1): clean", code == 0 and out == ["check_private_paths: clean"], f"{code} {out}")
    code, out = guard(d, allow=allow_one("dir/link.txt", 2))
    check(
        f"[{kind}] a wrong count on the link's entry fails",
        code == 1 and out == ["allowlist: dir/link.txt: count is 1, entry says 2", "check_private_paths: 0 finding(s), 1 allowlist problem(s)"],
        f"{code} {out}",
    )
    d = make_repo({"ok.txt": "fine\n"}, add=False)
    kind = link(d, "clean_link", "ok.txt")
    git(d, "add", "-A")
    code, out = guard(d)
    check(f"[{kind}] positive twin: a link to a clean target is clean", code == 0 and out == ["check_private_paths: clean"], f"{code} {out}")

    # ---- the repository itself passes its own guard with the shipped allowlist
    top = subprocess.run(["git", "-C", HERE, "rev-parse", "--show-toplevel"], capture_output=True, text=True).stdout.strip()
    r = subprocess.run([sys.executable, "-I", GUARD, "--root", HERE], capture_output=True, text=True, cwd=top, timeout=300)
    check(
        "this repository passes the guard with the shipped allowlist (exit 0, 'clean')",
        r.returncode == 0 and r.stdout.splitlines() == ["check_private_paths: clean"],
        f"{r.returncode} {r.stdout[-300:]}",
    )


run()
print(f"\n{n_checks} checks, {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
