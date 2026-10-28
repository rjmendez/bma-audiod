"""Wildlife clip capture for the analyzer (owner policy 2026-10-26). Spec: contracts/specs/clips.md.

AUDIO IS OFF BY DEFAULT ("I cannot keep voice recordings": the speech guard below cannot prove a clip holds no human voice); opt-in keeps it and purge_human() deletes a clip whose span later proves to hold a voice (owner 2026-10-27). Without
--clip-audio NO audio file (flac, wav, any PCM) is ever written; the capture runs VISUAL-ONLY: a spectrogram PNG, a signature JSON and
an index record, for the same events and under the same rules. The visual products are NOT INVERTIBLE TO SPEECH (clip_render.py,
clip_features.py): nothing below 3.5 kHz is represented (speech formants and pitch live below 3.4 kHz), time resolution is 50 ms per
column (non-overlapping), 96 frequency rows, 8-bit magnitude only, envelope 20 values/s, dominant frequency 10 values/s. With
--clip-audio (explicit opt-in) a short lossless clip of a wildlife sound is also kept, deleted after --audio-retention-h (24 h).

Policy (all of it, strictly):
 * event = reports of the same species within 3 s (+0.5 s jitter) of each other; certainty = the MAX confidence of the event
   (never an average); stored only when it is STRICTLY above --clip-min-conf (default 0.85; 0.05..1 accepted).
 * wildlife = taxa group bird / insect / amphibian / mammal / other-bio. Group null or missing (no taxa.json) is NOT stored;
   non_species must be exactly false; names in clip_exclude.json (domestic animals, mechanical, human) are never stored.
 * beam agreement (owner decision): the event must have been reported on at least --clip-min-beams (2) DISTINCT beams between its first
   and last report (the birds plugin reports per beam; isolated single-beam hits are the false positives). 1 disables the rule.
   The count is recorded as beams_agreeing in the sidecar and the index record.
 * out-of-region (bird_filtered / in_region false): stored under the same rule (> --clip-min-conf), flagged in_region:false;
   --clip-skip-out-of-region makes it never store.
 * PRIVACY GUARD, fail closed (it gates the sidecars as well as the audio). In [clip start - 5 s, clip end + 5 s] the tags (PANNs speech head) timeline must cover the whole span
   without a hole, with no row speech_active or speech_p >= --clip-speech-p (0.10, stricter than the tagger's 0.30), and no BirdNET
   row of any beam named Human*. Tagger off, hole, missing verdict (hold deadline) = nothing is stored. The audio is copied out of
   the ring at detection time; the verdict is awaited with the audio in RAM only (at most 8 pending clips of at most 12 s).
 * bounds: per-species-per-hour cap keeping the highest confidence, safety total-size cap for audio and sidecars, bounded writer
   queue (full = dropped and counted). Nothing here raises into the analyzer or blocks its window loop.
 * retention: audio deleted at capture age >= --audio-retention-h (24); sweep at start and every --clip-sweep-s (<= 600 s).
   Sidecars (spectrogram PNG, signature JSON, index records; no audio) stay --sidecar-retention-days (14).
Layout: <clips-dir>/YYYYMMDD/<id>.flac|.wav, <id>.png, <id>.json ; <clips-dir>/index.jsonl (append only; tombstones, never rewritten).
<clips-dir> defaults to <--out>/clips. No path, user name or drive letter is ever written into a record."""
import bisect, json, math, os, queue, re, threading, time
import numpy as np

import hist

HERE = os.path.dirname(os.path.abspath(__file__))
FS = 48000
ALLOWED_GROUPS = ("bird", "insect", "amphibian", "mammal", "other-bio")
STAGE_FLOOR = 0.5            # an event is only tracked (audio staged) once a report reaches min(this, --clip-min-conf); the --clip-min-conf rule is applied at close
MIN_CONF_LO, MIN_CONF_HI = 0.05, 1.0   # accepted --clip-min-conf range (below 0.05 every BirdNET report would stage audio)


def check_min_conf(v):
    v = float(v)
    if not (MIN_CONF_LO <= v <= MIN_CONF_HI):
        raise ValueError(f"--clip-min-conf must be between {MIN_CONF_LO} and {MIN_CONF_HI}, got {v}")
    return v


def _min_conf_arg(text):
    import argparse
    try:
        return check_min_conf(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))
GAP_TOL_S = 3.5              # same species within 3 s of the previous window end (+0.5 s jitter) extends the event
ORPHAN_S = 600.0             # a temp file / audio without a sidecar younger than this may be a write in progress
MAX_PENDING = 8
PURGE_TAIL_BYTES = 8 << 20   # purge_human reads at most this much of the end of tags.jsonl / birds.jsonl
TAGS_WINDOW_S = 5.0          # a tags row is stamped with the END of its 5 s window
AUDIO_EXT = (".flac", ".wav")
TMP_EXT = (".tmp", ".part")
ID_RE = re.compile(r"^(\d{13})-")
DAY_RE = re.compile(r"^\d{8}$")
COUNTERS = (
    "clips_skipped_not_wildlife", "clips_skipped_low_conf", "clips_skipped_single_beam", "clips_skipped_no_group", "clips_skipped_excluded",
    "clips_skipped_out_of_region", "clips_skipped_speech", "clips_skipped_human_label", "clips_skipped_tagger_unavailable",
    "clips_skipped_guard_hole", "clips_skipped_no_verdict", "clips_skipped_ring_gap", "clips_dropped_queue",
    "clips_dropped_pending_full", "clips_dropped_cap", "clips_evicted_cap", "clips_evicted_size", "clips_evicted_sidecar_size", "clips_purged_human",
    "clips_out_of_region_stored", "clips_stored", "clip_write_errors", "clip_sidecar_errors", "clip_index_errors",
    "clip_service_errors", "audio_expired", "audio_expire_retry", "orphans_deleted", "sweep_runs", "sweep_errors")


