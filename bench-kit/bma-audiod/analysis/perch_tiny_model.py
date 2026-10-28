"""A REAL, tiny, deterministic ONNX model with Perch's interface, for tests and contract fixtures (needs `onnx`; running it needs
onnxruntime). It is a bank of 8 single-frequency detectors, so a known tone maps to a known label with a known logit:

    inputs [batch, 160000] float32 (5 s at 32 kHz)
    c_j = (2/N) sum_t x[t] cos(2 pi f_j t / 32000)      s_j = (2/N) sum_t x[t] sin(2 pi f_j t / 32000)      N = 160000
    label[:, j]     = SCALE * (c_j^2 + s_j^2) - 0.1 j    (a tone A sin(2 pi f_j t) gives SCALE * A^2 - 0.1 j; the bias keeps the idle
                                                          ranking fixed: label 0 first, then 1, 2, ...)
    embedding[:, :] = concat(c, s)                       (16 values)

With SCALE 100 and the Perch normalisation (peak 0.25) a pure tone of f_j gives label j the logit 6.25 - 0.1 j and every other label -0.1 k.
Frequencies 1000 .. 8000 Hz in 1000 Hz steps are integer multiples of 32000/32, so each tone fits 1000 * k cycles exactly."""

import os
import numpy as np

FS, N = 32000, 160000
FREQS = [1000.0 * k for k in range(1, 9)]
SCALE = 100.0
LABELS = ["Poecile carolinensis", "Poecile atricapillus", "Cyanocitta cristata", "Oecanthus dulcisonans", "Insect", "Speech", "Alarm", "Cricket"]
LIST_NAME = "tiny_test"


def write_labels(path, labels=LABELS):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join([LIST_NAME] + list(labels)) + "\n")


def build(path, freqs=FREQS, scale=SCALE, poison=None):
    import onnx
    from onnx import TensorProto, helper, numpy_helper

    t = np.arange(N, dtype=np.float64) / FS
    wc = np.stack([2.0 / N * np.cos(2 * np.pi * f * t) for f in freqs], axis=1).astype(np.float32)
    ws = np.stack([2.0 / N * np.sin(2 * np.pi * f * t) for f in freqs], axis=1).astype(np.float32)
    nodes = [
        helper.make_node("MatMul", ["inputs", "wc"], ["c"]),
        helper.make_node("MatMul", ["inputs", "ws"], ["s"]),
        helper.make_node("Mul", ["c", "c"], ["c2"]),
        helper.make_node("Mul", ["s", "s"], ["s2"]),
        helper.make_node("Add", ["c2", "s2"], ["p"]),
        helper.make_node("Mul", ["p", "scale"], ["scaled"]),
        helper.make_node("Add", ["scaled", "bias"], ["label"]),
        helper.make_node("Concat", ["c", "s"], ["embedding"], axis=1),
    ]
    if poison in ("label", "embedding"):  # tests only: the named output gets a NaN added (a model that misbehaves)
        nodes[-2 if poison == "label" else -1].output[0] = poison + "_raw"
        nodes.append(helper.make_node("Add", [poison + "_raw", "nan"], [poison]))
    g = helper.make_graph(
        nodes,
        "tiny_perch",
        [helper.make_tensor_value_info("inputs", TensorProto.FLOAT, ["batch", N])],
        [
            helper.make_tensor_value_info("embedding", TensorProto.FLOAT, ["batch", 2 * len(freqs)]),
            helper.make_tensor_value_info("label", TensorProto.FLOAT, ["batch", len(freqs)]),
        ],
        [
            numpy_helper.from_array(wc, "wc"),
            numpy_helper.from_array(ws, "ws"),
            numpy_helper.from_array(np.array(scale, np.float32), "scale"),
            numpy_helper.from_array((-0.1 * np.arange(len(freqs))).astype(np.float32), "bias"),
        ]
        + ([numpy_helper.from_array(np.array(np.nan, np.float32), "nan")] if poison else []),
    )
    m = helper.make_model(g, opset_imports=[helper.make_opsetid("", 13)])
    m.ir_version = 8
    onnx.checker.check_model(m)
    onnx.save(m, path)
    return path


def build_dir(d):
    """-> (model_path, labels_path) written into directory d."""
    os.makedirs(d, exist_ok=True)
    mp, lp = os.path.join(d, "tiny_perch.onnx"), os.path.join(d, "labels.csv")
    build(mp)
    write_labels(lp)
    return mp, lp
