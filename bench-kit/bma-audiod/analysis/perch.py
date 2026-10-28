"""Perch 2.0 SHADOW plugin: Google's multi-taxa bioacoustic classifier (birds, frogs, crickets, katydids, mammals) run beside
BirdNET on the live windows, as a second opinion. METADATA ONLY: the plugin logs the top-5 labels with their raw logits, a flag
for the processed source, timestamps and the model version to <--out>/perch.jsonl, plus one `perch_agreement` aggregate per
minute (does Perch's top species match BirdNET's?). Interface: contracts/specs/perch.md.

Model: the community ONNX repack of Perch v2 (input `inputs` [batch, 160000] float32 = 5 s mono at 32 kHz; outputs `embedding`
[batch, 1536] and `label` [batch, 14795] logits; the label list is a separate file whose first line is the list's name and
whose other lines are the labels in output order). onnxruntime is imported lazily: a deploy must install it into
<model-dir>/overlay (run_analyzer.sh puts that directory on PYTHONPATH). Without the model, the labels or onnxruntime the
plugin is not started and the analyzer carries on (status.json plugins.perch.reason says "not started").

Per 5 s window of the 8 beams (48 kHz) two sources are classified, each mono, resampled 48k -> 32k (resample_poly 2:3, never
upsampled) and normalised the way the Perch reference code does (mean removed, peak scaled to 0.25):
  * `mixed`   - the mean of the 8 beams' waveforms (the array's summed signal),
  * `loudest` - the single beam with the highest RMS (the beam BirdNET always sees).

PRIVACY (owner policy): no audio is stored anywhere; audio exists only in the analyzer's RAM rings and the arrays of one call.
  * a window the tagger flagged as speech produces NO record, NO embedding and NO agreement entry: the SpeechGuard holds the
    tagger's per-window flags, and a Perch window is only written once the tagger's windows cover it completely and none of
    them is speech. Windows the tagger never covered (dropped, errored) are dropped too (fail closed), counted.
  * human-voice labels are never reported: when any label of the human-voice set is in a source's top 5 the whole window is
    dropped (counted), so such a label never appears in a record.
  * a window whose loudest beam is below the birds gate (-70 dBFS) produces nothing (counted).
  * embeddings (1536 float16 = 3072 bytes) are stored only with --perch-embeddings, in a size-bounded rotating pair of files.
"""

import hashlib, json, math, os, sys, threading, time
import numpy as np
from scipy import signal

FS = 48000  # the analyzer's beam rate
NB = 8
FS_MODEL = 32000  # what Perch 2.0 expects
WIN_S = 5.0
N_IN = int(WIN_S * FS)  # 240000 samples per beam
N_MODEL = int(WIN_S * FS_MODEL)  # 160000 samples fed to the model
GATE_DBFS = -70.0  # same as plugins.MultiBeamBirds (a test pins the equality)
BIRDS_WIN_S = 3.0  # plugins.MultiBeamBirds.win_s (a test pins the equality)
TARGET_PEAK = 0.25  # perch-hoplite normalize_audio target_peak for Perch v2
TOPK = 5
MIN_OVERLAP_S = 2.0  # a birds window counts for a Perch window when they overlap at least this long
AGREE_S = 60  # agreement records are per UTC-aligned minute
AGREE_GRACE_S = 15.0  # a minute is closed this long after its end (the birds windows of its tail arrive late)
PENDING_MAX_S = 30.0  # a window the tagger has not covered by then is dropped
COVER_TOL_S = 0.5  # tolerance for window-edge jitter when checking the tagger covers a Perch window
EMB_CAP_BYTES = 64 << 20  # rotation size of the embedding file: two generations, 128 MiB at most
RECORD_VERSION = 1

