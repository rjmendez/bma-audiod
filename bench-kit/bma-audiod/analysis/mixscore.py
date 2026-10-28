"""mixscore: BirdNET on the 8-beam MEAN of each 3 s window, compared once a minute with what the per-beam `birds` plugin reported.

Question (research report item 6): how much does per-beam scoring add over scoring the mixed signal, and does the mix lose weak
sources? Opt-in (host.py --mixscore, default OFF). Metadata only: no audio, no embeddings, nothing but species names and scores.

Window pairing. The plugin runs on the same 3 s grid as `birds` (win_s = hop_s = 3.0). The host hands it every plugin's records
in-process through `observe(plugin_name, records, t_wall)` (no file is parsed): the `birds` records of a window give the per-beam
side (`observe` is also called for a window with NO records, which is how an empty window is known), the `tags` records give the
speech flag. A mix window is matched to the birds window whose end time lies within PAIR_TOL_S of its own.

Speech rule (owner policy). A mix window is released only after the tagger has reported on it: the window is DROPPED (no record, no
count by species) if any tags window [t - 5, t] that overlaps it has speech_active, and also if the tags windows seen do not cover it
(fail closed, counted as `dropped_unknown`). Without the `tags` and `birds` plugins the plugin refuses to start (require()).
BirdNET's human labels (Human vocal / non-vocal / whistle) are dropped on both sides and counted, never reported.

Mix. m = mean of the 8 aligned beams (the window arrives already shifted by --beam-shift), scaled by g = sqrt(mean beam power / power
of m) so the mix has the same power as the average beam (a plain mean of uncorrelated beams loses 9 dB, which would handicap BirdNET); g
is capped at sqrt(8) (the uncorrelated value) so beams that cancel cannot be amplified without bound. Identical beams: g = 1, mix = beam.
`mix_gain_db` is in the record. Same gate as birds: the loudest beam below gate_dbfs means no scoring (both sides skip it).
"""

import math
import sys
import threading
import time

import numpy as np

NB = 8
PAIR_TOL_S = 1.5  # birds / mixscore window end times closer than this are the same window
TAGS_WIN_S = 5.0  # tags plugin window length
HOLD_S = 30.0  # a window still unresolved this long after its end (plugin clock) is resolved with what is known
EMIT_GRACE_S = 45.0  # a minute is emitted this long after it ended (HOLD_S + the time a heavy plugin may need)
BUCKET_S = 60.0
GAIN_CAP = math.sqrt(NB)
TOP_N = 5


def require(started):
    """The plugin names that must already be running. Raises RuntimeError (the host reports it and runs on without mixscore)."""
    missing = [n for n in ("tags", "birds") if n not in started]
    if missing:
        raise RuntimeError(
            "mixscore needs the running plugins: " + ", ".join(missing) + " (birds is the per-beam side of the comparison; tags is the speech rule)"
        )


def mean_mix(x):
    """(n, 8) float window -> (float32 mix of length n, gain in dB). See the module docstring."""
    x64 = np.asarray(x, dtype=np.float64)
    m = x64.mean(axis=1)
    pm = float((m**2).mean())
    pb = float((x64**2).mean())
    if pm < 1e-30 or pb < 1e-30:
        return m.astype(np.float32), 0.0
    g = min(math.sqrt(pb / pm), GAIN_CAP)
    if abs(g - 1.0) < 1e-9:  # round-off only (identical beams): the mix is the beam, exactly
        g = 1.0
    return (m * g).astype(np.float32), 20.0 * math.log10(g)


def is_human(common, scientific):
    for s in (common, scientific):
        if isinstance(s, str) and s.strip().lower().startswith("human "):
            return True
    return False


def species_key(d):
    return d.get("scientific") or d.get("common")


def _group(g):
    return g if g else "unknown"


class BirdNetScorer:
    """The default scorer seam: a second, private BirdNet instance (the TFLite interpreter inside birdnetlib is single-thread
    only and `birds` runs concurrently in the pool; a shared instance would need a lock that could stall `birds`)."""

    def __init__(self, classify_dir, min_conf, geo_config):
        sys.path.insert(0, classify_dir)
        from bma_classify import BirdNet, load_geo_config

        if geo_config:
            g = load_geo_config(geo_config)
            self.bn = BirdNet(min_conf, g["lat"], g["lon"], g["week"], g["threshold"])
        else:
            self.bn = BirdNet(min_conf)

    def __call__(self, audio48k, when):
        acc = self.bn.classify(audio48k, when=when)
        return list(acc), list(self.bn.filtered)


