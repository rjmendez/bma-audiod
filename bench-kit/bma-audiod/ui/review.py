#!/usr/bin/env python3
"""Label review for the dashboard (ui-http 1.9.0, contract verdict-labels 1.0.0).  Stdlib only.

Reads birds / tags / clips metadata, builds the review queue with analysis/review_queue.py, appends verdicts through
analysis/labels.py (the same library as tools/bma_labels.py).  One writer lock inside this process; the label file lock
of the library covers other processes.  Evidence = a stored clip (PNG and/or audio) overlapping the event; the queue serves only events
with evidence unless allow_no_clip.  No free text."""
import json, os, re, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
for _d in (HERE, os.path.join(HERE, "..", "analysis")):          # flat app dir (install copies labels.py, review_queue.py next to this file) or the repo layout
    sys.path.insert(0, _d)
import labels as L          # noqa: E402
import review_queue as Q    # noqa: E402

CLIP_SLACK_S = 0.05                                               # clip times are ms-rounded, events are not: overlap is judged with this slack on both sides
TARGET = 20                                                       # labels per species
NO_CAP = 10 ** 6
EVIDENCE_FIRST_PARAMS = {"limit": NO_CAP, "per_species_cap": NO_CAP, "audit_per_species_cap": NO_CAP}   # evidence-only mode: every eligible event is a candidate, the clip filter selects
CACHE_S = 30.0                                                   # queue cache; any write drops it
DEDUP_S = 30.0                                                    # same event + verdict within this: one record
MAX_SKIPS = 5000
MAX_PNG_BYTES = 1 << 20
MAX_AUDIO_BYTES = 16 << 20                                        # larger audio file: not served
MAX_RANGE_BYTES = 4 << 20                                         # one 206 answer carries at most this
AUDIO_TYPES = {".flac": "audio/flac", ".wav": "audio/wav"}
RANGE_RE = re.compile(r"bytes=([0-9]{1,15})?-([0-9]{1,15})?")
CLIP_ID_RE = re.compile(r"[0-9]{13}-[a-z0-9]{1,24}-b[1-8]")
REASONS = {"near": "near", "disagree": "disagree", "low": "low"}   # stratum -> reason code; audit picks are "audit"


class ReviewError(Exception):
    def __init__(self, status, code, title, detail, param=None):
        super().__init__(detail)
        self.status, self.code, self.title, self.detail, self.param = status, code, title, detail, param
        self.extra = None                                             # extra response headers (416 Content-Range)


