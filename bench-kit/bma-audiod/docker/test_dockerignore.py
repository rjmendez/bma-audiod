#!/usr/bin/env python3
"""Test for .dockerignore with a REAL `docker build`: a scratch context holds a copy of the real .dockerignore, the
machine-specific override files that .gitignore lists (they must never be baked into an image), their tracked templates
and ordinary source files. The image is `FROM scratch` + `COPY . /ctx/`, exported to a directory; the exact set of files
that made it into the image is asserted. Exit 77 (skipped, not passed) when no working docker/BuildKit is available.
Run from anywhere:  python test_dockerignore.py"""

import os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
IGNORE = os.path.join(HERE, "..", ".dockerignore")
GITIGNORE = os.path.join(HERE, "..", ".gitignore")
DOCKER = shutil.which("docker")
if not DOCKER:
    print("SKIP: docker not found")
    sys.exit(77)

# overrides: must NOT reach the image; kept: must reach it (positive twin: the same directories, the tracked templates)
OVERRIDES = [
    "pc/bma.local.ps1",
    ".env",
    "sub/dir/.env",
    "classify/geo_county.local.json",
    "scripts/private_names.txt",
    "data/analysis/x.jsonl",
    "models/panns_data/w.pth",
]
KEPT = [
    "pc/bma.local.example.ps1",
    ".env.example",
    "classify/geo_county.example.json",
    "scripts/check_private_paths.py",
    "analysis/host.py",
    "docker/entrypoint.sh",
]

fails = []
with tempfile.TemporaryDirectory() as td:
    ctx, out = os.path.join(td, "ctx"), os.path.join(td, "out")
    for rel in OVERRIDES + KEPT:
        p = os.path.join(ctx, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write("x\n")
    shutil.copy(IGNORE, os.path.join(ctx, ".dockerignore"))
    df = os.path.join(td, "Dockerfile")
    with open(df, "w") as f:
        f.write("FROM scratch\nCOPY . /ctx/\n")
    r = subprocess.run(
        [DOCKER, "build", "-f", df, "--output", "type=local,dest=" + out, ctx],
        capture_output=True,
        text=True,
        timeout=300,
        env=dict(os.environ, DOCKER_BUILDKIT="1"),
    )
    if r.returncode != 0:
        print("SKIP: docker build with BuildKit failed:\n" + r.stderr[-400:])
        sys.exit(77)
    got = sorted(
        os.path.relpath(os.path.join(d, f), os.path.join(out, "ctx")).replace(os.sep, "/") for d, _, fs in os.walk(os.path.join(out, "ctx")) for f in fs
    )
    want = sorted(KEPT + [".dockerignore"])
    if got != want:
        fails.append(f"image holds {got}, want {want}")

# every local-override file name that .gitignore lists must have a counterpart here (keeps this test honest when it grows)
listed = [ln.strip() for ln in open(GITIGNORE, encoding="utf-8") if ln.strip() and not ln.startswith("#")]
for pat in listed:
    if pat.endswith("/"):
        continue  # data directories, covered by the OVERRIDES entries above
    if not any(o == pat or o.endswith("/" + pat) for o in OVERRIDES):
        fails.append(f".gitignore lists {pat!r} but this test has no override file for it")

print("FAIL " + "\nFAIL ".join(fails) if fails else "PASS .dockerignore keeps every .gitignore'd override out of the image, templates and sources in")
sys.exit(1 if fails else 0)