# FSD50k class names that Perch 2.0's label list contains and that are human voices or human vocal sounds. A source with
# any of them in its top 5 is not reported at all. (Names not present in a given label list are simply never matched.)
HUMAN_VOICE_LABELS = frozenset(
    {
        "Speech",
        "Male_speech_and_man_speaking",
        "Female_speech_and_woman_speaking",
        "Child_speech_and_kid_speaking",
        "Conversation",
        "Chatter",
        "Speech_synthesizer",
        "Human_voice",
        "Whispering",
        "Shout",
        "Yell",
        "Screaming",
        "Singing",
        "Male_singing",
        "Female_singing",
        "Laughter",
        "Chuckle_and_chortle",
        "Giggle",
        "Crying_and_sobbing",
        "Cough",
        "Sneeze",
        "Breathing",
        "Gasp",
        "Sigh",
        "Burping_and_eructation",
        "Respiratory_sounds",
        "Crowd",
        "Cheering",
        "Human_group_actions",
        "Homo sapiens",
    }
)  # the one binomial among them: the real list names the human species as a bird-style label
INSECT_CLASS_LABELS = frozenset({"Insect", "Cricket"})  # non-species FSD50k classes that mean "insect"


def provider_list(device):
    """'cpu' -> CPU only; 'cuda' / 'cuda:N' -> CUDA on device N, CPU behind it (the caller checks the ACTIVE provider)."""
    if device == "cpu":
        return ["CPUExecutionProvider"]
    if device == "cuda" or (isinstance(device, str) and device.startswith("cuda:") and device[5:].isdigit()):
        return [("CUDAExecutionProvider", {"device_id": int(device[5:]) if ":" in device else 0}), "CPUExecutionProvider"]
    raise PerchUnavailable(f"not started: unknown device {device!r} (cpu, cuda, cuda:N)")


class PerchUnavailable(Exception):
    """The plugin cannot start (model, labels or onnxruntime missing or unusable). The message starts with 'not started'."""


def is_species(label):
    """Perch labels are species binomials ('Poecile carolinensis') or FSD50k classes ('Insect', 'Alarm', 'Wind_chime')."""
    return " " in label and "_" not in label


def read_labels(path):
    """-> (list_name, [label, ...]) from a label file: line 1 = the list's name, then one label per line in output order.
    Blank lines inside and duplicate labels are errors (they would shift or alias the output indices)."""
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        lines = f.read().replace("\r\n", "\n").split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    if len(lines) < 2:
        raise PerchUnavailable(f"not started: label file {os.path.basename(path)} has no labels")
    name, labels = lines[0], lines[1:]
    if any(not x.strip() for x in labels):
        raise PerchUnavailable(f"not started: label file {os.path.basename(path)} has a blank label line")
    if len(set(labels)) != len(labels):
        raise PerchUnavailable(f"not started: label file {os.path.basename(path)} has duplicate labels")
    return name, labels


def resample_32k(x48):
    """48 kHz mono -> 32 kHz (polyphase 2:3, anti-aliased); N_IN samples in, N_MODEL out."""
    y = signal.resample_poly(np.asarray(x48, np.float32), 2, 3).astype(np.float32)
    if y.shape[0] != N_MODEL:
        raise ValueError(f"resampled window has {y.shape[0]} samples, expected {N_MODEL}")
    return y


def peak_normalize(y, target=TARGET_PEAK):
    """Remove the mean, scale so the peak is `target` (all-zero input stays zero). The Perch reference does the same."""
    y = np.asarray(y, np.float32) - np.float32(np.mean(y, dtype=np.float64))
    peak = float(np.max(np.abs(y)))
    if peak <= 0.0:
        return np.zeros_like(y)
    return (y * np.float32(target / peak)).astype(np.float32)


def top_k_tail(logits, labels, k=TOPK):
    """(top_k, max_unstored_score): the k largest logits (as top_k) and the (k+1)th, the best logit not stored; None when there is no (k+1)th."""
    idx = np.argsort(-np.asarray(logits, np.float64), kind="stable")[: k + 1]
    return ([[labels[i], round(float(logits[i]), 3)] for i in idx[:k]], round(float(logits[idx[k]]), 3) if len(idx) > k else None)


def top_k(logits, labels, k=TOPK):
    """[[label, logit rounded to 3 decimals], ...] for the k largest logits, largest first, ties by lower index."""
    return top_k_tail(logits, labels, k)[0]


def db(x, floor=1e-12):
    return 20.0 * math.log10(max(float(x), floor))


