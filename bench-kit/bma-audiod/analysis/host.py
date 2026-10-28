#!/usr/bin/env python3
"""BMA analysis host: receives the bridge's 8 per-channel BMAA streams (slot = beam 0-7, 48 kHz, absolute
sample clock), aligns them, and runs analysis plugins on windows of all 8 beams.

PRIVACY / SCOPE: classification, embeddings and metadata only. NO audio is written to disk unless --clip-audio is given (owner 2026-10-26:
"I cannot keep voice recordings"; owner 2026-10-27: opt-in keeps them, confirmed human voices are purged; the supervisor passes it only with enable_clip_audio.txt). By default the wildlife clip capture (clips.py) is VISUAL-ONLY: a
spectrogram PNG (nothing below 3.5 kHz, 50 ms columns) and signatures, not invertible to speech, for a WILDLIFE sound whose BirdNET
certainty is above 0.85 (fail-closed speech guard); with --clip-audio a short lossless clip is also kept for 24 h (--audio-retention-h).
Nothing is transcribed. Audio otherwise lives only in RAM rings of ~12 s. Windows the tagger flags as speech produce no stored embedding. Outputs (JSONL, one object per record) under --out:
  {outputs}
  status.json (every 5 s)

Plugins that cannot start (missing model, no GPU memory) are reported in status.json and skipped; the others run.
Run:  host.py --listen 0.0.0.0:48300 --out <data-dir>/analysis
Paths: $BMA_DATA_DIR (default ./data) is the root; --out defaults to <data-dir>/analysis and --panns-dir to
<model-dir>/panns_data, <model-dir> = $BMA_MODEL_DIR (default <data-dir>/models).
The Perch 2.0 shadow plugin (perch.py) runs only with --perch-model (tools/fetch_perch.py downloads the files) and needs the
tags plugin (its speech flag keeps speech windows out of perch.jsonl) and onnxruntime in <model-dir>/overlay.
"""
import argparse, json, os, signal, socket, struct, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import hist, manifest as M, wiring as W  # noqa: E402
MF = M.load()                                    # analysis/plugins.manifest.json: the one registry of plugins (docs/design/w3-plugin-manifest.md)
__doc__ = M.fill_docstring(__doc__, MF)          # the Outputs list above is generated from it
FS, NB = 48000, 8
DATA_DIR = os.environ.get("BMA_DATA_DIR") or "data"
MODEL_DIR = os.environ.get("BMA_MODEL_DIR") or os.path.join(DATA_DIR, "models")
RING_S = 12


class Ring:
    """One slot: contiguous samples [start, end) in absolute (unwrapped) sample indices, circular storage."""

    def __init__(self, cap):
        self.cap = cap
        self.buf = np.zeros(cap, np.float32)
        self.start = self.end = None
        self.wraps = 0
        self.last32 = None
        self.gaps = 0

    def _unwrap(self, b32):
        if self.end is None:
            return b32
        base = self.end - (self.end % (1 << 32))
        best = min((base + b32 + k * (1 << 32) for k in (-1, 0, 1)), key=lambda c: abs(c - self.end))
        return best

    def push(self, b32, x):
        blk = self._unwrap(b32)
        n = len(x)
        if self.end is not None and blk != self.end:
            self.gaps += 1
            self.start = self.end = None
        if self.end is None:
            self.start = self.end = blk
        i = blk % self.cap
        first = min(n, self.cap - i)
        self.buf[i:i + first] = x[:first]
        if first < n:
            self.buf[:n - first] = x[first:]
        self.end = blk + n
        if self.end - self.start > self.cap:
            self.start = self.end - self.cap

    def get(self, a, n):
        i = a % self.cap
        if i + n <= self.cap:
            return self.buf[i:i + n].copy()
        return np.concatenate([self.buf[i:], self.buf[:i + n - self.cap]])


MAX_BEAM_SHIFT = 4


def parse_beam_shift(text):
    """'1,0,0,0,0,0,0,-1' -> [1, 0, 0, 0, 0, 0, 0, -1]; exactly NB integers, each |k| <= MAX_BEAM_SHIFT, else
    argparse.ArgumentTypeError (argparse prints its message and exits 2)."""
    parts = [x.strip() for x in str(text).split(",")]
    try:
        v = [int(x) for x in parts]
    except ValueError:
        raise argparse.ArgumentTypeError(f"--beam-shift needs {NB} comma-separated integers, got {text!r}") from None
    if len(v) != NB:
        raise argparse.ArgumentTypeError(f"--beam-shift needs exactly {NB} comma-separated integers, got {len(v)} in {text!r}")
    if any(abs(k) > MAX_BEAM_SHIFT for k in v):
        raise argparse.ArgumentTypeError(f"--beam-shift values must satisfy |k| <= {MAX_BEAM_SHIFT}, got {text!r}")
    return v


