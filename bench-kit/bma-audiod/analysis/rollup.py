"""Hourly rollup of the dsp / spatial streams, kept far beyond the raw dsp.jsonl retention.

One record per closed UTC hour, appended once (never rewritten) to <history>/hourly_rollup.jsonl:
  {"k":"hourly_rollup","v":1,"kind":"dsp","hour_s","windows","rms_dbfs_mean":[8],"band_pct_mean":[5],"centroid_hz_mean",
   "noise_floor_db":{"min","p50","max"},"transients":{"n","by_loudest_beam":[8],"excess_db_max"},
   "tonal":{"appeared_n","gone_n","state_n","electronic_line_s","lines":[{"bin_hz","state_n","appeared_n","gone_n",
            "dur_s_total","beam","beam_n","electronic"}]  (top 30)},
   "spatial":{"windows","directional_n","leader_count":[8],"rel_mean_db":[8],"decorr_mean"}}
An hour closes at its end + grace_s (late records are counted, not applied). The open hours live in a small state file
so a restart resumes them instead of dropping or double counting. electronic_line_s = sum over tonal_state records of
the number of lines flagged electronic x 10 s (the tonal analysis period). Disk: ~3 KB/hour.
"""

import os, time
from hist import RotatingJsonl, AtomicJson

NB = 8
STATE_PERIOD_S = 10.0


def _new():
    return {
        "w": 0,
        "rms": [0.0] * NB,
        "band": [0.0] * 5,
        "cen": 0.0,
        "nf": [],
        "tr_n": 0,
        "tr_beam": [0] * NB,
        "tr_exc": 0.0,
        "app": 0,
        "gone": 0,
        "states": 0,
        "el": 0,
        "lines": {},
        "sp_w": 0,
        "sp_dir": 0,
        "sp_lead": [0] * NB,
        "sp_rel": [0.0] * NB,
        "sp_dec": 0.0,
        "sp_dec_n": 0,
    }


def _line(acc, hz):
    return acc["lines"].setdefault(str(int(round(hz / 4.0) * 4)), {"n": 0, "app": 0, "gone": 0, "dur": 0.0, "beams": {}, "el": False})


