#!/usr/bin/env python3
"""Download the Perch 2.0 ONNX model and its label file into --dest, verified against pinned sha256 values (stdlib only).

    python fetch_perch.py --dest <model-dir>/perch

Files (both pinned to a repository COMMIT, not a branch, and to a size and a sha256 that were measured on the real download):
  perch_v2.onnx  https://huggingface.co/justinchuby/Perch-onnx @ da88bf8c37c0b3068fc706fe509f633b9fbd28c1   409,148,616 bytes
  labels.csv     https://huggingface.co/cgeorgiaw/Perch        @ c3c3c816976a6c1bb75140b8fbaef7397a6f708f   assets/labels.csv
The ONNX repack ships no label file; the labels are Google's own assets/labels.csv (first line = the list's name, then the 14,795
labels in output order) from the TF re-upload of the same Perch 2.0 model.
Licence: both Hugging Face pages declare `license: apache-2.0` in their metadata. Neither repository contains a licence file and
neither says anything about the WEIGHTS' terms; the original Kaggle model page (google/bird-vocalization-classifier) could not
be read by a script. So the weights' licence is UNVERIFIED: read it on Kaggle before this repo or its model files are published.

Behaviour: refuses to overwrite (exit 2, nothing downloaded, when either target exists); downloads to <name>.part (created
exclusively), checks size and sha256, then renames; a mismatch deletes the partial file and exits 1; any download failure exits 1.
The only network traffic is these two GETs. Exit 0 = both files in place and verified."""

import argparse, hashlib, os, sys, urllib.request

PINNED = [
    {
        "name": "perch_v2.onnx",
        "size": 409148616,
        "sha256": "bf0c8467a924cb074663970ca4a0ab1e143602121930209657d0dff5d5cefa1f",
        "url": "https://huggingface.co/justinchuby/Perch-onnx/resolve/da88bf8c37c0b3068fc706fe509f633b9fbd28c1/perch_v2.onnx",
    },
    {
        "name": "labels.csv",
        "size": 312716,
        "sha256": "e4d5c0397d8fb08bf90c6b13a34810af53504faad927e472fcc567793c9de057",
        "url": "https://huggingface.co/cgeorgiaw/Perch/resolve/c3c3c816976a6c1bb75140b8fbaef7397a6f708f/assets/labels.csv",
    },
]


def fetch(dest, files=None, log=print):
    """-> 0 ok, 1 download / verification failure, 2 refused (a target exists). `files`: list of {name,size,sha256,url}."""
    files = PINNED if files is None else files
    existing = [f["name"] for f in files if os.path.lexists(os.path.join(dest, f["name"]))]
    if existing:
        log(f"refusing to overwrite: {', '.join(existing)} already in {dest}")
        return 2
    os.makedirs(dest, exist_ok=True)
    done = []
    for f in files:
        target, part = os.path.join(dest, f["name"]), os.path.join(dest, f["name"]) + ".part"
        h, n = hashlib.sha256(), 0
        try:
            with urllib.request.urlopen(f["url"], timeout=60) as r, open(part, "xb") as out:
                for blk in iter(lambda: r.read(1 << 20), b""):
                    h.update(blk)
                    out.write(blk)
                    n += len(blk)
        except (OSError, ValueError) as e:  # URLError and HTTPError are OSErrors
            log(f"download of {f['name']} failed: {type(e).__name__}: {e}")
            _drop(part)
            return 1
        if n != f["size"] or h.hexdigest() != f["sha256"]:
            log(f"{f['name']}: got {n} bytes, sha256 {h.hexdigest()}; pinned {f['size']} bytes, sha256 {f['sha256']}: deleted")
            _drop(part)
            return 1
        if os.path.lexists(target):  # appeared while downloading
            log(f"refusing to overwrite: {f['name']} appeared during the download")
            _drop(part)
            return 2
        os.replace(part, target)
        done.append(f["name"])
        log(f"ok {f['name']}  {n} bytes  sha256 {f['sha256']}")
    return 0


def _drop(path):
    try:
        os.remove(path)
    except OSError:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", required=True, help="directory for perch_v2.onnx and labels.csv (created if absent)")
    a = ap.parse_args(argv)
    return fetch(a.dest)


if __name__ == "__main__":
    sys.exit(main())