class MixScore:
    name = "mixscore"
    win_s, hop_s = 3.0, 3.0
    heavy = True

    def __init__(self, classify_dir=None, min_conf=0.25, gate_dbfs=-70.0, geo_config=None, taxa=None, scorer=None, every=1):
        """every: score every N-th 3 s window (host.py --mixscore-every; the window grid stays the one `birds` uses, so the pairing is
        unchanged and the CPU cost divides by N). scorer(audio48k float32, datetime) -> (accepted, filtered): lists of {common, scientific, confidence[, in_region,
        non_species]}. None = BirdNetScorer. taxa: a taxa.Taxa (None = load the default table)."""
        if not isinstance(every, int) or isinstance(every, bool) or every < 1:
            raise ValueError(f"every must be an integer >= 1, got {every!r}")
        self.hop_s = 3.0 * every
        if taxa is None:
            import taxa as _taxa

            taxa = _taxa.Taxa()
        self.taxa = taxa
        self.scorer = scorer if scorer is not None else BirdNetScorer(classify_dir, min_conf, geo_config)
        self.gate = gate_dbfs
        self.min_conf = min_conf
        self.lock = threading.Lock()
        self.pending = []  # windows scored, not yet resolved
        self.birds_obs = []  # (t, {species: {conf, group}}) per birds window
        self.tags_obs = []  # (t, speech_active)
        self.clock = 0.0  # newest time seen from any plugin
        self.buckets = {}  # bucket start -> counters
        self.errors = 0
        self.last_error = None
        self.n_scored = 0
        self.n_gated = 0
        self.n_human_dropped = 0
        self.cpu_s = 0.0  # CPU seconds spent in process() (the pool thread's own CPU clock)
        self.n_calls = 0

    # ------------------------------------------------------------------ the window
    def process(self, x, t_wall):
        c0 = time.thread_time()
        try:
            return self._process(x, t_wall)
        finally:
            with self.lock:
                self.cpu_s += time.thread_time() - c0
                self.n_calls += 1

    def cost(self):
        """Measured CPU of this plugin for status.json: mean ms per process() call and amortised ms per second of audio
        (a call covers `hop_s` seconds of the stream). Empty until the first window."""
        with self.lock:
            if not self.n_calls:
                return {"every": int(round(self.hop_s / 3.0))}
            ms = 1000.0 * self.cpu_s / self.n_calls
            return {"every": int(round(self.hop_s / 3.0)), "cpu_ms_per_window": round(ms, 1), "cpu_ms_per_audio_s": round(ms / self.hop_s, 2)}

    def _process(self, x, t_wall):
        import datetime

        e = (np.asarray(x, dtype=np.float64) ** 2).mean(axis=0)
        lvl = 10.0 * math.log10(max(float(e.max()), 1e-24))
        if lvl < self.gate:
            with self.lock:
                self.n_gated += 1
                self._bucket(t_wall)["gated"] += 1
            return [], None
        mix, gain_db = mean_mix(x)
        try:
            acc, filt = self.scorer(mix, datetime.datetime.fromtimestamp(t_wall))
        except Exception as e:  # counted here (status meta) and by the host; the host carries on
            with self.lock:
                self.errors += 1
                self.last_error = f"{type(e).__name__}: {e}"[:300]
            raise
        mix_db = 10.0 * math.log10(max(float((mix.astype(np.float64) ** 2).mean()), 1e-24))
        top = {}
        human = 0
        for d in acc:
            if is_human(d.get("common"), d.get("scientific")):
                human += 1
                continue
            grp, non_sp = self.taxa.lookup(d.get("scientific"), d.get("common"))
            k = species_key(d)
            c = float(d.get("confidence") or 0.0)
            if k not in top or c > top[k]["confidence"]:
                top[k] = {
                    "common": d.get("common"),
                    "scientific": d.get("scientific"),
                    "confidence": round(c, 4),
                    "in_region": d.get("in_region") if "in_region" in d else None,
                    "non_species": d["non_species"] if "non_species" in d else (d.get("scientific") == d.get("common") or bool(non_sp)),
                    "group": grp,
                }
        with self.lock:
            self.n_scored += 1
            self.n_human_dropped += human
            self.pending.append(
                {
                    "t": float(t_wall),
                    "level_dbfs": round(lvl, 1),
                    "mix_dbfs": round(mix_db, 1),
                    "gain_db": round(gain_db, 2),
                    "top": top,
                    "n_filtered": len(filt),
                    "human": human,
                }
            )
        return [], None

    # ------------------------------------------------------------------ what the other plugins said
    def observe(self, plugin, recs, t_wall):
        """Called by the host after every other plugin's window with that plugin's records (also an empty list). Returns the
        records now ready to be written (window records and finished minutes). Never raises."""
        try:
            with self.lock:
                self.clock = max(self.clock, float(t_wall))
                if plugin == "birds":
                    sp = {}
                    for r in recs:
                        if r.get("k") != "bird":
                            continue
                        if is_human(r.get("common"), r.get("scientific")):
                            self.n_human_dropped += 1
                            continue
                        k = species_key(r)
                        c = float(r.get("confidence") or 0.0)
                        if k not in sp or c > sp[k]["confidence"]:
                            sp[k] = {"confidence": c, "group": r.get("group")}
                    self.birds_obs.append((float(t_wall), sp))
                    self.birds_obs = self.birds_obs[-64:]
                elif plugin == "tags":
                    active = any(bool(r.get("speech_active")) for r in recs)
                    self.tags_obs.append((float(t_wall), active))
                    self.tags_obs = self.tags_obs[-128:]
                return self._resolve(force=False)
        except Exception as e:  # never reaches the window loop
            self.errors += 1
            self.last_error = f"{type(e).__name__}: {e}"[:300]
            return []

    def flush(self):
        """Shutdown: resolve what is pending with what is known and emit every open minute (partial)."""
        with self.lock:
            return self._resolve(force=True)

    # ------------------------------------------------------------------ resolution (lock held)
    def _speech_state(self, t):
        """'speech' | 'unknown' | 'clear' | 'wait' for the mix window ending at t."""
        lo = t - self.win_s
        over = [(tt, a) for tt, a in self.tags_obs if tt > lo and tt - TAGS_WIN_S < t]
        if any(a for _, a in over):
            return "speech"  # known speech overrides everything, also before coverage is complete
        if not any(tt >= t + TAGS_WIN_S for tt, _ in self.tags_obs):
            return "wait"
        cur = lo  # the union of the overlapping tags windows must cover [lo, t]
        for tt, _ in sorted(over):
            if tt - TAGS_WIN_S > cur + 1e-9:
                break
            cur = max(cur, tt)
        return "clear" if cur >= t - 1e-9 else "unknown"

    def _birds_for(self, t):
        best = None
        for tb, sp in self.birds_obs:
            if abs(tb - t) <= PAIR_TOL_S and (best is None or abs(tb - t) < abs(best[0] - t)):
                best = (tb, sp)
        return None if best is None else best[1]

    def _bucket(self, t):
        b = math.floor(t / BUCKET_S) * BUCKET_S
        c = self.buckets.get(b)
        if c is None:
            c = self.buckets[b] = {
                "gated": 0,
                "scored": 0,
                "paired": 0,
                "unpaired": 0,
                "dropped_speech": 0,
                "dropped_unknown": 0,
                "both": 0,
                "beam_only": 0,
                "mix_only": 0,
                "diffs": [],
                "best": [],
                "mixc": [],
                "groups": {},
                "gain": [],
            }
        return c

    def _resolve(self, force):
        out = []
        keep = []
        for w in self.pending:
            t = w["t"]
            st = self._speech_state(t)
            old = self.clock - t > HOLD_S
            birds = self._birds_for(t)
            if st == "wait" and not (force or old):
                keep.append(w)
                continue
            if st == "wait":
                st = "unknown"
            if st == "clear" and birds is None and not (force or old):
                keep.append(w)
                continue
            b = self._bucket(t)
            if st == "speech":
                b["dropped_speech"] += 1
                continue
            if st == "unknown":
                b["dropped_unknown"] += 1
                continue
            b["scored"] += 1
            b["gain"].append(w["gain_db"])
            if w["top"]:
                out.append(
                    {
                        "type": "mixscore",
                        "v": 1,
                        "t": round(t, 3),
                        "level_dbfs": w["level_dbfs"],
                        "mix_dbfs": w["mix_dbfs"],
                        "mix_gain_db": w["gain_db"],
                        "n_filtered": w["n_filtered"],
                        "top": sorted(w["top"].values(), key=lambda d: -d["confidence"])[:TOP_N],
                    }
                )
            if birds is None:
                b["unpaired"] += 1
                continue
            b["paired"] += 1
            mixd = w["top"]
            for k in set(birds) | set(mixd):
                grp = _group(mixd[k]["group"] if k in mixd else birds[k]["group"])
                g = b["groups"].setdefault(grp, {"both": 0, "beam_only": 0, "mix_only": 0})
                if k in birds and k in mixd:
                    b["both"] += 1
                    g["both"] += 1
                    b["best"].append(birds[k]["confidence"])
                    b["mixc"].append(mixd[k]["confidence"])
                elif k in birds:
                    b["beam_only"] += 1
                    g["beam_only"] += 1
                else:
                    b["mix_only"] += 1
                    g["mix_only"] += 1
        self.pending = keep
        # minutes: emit a bucket once it is over by the grace and nothing pending belongs to it
        open_b = {math.floor(w["t"] / BUCKET_S) * BUCKET_S for w in self.pending}
        for bs in sorted(self.buckets):
            if bs in open_b:
                continue
            done = force or self.clock >= bs + BUCKET_S + EMIT_GRACE_S
            if not done:
                continue
            rec = self._compare(bs, self.buckets.pop(bs), partial=force and self.clock < bs + BUCKET_S + EMIT_GRACE_S)
            if rec is not None:
                out.append(rec)
        return out

    def _compare(self, bs, c, partial):
        if not any(c[k] for k in ("gated", "scored", "dropped_speech", "dropped_unknown")):
            return None
        n = len(c["best"])
        best = np.array(c["best"], dtype=np.float64)
        mixc = np.array(c["mixc"], dtype=np.float64)
        diff = mixc - best
        rec = {
            "type": "mixscore_compare",
            "v": 1,
            "t": bs,
            "interval_s": int(BUCKET_S),
            "min_conf": self.min_conf,
            "windows": {
                "gated": c["gated"],
                "scored": c["scored"],
                "paired": c["paired"],
                "unpaired": c["unpaired"],
                "dropped_speech": c["dropped_speech"],
                "dropped_unknown": c["dropped_unknown"],
            },
            "events": {"both": c["both"], "beam_only": c["beam_only"], "mix_only": c["mix_only"]},
            "both_conf": {
                "n": n,
                "best_beam_mean": round(float(best.mean()), 4) if n else None,
                "mix_mean": round(float(mixc.mean()), 4) if n else None,
                "diff_mean": round(float(diff.mean()), 4) if n else None,
                "mix_higher": int((diff > 1e-9).sum()),
            },
            "groups": {g: dict(v) for g, v in sorted(c["groups"].items())},
            "mix_gain_db_mean": round(float(np.mean(c["gain"])), 2) if c["gain"] else None,
        }
        if partial:
            rec["partial"] = True
        return rec

    def stats(self):
        with self.lock:
            return {
                "errors": self.errors,
                "scored": self.n_scored,
                "gated": self.n_gated,
                "human_dropped": self.n_human_dropped,
                "pending": len(self.pending),
                "last_error": self.last_error,
            }


def make(ctx):
    """Plugin factory named by analysis/plugins.manifest.json (ctx = manifest.Ctx)."""
    import os

    a = ctx.args
    require(ctx.started)
    if a.mixscore_every < 4:
        print(
            f"WARN: --mixscore-every {a.mixscore_every}: the analyzer CPU budget (about 30 ms per second of audio for all plugins) "
            "is exceeded below 4; the measured cost is in status.json plugins.mixscore",
            file=sys.stderr,
            flush=True,
        )
    return MixScore(os.path.abspath(ctx.classify_dir), geo_config=a.geo_config, taxa=ctx.taxa, every=a.mixscore_every)