class HourlyRollup:
    def __init__(self, hist_dir, cap_bytes=64 << 20, grace_s=120.0, clock=time.time):
        self.out = RotatingJsonl(os.path.join(hist_dir, "hourly_rollup.jsonl"), cap_bytes)
        self.state = AtomicJson(os.path.join(hist_dir, "hourly_rollup_state.json"))
        self.grace, self.clock = grace_s, clock
        self.open, self.last_closed, self.late, self.errors, self.written = {}, -1, 0, 0, 0
        st = self.state.read()
        if isinstance(st, dict) and isinstance(st.get("open"), dict):
            self.open = {int(h): a for h, a in st["open"].items()}
            self.last_closed = int(st.get("last_closed", -1))

    def feed(self, rec):
        try:
            self._feed(rec)
        except (KeyError, TypeError, ValueError, IndexError):
            self.errors += 1

    def _feed(self, r):
        k = r.get("k")
        if k not in ("dsp", "transient", "tonal_appeared", "tonal_gone", "tonal_state", "spatial"):
            return
        h = int(r["t"] // 3600) * 3600
        if h <= self.last_closed:
            self.late += 1
            return
        a = self.open.get(h)
        if a is None:
            a = self.open[h] = _new()
        if k == "dsp":
            a["w"] += 1
            a["cen"] += r["centroid_hz"]
            for i in range(NB):
                a["rms"][i] += r["rms_dbfs"][i]
            for i in range(5):
                a["band"][i] += r["band_pct"][i]
        elif k == "transient":
            a["tr_n"] += 1
            a["tr_beam"][int(r["loudest_beam"]) - 1] += 1
            a["tr_exc"] = max(a["tr_exc"], float(r["excess_db"]))
        elif k == "tonal_appeared":
            a["app"] += 1
            _line(a, r["hz"])["app"] += 1
        elif k == "tonal_gone":
            a["gone"] += 1
            ln = _line(a, r["hz"])
            ln["gone"] += 1
            ln["dur"] += float(r["lasted_s"])
        elif k == "tonal_state":
            a["states"] += 1
            a["nf"].append(float(r["noise_floor_db"]))
            for l in r["lines"]:
                ln = _line(a, l["hz"])
                ln["n"] += 1
                if l.get("electronic"):
                    ln["el"] = True
                    a["el"] += 1
                if l.get("beam") is not None:
                    ln["beams"][str(l["beam"])] = ln["beams"].get(str(l["beam"]), 0) + 1
        elif k == "spatial":
            a["sp_w"] += 1
            a["sp_dir"] += 1 if r["directional"] else 0
            a["sp_lead"][int(r["dominant_beam"]) - 1] += 1
            for i in range(NB):
                a["sp_rel"][i] += r["e_rel_db"][i]
            if r.get("decorr") is not None:
                a["sp_dec"] += r["decorr"]
                a["sp_dec_n"] += 1

    @staticmethod
    def _record(h, a):
        w, sw = max(a["w"], 1), max(a["sp_w"], 1)
        nf = sorted(a["nf"])
        lines = []
        for b, ln in a["lines"].items():
            bm = max(ln["beams"].items(), key=lambda kv: kv[1]) if ln["beams"] else (None, 0)
            lines.append(
                {
                    "bin_hz": int(b),
                    "state_n": ln["n"],
                    "appeared_n": ln["app"],
                    "gone_n": ln["gone"],
                    "dur_s_total": round(ln["dur"], 0),
                    "beam": int(bm[0]) if bm[0] else None,
                    "beam_n": bm[1],
                    "electronic": ln["el"],
                }
            )
        lines.sort(key=lambda d: (-(d["dur_s_total"] + STATE_PERIOD_S * d["state_n"]), d["bin_hz"]))
        return {
            "k": "hourly_rollup",
            "v": 1,
            "kind": "dsp",
            "hour_s": h,
            "windows": a["w"],
            "rms_dbfs_mean": [round(v / w, 1) for v in a["rms"]],
            "band_pct_mean": [round(v / w, 1) for v in a["band"]],
            "centroid_hz_mean": round(a["cen"] / w, 0),
            "noise_floor_db": ({"min": round(nf[0], 1), "p50": round(nf[len(nf) // 2], 1), "max": round(nf[-1], 1)} if nf else None),
            "transients": {"n": a["tr_n"], "by_loudest_beam": a["tr_beam"], "excess_db_max": round(a["tr_exc"], 1)},
            "tonal": {
                "appeared_n": a["app"],
                "gone_n": a["gone"],
                "state_n": a["states"],
                "electronic_line_s": int(a["el"] * STATE_PERIOD_S),
                "lines": lines[:30],
            },
            "spatial": {
                "windows": a["sp_w"],
                "directional_n": a["sp_dir"],
                "leader_count": a["sp_lead"],
                "rel_mean_db": [round(v / sw, 1) for v in a["sp_rel"]],
                "decorr_mean": round(a["sp_dec"] / a["sp_dec_n"], 3) if a["sp_dec_n"] else None,
            },
        }

    def tick(self, now=None):
        """Close every hour whose end + grace has passed, then persist the open hours."""
        now = self.clock() if now is None else now
        for h in sorted(self.open):
            if h + 3600 + self.grace <= now:
                if self.out.write(self._record(h, self.open[h])):
                    self.written += 1
                else:
                    self.errors += 1
                del self.open[h]
                self.last_closed = max(self.last_closed, h)
        self.save()

    def save(self):
        if not self.state.write({"v": 1, "last_closed": self.last_closed, "open": {str(h): a for h, a in self.open.items()}}):
            self.errors += 1

    @property
    def write_errors(self):
        return self.out.write_errors

    @property
    def rotate_fallbacks(self):
        return self.out.rotate_fallbacks
