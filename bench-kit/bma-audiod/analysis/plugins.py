"""Analysis plugins for the BMA360 8-beam stream (48 kHz, float32 in [-1, 1], windows shaped (N, 8)).

Every plugin returns metadata dicts only. No plugin writes audio anywhere (the one audio exception, wildlife clips, lives in
clips.py and is fed the plugins' rows by host.py; it is not a plugin). Embeddings are produced
only from windows the tagger did not flag as speech (see Tagger / EmbeddingStore).
"""

import hashlib, json, math, os, pickle, re, sys, time
import numpy as np
from scipy import signal
from scipy import fft as sfft

FS = 48000
NB = 8
BANDS = [(0, 100), (100, 1000), (1000, 4000), (4000, 12000), (12000, 24000)]


def db(x, floor=1e-12):
    return 20.0 * math.log10(max(float(x), floor))


# ---- provenance of a record (contract analysis-records 1.3.0): which model said it, that it is an opinion, the score tail
SCORE_K = 5  # labels kept in `top_k`
CLAIM = {"human_verified": False, "source": "model_opinion"}


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def weights_id(path):
    """sha256 of a weights file; a hub blob whose file name already is a sha256 (huggingface cache) is not re-read."""
    name = os.path.basename(os.path.realpath(path))
    if re.fullmatch(r"[0-9a-f]{64}", name):
        return name
    try:
        return file_sha256(path)
    except OSError:
        return None  # unreadable or absent weights: sha256 null


def model_block(name, version, variant, sha256):
    return {"name": name, "version": version, "variant": variant, "sha256": sha256}


def pkg_version(dist):
    try:
        import importlib.metadata as md

        return md.version(dist)
    except Exception:
        return None


def hub_weights(repo, cache_dir=None):
    """(revision, sha256) of a cached huggingface model's weights file; (None, None) when it is not in the local cache."""
    try:
        from huggingface_hub import try_to_load_from_cache

        for fn in ("model.safetensors", "pytorch_model.bin"):
            p = try_to_load_from_cache(repo, fn, cache_dir=cache_dir)
            if isinstance(p, str):
                return os.path.basename(os.path.dirname(p)), weights_id(p)
    except Exception:
        pass
    return None, None


def score_tail(scores, labels, order, k=SCORE_K):
    """(top_k, max_unstored_score) from scores already computed: `order` = indices by score, best first, at least k+1 long when the
    model has more than k labels. top_k = [[label, score 3 d.p.]] of the k best; max_unstored_score = the best score outside them
    (None when the model has no more labels)."""
    top = [[labels[i], round(float(scores[i]), 3)] for i in order[:k]]
    return top, (round(float(scores[order[k]]), 3) if len(order) > k else None)