# =====================================================================================================
class SpeechGuard:
    """The tagger's per-window speech flags, as time intervals of wall time (t_wall is a window's END, as everywhere).
    verdict(t0, t1) says what is known about a Perch window [t0, t1]:
      'speech'  - some tagger window overlapping it was flagged speech (decided at once),
      'clear'   - tagger windows overlap it, none is speech, and together they cover it (gaps <= COVER_TOL_S),
      'unknown' - the tagger saw its end but left a gap (a dropped or failed tagger window): treated as speech,
      None      - the tagger has not reached the window's end yet: wait."""

    def __init__(self, keep_s=180.0):
        self.keep_s = keep_s
        self.w = []
        self.lock = threading.Lock()

    def note(self, t0, t1, active):
        with self.lock:
            self.w.append((float(t0), float(t1), bool(active)))
            cut = float(t1) - self.keep_s
            if len(self.w) > 8 and self.w[0][1] < cut:
                self.w = [x for x in self.w if x[1] >= cut]

    def verdict(self, t0, t1):
        with self.lock:
            ws = sorted(x for x in self.w if x[1] > t0 and x[0] < t1)
            last_end = max((x[1] for x in self.w), default=None)
        if any(x[2] for x in ws):
            return "speech"
        if last_end is None or last_end < t1 - COVER_TOL_S:
            return None
        pos = t0
        for a, b, _ in ws:
            if a > pos + COVER_TOL_S:
                return "unknown"
            pos = max(pos, b)
        return "clear" if pos >= t1 - COVER_TOL_S else "unknown"


# =====================================================================================================
class PerchEmbeddingStore:
    """--perch-embeddings: float16 vectors in perch_emb.f16 plus one index line per vector in perch_emb.jsonl
    ({"i","t","src","beam","run"}); the vector of index i sits at byte (i - first_i_of_the_file) * 2 * dim. Numbering continues across restarts (i is unique over
    both generations); files whose vector count differs from their index lines start a new generation; a failed write is undone
    (both files cut back) so the rule never breaks. When perch_emb.f16
    reaches `cap` bytes both files rotate to *.1 (one previous generation, so the disk bound is 2 x cap). Never raises:
    failures are counted in `errors` and the vector is lost (add() returns None)."""

    def __init__(self, out_dir, cap=EMB_CAP_BYTES, run=None):
        self.dir, self.cap, self.run = out_dir, int(cap), run
        self.bin, self.idx = os.path.join(out_dir, "perch_emb.f16"), os.path.join(out_dir, "perch_emb.jsonl")
        self.errors, self.rotations = 0, 0
        self.lock = threading.Lock()
        self.n = self._next_index()  # numbering continues across analyzer restarts: "i" is unique over both generations
        self._aligned = False  # the vector file is checked against the index once, at the first add

    @staticmethod
    def _index_lines(path):
        """-> list of the "i" values of the valid index lines of path ([] when the file is missing or unreadable)."""
        try:
            with open(path, "rb") as f:
                lines = f.read().split(b"\n")
        except OSError:
            return []
        out = []
        for ln in lines:
            try:
                out.append(int(json.loads(ln)["i"]))
            except (ValueError, KeyError, TypeError):
                pass
        return out

    def _next_index(self):
        for path in (self.idx, self.idx + ".1"):
            ii = self._index_lines(path)
            if ii:
                return max(ii) + 1
        return 0

    def _rotate(self):
        os.replace(self.bin, self.bin + ".1")
        os.replace(self.idx, self.idx + ".1")
        self.rotations += 1

    @staticmethod
    def _size(path):
        return os.path.getsize(path) if os.path.exists(path) else 0

    @staticmethod
    def _truncate(path, size):
        if os.path.exists(path) and os.path.getsize(path) > size:
            with open(path, "r+b") as f:
                f.truncate(size)

    def add(self, vec, t, src, beam):
        data = np.asarray(vec).astype(np.float16).tobytes()
        with self.lock:
            sizes = None
            try:
                if not self._aligned:
                    # files left by an earlier run must hold exactly one vector per index line, else the offset rule is already
                    # broken: start a new generation instead of appending to them
                    if os.path.exists(self.bin) and self._size(self.bin) != len(self._index_lines(self.idx)) * len(data):
                        self._rotate()
                    self._aligned = True
                if os.path.exists(self.bin) and os.path.getsize(self.bin) + len(data) > self.cap:
                    self._rotate()
                sizes = (self._size(self.bin), self._size(self.idx))
                with open(self.bin, "ab") as f:
                    f.write(data)
                with open(self.idx, "ab") as f:
                    f.write(
                        (json.dumps({"i": self.n, "t": round(float(t), 3), "src": src, "beam": beam, "run": self.run}, separators=(",", ":")) + "\n").encode(
                            "utf-8"
                        )
                    )
            except OSError:
                self.errors += 1
                if sizes is not None:  # undo a half-written pair so every later index line still points at its own vector
                    for path, size in ((self.bin, sizes[0]), (self.idx, sizes[1])):
                        try:
                            self._truncate(path, size)
                        except OSError:
                            self.errors += 1
                return None
            self.n += 1
            return self.n - 1