def shift_margins(shifts):
    """-> (lead, tail): samples to cut from the start / end of the common region [lo, hi) so that every ring k can be read at
    [a + shifts[k], a + shifts[k] + n) for any window [a, a + n) inside [lo + lead, hi - tail) without leaving [lo, hi)."""
    return max(0, -min(shifts)), max(0, max(shifts))


def assemble_window(rings, a, n, shifts):
    """(n, NB) float32 window. Column k is ring k read at a + shifts[k] (k > 0 reads LATER content: it advances the beam)."""
    return np.stack([r.get(a + s, n) for r, s in zip(rings, shifts)], axis=1)


CAPS = {"dsp": 192 << 20}      # dsp.jsonl grows ~19 MB/day: 2 x 192 MB keeps ~19 days raw (others: 2 x 64 MB)


class Out:
    """Per-stream rotating JSONL under d (hist.RotatingJsonl: safe rotate, never raises, counted failures)."""

    def __init__(self, d, cap=64 << 20, caps=None):
        self.d, self.cap, self.caps, self.w = d, cap, CAPS if caps is None else caps, {}
        self.lock = threading.Lock()
        os.makedirs(d, exist_ok=True)

    def write(self, name, rec):
        with self.lock:
            w = self.w.get(name)
            if w is None:
                w = self.w[name] = hist.RotatingJsonl(os.path.join(self.d, name + ".jsonl"), self.caps.get(name, self.cap))
        w.write(rec)

    def counters(self):
        return (sum(w.write_errors for w in self.w.values()), sum(w.rotate_fallbacks for w in self.w.values()))


def feed_speech(plug, tag_plugin, recs, t_wall):
    """Hand one tagger verdict to every plugin in `plug` whose manifest entry declares consumes_speech (see wiring.feed_speech)."""
    W.feed_speech(W.consumers(MF, plug, "consumes_speech"), tag_plugin, recs, t_wall)