class ClipConfig:
    def __init__(self, min_conf=0.85, cap_per_species_hour=6, audio_retention_h=24.0, sidecar_retention_days=14.0, max_s=12.0,
                 pre_s=1.0, post_s=1.0, audio_cap_mb=512.0, sidecar_cap_mb=256.0, sweep_s=300.0, guard_s=5.0, speech_p=0.10,
                 hold_s=60.0, skip_out_of_region=False, queue_max=8, calibration=None, exclude_path=None, min_beams=2, audio=False, pcen=False):
        self.min_conf, self.cap_per_species_hour = check_min_conf(min_conf), int(cap_per_species_hour)
        self.stage_floor = min(STAGE_FLOOR, self.min_conf)                  # at the default 0.85 this is the old 0.5
        self.audio_retention_s = float(audio_retention_h) * 3600.0
        self.sidecar_retention_s = float(sidecar_retention_days) * 86400.0
        self.max_s, self.pre_s, self.post_s = min(float(max_s), 12.0), float(pre_s), float(post_s)
        self.audio_cap = int(audio_cap_mb * (1 << 20))
        self.sidecar_cap = int(sidecar_cap_mb * (1 << 20))
        self.sweep_s = max(1.0, min(float(sweep_s), 600.0))      # a sweep re-lists the store: never spin
        self.guard_s, self.speech_p, self.hold_s = float(guard_s), float(speech_p), float(hold_s)
        self.skip_out_of_region, self.queue_max = bool(skip_out_of_region), int(queue_max)
        self.calibration = calibration
        self.audio = bool(audio)                                   # --clip-audio: ALSO write the lossless audio clip (default: visual only)
        self.pcen = bool(pcen)                                     # --clip-pcen: the PNG uses the PCEN scale (default: dB)
        self.min_beams = int(min_beams)                           # distinct beams that must agree on the species (1 = rule off)
        self.exclude_path = exclude_path or os.path.join(HERE, "clip_exclude.json")


def add_args(ap):
    ap.add_argument("--clips-dir", help="wildlife clips + sidecars + index.jsonl (default: <--out>/clips)")
    ap.add_argument("--clip-min-conf", type=_min_conf_arg, default=0.85, help="store a wildlife clip only when the event's max BirdNET confidence is STRICTLY above this (0.05..1; events are tracked from min(0.5, this))")
    ap.add_argument("--clip-cap-per-species-hour", type=int, default=6, help="clips kept per species per UTC hour (highest confidence win)")
    ap.add_argument("--audio-retention-h", type=float, default=24.0, help="delete stored AUDIO this many hours after its capture time")
    ap.add_argument("--sidecar-retention-days", type=float, default=14.0, help="keep the spectrogram PNG, signature JSON and index record this long")
    ap.add_argument("--clip-max-s", type=float, default=12.0, help="longest clip (the RAM ring is 12 s)")
    ap.add_argument("--clip-pre-s", type=float, default=1.0)
    ap.add_argument("--clip-post-s", type=float, default=1.0)
    ap.add_argument("--clip-audio-cap-mb", type=float, default=512.0, help="safety cap for stored audio; over it, lowest confidence then oldest go first")
    ap.add_argument("--clip-sidecar-cap-mb", type=float, default=256.0, help="safety cap for sidecars; oldest go first")
    ap.add_argument("--clip-sweep-s", type=float, default=300.0, help="retention sweep interval in seconds (clamped to 1..600)")
    ap.add_argument("--clip-guard-s", type=float, default=5.0, help="privacy guard margin around the clip")
    ap.add_argument("--clip-speech-p", type=float, default=0.10, help="tagger speech_p at or above this blocks a clip")
    ap.add_argument("--clip-hold-s", type=float, default=60.0, help="how long a finished clip waits in RAM for the tagger verdict")
    ap.add_argument("--clip-min-beams", type=int, default=2, help="store a clip only when at least this many distinct beams reported the species within the event (1 disables)")
    ap.add_argument("--clip-audio", action="store_true", help="ALSO store the lossless audio clip (default OFF: visual-only, no audio file is ever written; "
                    "the supervisor passes it only when enable_clip_audio.txt exists in its root; the installer and container entrypoint never do; confirmed human voices are purged)")
    ap.add_argument("--clip-pcen", action="store_true", help="render the clip spectrogram PNG with PCEN-normalised levels instead of dB (default off; same rows, columns and 8 bits)")
    ap.add_argument("--clip-skip-out-of-region", action="store_true", help="never store a detection the location filter marked out of region")
    ap.add_argument("--clip-exclude", help="exclusion JSON (default: clip_exclude.json next to clips.py)")


def config_from_args(a, calibration):
    return ClipConfig(a.clip_min_conf, a.clip_cap_per_species_hour, a.audio_retention_h, a.sidecar_retention_days, a.clip_max_s,
                      a.clip_pre_s, a.clip_post_s, a.clip_audio_cap_mb, a.clip_sidecar_cap_mb, a.clip_sweep_s, a.clip_guard_s,
                      a.clip_speech_p, a.clip_hold_s, a.clip_skip_out_of_region, calibration=calibration, exclude_path=a.clip_exclude, min_beams=a.clip_min_beams, audio=a.clip_audio, pcen=getattr(a, "clip_pcen", False))


def to_i16(col):
    return np.clip(np.rint(np.asarray(col, np.float64) * 32768.0), -32768, 32767).astype(np.int16)


def make_ring_reader(rings, lock):
    """ring_reader(abs_start, n, beam) -> (abs_start_actual, int16 samples) of what the ring still holds of that span, or None.
    The span is clamped to [ring.start, ring.end): a gap resets the ring, so what is returned is always contiguous."""
    def reader(a0, n0, beam):
        with lock:
            r = rings[beam]
            if r.end is None:
                return None
            lo, hi = max(a0, r.start), min(a0 + n0, r.end)
            return None if hi <= lo else (lo, to_i16(r.get(lo, hi - lo)))
    return reader


def slug(s):
    return (re.sub(r"[^a-z0-9]+", "", (s or "unknown").lower()) or "unknown")[:24]


def id_time(name):
    m = ID_RE.match(name)
    return int(m.group(1)) / 1000.0 if m else None


def is_human(r):
    return any(str(r.get(k) or "").strip().lower().startswith("human") for k in ("common", "scientific"))


def load_exclusions(path):
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    return set(str(n).lower() for n in d["names"]), tuple(str(p).lower() for p in d["prefixes"])


def load_human_top(path):
    """-> (set of lower-case AudioSet labels, probability threshold). A tags row whose `top` list holds one of these labels at or above
    the threshold blocks a clip even when speech_p is low (laughter, screaming, coughing, humming, crowds ... are people too)."""
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    return set(str(n).lower() for n in d["human_top_labels"]), float(d["human_top_p"])