# =====================================================================================================
class DSPMonitor:
    """Always-on classical monitors: levels, band shares, centroid, transients, tonal-line tracker."""

    name = "dsp"
    win_s, hop_s = 1.0, 1.0
    heavy = False

    def __init__(self, mask_path=None, mask_tol_hz=None):
        """mask_path: optional JSON from learn_tonal_mask.py ({"lines_hz": [...], "tolerance_hz": 4.0}) naming the
        unit's own electronic lines. Masked lines are still tracked and listed in tonal_state (flagged
        "electronic": true) but never produce tonal_appeared / tonal_gone. No mask file = unchanged behaviour."""
        self.mask, self.mask_tol = [], 0.0
        if mask_path:
            with open(mask_path) as f:
                m = json.load(f)
            self.mask = sorted(float(h) for h in m["lines_hz"])
            self.mask_tol = float(mask_tol_hz if mask_tol_hz is not None else m.get("tolerance_hz", 4.0))
        self.frame = int(0.02 * FS)
        self.hist_db = []  # 20 ms total-energy history (dB), ~3 s for the transient median
        self.long = []  # mean-over-beams samples for the 10 s tonal analysis
        self.beam_tail = np.zeros((0, NB), np.float32)  # last BEAM_SEG per-beam samples, for tonal beam attribution
        self.lines = {}  # bin_hz -> dict(first, last, hits, misses, level)
        self.win_n = 0
        self.floor_ewma = None
        self.in_event = False

    def process(self, x, t_wall):
        out = []
        n = x.shape[0]
        rms = np.sqrt((x.astype(np.float64) ** 2).mean(axis=0))
        mean_sig = x.mean(axis=1)
        f, p = signal.welch(mean_sig, FS, nperseg=4096)
        tot = p.sum() + 1e-30
        shares = [float(p[(f >= lo) & (f < hi)].sum() / tot) for lo, hi in BANDS]
        centroid = float((f * p).sum() / tot)
        flat = float(np.exp(np.mean(np.log(p + 1e-30))) / (p.mean() + 1e-30))
        out.append(
            {
                "k": "dsp",
                "t": t_wall,
                "rms_dbfs": [round(db(r), 1) for r in rms],
                "peak_dbfs": round(db(np.abs(x).max()), 1),
                "band_pct": [round(100 * s, 1) for s in shares],
                "centroid_hz": round(centroid, 0),
                "flatness": round(flat, 4),
            }
        )
        # transients on 20 ms frames
        nfr = n // self.frame
        fe = (x[: nfr * self.frame].reshape(nfr, self.frame, NB).astype(np.float64) ** 2).mean(axis=1)
        tot_db = 10 * np.log10(np.maximum(fe.mean(axis=1), 1e-14))
        for i in range(nfr):
            self.hist_db.append(float(tot_db[i]))
            if len(self.hist_db) > 150:
                self.hist_db.pop(0)
            if len(self.hist_db) >= 100:
                med = float(np.median(self.hist_db))
                exc = self.hist_db[-1] - med
                if exc < 3.0:
                    self.in_event = False
                if exc > 8.0 and not self.in_event:
                    self.in_event = True  # one record per excursion; a sustained step is not a storm
                    per_beam = 10 * np.log10(np.maximum(fe[i], 1e-14))
                    out.append(
                        {
                            "k": "transient",
                            "t": t_wall - (nfr - 1 - i) * 0.02,
                            "excess_db": round(exc, 1),
                            "level_db": round(self.hist_db[-1], 1),
                            "loudest_beam": int(np.argmax(per_beam)) + 1,
                            "beam_spread_db": round(float(per_beam.max() - per_beam.min()), 1),
                        }
                    )
        # 10 s tonal / noise-floor summary
        self.long.append(mean_sig.astype(np.float32))
        self.beam_tail = np.concatenate([self.beam_tail, x.astype(np.float32)])[-self.BEAM_SEG :]
        self.win_n += 1
        if self.win_n % 10 == 0:
            out.extend(self._tonal(np.concatenate(self.long), t_wall, self.beam_tail))
            self.long = []
        return out, None

    def _masked(self, hz):
        return any(abs(hz - m) <= self.mask_tol for m in self.mask)

    BEAM_SEG = 65536  # samples (1.37 s) per beam used for tonal beam attribution
    BEAM_MARGIN_DB = 1.5  # top-two beam excess gap below which the beam is reported as null (near-omni array)
    BEAM_MIN_EXC_DB = 10.0  # the best beam must show the tone this far over its local floor in the segment, else null

    def beam_spec(self, yb):
        """Per-beam power spectrum (dB) of the last BEAM_SEG samples, computed once per tonal analysis (float32 FFT)."""
        seg = np.ascontiguousarray(yb[-self.BEAM_SEG :], dtype=np.float32)
        n = seg.shape[0]
        if n < 4096:
            return None
        pw = np.abs(sfft.rfft(seg * np.hanning(n).astype(np.float32)[:, None], axis=0)) ** 2
        return 10 * np.log10(np.maximum(pw, 1e-20)), n

    def beam_of(self, yb, hz, spec=None):
        """(beam 1..8 or None, margin dB) for a tone at hz: per-beam excess of the line bin over the local median
        (+-200 Hz, excluding +-5 bins), on the last BEAM_SEG samples. None when the two strongest beams are within
        BEAM_MARGIN_DB: adjacent beams correlate 0.92-0.96, so attribution is weak and must say so."""
        spec = spec or self.beam_spec(yb)
        if spec is None:
            return None, None
        pdb, n = spec
        df = FS / n
        c = int(round(hz / df))
        w, ex = int(200 / df), 5
        lo, hi = max(1, c - w), min(pdb.shape[0], c + w + 1)
        exc = []
        for b in range(NB):
            line = pdb[max(0, c - 2) : c + 3, b].max()
            ref = np.concatenate([pdb[lo : max(lo, c - ex), b], pdb[min(hi, c + ex + 1) : hi, b]])
            exc.append(float(line - np.median(ref)) if ref.size else float(line))
        order = np.argsort(exc)[::-1]
        margin = exc[order[0]] - exc[order[1]]
        ok = margin >= self.BEAM_MARGIN_DB and exc[order[0]] >= self.BEAM_MIN_EXC_DB
        return (int(order[0]) + 1 if ok else None), round(float(margin), 1)

    def _tonal(self, y, t_wall, yb=None):
        f, p = signal.welch(y.astype(np.float64), FS, nperseg=16384)
        pdb = 10 * np.log10(np.maximum(p, 1e-20))
        df = f[1] - f[0]
        k = max(3, int(400 / df) | 1)
        floor = signal.medfilt(pdb, k)
        exc = pdb - floor
        peaks = signal.find_peaks(exc, height=8.0, distance=max(1, int(5 / df)))[0]
        peaks = [i for i in peaks if f[i] > 20]
        spec = self.beam_spec(yb) if (yb is not None and len(peaks)) else None
        seen = {}
        for i in peaks:
            seen[int(round(f[i] / 4.0) * 4)] = (float(f[i]), float(exc[i]), float(pdb[i]))
        out = []
        for b, (fh, e, lv) in seen.items():
            ln = self.lines.get(b)
            if ln is None:
                ln = self.lines[b] = {"first": t_wall, "hits": 0, "misses": 0, "announced": False}
            ln.update(last=t_wall, f=fh, excess=e, level=lv)
            if yb is not None:
                bm, mg = self.beam_of(yb, fh, spec)
                if bm is not None or "beam" not in ln:  # sticky: keep the last CONFIDENT beam when a later segment is unclear
                    ln["beam"], ln["beam_margin_db"] = bm, mg
            ln["hits"] += 1
            ln["misses"] = 0
            if ln["hits"] >= 3 and not ln["announced"]:
                ln["announced"] = True
                ln["announced_masked"] = self._masked(fh)  # fixed at announce time so appeared/gone stay paired
                if not ln["announced_masked"]:
                    out.append(
                        {
                            "k": "tonal_appeared",
                            "t": t_wall,
                            "hz": round(fh, 1),
                            "excess_db": round(e, 1),
                            "beam": ln.get("beam"),
                            "beam_margin_db": ln.get("beam_margin_db"),
                        }
                    )
        for b in list(self.lines):
            if b not in seen:
                ln = self.lines[b]
                ln["misses"] += 1
                if ln["misses"] >= 2:
                    if ln["announced"] and not ln.get("announced_masked"):
                        out.append(
                            {
                                "k": "tonal_gone",
                                "t": t_wall,
                                "hz": round(ln.get("f", b), 1),
                                "lasted_s": round(ln["last"] - ln["first"], 0),
                                "beam": ln.get("beam"),
                            }
                        )
                    del self.lines[b]
        # noise floor: 10th percentile of 20 ms energy over the last 10 s, drift vs EWMA
        fr = (y[: len(y) // self.frame * self.frame].reshape(-1, self.frame).astype(np.float64) ** 2).mean(axis=1)
        nf = 10 * math.log10(max(float(np.percentile(fr, 10)), 1e-14))
        self.floor_ewma = nf if self.floor_ewma is None else 0.9 * self.floor_ewma + 0.1 * nf
        out.append(
            {
                "k": "tonal_state",
                "t": t_wall,
                "lines": sorted(
                    [
                        {
                            "hz": round(v.get("f", b), 1),
                            "excess_db": round(v.get("excess", 0), 1),
                            "age_s": round(t_wall - v["first"], 0),
                            "beam": v.get("beam"),
                            **({"electronic": True} if self._masked(v.get("f", b)) else {}),
                        }
                        for b, v in self.lines.items()
                        if v["announced"]
                    ],
                    key=lambda d: d["hz"],
                ),
                "noise_floor_db": round(nf, 1),
                "noise_floor_drift_db": round(nf - self.floor_ewma, 2),
            }
        )
        return out


def ring_decorr(y):
    """Cross-beam decorrelation index of one window y (N, 8), already band-passed: 1 - the mean zero-lag Pearson
    correlation over the 8 ring-adjacent beam pairs (i, i+1 mod 8). Unit-less, clipped to [0, 1]: 0 = every adjacent
    pair identical, larger = the beams hear different waveforms. UNVALIDATED proxy for wind/rain (turbulence at the
    mics decorrelates beams, a distant plane wave does not); no ground truth exists, calibrate on a quiet day."""
    g = y.T @ y  # 8 x 8 Gram matrix (one BLAS call), centred below
    m = y.sum(axis=0)
    g = g - np.outer(m, m) / y.shape[0]
    d = np.diag(g)
    cross = g[np.arange(NB), (np.arange(NB) + 1) % NB]  # ring-adjacent pairs (i, i+1 mod 8)
    r = float((cross / (np.sqrt(d * np.roll(d, -1)) + 1e-30)).mean())
    return round(min(1.0, max(0.0, 1.0 - r)), 3)


# =====================================================================================================
class Spatial:
    """Azimuth from the 8-beam energy ring. The ring ordering is measured (adjacent beams correlate 0.92-0.96,
    beam 1 vs 8 correlates 0.73); absolute beam centres are NOT calibrated, so azimuth is reported in the
    ring frame (beam 1 = 0 deg, equal 45 deg steps) and flagged uncalibrated."""

    name = "spatial"
    win_s, hop_s = 1.0, 1.0
    heavy = False

    def __init__(self, ring_offset_deg=0.0):
        self.sos = signal.butter(4, [200, 4000], btype="band", fs=FS, output="sos")
        self.ang = np.deg2rad(ring_offset_deg + np.arange(NB) * 360.0 / NB)
        self.r_mean, self.r_var, self.n = 0.0, 0.0, 0
        self.run = 0
        self.run_az = []

    def process(self, x, t_wall):
        y = signal.sosfilt(self.sos, x.astype(np.float64), axis=0)
        e = (y**2).mean(axis=0) + 1e-20
        p = e / e.sum()
        z = (p * np.exp(1j * self.ang)).sum()
        r = float(abs(z))
        az = float((np.rad2deg(np.angle(z)) + 360.0) % 360.0)
        e_db = 10 * np.log10(e)
        spread = float(e_db.max() - e_db.min())
        # directional = resultant length well above its own running baseline (diffuse noise sits near 0)
        std = math.sqrt(max(self.r_var, 0.0)) if self.n > 10 else 1.0
        directional = bool(self.n > 10 and r > self.r_mean + 4 * std and spread > 2.0)
        if not directional:  # learn the baseline only from non-directional windows
            self.n += 1
            d = r - self.r_mean
            self.r_mean += d / min(self.n, 600)
            self.r_var += (d * (r - self.r_mean) - self.r_var) / min(self.n, 600)
        out = [
            {
                "k": "spatial",
                "t": t_wall,
                "az_deg": round(az, 1),
                "R": round(r, 3),
                "dominant_beam": int(np.argmax(e)) + 1,
                "spread_db": round(spread, 1),
                "e_rel_db": [round(float(v - e_db.max()), 1) for v in e_db],
                "directional": directional,
                "calibrated": False,
                "decorr": ring_decorr(y),
            }
        ]
        if directional:
            self.run += 1
            self.run_az.append(az)
            if self.run == 2:
                out.append({"k": "source", "t": t_wall, "az_deg": round(az, 1), "dominant_beam": int(np.argmax(e)) + 1, "R": round(r, 3), "calibrated": False})
        else:
            self.run, self.run_az = 0, []
        return out, None


# =====================================================================================================
SPEECH_LABELS = {
    "Speech",
    "Male speech, man speaking",
    "Female speech, woman speaking",
    "Child speech, kid speaking",
    "Conversation",
    "Narration, monologue",
    "Babbling",
    "Speech synthesizer",
    "Shout",
    "Yell",
    "Whispering",
    "Chatter",
    "Hubbub, speech noise, speech babble",
    "Singing",
}


def best_beam(x):
    return int(np.argmax((x.astype(np.float64) ** 2).mean(axis=0)))


class Tagger:
    """PANNs CNN14 AudioSet tagging (527 classes) + 2048-d clip embedding. Models trained at 32 kHz: the 48 kHz
    beam is DOWNSAMPLED 2:3 (never upsampled). Speech is only classified, never kept: a window with speech
    probability >= SPEECH_P yields no embedding."""

    name = "tags"
    win_s, hop_s = 5.0, 2.0
    heavy = True
    SPEECH_P = 0.30

    def __init__(self, data_dir, device):
        import torch  # noqa: F401
        from panns_inference import AudioTagging
        from panns_inference.config import labels

        self.labels = list(labels)
        self.speech_idx = [i for i, l in enumerate(self.labels) if l in SPEECH_LABELS]
        ckpt = os.path.join(data_dir, "Cnn14_mAP=0.431.pth")
        self.at = AudioTagging(checkpoint_path=ckpt, device=device)
        self.device = device
        self.model = model_block("PANNs CNN14", pkg_version("panns_inference"), "best_beam_32k", weights_id(ckpt))

    def process(self, x, t_wall):
        b = best_beam(x)
        a = signal.resample_poly(x[:, b].astype(np.float32), 2, 3).astype(np.float32)  # 48k -> 32k
        clip, emb = self.at.inference(a[None, :])
        c = clip[0]
        full = np.argsort(c)[::-1]
        top = full[:5]
        speech = set(self.speech_idx)
        top_k, unstored = score_tail(c, self.labels, [i for i in full if i not in speech][: SCORE_K + 1])  # human-voice classes not listed
        speech_p = float(c[self.speech_idx].max()) if self.speech_idx else 0.0
        active = speech_p >= self.SPEECH_P
        rec = {
            "k": "tags",
            "t": t_wall,
            "beam": b + 1,
            "speech_p": round(speech_p, 3),
            "speech_active": active,
            "top": [[self.labels[i], round(float(c[i]), 3)] for i in top if c[i] >= 0.05],
            "emb_stored": not active,
            "model": dict(self.model),
            "claim": dict(CLAIM),
            "top_k": top_k,
            "max_unstored_score": unstored,
        }
        return [rec], (None if active else emb[0].astype(np.float32))


# =====================================================================================================
class EmbeddingStore:
    """Appends non-speech embeddings (float16) + a metadata line per vector. Never touches audio."""

    def __init__(self, out_dir):
        self.dir = out_dir
        self.n = 0

    def add(self, emb, meta):
        day = time.strftime("%Y%m%d", time.gmtime(meta["t"]))
        with open(os.path.join(self.dir, f"emb_{day}.f16"), "ab") as f:
            f.write(emb.astype(np.float16).tobytes())
        with open(os.path.join(self.dir, f"emb_{day}.jsonl"), "a") as f:
            f.write('{"i":%d,"t":%.3f,"top1":%s}\n' % (self.n, meta["t"], meta.get("top1_json", "null")))
        self.n += 1


class Novelty:
    """Online scene model on cosine geometry: MiniBatchKMeans over embeddings. score = distance to the nearest
    centroid divided by that distance's running median; novel when score > 3 for 3 windows in a row."""

    name = "novelty"

    def __init__(self, state_path, k=12, warmup=40, run=None, mv=None):
        """run: analyzer start (int) and mv: model-version string; both are stamped on every row when given
        (readers ignore unknown fields). The pickle also stores the sklearn version: a state fitted by another
        version appends '-from<ver>' to mv so the change is visible in the rows."""
        from sklearn.cluster import MiniBatchKMeans
        import sklearn

        self.path, self.k, self.warmup = state_path, k, warmup
        self.run, self.mv = run, mv
        self.sk = sklearn.__version__
        self.km = MiniBatchKMeans(n_clusters=k, batch_size=32, random_state=0, n_init=1)
        self.buf, self.fitted, self.seen = [], False, 0
        self.med = None
        self.streak = 0
        self.last_save = time.time()
        try:
            with open(state_path, "rb") as f:
                st = pickle.load(f)
            self.km, self.fitted, self.seen, self.med = st["km"], st["fitted"], st["seen"], st["med"]
            if st.get("sk") and st["sk"] != self.sk and self.mv is not None:
                self.mv = f"{self.mv}-from{st['sk']}"
        except (OSError, pickle.PickleError, KeyError, EOFError):
            pass

    def add(self, emb, t_wall):
        rows = self._add(emb, t_wall)
        if self.run is not None or self.mv is not None:
            for r in rows:
                r["run"], r["mv"] = self.run, self.mv
        return rows

    def _add(self, emb, t_wall):
        v = emb / (np.linalg.norm(emb) + 1e-9)
        self.seen += 1
        out = []
        if not self.fitted:
            self.buf.append(v)
            if len(self.buf) >= self.warmup:
                self.km.fit(np.stack(self.buf))
                self.fitted = True
                self.buf = []
            return [{"k": "novelty", "t": t_wall, "state": "warming_up", "seen": self.seen}]
        d = self.km.transform(v[None, :])[0]
        sc = int(np.argmin(d))
        dist = float(d[sc])
        self.med = dist if self.med is None else 0.98 * self.med + 0.02 * dist
        score = dist / max(self.med, 1e-6)
        self.km.partial_fit(v[None, :])
        novel = score > 3.0
        self.streak = self.streak + 1 if novel else 0
        out.append({"k": "novelty", "t": t_wall, "scene": sc, "dist": round(dist, 4), "score": round(score, 2), "novel": novel, "seen": self.seen})
        if self.streak == 3:
            out.append({"k": "novel_event", "t": t_wall, "scene": sc, "score": round(score, 2)})
        if time.time() - self.last_save > 300:
            self.save()
        return out

    def save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "wb") as f:
                pickle.dump({"km": self.km, "fitted": self.fitted, "seen": self.seen, "med": self.med, "sk": self.sk}, f)
            os.replace(tmp, self.path)
            self.last_save = time.time()
        except OSError:
            pass


# =====================================================================================================
DEFAULT_PROMPTS = [
    "room tone, quiet background noise",
    "silence",
    "a door closing",
    "a knock on a door",
    "footsteps",
    "glass breaking",
    "a dog barking",
    "a cat meowing",
    "a car passing",
    "a siren",
    "an alarm beeping",
    "a phone ringing",
    "water running",
    "typing on a keyboard",
    "a vacuum cleaner",
    "a fan or air conditioner humming",
    "wind blowing",
    "rain falling",
    "thunder",
    "bird song",
    "insects buzzing",
    "a motor or machinery running",
    "music playing",
    "an object falling",
    "a drill or power tool",
    "a microwave oven",
]


def _feat(o):
    """get_text_features / get_audio_features return a tensor on older transformers and a ModelOutput
    (projected embedding in pooler_output) on newer ones."""
    import torch

    if torch.is_tensor(o):
        return o
    for k in ("pooler_output", "text_embeds", "audio_embeds"):
        v = getattr(o, k, None)
        if v is not None:
            return v
    return o[0]


class ClapZeroShot:
    """LAION-CLAP (htsat-unfused, 48 kHz native) zero-shot scoring of text prompts. Speech prompts are excluded."""

    name = "clap"
    win_s, hop_s = 5.0, 5.0
    heavy = True

    def __init__(self, device, prompts=None):
        import torch
        from transformers import ClapModel, ClapProcessor

        self.torch = torch
        self.device = device
        self.proc = ClapProcessor.from_pretrained("laion/clap-htsat-unfused")
        self.model = ClapModel.from_pretrained("laion/clap-htsat-unfused").to(device).eval()
        self.prompts = prompts or DEFAULT_PROMPTS
        with torch.no_grad():
            ti = self.proc(text=["This is a sound of " + p for p in self.prompts], return_tensors="pt", padding=True)
            ti = {k: v.to(device) for k, v in ti.items()}
            t = _feat(self.model.get_text_features(**ti))
        self.text = torch.nn.functional.normalize(t, dim=-1)
        rev, sha = hub_weights("laion/clap-htsat-unfused")
        self.model_meta = model_block("laion/clap-htsat-unfused", rev, "best_beam_48k", sha)
        self.scale = float(self.model.logit_scale_a.exp().item()) if hasattr(self.model, "logit_scale_a") else 100.0

    def process(self, x, t_wall):
        torch = self.torch
        b = best_beam(x)
        with torch.no_grad():
            wav = [x[:, b].astype(np.float32)]
            try:  # newer transformers: `audio`; older: `audios`
                ai = self.proc(audio=wav, sampling_rate=FS, return_tensors="pt")
            except (TypeError, ValueError):
                ai = self.proc(audios=wav, sampling_rate=FS, return_tensors="pt")
            ai = {k: v.to(self.device) for k, v in ai.items()}
            a = torch.nn.functional.normalize(_feat(self.model.get_audio_features(**ai)), dim=-1)
            p = torch.softmax(self.scale * (a @ self.text.T), dim=-1)[0].cpu().numpy()
        order = np.argsort(p)[::-1][: SCORE_K + 1]
        top_k, unstored = score_tail(p, self.prompts, order)
        return [
            {
                "k": "clap",
                "t": t_wall,
                "beam": b + 1,
                "top": [[self.prompts[i], round(float(p[i]), 3)] for i in order[:3]],
                "model": dict(self.model_meta),
                "claim": dict(CLAIM),
                "top_k": top_k,
                "max_unstored_score": unstored,
            }
        ], None


# =====================================================================================================
class MultiBeamBirds:
    """BirdNET on the two strongest beams of each 3 s window (the existing classifier only sees one)."""

    name = "birds"
    win_s, hop_s = 3.0, 3.0
    heavy = True

    def __init__(self, classify_dir, min_conf=0.25, gate_dbfs=-70.0, geo_config=None, taxa=None):
        """taxa: a taxa.Taxa; None = load the default table (a missing table stamps group None and counts)."""
        sys.path.insert(0, classify_dir)
        if taxa is None:
            import taxa as _taxa

            taxa = _taxa.Taxa()
        self.taxa = taxa
        self.window_info = {}
        from bma_classify import BirdNet, load_geo_config

        # optional county-level location/date species filter (geo_config = path of a JSON {"lat","lon"}, host.py --geo-config); absent = off
        if geo_config:
            g = load_geo_config(geo_config)
            self.bn = BirdNet(min_conf, g["lat"], g["lon"], g["week"], g["threshold"])
            self.geo_on = True
        else:
            self.bn = BirdNet(min_conf)
            self.geo_on = False
        self.gate = gate_dbfs
        self.min_conf = min_conf

    def _provenance(self, dets):
        """model / claim / top_k / max_unstored_score / score_floor of one beam's window. The BirdNET library returns only detections
        >= min_conf, so the unstored tail is not exposed: max_unstored_score is None and score_floor says where the stored scores stop."""
        info = self.bn.model_info() if hasattr(self.bn, "model_info") else {"name": "BirdNET", "version": None, "sha256": None}
        ranked = sorted(dets, key=lambda d: -(d.get("confidence") or 0.0))[:SCORE_K]
        return {
            "model": model_block(info["name"], info["version"], "geo_filter" if self.geo_on else "no_filter", info["sha256"]),
            "claim": dict(CLAIM),
            "top_k": [[d.get("scientific") or d.get("common"), d.get("confidence")] for d in ranked],
            "max_unstored_score": None,
            "score_floor": self.min_conf,
        }

    REJECT_GEO = "geo_not_predicted"  # the only reason the library reports: species not on the location/date list

    def _stamp(self, rec, d, in_region):
        """Add in_region, non_species and group to a bird / bird_filtered row (forward-only; never drops it)."""
        grp, non_sp = self.taxa.lookup(rec["scientific"], rec["common"])
        rec["in_region"] = in_region
        rec["non_species"] = d["non_species"] if "non_species" in d else (rec["scientific"] == rec["common"] or bool(non_sp))
        rec["group"] = grp

    def process(self, x, t_wall):
        import datetime

        e = (x.astype(np.float64) ** 2).mean(axis=0)
        out = []
        when = datetime.datetime.fromtimestamp(t_wall)
        n_cls = 0
        for b in np.argsort(e)[::-1][:2]:
            lvl = db(math.sqrt(e[b]))
            if lvl < self.gate:
                continue
            n_cls += 1
            dets = self.bn.classify(x[:, b].astype(np.float32), when=when)
            prov = self._provenance(list(dets) + list(self.bn.filtered))
            for d in dets:
                rec = {
                    "k": "bird",
                    "t": t_wall,
                    "beam": int(b) + 1,
                    "level_dbfs": round(lvl, 1),
                    "common": d.get("common"),
                    "scientific": d.get("scientific"),
                    "confidence": d.get("confidence"),
                    "provenance": "model_opinion",
                }
                rec.update(prov)
                self._stamp(rec, d, d.get("in_region") if "in_region" in d else None)
                out.append(rec)
            for d in self.bn.filtered:  # what the location filter removed: audit trail only
                rec = {
                    "k": "bird_filtered",
                    "t": t_wall,
                    "beam": int(b) + 1,
                    "level_dbfs": round(lvl, 1),
                    "common": d.get("common"),
                    "scientific": d.get("scientific"),
                    "confidence": d.get("confidence"),
                    "in_region": False,
                    "provenance": "model_opinion",
                    "reject_reason": self.REJECT_GEO,
                }
                rec.update(prov)
                self._stamp(rec, d, False)
                out.append(rec)
        self.window_info = {"beams_classified": n_cls}
        return out, None
