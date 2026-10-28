#!/usr/bin/env python3
"""Event model (contract event-model 1.0.0, specs/event-model.md): per-beam bird reports -> events with a content-addressed id,
supersedes links and a conservation counter.  Pure, stdlib only, no I/O.  Shared by analysis/beam_agreement.py and analysis/labels.py."""
import hashlib, math, re

ID_VERSION = "ev1"
KINDS = ("bird", "bird_filtered")
REJECTIONS = ("other_kind", "out_of_region", "invalid", "hidden_human", "duplicate_window")
HUMAN_LABELS = frozenset(("human vocal", "human non-vocal", "human whistle"))
ID_RE = re.compile(r"^ev1-[0-9a-f]{24}$")


class ConservationError(AssertionError):
    """input != events + rejections.  Never caught inside the library."""


def _num(x):
    if not isinstance(x, (int, float)) or isinstance(x, bool):
        return False
    try:
        return math.isfinite(x)
    except OverflowError:                                 # an int too large for a float is garbage, never a crash
        return False


def key_of(o):
    return (o.get("scientific") or o["common"]).strip().lower()


def is_human(o):
    return any(isinstance(v, str) and v.strip().lower() in HUMAN_LABELS for v in (o.get("common"), o.get("scientific")))


def valid_report(o):
    return (isinstance(o, dict) and o.get("k") in KINDS and _num(o.get("t")) and o["t"] > 0
            and isinstance(o.get("beam"), int) and not isinstance(o.get("beam"), bool) and 1 <= o["beam"] <= 8
            and _num(o.get("confidence")) and 0 <= o["confidence"] <= 1
            and isinstance(o.get("common"), str) and o["common"] != ""
            and (o.get("scientific") is None or isinstance(o["scientific"], str)))


def t_ms(t):
    return int(round(t * 1000))


def member_of(o):
    """-> (window time in ms, beam)."""
    return (t_ms(o["t"]), o["beam"])


def event_id(key, members):
    """sha256 over `ev1\\n<key>\\n<first window ms>\\n<t_ms:beam;...>` (members sorted, duplicates collapsed) -> `ev1-` + first 24 hex."""
    ms = sorted(set(members))
    text = "%s\n%s\n%d\n%s" % (ID_VERSION, key, ms[0][0], ";".join("%d:%d" % m for m in ms))
    return "%s-%s" % (ID_VERSION, hashlib.sha256(text.encode("utf-8")).hexdigest()[:24])


class Cluster:
    """One event: the species key and the member rows in arrival order."""
    __slots__ = ("key", "t_start", "t_end", "rows")

    def __init__(self, key, o):
        self.key, self.t_start, self.t_end, self.rows = key, o["t"], o["t"], [o]

    def add(self, o):
        self.t_start, self.t_end = min(self.t_start, o["t"]), max(self.t_end, o["t"])
        self.rows.append(o)

    @property
    def members(self):
        return frozenset(member_of(o) for o in self.rows)

    @property
    def id(self):
        return event_id(self.key, self.members)

    def snapshot(self):
        return {"id": self.id, "key": self.key, "members": sorted([list(m) for m in self.members])}


class Result:
    """clusters in creation order + the accounting: `input` = reports offered, `reports` = reports inside clusters,
    `rejected` = {reason: n}, `duplicate_windows` = member rows whose (key, t_ms, beam) was already in the event (kept unless dedup)."""
    def __init__(self, clusters, n_input, rejected, duplicate_windows):
        self.clusters, self.input, self.rejected, self.duplicate_windows = clusters, n_input, rejected, duplicate_windows

    @property
    def reports(self):
        return sum(len(c.rows) for c in self.clusters)

    def counts(self):
        return {"input": self.input, "events": len(self.clusters), "reports": self.reports,
                "rejected": dict(self.rejected), "duplicate_windows": self.duplicate_windows}

    def check(self):
        """Hard invariant: input == reports in events + sum(rejected).  Raises ConservationError naming the numbers."""
        n_rej = sum(self.rejected.values())
        if self.input != self.reports + n_rej or set(self.rejected) != set(REJECTIONS) or min(self.rejected.values()) < 0:
            raise ConservationError("input %d != reports-in-events %d + rejected %d (%s)" % (self.input, self.reports, n_rej, self.rejected))
        return self.counts()


def build(rows, gap_s, *, kinds=("bird",), drop_human=True, dedup=False):
    """Chain reports of one species key into events (rows in the order given; last event per key; t_start - gap <= t <= t_end + gap).
    Rejection order: other_kind (not a dict / k not bird|bird_filtered), out_of_region (bird_filtered when not in `kinds`), invalid,
    hidden_human (when drop_human), duplicate_window (only when dedup: first report of a (key, t_ms, beam) wins)."""
    clusters, last, seen = [], {}, set()
    rej = dict.fromkeys(REJECTIONS, 0)
    n = dups = 0
    for o in rows:
        n += 1
        if not isinstance(o, dict) or o.get("k") not in KINDS:
            rej["other_kind"] += 1
            continue
        if o["k"] not in kinds:
            rej["out_of_region"] += 1
            continue
        if not valid_report(o):
            rej["invalid"] += 1
            continue
        if drop_human and is_human(o):
            rej["hidden_human"] += 1
            continue
        key = key_of(o)
        wk = (key,) + member_of(o)
        if wk in seen:
            dups += 1
            if dedup:
                rej["duplicate_window"] += 1
                continue
        seen.add(wk)
        c = last.get(key)
        if c is not None and c.t_start - gap_s <= o["t"] <= c.t_end + gap_s:
            c.add(o)
        else:
            c = Cluster(key, o)
            clusters.append(c)
            if key not in last or c.t_start >= last[key].t_start:
                last[key] = c
    return Result(clusters, n, rej, dups)


def diff(prev, clusters):
    """Membership changes between two runs.  `prev` = [snapshot dict], `clusters` = [Cluster].
    -> {"kept": [id], "superseded": {old_id: new_id}, "orphaned": [old_id], "new": [id], "membership_changed": n}.
    An old event is superseded by the current event of the same key whose members contain all of its members and differ; an old
    event whose members are spread over several current events (or none) is orphaned."""
    cur = {c.id: c for c in clusters}
    by_member = {}
    for c in clusters:
        for m in c.members:
            by_member.setdefault((c.key, m), c.id)
    kept, sup, orph = [], {}, []
    for p in prev:
        if p["id"] in cur:
            kept.append(p["id"])
            continue
        homes = {by_member.get((p["key"], (m[0], m[1]))) for m in p["members"]}
        if len(homes) == 1 and None not in homes:
            sup[p["id"]] = homes.pop()
        else:
            orph.append(p["id"])
    old = {p["id"] for p in prev}
    return {"kept": kept, "superseded": sup, "orphaned": orph, "new": [i for i in cur if i not in old and i not in sup.values()],
            "membership_changed": len(sup)}


def merge_aliases(table, d):
    """Fold one `diff` into the persistent alias table {old_id: current_id}: existing targets that were themselves superseded are
    re-pointed (chains stay one hop), new links are added.  -> the table (mutated)."""
    for old, new in d["superseded"].items():
        table[old] = new
    for old, tgt in list(table.items()):
        while tgt in d["superseded"]:
            tgt = d["superseded"][tgt]
        table[old] = tgt
    return table


def resolve(event_id_, table):
    """-> the current id for an id in the alias table, else the id itself."""
    return table.get(event_id_, event_id_)