# =====================================================================================================
def _genus(sci):
    return (sci or "").strip().lower().split(" ")[0]


class Agreement:
    """Per-minute comparison of Perch's top species with BirdNET's, on the SAME windows, without joining raw rows later.

    Perch window P = [t1 - 5, t1] (its `loudest` source's best species decides). Birds windows B = [b - 3, b] (b = the birds
    plugin's window end) overlap P by min(t1, b) - max(t1 - 5, b - 3) seconds; those overlapping >= MIN_OVERLAP_S and that
    classified at least one beam are P's birds opinion: BirdNET's top species = the highest-confidence non-non_species `bird`
    row of any of them. A P with no such birds window is `birds_missing` (BirdNET has no opinion: gated, dropped or late), which
    is NOT silence. Perch names a species when its best species logit >= min_logit. Per window exactly one of:
      both_same_species  both name the same scientific name (case-insensitive)
      both_same_genus    both name species of the same genus, different species
      both_differ        both name a species, different genera
      perch_only         Perch names a species, BirdNET is silent      birds_only  the reverse
      both_silent        neither names one                              birds_missing  see above
    so windows = sum of the seven. Also per minute: perch_top1_groups (group of Perch's overall top-1 label of the loudest
    source: bird|insect|amphibian|mammal|other-bio|non_species|unknown) and insect {perch, birds, both}: windows whose Perch
    top-1 group is insect, windows whose BirdNET top species group is insect (birds windows only), and both."""

    CATS = ("both_same_species", "both_same_genus", "both_differ", "perch_only", "birds_only", "both_silent", "birds_missing")

    def __init__(self, mv, min_logit):
        self.mv, self.min_logit = mv, float(min_logit)
        self.perch = {}  # minute index -> [(t1, best_species_sci|None, top1_group)]
        self.birds = []  # (b0, b1, available, top|None)  top = (scientific lower, group, confidence)
        self.lock = threading.Lock()
        self.emitted = 0

    def add_birds(self, b0, b1, available, top):
        with self.lock:
            self.birds.append((float(b0), float(b1), bool(available), top))

    def add_perch(self, t1, best_sci, best_logit, top1_group):
        named = best_sci if (best_sci is not None and best_logit is not None and best_logit >= self.min_logit) else None
        with self.lock:
            self.perch.setdefault(int(t1 // AGREE_S), []).append((float(t1), named, top1_group))

    def _classify(self, t1, named):
        p0, opinion, any_avail = t1 - WIN_S, None, False
        for b0, b1, avail, top in self.birds:
            if avail and min(t1, b1) - max(p0, b0) >= MIN_OVERLAP_S:
                any_avail = True
                if top is not None and (opinion is None or top[2] > opinion[2]):
                    opinion = top
        if not any_avail:
            return "birds_missing", None
        if named is None and opinion is None:
            return "both_silent", opinion
        if named is None:
            return "birds_only", opinion
        if opinion is None:
            return "perch_only", opinion
        if named.lower() == opinion[0]:
            return "both_same_species", opinion
        if _genus(named) == _genus(opinion[0]):
            return "both_same_genus", opinion
        return "both_differ", opinion

    def close(self, now_t, final=False):
        """Records for every minute that has ended by now_t - AGREE_GRACE_S (all minutes when final, marked partial)."""
        out = []
        with self.lock:
            for m in sorted(self.perch):
                if not final and now_t < (m + 1) * AGREE_S + AGREE_GRACE_S:
                    continue
                rows = self.perch.pop(m)
                c = {k: 0 for k in self.CATS}
                groups, ins = {}, {"perch": 0, "birds": 0, "both": 0}
                for t1, named, grp in rows:
                    cat, opinion = self._classify(t1, named)
                    c[cat] += 1
                    groups[grp] = groups.get(grp, 0) + 1
                    p_ins = grp == "insect"
                    b_ins = opinion is not None and opinion[1] == "insect"
                    ins["perch"] += 1 if p_ins else 0
                    ins["birds"] += 1 if b_ins else 0
                    ins["both"] += 1 if (p_ins and b_ins) else 0
                rec = {
                    "k": "perch_agreement",
                    "v": RECORD_VERSION,
                    "t": float(m * AGREE_S),
                    "mv": self.mv,
                    "min_logit": self.min_logit,
                    "windows": len(rows),
                    **c,
                    "perch_top1_groups": dict(sorted(groups.items())),
                    "insect": ins,
                }
                if final and now_t < (m + 1) * AGREE_S + AGREE_GRACE_S:
                    rec["partial"] = True
                out.append(rec)
            if self.perch:
                lo = min(self.perch) * AGREE_S - 10.0
            else:
                lo = now_t - 2 * AGREE_S
            self.birds = [b for b in self.birds if b[1] >= lo]
            self.emitted += len(out)
        return out


# =====================================================================================================
class Perch:
    name = "perch"
    win_s, hop_s = WIN_S, WIN_S
    heavy = True

    def __init__(
        self,
        model_path,
        labels_path,
        guard,
        taxa=None,
        gate_dbfs=GATE_DBFS,
        embeddings_dir=None,
        threads=4,
        min_logit=0.0,
        run=None,
        emb_cap=EMB_CAP_BYTES,
        device="cpu",
        ort_dir=None,
    ):
        """guard: the SpeechGuard the host feeds from the tagger (required: it is what keeps speech out). taxa: a taxa.Taxa
        used ONLY for the group of Perch labels (its own instance: the lookups must not inflate the birds counters).
        embeddings_dir: None = no embeddings stored (default), else the directory of perch_emb.f16 / perch_emb.jsonl."""
        try:
            self._init(model_path, labels_path, guard, taxa, gate_dbfs, embeddings_dir, threads, min_logit, run, emb_cap, device, ort_dir)
        except PerchUnavailable:
            raise
        except Exception as e:
            raise PerchUnavailable(f"not started: {type(e).__name__}: {e}") from e

    def _init(self, model_path, labels_path, guard, taxa, gate_dbfs, embeddings_dir, threads, min_logit, run, emb_cap, device, ort_dir):
        if guard is None:
            raise PerchUnavailable("not started: no speech guard")
        providers = provider_list(device)
        if ort_dir and ort_dir not in sys.path:
            sys.path.append(ort_dir)  # appended: nothing in the venv is shadowed
        if device != "cpu":
            try:
                import torch  # noqa: F401  loads the CUDA and cuDNN libraries onnxruntime-gpu needs into this process
            except ImportError:
                pass
        try:
            import onnxruntime as ort
        except ImportError as e:
            raise PerchUnavailable(f"not started: onnxruntime is not installed ({e}); install it into <model-dir>/overlay") from e
        if not os.path.isfile(model_path):
            raise PerchUnavailable(f"not started: model file not found: {os.path.basename(str(model_path))}")
        if not labels_path or not os.path.isfile(labels_path):
            raise PerchUnavailable(f"not started: label file not found: {os.path.basename(str(labels_path))}")
        self.list_name, self.labels = read_labels(labels_path)
        so = ort.SessionOptions()
        so.intra_op_num_threads = int(threads)
        so.inter_op_num_threads = 1
        self.sess = ort.InferenceSession(model_path, so, providers=providers)
        self.device = device
        if device != "cpu" and self.sess.get_providers()[0] != "CUDAExecutionProvider":
            raise PerchUnavailable(f"not started: {device} requested but the active provider is {self.sess.get_providers()[0]}")
        ins, outs = self.sess.get_inputs(), {o.name: o for o in self.sess.get_outputs()}
        if len(ins) != 1 or ins[0].shape[-1] != N_MODEL:
            raise PerchUnavailable(f"not started: model input is not [batch, {N_MODEL}]: {[(i.name, i.shape) for i in ins]}")
        if "label" not in outs or "embedding" not in outs:
            raise PerchUnavailable(f"not started: model lacks a `label` or `embedding` output: {sorted(outs)}")
        if outs["label"].shape[-1] != len(self.labels):
            raise PerchUnavailable(f"not started: model has {outs['label'].shape[-1]} labels, the label file {len(self.labels)}")
        self.in_name = ins[0].name
        h = hashlib.sha256()
        with open(model_path, "rb") as f:
            for blk in iter(lambda: f.read(1 << 20), b""):
                h.update(blk)
        self.mv = f"{os.path.basename(model_path)}@{h.hexdigest()[:12]}"
        self.model_sha256 = h.hexdigest()
        self.guard, self.gate, self.min_logit, self.run = guard, float(gate_dbfs), float(min_logit), run
        self.taxa = taxa
        self.species_idx = np.array([i for i, l in enumerate(self.labels) if is_species(l) and l not in HUMAN_VOICE_LABELS], dtype=np.int64)
        self._group = {}
        self.store = PerchEmbeddingStore(embeddings_dir, emb_cap, run) if embeddings_dir else None
        self.agree = Agreement(self.mv, self.min_logit)
        self.pending = []
        self.latest = 0.0
        self.n = {"gated": 0, "speech_skipped": 0, "voice_skipped": 0, "unknown_dropped": 0, "nonfinite": 0}

    # ---- helpers
    def group_of(self, lab):
        """taxa group of a label: non_species / insect for FSD classes, the taxa table's group otherwise, 'unknown' w/o table."""
        g = self._group.get(lab)
        if g is None:
            if not is_species(lab):
                g = "insect" if lab in INSECT_CLASS_LABELS else "non_species"
            else:
                g = (self.taxa.lookup(lab)[0] if self.taxa is not None else None) or "unknown"
            self._group[lab] = g
        return g

    def stats(self):
        return {
            **self.n,
            "pending": len(self.pending),
            "agreement_records": self.agree.emitted,
            "mv": self.mv,
            "device": self.device,
            "embeddings_stored": self.store.n if self.store else 0,
            "embedding_errors": self.store.errors if self.store else 0,
        }

    def feed_birds(self, t_wall, recs, info=None):
        """The host calls this after every birds window (empty recs included) so silence is told from absence."""
        avail = (info or {}).get("beams_classified", 1) > 0
        top = None
        for r in recs:
            if r.get("k") == "bird" and not r.get("non_species") and r.get("scientific"):
                c = r.get("confidence") or 0.0
                if top is None or c > top[2]:
                    top = (r["scientific"].strip().lower(), r.get("group"), c)
        self.agree.add_birds(t_wall - BIRDS_WIN_S, t_wall, avail, top)

    # ---- the plugin interface
    def process(self, x, t_wall):
        self.latest = max(self.latest, t_wall)
        if x.ndim != 2 or x.shape != (N_IN, NB) or not np.isfinite(x).all():
            self.n["nonfinite"] += 1
        else:
            self._infer(x, t_wall)
        out = self._resolve()
        out += self.agree.close(self.latest)
        return out, None

    def _infer(self, x, t_wall):
        e = (x.astype(np.float64) ** 2).mean(axis=0)
        loud = int(np.argmax(e))
        if db(math.sqrt(e[loud])) < self.gate:
            self.n["gated"] += 1
            return
        mixed = x.astype(np.float64).mean(axis=1)
        srcs = [("mixed", None, mixed, db(math.sqrt(float((mixed**2).mean())))), ("loudest", loud + 1, x[:, loud].astype(np.float64), db(math.sqrt(e[loud])))]
        batch = np.stack([peak_normalize(resample_32k(s[2])) for s in srcs])
        o = self.sess.run(["embedding", "label"], {self.in_name: batch})
        emb, logits = o[0], o[1]
        if not (np.isfinite(emb).all() and np.isfinite(logits).all()):
            self.n["nonfinite"] += 1  # a NaN/inf output makes the ranking and the voice check meaningless: drop the window
            return
        recs, bests = [], []
        for j, (src, beam, _, lvl) in enumerate(srcs):
            l = logits[j]
            top, unstored = top_k_tail(l, self.labels)
            if any(t[0] in HUMAN_VOICE_LABELS for t in top):
                self.n["voice_skipped"] += 1
                return  # a human-voice label in the top 5: nothing of this window is kept
            bs = int(self.species_idx[np.argmax(l[self.species_idx])]) if self.species_idx.size else None
            bests.append(None if bs is None else (self.labels[bs], float(l[bs])))
            recs.append(
                {
                    "k": "perch",
                    "v": RECORD_VERSION,
                    "t": t_wall,
                    "src": src,
                    "beam": beam,
                    "level_dbfs": round(lvl, 1),
                    "top": top,
                    "top1_group": self.group_of(top[0][0]),
                    "best_species": None if bs is None else [self.labels[bs], round(float(l[bs]), 3)],
                    "mv": self.mv,
                    "run": self.run,
                    "emb_i": None,
                    "model": {"name": "Perch", "version": "2.0", "variant": src, "sha256": self.model_sha256},
                    "claim": {"human_verified": False, "source": "model_opinion"},
                    "max_unstored_score": unstored,
                }
            )
        self.pending.append({"t0": t_wall - WIN_S, "t1": t_wall, "recs": recs, "emb": emb.astype(np.float32), "best": bests})

    def _resolve(self):
        out = []
        for it in list(self.pending):
            v = self.guard.verdict(it["t0"], it["t1"])
            if v is None and self.latest - it["t1"] > PENDING_MAX_S:
                v = "unknown"
            if v is None:
                continue
            self.pending.remove(it)
            if v == "speech":
                self.n["speech_skipped"] += 1
            elif v == "unknown":
                self.n["unknown_dropped"] += 1
            else:
                out += self._emit(it)
        return out

    def _emit(self, it):
        recs = it["recs"]
        if self.store is not None:
            for j, r in enumerate(recs):
                r["emb_i"] = self.store.add(it["emb"][j], it["t1"], r["src"], r["beam"])
        loud = recs[1]
        best = it["best"][1]
        self.agree.add_perch(it["t1"], best[0] if best else None, best[1] if best else None, loud["top1_group"])
        return recs

    def flush(self):
        """Shutdown: windows the tagger has not confirmed are dropped (fail closed); open minutes are written, marked partial."""
        self.n["unknown_dropped"] += len(self.pending)
        self.pending = []
        return self.agree.close(self.latest, final=True)


def make(ctx):
    """Plugin factory named by analysis/plugins.manifest.json (ctx = manifest.Ctx). Needs the tags plugin: its speech flag keeps speech
    windows out of perch.jsonl. The speech guard it builds is shared with the tags feed through ctx.shared["speech_guard"]."""
    import taxa as _taxa

    a = ctx.args
    if "tags" not in ctx.started:
        raise PerchUnavailable("not started: needs the tags plugin (its speech flag keeps speech windows out of perch.jsonl)")
    guard = ctx.shared["speech_guard"] = SpeechGuard()
    return Perch(
        a.perch_model,
        a.perch_labels or os.path.join(os.path.dirname(os.path.abspath(a.perch_model)), "labels.csv"),
        guard,
        taxa=_taxa.Taxa(a.taxa),
        embeddings_dir=ctx.out_dir if a.perch_embeddings else None,
        threads=a.perch_threads,
        min_logit=a.perch_min_logit,
        run=ctx.run_id,
        device=(ctx.dev if a.perch_device == "auto" else a.perch_device),
        ort_dir=a.perch_ort_dir,
    )
