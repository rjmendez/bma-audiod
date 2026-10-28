#!/usr/bin/env python3
"""One-time (idempotent) model download into the BMA_MODEL_DIR volume: PANNs CNN14 + its label table, and CLAP
htsat-unfused (weights: its pytorch_model.bin; the same set analysis/install_models.sh installs by hand). Needs network access:
  docker compose --profile setup run --rm models
Usage: fetch_models.py MODEL_DIR"""

import os, sys, urllib.request

PANNS = {
    "Cnn14_mAP=0.431.pth": "https://zenodo.org/records/3987831/files/Cnn14_mAP%3D0.431.pth?download=1",
    "class_labels_indices.csv": "https://raw.githubusercontent.com/qiuqiangkong/audioset_tagging_cnn/master/metadata/class_labels_indices.csv",
}


def have(path):
    return os.path.exists(path) and os.path.getsize(path) > 0


def main(argv):
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    root = argv[1]
    pdir = os.path.join(root, "panns_data")
    os.makedirs(pdir, exist_ok=True)
    for name, url in PANNS.items():
        dst = os.path.join(pdir, name)
        if have(dst):
            print("have", dst, flush=True)
            continue
        tmp = dst + ".part"
        urllib.request.urlretrieve(url, tmp)
        if os.path.getsize(tmp) == 0:
            print("empty download:", url, file=sys.stderr)
            return 1
        os.replace(tmp, dst)
        print("fetched", dst, os.path.getsize(dst), "bytes", flush=True)
    from huggingface_hub import snapshot_download

    p = snapshot_download("laion/clap-htsat-unfused", allow_patterns=["*.json", "*.txt", "*.safetensors", "pytorch_model.bin"])
    print("CLAP at", p, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
