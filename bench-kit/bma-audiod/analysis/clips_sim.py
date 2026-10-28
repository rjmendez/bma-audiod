"""Test support for clips.py (used by test_clips.py and contracts/tools/make_clips_fixtures.py; not imported by the analyzer).
Drives the REAL ClipService, ClipStore and host.Ring with a deterministic signal in which every sample is a function of
(beam, absolute sample index), so a stored clip can be compared sample for sample. Only the clock and the models are simulated:
BirdNET and PANNs are replaced by the rows they emit (the exact dict shapes of MultiBeamBirds.process / Tagger.process)."""

import os, sys, threading
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import clips as CL  # noqa: E402
import host  # noqa: E402

FS = 48000
BASE = 1000 * FS  # absolute sample index of second 0
H0 = 1792000000 - (1792000000 % 3600)  # a UTC hour boundary
T0 = float(H0 + 600)  # wall time of second 0 (10 minutes into an hour)


def sig(beam, n0, n, loud=()):
    """int16 expected content of one beam: loud beams span +-10000, the others +-1000 (BirdNET looks at the two loudest)."""
    r = 20001 if beam in loud else 2001
    idx = np.arange(n0, n0 + n, dtype=np.int64)
    return (((idx * 7 + beam * 1000) % r) - r // 2).astype(np.int16)


def row(sci, conf, beam=1, kind="bird", group="bird", non_species=False, in_region=True, common=None, t=0.0):
    r = {
        "k": kind,
        "t": t,
        "beam": beam,
        "level_dbfs": -50.0,
        "common": common or sci + " (common)",
        "scientific": sci,
        "confidence": conf,
        "provenance": "model_opinion",
        "in_region": in_region if kind == "bird" else False,
        "non_species": non_species,
        "group": group,
    }
    if kind == "bird_filtered":
        r["reject_reason"] = "geo_not_predicted"
    return r


def tag_row(p=0.0, active=None, t=0.0, top=None):
    """p None = a speech_p that is not a number; p "missing" = no speech_p key at all (both must count as flagged)."""
    a = (isinstance(p, (int, float)) and p >= 0.30) if active is None else active
    r = {"k": "tags", "t": t, "beam": 1, "speech_p": p, "speech_active": a, "top": [] if top is None else top, "emb_stored": not a}
    if p == "missing":
        del r["speech_p"]
    return r


class Sim:
    def __init__(self, root, loud=(0, 1), delay=3.5, tagger=True, t0=T0, wait=None, replace=os.replace, remove=os.remove, start_threads=False, **cfgkw):
        self.rings = [host.Ring(host.RING_S * FS) for _ in range(8)]
        self.lock = threading.Lock()
        self.loud, self.delay, self.t0 = tuple(loud), delay, t0
        self.pushed = BASE
        self.now = t0
        self.cfg = CL.ClipConfig(**cfgkw)
        self.svc = CL.ClipService(
            root,
            self.cfg,
            CL.make_ring_reader(self.rings, self.lock),
            tagger,
            "birdnetlib-test",
            clock=lambda: self.now,
            start_threads=start_threads,
            wait=wait,
            replace=replace,
            remove=remove,
        )
        self.root = root
        self.max_call_s = 0.0

    def a(self, sec):
        return BASE + int(round(sec * FS))

    def advance(self, to_sec):
        """Push audio into the real rings up to second `to_sec` (in 4800-sample blocks, like the bridge)."""
        end = self.a(to_sec)
        while self.pushed < end:
            n = min(4800, end - self.pushed)
            for b in range(8):
                self.rings[b].push(self.pushed % (1 << 32), (sig(b, self.pushed, n, self.loud).astype(np.float32) / 32768.0))
            self.pushed += n
        self.now = max(self.now, self.t0 + to_sec)

    def x(self, start_sec, dur):
        s, n = self.a(start_sec), int(dur * FS)
        return np.stack([r.get(s, n) for r in self.rings], axis=1)

    def birds(self, start_sec, rows, dur=3.0):
        self.advance(start_sec + dur + self.delay)
        t_wall = self.t0 + start_sec + dur
        x = self.x(start_sec, dur)
        self.svc.note_window("birds", t_wall, self.a(start_sec), int(dur * FS))
        import time

        t = time.perf_counter()
        self.svc.on_plugin("birds", [dict(r, t=t_wall) for r in rows], x, t_wall)
        self.max_call_s = max(self.max_call_s, time.perf_counter() - t)

    def tags(self, start_sec, p=0.0, active=None, dur=5.0, rows=None, top=None):
        self.advance(start_sec + dur + self.delay)
        t_wall = self.t0 + start_sec + dur
        self.svc.note_window("tags", t_wall, self.a(start_sec), int(dur * FS))
        self.svc.on_plugin("tags", [tag_row(p, active, t_wall, top)] if rows is None else rows, None, t_wall)

    def play(self, birds, tags=None, k_end=12, tags_cover=(-10, 140), drain=True):
        """birds: {window index k (start 3k s): [rows]}; tags: explicit [(start_s, p[, speech_active[, top labels]])] (default hop-2 coverage of tags_cover, p 0).
        Windows are dispatched in order of their end time, the way the host does."""
        tw = tags if tags is not None else [(s, 0.0) for s in range(tags_cover[0], tags_cover[1], 2)]
        ev = [(w[0] + 5.0, 1, "t", w) for w in tw] + [(3.0 * k + 3.0, 0, "b", k) for k in range(k_end)]
        for _, _, kind, v in sorted(ev, key=lambda e: (e[0], e[1])):
            if kind == "t":
                self.tags(v[0], v[1], v[2] if len(v) > 2 else None, top=v[3] if len(v) > 3 else None)
            else:
                self.birds(3.0 * v, birds.get(v, []))
            if drain:
                self.svc.drain()
        self.svc.tick(self.now)
        if drain:
            self.svc.drain()

    # ---- inspection --------------------------------------------------------------------------------------------------
    def log(self, name, rows, raw=()):
        """Write <out>/<name> (JSONL of rows, then the raw lines) next to the clips dir, where ClipService.purge_human reads the persisted logs."""
        import json

        d = os.path.dirname(os.path.abspath(self.root))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "wb") as f:
            for r in rows:
                f.write((json.dumps(r) + "\n").encode())
            for r in raw:
                f.write(r + b"\n")

    def clip_ids(self):
        out = []
        if not os.path.isdir(self.root):
            return out
        for d in sorted(os.listdir(self.root)):
            p = os.path.join(self.root, d)
            if CL.DAY_RE.match(d) and os.path.isdir(p):
                out += [(d, n[:-5]) for n in sorted(os.listdir(p)) if n.endswith(".json")]
        return out

    def files(self):
        out = []
        for dp, _, fs in os.walk(self.root):
            out += [os.path.relpath(os.path.join(dp, f), self.root).replace(os.sep, "/") for f in fs]
        return sorted(out)

    def audio_files(self):
        return [f for f in self.files() if f.endswith((".flac", ".wav", ".flac.tmp", ".wav.tmp", ".part"))]

    def sidecar(self, day, cid):
        import json

        with open(os.path.join(self.root, day, cid + ".json"), encoding="utf-8") as f:
            return json.load(f)

    def index(self):
        import json

        p = os.path.join(self.root, "index.jsonl")
        if not os.path.exists(p):
            return []
        with open(p, "rb") as f:
            return [json.loads(l) for l in f.read().split(b"\n") if l]