def iso_z(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


class Review:
    def __init__(self, root, labels_dir=None, taxa_path=None, clock=time.time, clips_dir=None, allow_no_clip=False):
        self.root, self.clock = root, clock
        self.allow_no_clip = bool(allow_no_clip)
        self.store = L.LabelStore(labels_dir or os.path.join(root, "labels"))
        self.taxa = L.Taxa(taxa_path)
        self.clips_dir = clips_dir or os.path.join(root, "analysis", "clips")
        self.lock = threading.RLock()                                     # the single writer inside this process; also guards the cache
        self.skipped = []
        self.cache = None
        self.clip_cache = None

    # ---- loading
    def _data_path(self, name):
        return os.path.join(self.root, "analysis", name)

    def _load(self):
        now = self.clock()
        with self.lock:
            c = self.cache
            if c is not None and now - c["t"] < CACHE_S:
                return c
            try:
                recs, _bad, _part = self.store.read()
                rows = L.read_rotated(self._data_path("birds.jsonl"))
                trows = L.read_rotated(self._data_path("tags.jsonl"))
            except OSError as ex:
                raise ReviewError(503, "labels_unavailable", "Label store unavailable", "label store unavailable: %s" % type(ex).__name__)
            events = L.iter_events(rows, self.taxa)
            tags = L.Tags(trows)
            folded = L.fold(recs)
            queue = Q.build_queue(events, folded, tags, {} if self.allow_no_clip else EVIDENCE_FIRST_PARAMS, now, taxa_loaded=self.taxa.loaded)
            art = [(L.species_key(o), o["t"], o["artifact_flags"]) for o in rows
                   if L.valid_bird(o) and isinstance(o.get("artifact_flags"), dict) and isinstance(o["artifact_flags"].get("flags"), list)]
            self.cache = {"t": now, "events": {e.event_id: e for e in events}, "tags": tags, "folded": folded, "queue": queue,
                          "items": {i["event_id"]: i for i in queue["items"]}, "art": art, "recs": recs}
            return self.cache

    def _drop(self):
        with self.lock:
            self.cache = None

    # ---- clips
    def _clip_index(self):
        """-> [clip record] from clips/index.jsonl folded by id (clip_stored creates, clip_evicted / clip_sidecar_expired remove)."""
        p = os.path.join(self.clips_dir, "index.jsonl")
        try:
            st = os.stat(p)
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            return []
        if self.clip_cache and self.clip_cache[0] == key:
            return self.clip_cache[1]
        table = {}
        for r in L.read_rotated(p):
            if r.get("k") == "clip_stored" and isinstance(r.get("id"), str) and (isinstance(r.get("png"), str) or isinstance(r.get("audio_file"), str)):
                table[r["id"]] = r
            elif r.get("k") in ("clip_evicted", "clip_sidecar_expired"):
                table.pop(r.get("id"), None)
        out = [r for r in table.values() if L.is_num(r.get("t_start")) and L.is_num(r.get("t_end")) and isinstance(r.get("species"), dict)]
        self.clip_cache = (key, out)
        return out

    def clip_for(self, ev):
        """-> {id, png, audio, t_start, t_end} of the best stored clip overlapping the event that still has a PNG and/or an audio file, else None.
        Audio beats PNG-only; then the higher confidence."""
        best, best_key = None, None
        for r in self._clip_index():
            sp = r["species"]
            key = (sp.get("scientific") or sp.get("common") or "").strip().lower()
            if key != ev.key or not (r["t_start"] <= ev.t_end + CLIP_SLACK_S and r["t_end"] >= ev.t_start - CLIP_SLACK_S) or not CLIP_ID_RE.fullmatch(r["id"]):
                continue
            png, audio = self.clip_path(r["id"]) is not None, self.audio_path(r["id"]) is not None
            if not (png or audio):
                continue
            k = (audio, r.get("confidence_max") or 0, r["id"])
            if best_key is None or k > best_key:
                best_key, best = k, {"id": r["id"], "png": png, "audio": audio, "t_start": r["t_start"], "t_end": r["t_end"]}
        return best

    def clip_id_for(self, ev):
        c = self.clip_for(ev)
        return c["id"] if c and c["png"] else None

    def _clip_file(self, clip_id, ext):
        if not isinstance(clip_id, str) or not CLIP_ID_RE.fullmatch(clip_id):
            return None
        day = time.strftime("%Y%m%d", time.gmtime(int(clip_id[:13]) / 1000.0))
        p = os.path.join(self.clips_dir, day, clip_id + ext)
        return p if os.path.isfile(p) else None

    def clip_path(self, clip_id):
        """analysis/clips/<day>/<id>.png for a strictly validated id, else None.  The day is derived from the id, never taken from the client."""
        return self._clip_file(clip_id, ".png")

    def audio_path(self, clip_id):
        """analysis/clips/<day>/<id>.flac|.wav for a strictly validated id while the file exists, else None."""
        for ext in AUDIO_TYPES:
            p = self._clip_file(clip_id, ext)
            if p is not None:
                return p
        return None

    def clip_png(self, clip_id):
        p = self.clip_path(clip_id)
        if p is None:
            raise ReviewError(404, "not_found", "Not found", "no such clip")
        try:
            with open(p, "rb") as f:
                data = f.read(MAX_PNG_BYTES + 1)
        except OSError:
            raise ReviewError(404, "not_found", "Not found", "no such clip")
        if len(data) > MAX_PNG_BYTES or not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ReviewError(404, "not_found", "Not found", "no such clip")
        return data

    def clip_audio(self, clip_id, range_header=None):
        """-> (status 200|206, content type, body, extra headers).  Single byte range, at most MAX_RANGE_BYTES per answer; a malformed Range
        is ignored (whole file); unsatisfiable = 416 with Content-Range: bytes */size.  404 when the audio is absent / expired / too large."""
        p = self.audio_path(clip_id)
        if p is None:
            raise ReviewError(404, "not_found", "Not found", "no such clip")
        try:
            size = os.path.getsize(p)
            if not 0 < size <= MAX_AUDIO_BYTES:
                raise ReviewError(404, "not_found", "Not found", "no such clip")
            ctype = AUDIO_TYPES[os.path.splitext(p)[1]]
            m = RANGE_RE.fullmatch(range_header) if isinstance(range_header, str) else None
            lo = hi = None
            if m and (m.group(1) or m.group(2)):
                a, b = m.group(1), m.group(2)
                if a is None:                                         # suffix: the last n bytes
                    n = int(b)
                    lo, hi = (size if n == 0 else max(0, size - n)), size - 1
                else:
                    lo, hi = int(a), (int(b) if b is not None else size - 1)
                    if b is not None and hi < lo:
                        lo = hi = None                                # invalid range: ignored
                if lo is not None:
                    if lo >= size:
                        e = ReviewError(416, "range_not_satisfiable", "Range not satisfiable", "range outside the clip")
                        e.extra = {"Content-Range": "bytes */%d" % size}
                        raise e
                    hi = min(hi, size - 1, lo + MAX_RANGE_BYTES - 1)
            with open(p, "rb") as f:
                if lo is None:
                    return 200, ctype, f.read(), {"Accept-Ranges": "bytes"}
                f.seek(lo)
                data = f.read(hi - lo + 1)
            return 206, ctype, data, {"Accept-Ranges": "bytes", "Content-Range": "bytes %d-%d/%d" % (lo, lo + len(data) - 1, size)}
        except OSError:
            raise ReviewError(404, "not_found", "Not found", "no such clip")

    # ---- items
    def _item_json(self, c, qitem):
        ev = c["events"][qitem["event_id"]]
        out = {"event_id": ev.event_id, "species": {"common": ev.common, "scientific": ev.scientific}, "confidence": round(ev.max_conf, 4),
               "beams": sorted(ev.beams), "best_beam": ev.best_beam, "t_start_s": round(ev.t_start, 3), "t_utc": iso_z(ev.t_start),
               "in_region": ev.in_region, "group": ev.producer_group or ev.taxa_group, "non_species": bool(ev.non_species),
               "reason": "audit" if qitem["selection"] == "audit" else REASONS.get(qitem["stratum"], qitem["stratum"])}
        fl = sorted({f for k, t, a in c["art"] if k == ev.key and ev.t_start <= t <= ev.t_end for f in a["flags"] if isinstance(f, str)})
        arts = [a for k, t, a in c["art"] if k == ev.key and ev.t_start <= t <= ev.t_end]
        if arts:
            out["artifact_flags"] = {"flags": fl, "level": max((a.get("level") for a in arts if a.get("level") in ("none", "suspect", "likely")),
                                                              key=("none", "suspect", "likely").index, default="none")}
        cl = self.clip_for(ev)
        if cl:
            if cl["png"]:
                out["clip_png"] = "/api/review/clip?id=" + cl["id"]
            if cl["audio"]:
                out["clip_audio"] = "/api/review/audio?id=" + cl["id"]
            out["clip_span"] = {"clip_start_s": round(cl["t_start"], 3), "clip_end_s": round(cl["t_end"], 3),
                                "event_start_s": round(ev.t_start, 3), "event_end_s": round(ev.t_end, 3)}
        return out

    @staticmethod
    def _labelled(c, qitem):
        """Joined on event id and, when both carry one, the model digest (labels.label_for)."""
        return L.label_for(c["folded"], c["events"][qitem["event_id"]]) is not None

    def _todo(self, c):
        """-> (items to serve, count of candidates without evidence).  Candidates = queue items not skipped and not labelled.  Without
        allow_no_clip only items with evidence are served; audio first (stable within the queue order)."""
        skip = set(self.skipped)
        cand = [i for i in c["queue"]["items"] if i["event_id"] not in skip and not self._labelled(c, i)]
        rows, n_none = [], 0
        for i in cand:
            cl = self.clip_for(c["events"][i["event_id"]])
            n_none += cl is None
            if cl or self.allow_no_clip:
                rows.append((not (cl and cl["audio"]), i))
        rows.sort(key=lambda x: x[0])                                 # stable: audio first, the queue order otherwise
        return [i for _a, i in rows], n_none

    def _next(self, c):
        todo, _none = self._todo(c)
        return (self._item_json(c, todo[0]) if todo else None), len(todo)

    def next(self):
        with self.lock:
            c = self._load()
            item, depth = self._next(c)
            return {"item": item, "queue_depth": depth}

    def stats(self):
        with self.lock:
            c = self._load()
            todo, no_clip = self._todo(c)
            depth = len(todo)
            verdicts = {v: 0 for v in L.VERDICTS}
            per = {}
            joined = []
            for e, r in c["folded"].items():
                ev = c["events"].get(e)
                r = L.label_for(c["folded"], ev) if ev is not None else r
                if r is not None:
                    joined.append(r)
            for r in joined:
                if r.get("verdict") in verdicts:
                    verdicts[r["verdict"]] += 1
                k = r.get("species_key")
                if isinstance(k, str):
                    per[k] = per.get(k, 0) + 1
            species = [{"species_key": k, "labels": n} for k, n in sorted(per.items(), key=lambda kv: (-kv[1], kv[0]))]
            return {"labelled": len(joined), "verdicts": verdicts, "target": TARGET,
                    "species_at_target": sum(1 for n in per.values() if n >= TARGET), "queue_depth": depth, "no_clip": no_clip, "species": species}

    # ---- writes
    def verdict(self, event_id, verdict, wrong_reason=None):
        if not isinstance(event_id, str) or not L.EVENT_ID_RE.match(event_id):
            raise ReviewError(422, "invalid_parameter", "Invalid parameter", "event_id: expected an id such as 1792940400000-common-virtuoso-katydid", "event_id")
        if not isinstance(verdict, str) or verdict not in L.VERDICTS:
            raise ReviewError(422, "invalid_parameter", "Invalid parameter", "verdict: must be one of %s" % ", ".join(L.VERDICTS), "verdict")
        if wrong_reason is not None and (not isinstance(wrong_reason, str) or wrong_reason not in L.WRONG_REASONS):
            raise ReviewError(422, "invalid_parameter", "Invalid parameter", "wrong_reason: must be one of %s" % ", ".join(L.WRONG_REASONS), "wrong_reason")
        with self.lock:
            c = self._load()
            ev = c["events"].get(event_id)
            if ev is None or L.speech_reason(ev, c["tags"]) is not None:
                raise ReviewError(422, "not_labelable", "Not labelable", L.NOT_LABELABLE, "event_id")
            now = self.clock()
            try:
                recs, _b, _p = self.store.read()
            except OSError as ex:
                raise ReviewError(503, "labels_unavailable", "Label store unavailable", "label store unavailable: %s" % type(ex).__name__)
            last = L.label_for(L.fold(recs), ev)
            if (last is not None and last.get("verdict") == verdict and last.get("wrong_reason") == wrong_reason
                    and 0 <= now - last["t_label"] < DEDUP_S):
                label_id, recorded = last["label_id"], False
            else:
                item = c["items"].get(event_id)
                rec_item = {"stratum": item["stratum"], "p_include": item["p_include"], "queue_id": c["queue"]["queue_id"]} if item else None
                try:
                    rec = L.make_record(ev, verdict, "clip_visual" if self.clip_id_for(ev) else "metadata_only", wrong_reason=wrong_reason,
                                        source="ui", item=rec_item, t_label=now)
                    self.store.append(rec)
                except L.LabelError as ex:
                    msg = str(ex)
                    if "busy" in msg:
                        raise ReviewError(503, "labels_unavailable", "Label store unavailable", "label store unavailable: busy")
                    raise ReviewError(422, "invalid_parameter", "Invalid parameter", msg, msg.split(":")[0])
                except OSError as ex:
                    raise ReviewError(503, "labels_unavailable", "Label store unavailable", "label store unavailable: %s" % type(ex).__name__)
                label_id, recorded = rec["label_id"], True
                self._drop()
            out = self.next()
            return dict(out, recorded=recorded, label_id=label_id)

    def skip(self, event_id):
        if not isinstance(event_id, str) or not L.EVENT_ID_RE.match(event_id):
            raise ReviewError(422, "invalid_parameter", "Invalid parameter", "event_id: expected an id such as 1792940400000-common-virtuoso-katydid", "event_id")
        with self.lock:
            if event_id not in self.skipped:
                self.skipped.append(event_id)
                del self.skipped[:-MAX_SKIPS]
            out = self.next()
            return dict(out, recorded=False, label_id=None)
