"""Advisory artifact flags for insect and mechanical detections (contract: contracts/specs/artifact-gate.md, 1.0.0).

Pure and deterministic, standard library only: no I/O, no clock, no environment, no mutation of the arguments. The gate never
deletes, hides or rewrites a record; it only answers "is this label implausible or suspect right now?" with closed reason codes and
the numbers it used.

  evaluate(group=, label=, scientific=, t=, ambient=, transients=, cfg=) -> None | {"v":1,"flags":[...],"level":...,"evidence":{...}}

None means "say nothing": the group is out of scope (bird, mammal, amphibian, other-bio, unknown) or the label is a hidden one
(human voice / speech: the policy is that no output of any kind exists for it). Fail-open means no flag, never "flag everything"."""
import bisect, math, statistics

REASONS = ("cold_insect", "burst_coincident", "temp_unknown", "mechanical_label")
LEVELS = ("none", "suspect", "likely")
TEMP_MIN_C, TEMP_MAX_C = -90.0, 60.0               # ambient-temp contract range
HOUR_S = 3600


def is_hidden_label(*names):
    """True for any human-voice label: 'Human vocal', 'Human non-vocal', 'Human whistle', ... (every label starting with
    'human') and 'Speech'. Case and surrounding blanks ignored. Anything that is not a string is ignored."""
    for n in names:
        if isinstance(n, str):
            s = n.strip().lower()
            if s.startswith("human") or s == "speech":
                return True
    return False


class GateConfig:
    """All knobs. Defaults are the measured, provisional ones of docs/design/artifact-gate.md (section 1.2)."""
    __slots__ = ("floors_c", "temp_max_age_s", "burst_window_s", "burst_min_transients", "burst_max_spread_db",
                 "scope_groups", "label_groups")

    def __init__(self, floors_c=(("insect", 5.0),), temp_max_age_s=3 * HOUR_S, burst_window_s=30.0, burst_min_transients=20,
                 burst_max_spread_db=3.0, scope_groups=("insect",), label_groups=("mechanical",)):
        self.floors_c = tuple((str(g), float(f)) for g, f in floors_c)       # group -> floor; a group without one has no temperature rule
        self.temp_max_age_s = int(temp_max_age_s)
        self.burst_window_s = float(burst_window_s)
        self.burst_min_transients = int(burst_min_transients)
        self.burst_max_spread_db = float(burst_max_spread_db)
        self.scope_groups = tuple(scope_groups)
        self.label_groups = tuple(label_groups)

    def floor_for(self, group):
        for g, f in self.floors_c:
            if g == group:
                return f
        return None


DEFAULT = GateConfig()


def _usable_temp(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and TEMP_MIN_C <= v <= TEMP_MAX_C


class AmbientIndex:
    """ambient_temp.jsonl records -> temperature lookup by the reader rule of specs/ambient-temp.md. Records that are not objects, are
    not kind ambient_temp v1, have an hour that is not an integer multiple of 3600, or a temperature that is not a finite number in
    -90..60 are skipped and counted in `skipped`. The LAST record of an hour wins."""

    def __init__(self, records=()):
        by_hour, self.skipped = {}, 0
        for r in records:
            if (not isinstance(r, dict) or r.get("k") != "ambient_temp" or r.get("v") != 1 or isinstance(r.get("t_hour_utc"), bool)
                    or not isinstance(r.get("t_hour_utc"), int) or r["t_hour_utc"] < 0 or r["t_hour_utc"] % HOUR_S
                    or not _usable_temp(r.get("temp_c"))):
                self.skipped += 1
                continue
            by_hour[r["t_hour_utc"]] = float(r["temp_c"])
        self._hours = sorted(by_hour)
        self._temps = [by_hour[h] for h in self._hours]

    def __len__(self):
        return len(self._hours)

    def span(self):
        """(first hour, last hour) or (None, None)"""
        return (self._hours[0], self._hours[-1]) if self._hours else (None, None)

    def temp_at(self, t, max_age_s=3 * HOUR_S):
        """-> (temp_c, age_s) or (None, None). h = floor(t / 3600) * 3600; the record with the largest hour <= h (never a later hour,
        never interpolated) is used only when h - hour <= max_age_s; age_s is that difference."""
        if not (isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t)):
            raise ValueError("t must be finite")
        h = math.floor(t / HOUR_S) * HOUR_S
        i = bisect.bisect_right(self._hours, h) - 1
        if i < 0 or h - self._hours[i] > max_age_s:
            return None, None
        return self._temps[i], int(h - self._hours[i])


class TransientIndex:
    """(t, beam_spread_db) pairs of the dsp `transient` records, sorted by t. Pairs with a non-finite t or spread, or a negative
    spread, are skipped and counted in `skipped`."""

    def __init__(self, pairs=()):
        good, self.skipped = [], 0
        for p in pairs:
            try:
                t, s = float(p[0]), float(p[1])
            except (TypeError, ValueError, IndexError):
                self.skipped += 1
                continue
            if not (math.isfinite(t) and math.isfinite(s)) or s < 0:
                self.skipped += 1
                continue
            good.append((t, s))
        good.sort()
        self._t = [p[0] for p in good]
        self._s = [p[1] for p in good]

    def __len__(self):
        return len(self._t)

    def window(self, t, half_s):
        """-> (n, median beam_spread_db or None) over the transients with t - half_s <= time <= t + half_s (both ends inclusive)."""
        lo = bisect.bisect_left(self._t, t - half_s)
        hi = bisect.bisect_right(self._t, t + half_s)
        n = hi - lo
        return n, (statistics.median(self._s[lo:hi]) if n else None)


def _r1(x):
    return None if x is None else round(float(x), 1)


def evaluate(*, group, label, scientific, t, ambient, transients, cfg=DEFAULT):
    """One detection -> None (out of scope or hidden label: the caller emits nothing) or the advisory verdict
    {"v":1,"flags":[sorted REASONS],"level":"none|suspect|likely","evidence":{temp_c,temp_age_s,floor_c,burst_n,burst_spread_db,window_s}}.
    `ambient` None = no temperature feed (temp_unknown); `transients` None = no transient feed (burst_n null, no burst flag)."""
    if not (isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t)):
        raise ValueError("t must be finite")
    if is_hidden_label(label, scientific):
        return None
    flags, ev = [], {"temp_c": None, "temp_age_s": None, "floor_c": None, "burst_n": None, "burst_spread_db": None, "window_s": None}
    if group in cfg.scope_groups:
        floor = cfg.floor_for(group)
        temp, age = ambient.temp_at(t, cfg.temp_max_age_s) if ambient is not None else (None, None)
        if floor is not None:
            ev["floor_c"] = floor
            if temp is None:
                flags.append("temp_unknown")
            elif temp < floor:                                  # strict: exactly the floor is not flagged
                flags.append("cold_insect")
            ev["temp_c"], ev["temp_age_s"] = _r1(temp), age
        if transients is not None:
            n, spread = transients.window(t, cfg.burst_window_s)
            ev["burst_n"], ev["burst_spread_db"], ev["window_s"] = n, _r1(spread), cfg.burst_window_s
            if n >= cfg.burst_min_transients and spread is not None and spread <= cfg.burst_max_spread_db:
                flags.append("burst_coincident")
    elif group in cfg.label_groups:
        flags.append("mechanical_label")
    else:
        return None
    flags.sort()
    level = "likely" if "cold_insect" in flags else "suspect" if "burst_coincident" in flags else "none"
    return {"v": 1, "flags": flags, "level": level, "evidence": ev}