def pick_device():
    try:
        import torch
        if not torch.cuda.is_available():
            return "cpu"
        best, free = 0, -1
        for i in range(torch.cuda.device_count()):
            fr, _ = torch.cuda.mem_get_info(i)
            if fr > free:
                best, free = i, fr
        return f"cuda:{best}" if free > 1.5e9 else "cpu"
    except Exception:
        return "cpu"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--listen", default="0.0.0.0:48300")
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "analysis"), help="output directory (default <data-dir>/analysis)")
    ap.add_argument("--panns-dir", default=os.path.join(MODEL_DIR, "panns_data"), help="PANNs labels + weights (default <model-dir>/panns_data)")
    ap.add_argument("--classify-dir", default=HERE if os.path.exists(os.path.join(HERE, "bma_classify.py"))
                    else os.path.join(HERE, "..", "classify"), help="directory holding bma_classify.py")
    ap.add_argument("--device", default="auto")
    ap.add_argument("--no", action="append", default=[], help=M.no_help_text(MF))
    ap.add_argument("--secs", type=float, default=0.0, help="exit after this many seconds (tests)")
    ap.add_argument("--tonal-mask", help="JSON from learn_tonal_mask.py: the unit's own electronic lines; they stay in "
                    "tonal_state flagged electronic but produce no tonal_appeared/tonal_gone (default: no mask)")
    ap.add_argument("--taxa", help="taxa.json (ui/make_taxa.py) for the group stamp; default taxa.json next to host.py")
    ap.add_argument("--history-dir", help="rollups, config snapshots, daily summary (default: ../history next to --out; '' disables)")
    ap.add_argument("--calibration", help="dspd-shaped calibration JSON to snapshot (default calibration.json next to host.py)")
    ap.add_argument("--geo-config", help='county-level location/date species filter for the birds plugin: JSON {"lat","lon"} (default: off)')
    M.add_flags(ap, MF, "geo")                       # mixscore, mixscore-every (manifest cli_slot geo)
    ap.add_argument("--tonal-mask-tol", type=float, default=None, help="Hz tolerance around each mask line (default: the file's, 4.0)")
    try:
        import clips as CL                           # wildlife clips are optional: a copy without clips.py runs exactly as before
        CL.add_args(ap)
    except Exception as e:
        CL = None
        print(f"WARN: clips unavailable (module not importable): {type(e).__name__}: {e}", file=sys.stderr, flush=True)
    M.add_flags(ap, MF, "plugins")                   # coherence, perch, bursts, indices (manifest cli_slot plugins)
    ap.add_argument("--beam-shift", type=parse_beam_shift, default=[0] * NB, metavar="K1,...,K8",
                    help="exact integer per-beam sample-shift correction, 8 comma-separated integers, |k| <= 4 (default all 0 = "
                    "no change). Shift k means the window for that beam is read k samples LATER in the ring (k=+1 advances the "
                    "content by one sample: use it for a beam that arrives one sample late; k=-1 delays a beam that arrives early)")
    M.add_flags(ap, MF, "tail")                      # the flags of plugins added after the manifest (cli_slot tail, the default)
    a = ap.parse_args()

    sys.path.insert(0, os.path.abspath(a.classify_dir))
    from bma_classify import _parse_datagram  # same BMAA parser the classifier uses
    import plugins as P

    import taxa as T, runmeta, rollup, daily, analyzer_history
    failed = M.load_modules(MF)                       # a required plugin module that cannot be imported stops the host; an optional one is reported and its plugin disabled
    for f in failed.values():
        print(f["warn"], file=sys.stderr, flush=True)
    out = Out(a.out)
    taxa = T.Taxa(a.taxa)
    hdir = a.history_dir if a.history_dir is not None else os.path.join(os.path.dirname(os.path.abspath(a.out)), "history")
    run_id = int(time.time())
    hroll = hdaily = hcfg = hanalyzer = None
    if hdir:
        try:
            hanalyzer = analyzer_history.AnalyzerHistory(hdir)       # analyzer_sample every 30 s (run-history)
            hroll, hdaily = rollup.HourlyRollup(hdir), daily.DailySummary(os.path.join(hdir, "daily_summary.json"))
        except Exception as e:                      # history is best effort: the analyzer runs without it
            print(f"WARN: history disabled: {e}", file=sys.stderr, flush=True)
            hroll = hdaily = None
    dev = pick_device() if a.device == "auto" else a.device
    status = {"started": time.time(), "device": dev, "plugins": {}, "rx": {"datagrams": 0, "bad": 0, "gaps": 0}}
    plug = []
    started, shared = set(), {}                       # started: names of the plugins running so far (make(ctx) reads it); shared: explicit hand-offs between plugins

    def add(name, factory):
        if name in a.no:
            status["plugins"][name] = {"enabled": False, "reason": "disabled by --no"}
            return
        try:
            p = factory()
            plug.append(p)
            started.add(name)
            status["plugins"][name] = {"enabled": True, "windows": 0, "records": 0, "errors": 0, "dropped": 0,
                                       "last_t": None, "last_error": None}
        except Exception as e:  # missing weights, no memory, import error: say so, keep the rest running
            status["plugins"][name] = {"enabled": False, "reason": f"{type(e).__name__}: {e}"[:300]}
            print(f"WARN: plugin {name} not started: {e}", file=sys.stderr, flush=True)

    ctx = M.Ctx(a, taxa, dev, run_id, a.classify_dir, a.out, started, shared, plug)

    def start(entry):
        if entry["name"] in failed:
            raise ImportError(failed[entry["name"]]["reason"])
        return M.build(entry, ctx)

    for entry in MF["plugins"]:                       # registration order = status.json order = process order
        reason = M.gate_closed(entry, a, a.no)
        if reason:
            if not M.gate_silent(entry):
                status["plugins"][entry["name"]] = {"enabled": False, "reason": reason}
        else:
            add(entry["name"], lambda e=entry: start(e))
    store = P.EmbeddingStore(a.out)
    try:
        import sklearn
        mv = f"km12-sk{sklearn.__version__}"
    except Exception:
        mv = "km12"
    novelty = P.Novelty(os.path.join(a.out, "novelty_state.pkl"), run=run_id, mv=mv)
    if hdir:
        calib = a.calibration or os.path.join(HERE, "calibration.json")
        try:
            from bma_classify import load_geo_config
        except Exception:
            load_geo_config = None
        hcfg = runmeta.ConfigWatcher(hdir, lambda: runmeta.build_config(
            HERE, a.tonal_mask, a.tonal_mask_tol, calib, a.geo_config, load_geo_config,
            a.panns_dir, dev, 12, novelty.mv, a.beam_shift), run_id)
        hcfg.poll()
    status["plugins"]["novelty"] = {"enabled": True, "note": "fed by tags embeddings (non-speech only)"}

    rings = [Ring(RING_S * FS) for _ in range(NB)]
    lock = threading.Lock()
    host, _, port = a.listen.rpartition(":")
    clips = None
    if CL is not None and "clips" not in a.no:       # wildlife clip capture (clips.py); any failure to start leaves the analyzer unchanged
        try:
            ring_reader = CL.make_ring_reader(rings, lock)
            bn = getattr(plug[[p.name for p in plug].index("birds")], "bn", None) if "birds" in [p.name for p in plug] else None
            clips = CL.ClipService(a.clips_dir or os.path.join(a.out, "clips"),
                                   CL.config_from_args(a, a.calibration or os.environ.get("BMA_CALIBRATION_FILE") or os.path.join(HERE, "calibration.json")),
                                   ring_reader, status["plugins"].get("tags", {}).get("enabled", False), getattr(bn, "version", None))
            clips.start()
        except Exception as e:
            clips = None
            print(f"WARN: clips disabled: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    sock.bind((host or "0.0.0.0", int(port)))
    sock.settimeout(0.5)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    def rx():
        while not stop.is_set():
            try:
                pkt, _ = sock.recvfrom(4096)
            except socket.timeout:
                continue
            d = _parse_datagram(pkt)
            if d is None or d["slot"] >= NB:
                status["rx"]["bad"] += 1
                continue
            with lock:
                rings[d["slot"]].push(d["block"], d["data"])
            status["rx"]["datagrams"] += 1

    threading.Thread(target=rx, daemon=True).start()
    pool = ThreadPoolExecutor(max_workers=max(3, sum(1 for p in plug if p.heavy)))      # one worker per heavy plugin: none waits for another
    status["pool_workers"] = pool._max_workers
    nxt = {}
    busy = {}
    wiring = W.Wiring(MF, plug, out=out, status=status, clips=clips, store=store, novelty=novelty, shared=shared)

    def run_plugin(p, x, t_wall):
        st = status["plugins"][p.name]
        try:
            recs, emb = p.process(x, t_wall)
            for r in recs:
                out.write(p.name, r)
                if hroll is not None:
                    hroll.feed(r)
            if hdaily is not None:
                hdaily.on_window(p.name, recs, t_wall, wiring.window_info(p))
            st["records"] += len(recs)
            st["windows"] += 1
            st["last_t"] = t_wall
            wiring.after_process(p, recs, emb, x, t_wall, st)       # the declared hand-offs between plugins (wiring.py)
        except Exception as e:
            st["errors"] += 1
            if hdaily is not None:
                hdaily.on_error(p.name, t_wall)
            st["last_error"] = f"{type(e).__name__}: {e}"[:300]
            print(f"WARN: plugin {p.name}: {e}", file=sys.stderr, flush=True)
        finally:
            busy[p.name] = False

    last_status = last_hist = 0.0

    def meta_counters():
        we, rf = out.counters()
        for w in (hroll.out if hroll else None, hcfg.out if hcfg else None, hanalyzer.out if hanalyzer else None):
            if w is not None:
                we, rf = we + w.write_errors, rf + w.rotate_fallbacks
        return {"write_errors": we, "rotate_fallbacks": rf, "taxa_missing": taxa.missing, "taxa_unmapped": taxa.unmapped,
                "config_write_errors": hcfg.errors if hcfg else 0, "rollup_errors": hroll.errors if hroll else 0,
                "daily_flush_errors": hdaily.errors if hdaily else 0, "history_dropped": hroll.late if hroll else 0,
                **(clips.counters() if clips else {}),
                **plugin_counters()}

    running = {p.name: p for p in plug}

    def plugin_counters():
        """meta.<plugin>_errors of the running plugins (manifest meta_counters); an opt-in plugin's counter exists only while it runs."""
        res = {}
        # always-present counters first, the opt-in ones after them: the key order of meta in status.json is the one master wrote
        for name, p, keys, only_running in sorted(M.meta_counters(MF, running), key=lambda r: r[3]):
            if p is None and only_running:
                continue
            res[name] = sum(p.stats()[k] for k in keys) if p is not None else 0
        return res

    def hist_tick(now):
        for fn in (lambda: hroll.tick(now) if hroll else None, lambda: hdaily.flush() if hdaily else None,
                   lambda: hcfg.poll() if hcfg else None):
            try:
                fn()
            except Exception as e:                  # history never takes the analyzer down
                print(f"WARN: history tick: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
    t0 = time.time()
    while not stop.is_set():
        time.sleep(0.25)
        now = time.time()
        if a.secs and now - t0 > a.secs:
            break
        if clips is not None:
            clips.tick(now)
        with lock:
            if any(r.end is None for r in rings):
                common = None
            else:
                lo, hi = max(r.start for r in rings), min(r.end for r in rings)
                common = (lo, hi) if hi > lo else None
        if common:
            lo, hi = common
            lead, tail = shift_margins(a.beam_shift)             # no ring is ever read outside [start, end)
            lo, hi = lo + lead, hi - tail
            for p in plug:
                n, hop = int(p.win_s * FS), int(p.hop_s * FS)
                if p.name not in nxt or nxt[p.name] < lo:
                    nxt[p.name] = max(lo, (hi - n) // hop * hop) if hi - lo >= n else None
                    if nxt[p.name] is None:
                        del nxt[p.name]
                        continue
                if hi - nxt[p.name] > 4 * hop + n:          # fell behind: skip ahead, count it
                    skip = (hi - n) // hop * hop
                    nd = max(1, (skip - nxt[p.name]) // hop)
                    status["plugins"][p.name]["dropped"] += nd
                    if hdaily is not None:
                        hdaily.on_dropped(p.name, nd, now)
                    nxt[p.name] = skip
                if nxt[p.name] + n <= hi and not busy.get(p.name):
                    with lock:
                        x = assemble_window(rings, nxt[p.name], n, a.beam_shift)
                    t_wall = now - (hi - (nxt[p.name] + n)) / FS
                    if clips is not None:
                        clips.note_window(p.name, t_wall, nxt[p.name], n)
                    nxt[p.name] += hop
                    if p.heavy:
                        busy[p.name] = True
                        pool.submit(run_plugin, p, x, t_wall)
                    else:
                        run_plugin(p, x, t_wall)
        if now - last_status >= 5:
            last_status = now
            status["t"] = now
            for p in wiring.cost_plugins:                    # plugins that declared hooks.cost (they measure their own CPU: mixscore) publish it in their entry
                status["plugins"][p.name].update(p.cost())
            status["uptime_s"] = round(now - status["started"], 0)
            status["rx"]["gaps"] = sum(r.gaps for r in rings)
            status["contiguous_s"] = round((common[1] - common[0]) / FS, 1) if common else 0.0
            status["embeddings_stored"] = store.n
            status["audio_written"] = bool(clips.audio_written()) if clips else False
            if clips:
                status["clips"] = clips.status_block()
            status["meta"] = meta_counters()
            if hdaily is not None:
                hdaily.on_status(now, status["rx"], status["contiguous_s"], 5.0)
            if hanalyzer is not None:
                hanalyzer.tick(status, now)
            if now - last_hist >= 60:
                last_hist = now
                hist_tick(now)
            try:
                tmp = os.path.join(a.out, "status.json.tmp")
                with open(tmp, "w") as f:
                    json.dump(status, f)
                os.replace(tmp, os.path.join(a.out, "status.json"))
            except OSError:
                pass
    if clips is not None:
        clips.stop()
    for p in wiring.flush_plugins:                           # plugins holding an open aggregation bucket (hooks.flush) emit it now
        try:
            for r in p.flush():
                out.write(p.name, r)
                if hroll is not None:
                    hroll.feed(r)
        except Exception as e:
            print(f"WARN: plugin {p.name} flush: {e}", file=sys.stderr, flush=True)
    try:                                                     # the flush closes intervals: publish the final meta counters (untagged windows, errors)
        status["meta"] = meta_counters()
        tmp = os.path.join(a.out, "status.json.tmp")
        with open(tmp, "w") as f:
            json.dump(status, f)
        os.replace(tmp, os.path.join(a.out, "status.json"))
    except Exception as e:
        print(f"WARN: final status: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
    novelty.save()
    hist_tick(time.time())
    if hroll is not None:
        hroll.save()
    pool.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()
