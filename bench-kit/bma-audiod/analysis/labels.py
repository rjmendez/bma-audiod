#!/usr/bin/env python3
"""Verdict label store (contract verdict-labels 1.1.0): event identity, event clustering, the speech guard and the append-only
owner-verdict file `labels/labels.jsonl`.  Library only; the CLI is tools/bma_labels.py, the sampler analysis/review_queue.py.

Reads JSONL metadata only (birds, tags).  NEVER reads audio, embeddings (emb_*) or novelty, never writes under analysis/ or history/,
never touches the network.  stdlib only.

Event clustering is a port of the dashboard's `DetStore.add` (ui/bma_ui.py): ALL valid `bird` and `bird_filtered` rows, in file order,
merged per species key while `t_start - EVENT_GAP_S <= t <= t_end + EVENT_GAP_S`; the eligibility filters are applied to the event
afterwards.  analysis/test_labels.py runs both implementations over the same rows and compares the id sets."""
import bisect, json, os, re, secrets, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import events as E  # noqa: E402

EVENT_GAP_S = 3.0                                    # = ui/bma_ui.py EVENT_GAP_S (parity-tested)
VERDICTS = ("correct", "wrong", "unsure", "artifact")
WRONG_REASONS = ("other_species", "non_bird_sound", "mechanical", "insect_or_amphibian", "no_sound_seen")
BASES = ("field", "clip_visual", "metadata_only")
SOURCES = ("cli", "ui")
STRATA = ("disagree", "near", "low", "hi", "artifact_probe")
SELECTIONS = ("uncertainty", "probe", "audit")
PROVENANCE = "owner_verdict"
# Speech guard.  Labels of the analyzer's `tags` plugin (a copy of analysis/plugins.py SPEECH_LABELS, pinned by test_labels.py)
# plus every label that starts with a prefix of HUMAN_LABEL_PREFIXES ("Human vocal", "Human non-vocal", "Human whistle" ...).
SPEECH_LABELS = frozenset({"Speech", "Male speech, man speaking", "Female speech, woman speaking", "Child speech, kid speaking",
                           "Conversation", "Narration, monologue", "Babbling", "Speech synthesizer", "Shout", "Yell",
                           "Whispering", "Chatter", "Hubbub, speech noise, speech babble", "Singing"})
HUMAN_LABEL_PREFIXES = ("human",)
SPEECH_MARGIN_S = 5.0
SPEECH_P_MIN = 0.15                                  # half the tagger's own cut (plugins.py Tagger.SPEECH_P = 0.30)
NOT_LABELABLE = "not_labelable"                      # the ONE refusal code of `add`: speech, untagged and unknown events look the same
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
EVENT_ID_RE = re.compile(r"^[0-9]{1,16}-[a-z0-9-]{0,60}$")
SPECIES_KEY_RE = re.compile(r"^[a-z0-9 .-]{1,60}$")
FORBIDDEN_KEY_PREFIXES = ("lat", "lon", "audio", "path", "note", "text", "url", "clip")
RECORD_KEYS = frozenset({"k", "v", "label_id", "t_label", "event_id", "t_start_ms", "species_key", "verdict", "wrong_reason",
                         "corrected_key", "basis", "source", "stratum", "p_include", "queue_id", "model", "provenance", "model_sha256"})
MODEL_KEYS = frozenset({"conf_max", "n_beams", "best_beam", "group", "in_region"})


class LabelError(ValueError):
    """A record or request that violates the contract; the message names the field, never an event's label."""


# ------------------------------------------------------------------------------------------------ reading
def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and x not in (float("inf"), float("-inf"))


