#!/usr/bin/env python3
"""Tests for the dashboard's configuration defaults: nothing is a private path or address; every default comes from
BMA_DATA_DIR, BMA_BOARD and BMA_DSPD_TOKEN_FILE (or is empty). The unit under test is the real bma_ui.make_parser /
board_url_from_env / build; the environment is passed in as a dict, exactly as the process environment would be.
Same conventions as test_bma_ui.py (PASS/FAIL lines, a final 'N checks, 0 failure(s)' line, non-zero exit on failure).

    python test_bma_ui_config.py"""

import os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bma_ui as U  # noqa: E402

n_checks, fails = 0, []


def check(name, ok, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  [" + detail + "]"))
    if not ok:
        fails.append(name)


def parse(env, argv=()):
    return U.make_parser(env).parse_args(list(argv))


def run():
    # data dir: unset -> ./data relative; set -> that root, with the documented subdirectories
    a = parse({})
    check("no env: pipeline root is ./data", a.pipeline_root == "data", a.pipeline_root)
    check("no env: reports dir is data/reports", a.reports_dir == os.path.join("data", "reports"), a.reports_dir)
    check("no env: rollup dir is data/reports/ui", a.rollup_dir == os.path.join("data", "reports", "ui"), a.rollup_dir)
    root = os.path.join(tempfile.gettempdir(), "bma_cfg_root")
    b = parse({"BMA_DATA_DIR": root})
    check("BMA_DATA_DIR set: pipeline root is that dir", b.pipeline_root == root, b.pipeline_root)
    check("BMA_DATA_DIR set: reports dir is <root>/reports", b.reports_dir == os.path.join(root, "reports"), b.reports_dir)
    check("BMA_DATA_DIR set: rollup dir is <root>/reports/ui", b.rollup_dir == os.path.join(root, "reports", "ui"), b.rollup_dir)
    c = parse({"BMA_DATA_DIR": root}, ["--pipeline-root", "elsewhere", "--reports-dir", "r", "--rollup-dir", "u"])
    check(
        "explicit flags beat BMA_DATA_DIR",
        (c.pipeline_root, c.reports_dir, c.rollup_dir) == ("elsewhere", "r", "u"),
        str((c.pipeline_root, c.reports_dir, c.rollup_dir)),
    )

    # board url: an IP address in BMA_BOARD gives the dspd URL (firmware port 8470); a MAC, junk or nothing gives no board
    check("no env: no board url (empty)", a.board_url == "", repr(a.board_url))
    check(
        "BMA_BOARD IPv4: http://<ip>:8470", parse({"BMA_BOARD": "192.0.2.7"}).board_url == "http://192.0.2.7:8470", parse({"BMA_BOARD": "192.0.2.7"}).board_url
    )
    check(
        "BMA_BOARD IPv6: bracketed host",
        U.board_url_from_env({"BMA_BOARD": "2001:db8::7"}) == "http://[2001:db8::7]:8470",
        U.board_url_from_env({"BMA_BOARD": "2001:db8::7"}),
    )
    check("BMA_BOARD a MAC address: no URL (the supervisor resolves it)", U.board_url_from_env({"BMA_BOARD": "00-00-5E-00-53-01"}) == "")
    check("BMA_BOARD a host name: no URL", U.board_url_from_env({"BMA_BOARD": "board.example"}) == "")
    check("BMA_BOARD empty string: no URL", U.board_url_from_env({"BMA_BOARD": ""}) == "")
    check("--board-url beats BMA_BOARD", parse({"BMA_BOARD": "192.0.2.7"}, ["--board-url", "http://198.51.100.1:9"]).board_url == "http://198.51.100.1:9")

    # token file: no default inside anyone's home directory
    check("no env: token file is empty (no home-dir default)", a.token_file == "", repr(a.token_file))
    check("BMA_DSPD_TOKEN_FILE is the default token file", parse({"BMA_DSPD_TOKEN_FILE": "tok.txt"}).token_file == "tok.txt")
    check("--token-file beats BMA_DSPD_TOKEN_FILE", parse({"BMA_DSPD_TOKEN_FILE": "tok.txt"}, ["--token-file", "x"]).token_file == "x")

    # build(): the board panel is off without a URL, on with one (positive twin), and --no-board still wins
    with tempfile.TemporaryDirectory() as td:

        def board_enabled(env, argv=()):
            args = parse(dict(env, BMA_DATA_DIR=td), argv)
            return U.build(args).board.enabled

        check("build: no BMA_BOARD -> board disabled", board_enabled({}) is False)
        check("build: BMA_BOARD IP -> board enabled (positive twin)", board_enabled({"BMA_BOARD": "192.0.2.7"}) is True)
        check("build: BMA_BOARD IP + --no-board -> board disabled", board_enabled({"BMA_BOARD": "192.0.2.7"}, ["--no-board"]) is False)
        b2 = U.build(parse({"BMA_BOARD": "192.0.2.7", "BMA_DATA_DIR": td}))
        check(
            "build: the App reads from the data dir (beam_bands under <root>/reports)",
            b2.paths["beam_bands"] == os.path.join(td, "reports", "beam_bands.jsonl"),
            b2.paths["beam_bands"],
        )
        check("build: the App reads birds.jsonl from <root>/analysis", b2.paths["birds"] == os.path.join(td, "analysis", "birds.jsonl"), b2.paths["birds"])


run()
print(f"\n{n_checks} checks, {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
