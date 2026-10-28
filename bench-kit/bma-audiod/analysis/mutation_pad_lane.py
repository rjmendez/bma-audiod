#!/usr/bin/env python3
"""Mutation proof for the provenance fields of the pad lane (contract pad-lane 1.1.0): each mutation is applied to a TEMP COPY of analysis/ + tools/
(the repo is never touched), test_pad_lane.py --unit-only runs on the copy and must go red. Exit 0 only when every mutation was caught.
    python -I mutation_pad_lane.py"""

import os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BMA = os.path.dirname(HERE)

M = [
    (
        "tags_pad model: the variant stays the production one (best_beam_32k)",
        [('P.model_block(self.model["name"], self.model["version"], VARIANT, self.model["sha256"])     # variant', "dict(self.model)     # variant")],
    ),
    (
        "tags_pad model: the digest is dropped",
        [('self.model["version"], VARIANT, self.model["sha256"])     # variant', 'self.model["version"], VARIANT, None)     # variant')],
    ),
    (
        "tags_pad model: the shared production block is mutated in place",
        [
            (
                "            self.model = share.model                         # the production",
                '            self.model = share.model; share.model["variant"] = VARIANT                         # the production',
            )
        ],
    ),
    (
        "tags_pad claim: absent",
        [
            (
                '"model": dict(self.model), "claim": dict(P.CLAIM), "top_k": top_k, "max_unstored_score": unstored}], None\n\n\nclass PadClap',
                '"model": dict(self.model), "top_k": top_k, "max_unstored_score": unstored}], None\n\n\nclass PadClap',
            )
        ],
    ),
    ("tags_pad top_k: the human-voice classes are listed", [("[i for i in full if i not in speech][:P.SCORE_K + 1]", "list(full)[:P.SCORE_K + 1]")]),
    (
        "tags_pad max_unstored_score: always null",
        [
            (
                '"top_k": top_k, "max_unstored_score": unstored}], None\n\n\nclass PadClap',
                '"top_k": top_k, "max_unstored_score": None}], None\n\n\nclass PadClap',
            )
        ],
    ),
    (
        "clap_pad model: the variant stays best_beam_48k",
        [
            (
                'self.model_meta = P.model_block(self.model_meta["name"], self.model_meta["version"], VARIANT, self.model_meta["sha256"])',
                "self.model_meta = dict(self.model_meta)",
            )
        ],
    ),
    (
        "clap_pad claim: human_verified true",
        [
            (
                '"model": dict(self.model_meta), "claim": dict(P.CLAIM)',
                '"model": dict(self.model_meta), "claim": {"human_verified": True, "source": "model_opinion"}',
            )
        ],
    ),
    (
        "clap_pad top_k: only the 3 shown",
        [("top_k, unstored = P.score_tail(p, self.prompts, order)", "top_k, unstored = P.score_tail(p, self.prompts, order[:3])")],
    ),
    (
        "clap_pad max_unstored_score: absent",
        [('"top_k": top_k, "max_unstored_score": unstored}], None\n\n\ndef make_tags_pad', '"top_k": top_k}], None\n\n\ndef make_tags_pad')],
    ),
]


def run(name, edits):
    root = tempfile.mkdtemp(prefix="mutpad_")
    try:
        for d in ("analysis", "tools", "classify", "contracts"):
            shutil.copytree(os.path.join(BMA, d), os.path.join(root, d), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        p = os.path.join(root, "analysis", "pad_lane.py")
        s = open(p, encoding="utf-8", newline="").read()
        for old, new in edits:
            if s.count(old) != 1:
                return name, None, f"mutation does not apply ({s.count(old)} matches): {old[:60]!r}"
            s = s.replace(old, new)
        open(p, "w", encoding="utf-8", newline="").write(s)
        r = subprocess.run(
            [sys.executable, "-I", os.path.join(root, "analysis", "test_pad_lane.py"), "--unit-only"],
            capture_output=True,
            text=True,
            cwd=os.path.join(root, "analysis"),
            timeout=600,
        )
        red = [l[5:90] for l in r.stdout.splitlines() if l.startswith("FAIL ")]
        return name, (r.returncode != 0), red or r.stderr[-200:]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    bad = 0
    base = subprocess.run([sys.executable, "-I", os.path.join(HERE, "test_pad_lane.py"), "--unit-only"], capture_output=True, text=True, cwd=HERE, timeout=600)
    print("baseline exit", base.returncode, "(must be 0)")
    if base.returncode:
        return 1
    for name, edits in M:
        n, caught, why = run(name, edits)
        print(("RED  " if caught else "GREEN ") + n + "  <- " + (str(why[:2]) if caught else str(why)))
        bad += 0 if caught else 1
    print(f"{len(M) - bad}/{len(M)} mutations caught")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