def read_jsonl(path):
    """-> (records, bad_lines, partial).  Whole lines only; a trailing line without a newline is `partial` (skipped, counted);
    a line that is not a JSON object is a bad line (skipped, counted).  A missing file is ([], 0, 0); an unreadable one raises OSError."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return [], 0, 0
    parts = data.split(b"\n")
    tail = parts.pop()
    partial = 1 if tail.strip() else 0
    recs, bad = [], 0
    for ln in parts:
        if not ln.strip():
            continue
        try:
            o = json.loads(ln.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            bad += 1
            continue
        if isinstance(o, dict):
            recs.append(o)
        else:
            bad += 1
    return recs, bad, partial


def read_rotated(path):
    """The rotated generation first (`path.1`, older), then `path`: file order = arrival order."""
    out = []
    for p in (path + ".1", path):
        recs, _bad, _part = read_jsonl(p)
        out += recs
    return out


# ------------------------------------------------------------------------------------------------ events
def valid_bird(o):
    """= ui/bma_ui.py valid_bird (parity-tested)."""
    return (isinstance(o, dict) and o.get("k") in ("bird", "bird_filtered") and is_num(o.get("t")) and o["t"] > 0
            and isinstance(o.get("beam"), int) and not isinstance(o.get("beam"), bool) and 1 <= o["beam"] <= 8
            and is_num(o.get("confidence")) and 0 <= o["confidence"] <= 1
            and isinstance(o.get("common"), str) and o["common"] != "")


def species_key(o):
    return (o.get("scientific") or o["common"]).strip().lower()


def make_event_id(t_start, key):
    return "%d-%s" % (int(t_start * 1000), re.sub(r"[^a-z0-9]+", "-", key)[:60])


class Taxa:
    """Insect / mechanical classification from ui/taxa.json (the dashboard's table; `--taxa`).  A missing or malformed file is an EMPTY table
    with `error` set: nothing is classified, nothing crashes.  Mirrors ui/bma_ui.py Taxa.classify (parity-tested)."""
    def __init__(self, path=None):
        self.explicit, self.genus, self.error = {}, {}, None
        if path is None:
            return
        try:
            with open(path, "rb") as f:
                d = json.loads(f.read())
            if not isinstance(d, dict) or d.get("version") != 1 or not isinstance(d.get("genera"), dict) or not isinstance(d.get("explicit_labels"), dict):
                raise ValueError("unexpected structure")
            genus, explicit = {}, {}
            for grp, names in d["genera"].items():
                for n in names:
                    genus[n.lower()] = grp
            for lab, e in d["explicit_labels"].items():
                explicit[lab.lower()] = (e["group"], bool(e["non_species"]))
            self.explicit, self.genus = explicit, genus
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as ex:
            self.error = "taxa unusable (%s)" % type(ex).__name__

    @property
    def loaded(self):
        return bool(self.explicit or self.genus)

    def classify(self, scientific, common, flag):
        for lab in (common, scientific):
            hit = self.explicit.get((lab or "").strip().lower())
            if hit:
                return hit[0], hit[1]
        parts = (scientific or "").split()
        if len(parts) >= 2 and parts[0].lower() in self.genus:
            return self.genus[parts[0].lower()], False
        return "other-bio", bool(flag)


class Event:
    __slots__ = ("key", "common", "scientific", "t_start", "t_end", "best_beam", "best_conf", "beams", "reports", "max_conf",
                 "has_true", "has_false", "producer_group", "taxa_group", "non_species", "event_id", "content_id", "shas")

    def __init__(self, key, rep, taxa):
        self.key, self.common, self.scientific = key, rep["common"], rep.get("scientific") or ""
        self.taxa_group, self.non_species = taxa.classify(self.scientific, self.common, rep.get("non_species"))
        self.t_start = self.t_end = rep["t"]
        self.best_beam, self.best_conf = rep["beam"], rep["confidence"]
        self.beams = {rep["beam"]}
        self.reports, self.max_conf = 1, rep["confidence"]
        self.has_true = rep.get("in_region") is True
        self.has_false = rep.get("in_region") is False
        g = rep.get("group")
        self.producer_group = g if isinstance(g, str) and g else None       # null on most live rows: the schema allows null
        self.event_id = self.content_id = None
        self.shas = set()
        self._sha(rep)

    def _sha(self, rep):
        m = rep.get("model")
        d = m.get("sha256") if isinstance(m, dict) else None
        if isinstance(d, str) and SHA256_RE.match(d):
            self.shas.add(d)

    @property
    def model_sha256(self):
        """The one model digest of every report of the event (analysis-records 1.3.0 `model.sha256`); None when no report carries one or they differ."""
        return next(iter(self.shas)) if len(self.shas) == 1 else None

    def merge(self, rep, taxa):
        self.t_start, self.t_end = min(self.t_start, rep["t"]), max(self.t_end, rep["t"])
        self.beams.add(rep["beam"])
        self.reports += 1
        if rep["confidence"] > self.best_conf:
            self.best_conf, self.best_beam = rep["confidence"], rep["beam"]
        self.max_conf = max(self.max_conf, rep["confidence"])
        self._sha(rep)
        self.has_true = self.has_true or rep.get("in_region") is True
        self.has_false = self.has_false or rep.get("in_region") is False
        if self.taxa_group == "other-bio" and rep.get("non_species"):       # unmapped label keeps the producer flag (OR), as the dashboard does
            self.non_species = self.non_species or bool(rep.get("non_species"))
        if self.producer_group is None and isinstance(rep.get("group"), str) and rep.get("group"):
            self.producer_group = rep["group"]

    @property
    def in_region(self):
        """Event level: True when ANY report of the event is in region, False when any is filtered and none in region, else None."""
        return True if self.has_true else (False if self.has_false else None)


def iter_events_counted(rows, taxa=None):
    """Cluster rows exactly as ui/bma_ui.py DetStore does (through analysis/events.py) -> ([Event] in creation order, events.Result).
    Rows are consumed in the order given; human rows are kept here (the speech guard decides eligibility)."""
    taxa = taxa or Taxa()
    res = E.build(rows, EVENT_GAP_S, kinds=E.KINDS, drop_human=False)
    events = []
    for c in res.clusters:
        reps = [dict(o, in_region=False) if o["k"] == "bird_filtered" else o for o in c.rows]
        ev = Event(c.key, reps[0], taxa)
        for o in reps[1:]:
            ev.merge(o, taxa)
        ev.event_id = make_event_id(ev.t_start, ev.key)
        ev.content_id = c.id
        events.append(ev)
    return events, res


def iter_events(rows, taxa=None):
    return iter_events_counted(rows, taxa)[0]


def resolve_labels(records, events):
    """Where does each folded verdict point NOW?  -> {"exact": n, "superseded": {old_event_id: current_event_id}, "orphaned": [event_id],
    "ambiguous": n}.  exact = the record's event_id is a current event.  superseded = it is not, but a current event of the SAME species
    key spans the record's t_start_ms (the event grew or merged after the label was written); the earliest such event wins and a second
    candidate counts as ambiguous.  orphaned = no current event of that key spans it."""
    cur = {e.event_id for e in events}
    by_key = {}
    for e in events:
        by_key.setdefault(e.key, []).append(e)
    out = {"exact": 0, "superseded": {}, "orphaned": [], "ambiguous": 0}
    for eid, r in sorted(fold(records).items()):
        if eid in cur:
            out["exact"] += 1
            continue
        ms = r.get("t_start_ms")
        hits = sorted((e for e in by_key.get(r.get("species_key"), ()) if is_num(ms) and int(e.t_start * 1000) <= ms <= int(e.t_end * 1000)),
                      key=lambda e: (e.t_start, e.event_id))
        if not hits:
            out["orphaned"].append(eid)
            continue
        out["superseded"][eid] = hits[0].event_id
        out["ambiguous"] += 1 if len(hits) > 1 else 0
    return out


# ------------------------------------------------------------------------------------------------ speech guard
def is_speech_label(name):
    """True for a PANNs speech label or any label starting with a Human prefix (case-insensitive)."""
    n = (name or "").strip().lower()
    return n.startswith(HUMAN_LABEL_PREFIXES) or n in {s.lower() for s in SPEECH_LABELS}


class Tags:
    """The tagger's speech evidence as three parallel arrays (t, speech_p, active), sorted by time.  Only `k == "tags"` rows are read."""
    def __init__(self, rows):
        pts = sorted((float(r["t"]), float(r["speech_p"]) if is_num(r.get("speech_p")) else 0.0, r.get("speech_active") is True)
                     for r in rows if isinstance(r, dict) and r.get("k") == "tags" and is_num(r.get("t")))
        self.t = [p[0] for p in pts]
        self.p = [p[1] for p in pts]
        self.active = [p[2] for p in pts]

    def window(self, t0, t1, margin_s):
        i, j = bisect.bisect_left(self.t, t0 - margin_s), bisect.bisect_right(self.t, t1 + margin_s)
        return i, j


def speech_reason(ev, tags, margin_s=SPEECH_MARGIN_S, p_min=SPEECH_P_MIN, include_untagged=False):
    """-> None when the event may be reviewed, else exactly one of speech_label, speech_tag, speech_unknown (fail closed)."""
    if is_speech_label(ev.common) or is_speech_label(ev.scientific):
        return "speech_label"
    i, j = tags.window(ev.t_start, ev.t_end, margin_s)
    for n in range(i, j):
        if tags.active[n] or tags.p[n] >= p_min:
            return "speech_tag"
    if i == j and not include_untagged:
        return "speech_unknown"
    return None


# ------------------------------------------------------------------------------------------------ records
def validate_record(rec):
    """Strict writer-side check of one verdict record; raises LabelError(<field>: <why>).  The reader is lenient (unknown fields ignored)."""
    if not isinstance(rec, dict):
        raise LabelError("record: not an object")
    for k in rec:
        if k not in RECORD_KEYS:
            raise LabelError("%s: unknown top-level key" % k)
        if k.lower().startswith(FORBIDDEN_KEY_PREFIXES):
            raise LabelError("%s: forbidden key" % k)
    for k in ("k", "v", "label_id", "t_label", "event_id", "t_start_ms", "species_key", "verdict", "wrong_reason", "corrected_key",
              "basis", "source", "stratum", "p_include", "queue_id", "model", "provenance"):
        if k not in rec:
            raise LabelError("%s: missing" % k)
    if rec["k"] != "verdict" or rec["v"] != 1 or isinstance(rec["v"], bool):
        raise LabelError("k/v: must be verdict / 1")
    if not (isinstance(rec["label_id"], str) and re.fullmatch(r"[0-9a-f]{16}", rec["label_id"])):
        raise LabelError("label_id: must be 16 hex chars")
    if not (is_num(rec["t_label"]) and rec["t_label"] > 0):
        raise LabelError("t_label: must be a positive number")
    if not (isinstance(rec["event_id"], str) and EVENT_ID_RE.match(rec["event_id"])):
        raise LabelError("event_id: must match [0-9]{1,16}-[a-z0-9-]{0,60}")
    if not (isinstance(rec["t_start_ms"], int) and not isinstance(rec["t_start_ms"], bool) and rec["t_start_ms"] > 0):
        raise LabelError("t_start_ms: must be a positive integer")
    if rec["event_id"].split("-", 1)[0] != str(rec["t_start_ms"]):
        raise LabelError("t_start_ms: does not match the event_id prefix")
    for fld in ("species_key",):
        if not (isinstance(rec[fld], str) and SPECIES_KEY_RE.match(rec[fld])):
            raise LabelError("%s: must match [a-z0-9 .-]{1,60}" % fld)
    if rec["verdict"] not in VERDICTS:
        raise LabelError("verdict: must be one of %s" % ", ".join(VERDICTS))
    if rec["verdict"] == "wrong":
        if rec["wrong_reason"] not in WRONG_REASONS:
            raise LabelError("wrong_reason: required for verdict wrong, one of %s" % ", ".join(WRONG_REASONS))
    elif rec["wrong_reason"] is not None:
        raise LabelError("wrong_reason: only allowed with verdict wrong")
    if rec["corrected_key"] is not None:
        if rec["wrong_reason"] != "other_species":
            raise LabelError("corrected_key: only allowed with wrong_reason other_species")
        if not (isinstance(rec["corrected_key"], str) and SPECIES_KEY_RE.match(rec["corrected_key"])):
            raise LabelError("corrected_key: must match [a-z0-9 .-]{1,60}")
        if is_speech_label(rec["corrected_key"]):
            raise LabelError("corrected_key: not labelable")
    if rec["basis"] not in BASES:
        raise LabelError("basis: must be one of %s" % ", ".join(BASES))
    if rec["source"] not in SOURCES:
        raise LabelError("source: must be one of %s" % ", ".join(SOURCES))
    if rec["stratum"] is not None and rec["stratum"] not in STRATA:
        raise LabelError("stratum: must be null or one of %s" % ", ".join(STRATA))
    if rec["p_include"] is not None and not (is_num(rec["p_include"]) and 0 < rec["p_include"] <= 1):
        raise LabelError("p_include: must be null or in (0, 1]")
    if rec["queue_id"] is not None and not (isinstance(rec["queue_id"], str) and re.fullmatch(r"[0-9a-f]{12}", rec["queue_id"])):
        raise LabelError("queue_id: must be null or 12 hex chars")
    m = rec["model"]
    if not isinstance(m, dict) or set(m) != MODEL_KEYS:
        raise LabelError("model: must have exactly %s" % ", ".join(sorted(MODEL_KEYS)))
    if not (is_num(m["conf_max"]) and 0 <= m["conf_max"] <= 1):
        raise LabelError("model.conf_max: must be in [0, 1]")
    if not (isinstance(m["n_beams"], int) and not isinstance(m["n_beams"], bool) and 1 <= m["n_beams"] <= 8):
        raise LabelError("model.n_beams: must be 1..8")
    if not (isinstance(m["best_beam"], int) and not isinstance(m["best_beam"], bool) and 1 <= m["best_beam"] <= 8):
        raise LabelError("model.best_beam: must be 1..8")
    if m["group"] is not None and not isinstance(m["group"], str):
        raise LabelError("model.group: must be null or a string")
    if m["in_region"] is not None and not isinstance(m["in_region"], bool):
        raise LabelError("model.in_region: must be null or a boolean")
    if rec["provenance"] != PROVENANCE:
        raise LabelError("provenance: must be %s" % PROVENANCE)
    if "model_sha256" in rec and not (isinstance(rec["model_sha256"], str) and SHA256_RE.match(rec["model_sha256"])):
        raise LabelError("model_sha256: must be 64 lowercase hex chars")
    return rec


def make_record(ev, verdict, basis, *, wrong_reason=None, corrected_key=None, source="cli", item=None, t_label=None, label_id=None):
    """Build and validate a verdict record for an Event.  `item` = a queue item (stratum / p_include / queue_id are copied) or None."""
    item = item or {}
    rec = {"k": "verdict", "v": 1, "label_id": label_id or secrets.token_hex(8),
           "t_label": round(time.time() if t_label is None else t_label, 3),
           "event_id": ev.event_id, "t_start_ms": int(ev.t_start * 1000), "species_key": ev.key,
           "verdict": verdict, "wrong_reason": wrong_reason, "corrected_key": corrected_key, "basis": basis, "source": source,
           "stratum": item.get("stratum"), "p_include": item.get("p_include"), "queue_id": item.get("queue_id"),
           "model": {"conf_max": round(ev.max_conf, 4), "n_beams": len(ev.beams), "best_beam": ev.best_beam,
                     "group": ev.producer_group, "in_region": ev.in_region},
           "provenance": PROVENANCE}
    if ev.model_sha256 is not None:
        rec["model_sha256"] = ev.model_sha256
    return validate_record(rec)


def dumps(rec):
    return json.dumps(rec, separators=(",", ":"), sort_keys=False)


# ------------------------------------------------------------------------------------------------ the store
class LabelStore:
    """`<labels-dir>/labels.jsonl`: append-only, never rewritten, never rotated.  Writers serialise on `<file>.lock` (created O_EXCL);
    the line is written with ONE os.write on a descriptor opened O_APPEND | O_BINARY, so Windows never turns LF into CRLF."""
    LOCK_WAIT_S = 10.0
    LOCK_STALE_S = 60.0

    def __init__(self, labels_dir):
        self.dir = labels_dir
        self.path = os.path.join(labels_dir, "labels.jsonl")

    def read(self):
        """-> (records, bad_lines, partial).  A missing file is empty; an unreadable one raises OSError (the CLI exits 2)."""
        return read_jsonl(self.path)

    def _lock(self):
        lock = self.path + ".lock"
        deadline = time.monotonic() + self.LOCK_WAIT_S
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        while True:
            try:
                os.close(os.open(lock, flags))
                return lock
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(lock) > self.LOCK_STALE_S:
                        os.remove(lock)                           # a writer died holding it
                        continue
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    raise LabelError("labels file busy: lock held for more than %d s" % self.LOCK_WAIT_S)
                time.sleep(0.003)
            except PermissionError:                              # Windows: the holder is deleting it right now
                if time.monotonic() > deadline:
                    raise LabelError("labels file busy: lock not acquirable")
                time.sleep(0.003)

    def append(self, rec):
        """Validate, then append exactly one LF-terminated line.  A last line without a newline (a writer killed mid-append) is
        isolated by a leading newline.  -> the number of bytes written."""
        validate_record(rec)
        line = (dumps(rec) + "\n").encode("utf-8")
        os.makedirs(self.dir, exist_ok=True)
        lock = self._lock()
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
            try:
                size = os.fstat(fd).st_size
                prefix = b""
                if size:
                    os.lseek(fd, size - 1, os.SEEK_SET)
                    if os.read(fd, 1) != b"\n":
                        prefix = b"\n"
                n = os.write(fd, prefix + line)
                if n != len(prefix) + len(line):
                    raise OSError("short write to labels file")
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            try:
                os.remove(lock)
            except OSError:
                pass
        return len(line)


class Folded(dict):
    """fold() result: {event_id: latest record}; .hist = {event_id: {model_sha256 or None: (key, record)}} for the digest join."""
    hist = None


def fold(records):
    """-> {event_id: record}: per event the record with the greatest (t_label, line number) wins; earlier lines are history.
    Records that are not verdict v1 with a usable event_id and t_label are ignored (readers skip unknown versions)."""
    best = {}
    hist = {}
    for n, r in enumerate(records):
        if not (isinstance(r, dict) and r.get("k") == "verdict" and r.get("v") == 1 and isinstance(r.get("event_id"), str)
                and is_num(r.get("t_label"))):
            continue
        key = (r["t_label"], n)
        cur = best.get(r["event_id"])
        if cur is None or key >= cur[0]:
            best[r["event_id"]] = (key, r)
        h = hist.setdefault(r["event_id"], {})
        d = r.get("model_sha256")
        if d not in h or key >= h[d][0]:
            h[d] = (key, r)
    out = Folded((e, v[1]) for e, v in best.items())
    out.hist = hist
    return out


def label_for(folded, ev):
    """The folded verdict of an Event, or None. Joined on event id and, when both the record and the event carry one, on the model
    digest: a verdict given under another model's output is not applied to this event."""
    r = folded.get(ev.event_id)
    if r is None:
        return None
    h = getattr(folded, "hist", None)
    if h is not None and ev.model_sha256 is not None:
        cands = [v for d, v in h.get(ev.event_id, {}).items() if d is None or d == ev.model_sha256]
        return max(cands, key=lambda v: v[0])[1] if cands else None
    d = r.get("model_sha256")
    return r if (d is None or ev.model_sha256 is None or d == ev.model_sha256) else None


# ------------------------------------------------------------------------------------------------ ambient temperature (artifact probe)
class Ambient:
    """history/ambient_temp.jsonl reader.  Rule of specs/ambient-temp.md: the nearest PAST hour record, at most 3 h old, else unknown."""
    MAX_AGE_S = 3 * 3600

    def __init__(self, rows):
        by_hour = {int(r["t_hour_utc"]): float(r["temp_c"]) for r in rows         # a repeated hour: the last line wins (spec step 4)
                   if isinstance(r, dict) and r.get("k") == "ambient_temp" and is_num(r.get("t_hour_utc")) and is_num(r.get("temp_c"))}
        pts = sorted(by_hour.items())
        self.h = [p[0] for p in pts]
        self.c = [p[1] for p in pts]

    def at(self, t):
        hr = int(t) // 3600 * 3600                                   # spec step 1: the event's hour
        i = bisect.bisect_right(self.h, hr) - 1                      # step 2: nearest PAST record
        if i < 0 or hr - self.h[i] > self.MAX_AGE_S:                 # step 3: at most 3 h old, else unknown
            return None
        return self.c[i]