def uncovered(lo, hi, ivs):
    """Parts of [lo, hi) not covered by the union of intervals."""
    out, cur = [], lo
    for s, e in sorted(ivs):
        if e <= cur:
            continue
        if s > cur:
            out.append((cur, min(s, hi)))
        cur = max(cur, e)
        if cur >= hi:
            break
    if cur < hi:
        out.append((cur, hi))
    return [(a, b) for a, b in out if b > a]


# ======================================================================================================================
class ClipStore:
    """Filesystem side: atomic writes, index, caps, retention sweep. Used by the writer and sweeper threads (fs_lock)."""

    def __init__(self, root, cfg, counters, model_version=None, clock=time.time, replace=os.replace, remove=os.remove):
        self.root, self.cfg, self.c = root, cfg, counters
        self.clock, self.replace, self.remove = clock, replace, remove
        self.lock = threading.RLock()            # table + files; the analyzer thread never takes it (status reads audio_stats lock-free)
        self.sweep_mu = threading.Lock()         # one sweep at a time
        self.index = hist.RotatingJsonl(os.path.join(root, "index.jsonl"), 16 << 20)
        self.table = {}                  # id -> {t, conf, key, audio_bytes, side_bytes, day, audio(name or None)}
        self.audio_written = False
        self.last_stored_t = None
        self.sf = self.codec = None
        if cfg.audio:                                # visual-only never imports the audio encoder: no code path can write PCM
            import soundfile as sf
            self.sf = sf
            self.codec = "flac" if "FLAC" in sf.available_formats() else "wav"
        os.makedirs(root, exist_ok=True)
        self.rescan()

    # ---- bookkeeping -------------------------------------------------------------------------------------------
    def rescan(self):
        """Rebuild the table from the sidecars on disk. Done once at construction (a sweep keeps the table incrementally: it must not
        re-read every JSON each time, that costs about 40 ms per sidecar on a network or 9p mount)."""
        with self.lock:
            table = {}
            try:
                days = sorted(d for d in os.listdir(self.root) if DAY_RE.match(d) and os.path.isdir(os.path.join(self.root, d)))
            except OSError:
                self.table = table
                return
            for day in days:
                d = os.path.join(self.root, day)
                try:
                    names = os.listdir(d)
                except OSError:
                    continue
                for n in names:
                    if not n.endswith(".json"):
                        continue
                    try:
                        with open(os.path.join(d, n), "r", encoding="utf-8") as f:
                            j = json.load(f)
                        cid = n[:-5]
                        table[cid] = {"t": j["t_start"], "conf": j["confidence_max"], "key": (j["species"]["scientific"] or j["species"]["common"], int(j["t_start"] // 3600)),
                                      "t_end": j.get("t_end"), "day": day, "audio": None, "audio_bytes": 0, "side_bytes": 0, "expired": bool(j.get("audio_expired")) or j.get("audio_stored") is False}
                    except (OSError, ValueError, KeyError, TypeError):
                        continue
                for n in names:
                    cid, ext = os.path.splitext(n)
                    e = table.get(cid)
                    if e is None:
                        continue
                    try:
                        sz = os.path.getsize(os.path.join(d, n))
                    except OSError:
                        continue
                    if ext in AUDIO_EXT:
                        e["audio"], e["audio_bytes"] = n, sz
                    elif ext in (".png", ".json"):
                        e["side_bytes"] += sz
            self.table = table

    def audio_stats(self):
        """(files, bytes) of stored audio. Lock-free on purpose: the analyzer's status block calls it every 5 s and must never wait for a
        sweep; list() of the dict values is atomic under the GIL."""
        ents = list(self.table.values())
        return (sum(1 for e in ents if e["audio"]), sum(e["audio_bytes"] for e in ents))

    def _path(self, day, name):
        return os.path.join(self.root, day, name)

    def _rm(self, path):
        """-> True when the file is gone. A PermissionError / OSError (Windows sharing violation, 9p) leaves it for the next sweep."""
        try:
            self.remove(path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False

    def _tomb(self, kind, cid, reason=None):
        self.c["clip_index_errors"] += 0 if self.index.write({"k": kind, "v": 1, "id": cid, "t": round(self.clock(), 3), **({"reason": reason} if reason else {})}) else 1

    # ---- write -------------------------------------------------------------------------------------------------
    def write(self, item):
        """item: dict from ClipService (pcm int16, meta, sidecar parts). -> 'stored' | 'dropped_cap' | 'error'."""
        with self.lock:
            if self.cfg.cap_per_species_hour <= 0:                                # --clip-cap-per-species-hour 0: nothing may be kept
                self.c["clips_dropped_cap"] += 1
                return "dropped_cap"
            key = item["key"]
            same = [(e["conf"], -e["t"], cid) for cid, e in self.table.items() if e["key"] == key]
            victim = None
            if len(same) >= self.cfg.cap_per_species_hour:
                lowest = min(same)                                            # lowest confidence; among equals the NEWEST (older wins ties)
                if item["conf"] <= lowest[0]:
                    self.c["clips_dropped_cap"] += 1
                    return "dropped_cap"
                victim = lowest[2]
            res = self._write_files(item)
            if res != "stored":
                return res
            if victim is not None:
                self._evict(victim, "species_hour_cap")
                self.c["clips_evicted_cap"] += 1
            self._enforce_caps()
            return "stored"

    def _write_files(self, item):
        cid, day = item["id"], item["day"]
        try:
            os.makedirs(os.path.join(self.root, day), exist_ok=True)
        except OSError:
            self.c["clip_write_errors"] += 1
            return "error"
        ext = "." + self.codec if self.cfg.audio else ""
        apath, tmp = self._path(day, cid + ext), self._path(day, cid + ext + ".tmp")
        abytes = 0
        if self.cfg.audio:
            try:
                self.sf.write(tmp, item["pcm"], FS, subtype="PCM_16", format=self.codec.upper())
                self.replace(tmp, apath)
            except (OSError, RuntimeError, ValueError):
                self._rm(tmp)
                self.c["clip_write_errors"] += 1
                return "error"
            self.audio_written = True
            abytes = os.path.getsize(apath)
        png_ok = False
        ptmp, ppath = self._path(day, cid + ".png.tmp"), self._path(day, cid + ".png")
        try:
            import clip_render
            clip_render.write_png(ptmp, item["pcm"], pcen=self.cfg.pcen)
            self.replace(ptmp, ppath)
            png_ok = True
        except Exception:                                                     # PIL missing, disk full ...: the clip is still worth keeping
            self._rm(ptmp)
            self.c["clip_sidecar_errors"] += 1
        sc = dict(item["sidecar"], audio_stored=bool(self.cfg.audio), audio_file=(f"{day}/{cid}{ext}" if self.cfg.audio else None), codec=self.codec, png=(f"{day}/{cid}.png" if png_ok else None),
                  json=f"{day}/{cid}.json", audio_bytes=abytes)
        if not hist.AtomicJson(self._path(day, cid + ".json"), replace=self.replace).write(sc):
            for p in ((apath,) if self.cfg.audio else ()) + (ppath, self._path(day, cid + ".json.tmp")):
                self._rm(p)
            self.c["clip_sidecar_errors"] += 1
            self.c["clip_write_errors"] += 1
            return "error"
        rec = {"k": "clip_stored", "v": 1, "id": cid, "t_start": sc["t_start"], "t_end": sc["t_end"], "species": sc["species"],
               "group": sc["group"], "confidence_max": sc["confidence_max"], "beam": sc["beam"], "beams_agreeing": sc["beams_agreeing"], "in_region": sc["in_region"], "audio_stored": sc["audio_stored"],
               "audio_file": sc["audio_file"], "audio_bytes": abytes, "codec": self.codec, "png": sc["png"], "json": sc["json"],
               "duration_s": sc["duration_s"], "model_version": sc["model_version"], "provenance": "model_opinion"}
        if not self.index.write(rec):
            self.c["clip_index_errors"] += 1
        side = sum(os.path.getsize(p) for p in (ppath, self._path(day, cid + ".json")) if os.path.exists(p))
        self.table[cid] = {"t": sc["t_start"], "t_end": sc["t_end"], "conf": sc["confidence_max"], "key": item["key"], "day": day, "audio": (cid + ext) if self.cfg.audio else None,
                           "audio_bytes": abytes, "side_bytes": side, "expired": not self.cfg.audio}      # visual-only: no audio, nothing to expire
        self.c["clips_stored"] += 1
        if sc["in_region"] is False:
            self.c["clips_out_of_region_stored"] += 1
        self.last_stored_t = self.clock()
        return "stored"

    # ---- eviction / expiry -----------------------------------------------------------------------------------
    def _evict(self, cid, reason):
        e = self.table.pop(cid, None)
        if e is None:
            return
        for ext in (".flac", ".wav", ".png", ".json"):
            self._rm(self._path(e["day"], cid + ext))
        self._tomb("clip_evicted", cid, reason)

    def _expire_audio(self, cid, reason):
        """Delete the audio of a stored clip, then mark the sidecar and append the tombstone. False = still there (retry next sweep)."""
        e = self.table.get(cid)
        day = e["day"] if e else None
        gone = True
        for ext in AUDIO_EXT:
            p = self._path(day, cid + ext) if day else None
            if p and os.path.exists(p) and not self._rm(p):
                gone = False
        if not gone:
            self.c["audio_expire_retry"] += 1
            return False
        self._mark_expired(cid, day, reason)
        if e:
            e["audio"], e["audio_bytes"] = None, 0
        self.c["audio_expired"] += 1
        return True

    def _mark_expired(self, cid, day, reason):
        if day:
            aj = hist.AtomicJson(self._path(day, cid + ".json"), replace=self.replace)
            j = aj.read()
            if isinstance(j, dict) and not j.get("audio_expired"):
                j.update(audio_expired=True, audio_file=None, audio_expired_t=round(self.clock(), 3), audio_expired_reason=reason)
                if not aj.write(j):
                    self.c["clip_sidecar_errors"] += 1
        e = self.table.get(cid)
        if e:
            e["expired"] = True
        self._tomb("clip_audio_expired", cid, reason)

    def _enforce_caps(self):
        """Safety caps. Audio: lowest confidence, then oldest, goes first. Sidecars: oldest first."""
        while True:
            files, total = self.audio_stats()
            if total <= self.cfg.audio_cap or files == 0:
                break
            cid = min((c for c, e in self.table.items() if e["audio"]), key=lambda c: (self.table[c]["conf"], self.table[c]["t"]))
            if not self._expire_audio(cid, "size_cap"):
                break
            self.c["clips_evicted_size"] += 1
        while sum(e["side_bytes"] for e in self.table.values()) > self.cfg.sidecar_cap and self.table:
            cid = min(self.table, key=lambda c: self.table[c]["t"])
            if not self._drop_sidecars(cid, "size_cap"):
                break
            self.c["clips_evicted_sidecar_size"] += 1

    def _drop_sidecars(self, cid, reason):
        e = self.table.get(cid)
        if e is None:
            return True
        ok = True
        for ext in (".flac", ".wav", ".png", ".json"):
            p = self._path(e["day"], cid + ext)
            if os.path.exists(p) and not self._rm(p):
                ok = False
        if ok:
            self.table.pop(cid, None)
            self._tomb("clip_sidecar_expired", cid, reason)
        return ok

    # ---- retention sweep -------------------------------------------------------------------------------------------
    def sweep(self, now=None):
        now = self.clock() if now is None else now
        with self.sweep_mu:
            self.c["sweep_runs"] += 1
            self.last_sweep_t = now
            try:
                self._sweep(now)
            except Exception:
                self.c["sweep_errors"] += 1

    def _sweep(self, now):
        """Listing and stat calls run WITHOUT the store lock; the lock is taken only to apply one day's decisions (a few removes), so the
        writer is held up for milliseconds and the analyzer thread never (it does not take this lock)."""
        try:
            days = sorted(d for d in os.listdir(self.root) if DAY_RE.match(d) and os.path.isdir(os.path.join(self.root, d)))
        except OSError:
            return
        for day in days:
            self._sweep_day(day, now)
        with self.lock:
            self._enforce_caps()

    def _sweep_day(self, day, now):
        cfg = self.cfg
        dpath = os.path.join(self.root, day)
        try:
            names = sorted(os.listdir(dpath))
        except OSError:
            return
        with self.lock:
            known = set(self.table)
        mts = {}
        for n in names:                                        # stat (outside the lock) only what could be an orphan or a temp file
            if n.endswith(TMP_EXT) or os.path.splitext(n)[0] not in known:
                try:
                    mts[n] = os.path.getmtime(os.path.join(dpath, n))
                except OSError:
                    pass
        with self.lock:
            audio_here = set()
            for n in names:
                p = os.path.join(dpath, n)
                cap_t = id_time(n)
                is_tmp = n.endswith(TMP_EXT)
                base = n[:-4] if is_tmp and n.endswith(".tmp") else n
                audioish = base.endswith(AUDIO_EXT) or (is_tmp and (n.endswith(".part") or base.endswith(AUDIO_EXT)))
                cid = os.path.splitext(n)[0]
                if is_tmp:
                    mt = mts.get(n)
                    if mt is None:
                        continue
                    cap_t = mt if cap_t is None else cap_t
                    if now - mt >= ORPHAN_S or (audioish and now - cap_t >= cfg.audio_retention_s):
                        if self._rm(p):
                            self.c["orphans_deleted"] += 1
                        elif audioish:
                            self.c["audio_expire_retry"] += 1
                elif cid in self.table:
                    if n.endswith(AUDIO_EXT):
                        audio_here.add(cid)
                        if now - (self.table[cid]["t"] if cap_t is None else cap_t) >= cfg.audio_retention_s:
                            self._expire_audio(cid, "age")
                else:                                         # no sidecar (table entry) for this file: a crash leftover
                    mt = mts.get(n)
                    if mt is None or not n.endswith(AUDIO_EXT + (".png", ".json")):
                        continue
                    cap_t = mt if cap_t is None else cap_t
                    expired = now - cap_t >= (cfg.audio_retention_s if n.endswith(AUDIO_EXT) else cfg.sidecar_retention_s)
                    if now - mt >= ORPHAN_S or expired:
                        if self._rm(p):
                            self.c["orphans_deleted"] += 1
                        elif n.endswith(AUDIO_EXT):
                            self.c["audio_expire_retry"] += 1
            for cid, e in list(self.table.items()):
                if e["day"] != day:
                    continue
                if e["audio"] and cid not in audio_here and not any(os.path.exists(self._path(day, cid + x)) for x in AUDIO_EXT):
                    e["audio"], e["audio_bytes"] = None, 0           # the audio vanished without a tombstone
                if not e["audio"] and not e["expired"]:
                    self._mark_expired(cid, day, "reconcile")
                if now - e["t"] >= cfg.sidecar_retention_s:
                    self._drop_sidecars(cid, "age")
            try:
                if not os.listdir(dpath):
                    os.rmdir(dpath)
            except OSError:
                pass


# ======================================================================================================================
class ClipService:
    """Policy + staging. Public methods never raise (service_errors) and never block (bounded queue, put_nowait)."""

    def __init__(self, root, cfg, ring_reader, tagger_enabled, model_version=None, clock=time.time, start_threads=True,
                 wait=None, replace=os.replace, remove=os.remove):
        self.cfg, self.ring_reader, self.tagger_enabled = cfg, ring_reader, bool(tagger_enabled)
        self.model_version, self.clock = model_version, clock
        self.log_dir = os.path.dirname(os.path.abspath(root))      # <out>: tags.jsonl and birds.jsonl sit next to the clips dir
        self.c = {k: 0 for k in COUNTERS}
        self.mu = threading.Lock()
        self.store = ClipStore(root, cfg, self.c, model_version, clock, replace, remove)
        self.names, self.prefixes = load_exclusions(cfg.exclude_path)
        self.human_top, self.human_top_p = load_human_top(cfg.exclude_path)
        self.azimuths = self._load_azimuths(cfg.calibration)
        self.q = queue.Queue(maxsize=cfg.queue_max)
        self.noted = {}                  # (plugin, t_wall) -> (start, n)
        self.tag_inflight = []           # (start, end) of tags windows noted and not yet processed
        self.tags_tl = []                # (start, end, speech_p, flagged) of processed tags windows
        self.birds_tl = []               # (start, end, human) of processed birds windows
        self.events = {}                 # species key -> open event
        self.pending = []                # assembled clips waiting for the guard verdict
        self.stop_ev = threading.Event()
        self.wait = wait or (lambda s: self.stop_ev.wait(s))
        self.threads = []
        self.start_threads = start_threads

    @staticmethod
    def _load_azimuths(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                az = json.load(f)["profile"]["beam_azimuth_deg"]
            return [float(v) for v in az] if len(az) == 8 else None
        except (OSError, ValueError, KeyError, TypeError):
            return None

    # ---- lifecycle ---------------------------------------------------------------------------------------------------
    def start(self):
        self.store.sweep()                                   # crash leftovers go before the first window
        if self.start_threads:
            for fn, nm in ((self._writer_loop, "clip-writer"), (self._sweeper_loop, "clip-sweeper")):
                t = threading.Thread(target=fn, name=nm, daemon=True)
                t.start()
                self.threads.append(t)

    def stop(self):
        self.stop_ev.set()
        for t in self.threads:
            t.join(timeout=2.0)

    def _writer_loop(self):
        while not self.stop_ev.is_set():
            try:
                self._process(self.q.get(timeout=0.25))
            except queue.Empty:
                continue

    def _sweeper_loop(self):
        while not self.stop_ev.is_set():
            self.wait(self.cfg.sweep_s)
            if not self.stop_ev.is_set():
                self.store.sweep()
                self.purge_human()

    # ---- human-voice purge (audio clips are kept; a clip later CONFIRMED to hold a voice is deleted whole) ---------------
    def _read_tail_rows(self, name, needle):
        """Parsed JSON rows of the last PURGE_TAIL_BYTES of <out>/<name> whose line contains `needle` (lower-case bytes). Garbage lines are skipped."""
        with open(os.path.join(self.log_dir, name), "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - PURGE_TAIL_BYTES))
            data = f.read()
        if size > PURGE_TAIL_BYTES:
            data = data.split(b"\n", 1)[-1]                              # the first line is cut
        rows = []
        for line in data.split(b"\n"):
            if needle in line.lower():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict):
                    rows.append(r)
        return rows

    def _human_spans(self):
        """-> sorted [(a, b)] of wall time the persisted logs confirm as human voice: a tags window (t - 5, t) flagged by the capture predicate,
        a human BirdNET row as the point (t, t). A missing or unreadable log gives no spans."""
        spans = []
        for name, needle in (("tags.jsonl", b'"tags"'), ("birds.jsonl", b"human")):
            try:
                rows = self._read_tail_rows(name, needle)
            except OSError:
                continue
            for r in rows:
                t = r.get("t")
                if not isinstance(t, (int, float)) or isinstance(t, bool) or not math.isfinite(t):
                    continue
                if name == "tags.jsonl":
                    p = r.get("speech_p")
                    if r.get("k") == "tags" and (bool(r.get("speech_active")) or (isinstance(p, (int, float)) and not isinstance(p, bool) and p >= self.cfg.speech_p)
                                                 or self._human_top(r.get("top"))):
                        spans.append((t - TAGS_WINDOW_S, t))
                elif is_human(r):
                    spans.append((t, t))
        spans.sort()
        return spans

    def purge_human(self):
        """Delete every stored clip (audio, PNG, JSON; tombstone reason human_voice) whose span +- guard_s holds a confirmed human voice in the
        persisted logs. Reads the logs only while an audio file is stored. Never raises. -> number purged."""
        try:
            ents = list(self.store.table.items())
            if not any(e.get("audio") for _, e in ents):
                return 0
            spans = self._human_spans()
            if not spans:
                return 0
            starts, g, hit = [a for a, _ in spans], self.cfg.guard_s, []
            for cid, e in ents:
                t0, t1 = e.get("t"), e.get("t_end")
                if not isinstance(t0, (int, float)) or not isinstance(t1, (int, float)):
                    continue
                lo, hi = t0 - g, t1 + g
                for a, b in spans[bisect.bisect_left(starts, lo - TAGS_WINDOW_S):bisect.bisect_right(starts, hi)]:
                    if (a < hi and b > lo) if a < b else (lo <= a <= hi):    # a window overlaps the span; a point row lies inside it
                        hit.append(cid)
                        break
            n = 0
            for cid in hit:
                with self.store.lock:
                    if cid in self.store.table:
                        self.store._evict(cid, "human_voice")
                        self.c["clips_purged_human"] += 1
                        n += 1
            return n
        except Exception:
            self.c["clip_service_errors"] += 1
            return 0

    def _process(self, item):
        try:
            self.store.write(self._build(item))
        except Exception:
            self.c["clip_service_errors"] += 1

    def drain(self):
        """Process queued clips in the calling thread (tests, and the writer when threads are off)."""
        while True:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                return
            self._process(item)

    # ---- host hooks --------------------------------------------------------------------------------------------------
    def note_window(self, name, t_wall, start_abs, n):
        try:
            if name not in ("tags", "birds"):
                return
            with self.mu:
                self.noted[(name, t_wall)] = (int(start_abs), int(n))
                if len(self.noted) > 64:
                    for k in sorted(self.noted, key=lambda k: k[1])[:len(self.noted) - 64]:
                        self.noted.pop(k, None)
                if name == "tags":
                    self.tag_inflight.append((int(start_abs), int(start_abs) + int(n)))
        except Exception:
            self.c["clip_service_errors"] += 1

    def on_plugin(self, name, recs, x, t_wall):
        try:
            with self.mu:
                w = self.noted.pop((name, t_wall), None)
                if w is None:
                    return
                if name == "tags":
                    self._on_tags(w, recs)
                elif name == "birds":
                    self._on_birds(w, recs, x, t_wall)
                self._evaluate()
        except Exception:
            self.c["clip_service_errors"] += 1

    def tick(self, now=None):
        try:
            with self.mu:
                now = self.clock() if now is None else now
                for ev in list(self.events.values()):
                    if not ev["closed"] and now - ev["last_wall"] > GAP_TOL_S + self.cfg.guard_s:
                        self._close(ev)
                self._evaluate(now)
        except Exception:
            self.c["clip_service_errors"] += 1

    # ---- tags / birds ------------------------------------------------------------------------------------------------
    def _on_tags(self, w, recs):
        s, n = w
        self.tag_inflight = [iv for iv in self.tag_inflight if iv[0] > s]      # windows run in order: older ones are dead
        if not recs:
            return                                                          # processed but no row: no coverage (fail closed)
        r = recs[0]
        p = r.get("speech_p")
        flagged = bool(r.get("speech_active")) or not isinstance(p, (int, float)) or p >= self.cfg.speech_p or self._human_top(r.get("top"))
        self.tags_tl.append((s, s + n, p if isinstance(p, (int, float)) else None, flagged))
        del self.tags_tl[:-400]

    def _human_top(self, top):
        """True when the tagger's top labels name a human sound (fail closed: a `top` that is not a list of [label, p] pairs counts)."""
        if top is None:
            return False
        try:
            return any(str(lab).strip().lower() in self.human_top and float(p) >= self.human_top_p for lab, p in top)
        except (TypeError, ValueError):
            return True

    def _static_reason(self, r):
        g = r.get("group")
        if g is None:
            return "no_group"
        if g not in ALLOWED_GROUPS or r.get("non_species") is not False:
            return "not_wildlife"
        for k in ("scientific", "common"):
            v = str(r.get(k) or "").strip().lower()
            if v and (v in self.names or v.startswith(self.prefixes)):
                return "excluded"
        if self.cfg.skip_out_of_region and r.get("in_region") is False:
            return "out_of_region"
        return None

    def _on_birds(self, w, recs, x, t_wall):
        s, n = w
        end = s + n
        human = any(is_human(r) for r in recs if r.get("k") in ("bird", "bird_filtered"))
        self.birds_tl.append((s, end, human))
        del self.birds_tl[:-400]
        by = {}
        for r in recs:
            if r.get("k") in ("bird", "bird_filtered") and isinstance(r.get("confidence"), (int, float)):
                by.setdefault(r.get("scientific") or r.get("common") or "?", []).append(r)
        energies = beams = None
        for key, rs in by.items():
            best = max(rs, key=lambda r: r["confidence"])
            ev = self.events.get(key)
            if ev is not None and s - ev["last_end"] > GAP_TOL_S * FS:
                self._close(ev)
                ev = None
            if ev is None:
                if best["confidence"] < self.cfg.stage_floor:
                    continue
                ev = self.events[key] = {"key": key, "sci": best.get("scientific"), "common": best.get("common"), "group": best.get("group"),
                                         "reason": self._static_reason(best), "windows": [], "beam_set": set(), "closed": False, "pre": None, "post": None}
            ev["last_end"], ev["last_wall"] = end, t_wall
            if ev["reason"] is not None:
                continue
            ev["beam_set"].update(int(r["beam"]) for r in rs)       # every beam that reported the species in this event (1-based; never pruned)
            if energies is None:
                e = (x.astype(np.float64) ** 2).mean(axis=0)
                energies = [round(10 * math.log10(max(float(v), 1e-20)), 1) for v in e]
                beams = [int(b) for b in np.argsort(e)[::-1][:2]]
            win = {"start": s, "end": end, "conf": float(best["confidence"]), "beam": int(best["beam"]) - 1, "in_region": best.get("in_region"),
                   "t_wall": t_wall, "energies": energies, "beams": sorted({int(r["beam"]) - 1 for r in rs}), "seg": {b: to_i16(x[:, b]) for b in beams},
                   "level": best.get("level_dbfs"), "kind": best.get("k")}
            if not ev["windows"]:
                pre = int(self.cfg.pre_s * FS)
                try:
                    got = {b: self.ring_reader(s - pre, pre, b) for b in beams} if pre > 0 else {}
                    ev["pre"] = {b: g for b, g in got.items() if g is not None}
                except Exception:
                    self.c["clip_service_errors"] += 1
            ev["windows"].append(win)
            pk = max(ev["windows"], key=lambda w_: w_["conf"])
            pc = (pk["start"] + pk["end"]) // 2
            lim, lc = int((self.cfg.max_s / 2 + 3.0) * FS), (win["start"] + win["end"]) // 2
            # only what a clip centred on the peak can use: windows near the peak or near the newest window (the peak may move later)
            ev["windows"] = [w_ for w_ in ev["windows"] if min(abs((w_["start"] + w_["end"]) // 2 - pc), abs((w_["start"] + w_["end"]) // 2 - lc)) <= lim]
        post_n = int(self.cfg.post_s * FS)
        for ev in self.events.values():               # post-roll: the first post_s of the window right after the event's last window
            if ev["reason"] is None and ev["windows"] and post_n > 0 and ev["windows"][-1]["end"] == s:
                bs = {w_["beam"] for w_ in ev["windows"]} | set(ev["windows"][-1]["seg"])
                ev["post"] = {b: (s, to_i16(x[:post_n, b])) for b in bs}
        for ev in list(self.events.values()):
            if not ev["closed"] and s - ev["last_end"] > GAP_TOL_S * FS:
                self._close(ev)
        live =[e for e in self.events.values() if e["reason"] is None and e["windows"] and not e["closed"]]
        while len(live) > MAX_PENDING:
            low = min(live, key=lambda e: max(w_["conf"] for w_ in e["windows"]))
            live.remove(low)
            self.events.pop(low["key"], None)
            self.c["clips_dropped_pending_full"] += 1

    def _close(self, ev):
        ev["closed"] = True
        self.events.pop(ev["key"], None)
        if ev["reason"] is not None:
            self.c["clips_skipped_" + ev["reason"]] += 1
            return
        if not ev["windows"]:
            return
        mx = max(w["conf"] for w in ev["windows"])
        if not mx > self.cfg.min_conf:
            self.c["clips_skipped_low_conf"] += 1
            return
        if len(ev["beam_set"]) < self.cfg.min_beams:
            self.c["clips_skipped_single_beam"] += 1
            return
        if not self.tagger_enabled:
            self.c["clips_skipped_tagger_unavailable"] += 1
            return
        peak = max(ev["windows"], key=lambda w: w["conf"])
        post = int(self.cfg.post_s * FS)
        last_end = max(w["end"] for w in ev["windows"])
        if ev["post"] is None and post > 0:            # no window followed directly (skipped windows): whatever the ring still holds
            try:
                g = self.ring_reader(last_end, post, peak["beam"])
                ev["post"] = {peak["beam"]: g} if g is not None else None
            except Exception:
                self.c["clip_service_errors"] += 1
        clip = self._assemble(ev, peak, mx)
        if clip is None:
            self.c["clips_skipped_ring_gap"] += 1
            return
        if len(self.pending) >= MAX_PENDING:
            self.c["clips_dropped_pending_full"] += 1
            return
        self.pending.append(clip)

    def _assemble(self, ev, peak, mx):
        b = peak["beam"]
        segs = []
        if ev["pre"] and b in ev["pre"]:
            segs.append(ev["pre"][b])
        segs += [(w["start"], w["seg"][b]) for w in ev["windows"] if b in w["seg"]]
        if ev["post"] is not None and b in ev["post"]:
            segs.append(ev["post"][b])
        segs.sort(key=lambda s: s[0])
        runs = []
        for st, arr in segs:
            if runs and st < runs[-1][0] + len(runs[-1][1]):
                cut = runs[-1][0] + len(runs[-1][1]) - st
                st, arr = st + cut, arr[cut:]
                if len(arr) == 0:
                    continue
            if runs and st == runs[-1][0] + len(runs[-1][1]):
                runs[-1] = (runs[-1][0], np.concatenate([runs[-1][1], arr]))
            else:
                runs.append((st, arr))
        pc = (peak["start"] + peak["end"]) // 2
        run = next(((st, a) for st, a in runs if st <= pc < st + len(a)), None)
        if run is None:
            return None
        st, arr = run
        mxn = int(self.cfg.max_s * FS)
        capped = len(arr) > mxn
        if capped:
            lo = min(max(pc - mxn // 2, st), st + len(arr) - mxn)
            arr, st = arr[lo - st:lo - st + mxn], lo
        if len(arr) < FS:
            return None
        end = st + len(arr)
        first = min(w["start"] for w in ev["windows"])
        last = max(w["end"] for w in ev["windows"])
        pre_s, post_s = max(0, first - st) / FS, max(0, end - last) / FS
        t_end_peak = peak["t_wall"]
        t0 = t_end_peak - (peak["end"] - st) / FS
        return {"ev": ev, "peak": peak, "conf": mx, "beam": b, "start": st, "end": end, "pcm": arr, "t_start": t0, "t_end": t0 + len(arr) / FS,
                "pre_s": pre_s, "post_s": post_s, "capped": capped, "ring_truncated": (not capped) and (pre_s < self.cfg.pre_s - 1e-6 or post_s < self.cfg.post_s - 1e-6),
                "deadline": self.clock() + (end - last) / FS + 2 * self.cfg.guard_s + self.cfg.hold_s,
                "beams_reported": sorted(ev["beam_set"])}

    # ---- the privacy guard ---------------------------------------------------------------------------------------
    def _verdict(self, cl):
        """-> ('ok', info) | ('speech'|'human_label'|'guard_hole'|..., None) | None (still waiting)."""
        g = int(self.cfg.guard_s * FS)
        lo, hi = cl["start"] - g, cl["end"] + g
        for s, e, h in self.birds_tl:
            if h and s < hi and e > lo:
                return "human_label", None
        near = [(s, e, p, f) for s, e, p, f in self.tags_tl if s < hi and e > lo]
        if any(f for _, _, _, f in near):
            return "speech", None
        holes = uncovered(lo, hi, [(s, e) for s, e, _, _ in near])
        if holes:
            infl = [iv for iv in self.tag_inflight if iv[0] < hi and iv[1] > lo]
            holes = [h for a, b in holes for h in uncovered(a, b, infl)]
            latest = max([e for _, e, _, _ in self.tags_tl] + [e for _, e in self.tag_inflight] + [lo])
            if holes and any(a < latest - 1 for a, _ in holes):
                return "guard_hole", None
            return None
        if not (self.birds_tl and max(e for _, e, _ in self.birds_tl) >= hi):
            return None                                                     # BirdNET has not reported up to the end of the span yet
        if not any(s >= hi for s, _, _, _ in self.tags_tl):
            return None                                                     # a tags window that starts inside the span may still be on its way
        mp = max([p for _, _, p, _ in near if p is not None] + [0.0])
        return "ok", {"tags_windows": len(near), "max_speech_p": round(mp, 3)}

    def _evaluate(self, now=None):
        now = self.clock() if now is None else now
        keep = []
        for cl in self.pending:
            v = self._verdict(cl)
            if v is None:
                if now >= cl["deadline"]:
                    self.c["clips_skipped_no_verdict"] += 1
                else:
                    keep.append(cl)
                continue
            why, info = v
            if why == "ok":
                cl["guard"] = info
                try:
                    self.q.put_nowait(cl)
                except queue.Full:
                    self.c["clips_dropped_queue"] += 1
            else:
                self.c["clips_skipped_" + why] += 1
        self.pending = keep

    def _scale_fields(self):
        if self.cfg.pcen:
            import clip_render
            return {"scale": "pcen", "pcen": clip_render.pcen_params()}
        return {"scale": "log", "db_lo": -110.0, "db_hi": -30.0}

    # ---- sidecar assembly (writer thread) --------------------------------------------------------------------------
    def _build(self, cl):
        import clip_features
        ev, pk = cl["ev"], cl["peak"]
        t0 = cl["t_start"]
        cid = f"{int(round(t0 * 1000)):013d}-{slug(ev['sci'] or ev['common'])}-b{cl['beam'] + 1}"
        day = time.strftime("%Y%m%d", time.gmtime(t0))
        iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int(round((t % 1) * 1000)):03d}Z"
        sig = clip_features.signatures(cl["pcm"], FS, pk["energies"], self.azimuths)
        env = clip_features.envelope(cl["pcm"], FS)
        sc = {
            "k": "clip", "v": 1, "id": cid, "t_start": round(t0, 3), "t_end": round(cl["t_end"], 3),
            "t_start_utc": iso(t0), "t_end_utc": iso(cl["t_end"]),
            "species": {"scientific": ev["sci"], "common": ev["common"]}, "group": ev["group"], "confidence_max": cl["conf"],
            "reports": [{"t_end": round(w["t_wall"], 3), "confidence": w["conf"], "beam": w["beam"] + 1, "in_region": w["in_region"]} for w in ev["windows"]],
            "beam": cl["beam"] + 1, "beams_reported": cl["beams_reported"], "beams_agreeing": len(cl["beams_reported"]), "in_region": pk["in_region"],
            "model": "BirdNET", "model_version": self.model_version, "provenance": "model_opinion",
            "sample_rate": FS, "channels": 1, "subtype": "PCM_16", "samples": int(len(cl["pcm"])), "duration_s": round(len(cl["pcm"]) / FS, 3),
            "pre_roll_s": round(cl["pre_s"], 3), "post_roll_s": round(cl["post_s"], 3), "capped": cl["capped"], "ring_truncated": cl["ring_truncated"],
            "start_sample": int(cl["start"]), "speech_guard": dict(cl["guard"], guard_s=self.cfg.guard_s, speech_p_threshold=self.cfg.speech_p, human_labels_seen=False),
            "audio_stored": bool(self.cfg.audio), "audio_expired": False, "audio_file": None, "png": None,
            "spectrogram": {"rows": 96, "col_s": 0.05, "columns": clip_features.n_frames(len(cl["pcm"]), 2400), "f_lo_hz": 3500.0, "f_hi_hz": 24000.0, **self._scale_fields()},
            "signatures": sig, "envelope": env,
        }
        return {"id": cid, "day": day, "pcm": cl["pcm"], "sidecar": sc, "conf": cl["conf"], "t": t0,
                "key": (ev["sci"] or ev["common"], int(t0 // 3600))}

    # ---- status ------------------------------------------------------------------------------------------------------
    def counters(self):
        return dict(self.c)

    def audio_written(self):
        return bool(self.store.audio_written)

    def status_block(self):
        n, b = self.store.audio_stats()
        with self.mu:
            pend = len(self.events) + len(self.pending)
        return {"enabled": True, "audio": bool(self.cfg.audio), "stored": self.c["clips_stored"], "pending": pend, "queue_depth": self.q.qsize(),
                "audio_files": n, "audio_bytes": b, "last_stored_t": self.store.last_stored_t}
