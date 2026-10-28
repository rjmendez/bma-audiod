"""Per-UTC-day rollup written atomically to <history>/daily_summary.json: windows per plugin, records per plugin.kind,
errors/dropped, audio seconds analysed, ring-reset time, effort denominators (birds windows and those where no beam
passed the gate, speech-suppressed tag windows, directional spatial windows) and rx datagram/gap deltas. The file is
loaded at start and kept adding to, so a restart neither loses nor doubles a day (restarts counts them). 400 days kept.
{"k":"daily_summary","v":1,"days":{"YYYYMMDD":{...}}}
"""

import os, time, threading
from hist import AtomicJson

KEEP_DAYS = 400


def _day(t):
    return time.strftime("%Y%m%d", time.gmtime(t))


def _blank():
    return {
        "restarts": 0,
        "windows": {},
        "records": {},
        "errors": {},
        "dropped": {},
        "audio_s": 0.0,
        "ring_reset_s": 0.0,
        "birds_windows": 0,
        "birds_no_beam_over_gate": 0,
        "tags_speech_suppressed": 0,
        "spatial_directional": 0,
        "rx_datagrams": 0,
        "rx_bad": 0,
        "rx_gaps": 0,
        "uptime_s": 0.0,
    }


class DailySummary:
    def __init__(self, path, clock=time.time, replace=os.replace):
        self.io = AtomicJson(path, replace)
        self.clock, self.lock, self.dirty = clock, threading.Lock(), False
        d = self.io.read()
        self.days = d["days"] if isinstance(d, dict) and isinstance(d.get("days"), dict) else {}
        self.day(self.clock())["restarts"] += 1
        self.dirty = True
        self._last_rx = (0, 0, 0)

    def day(self, t):
        d = self.days.setdefault(_day(t), _blank())
        for k, v in _blank().items():  # a day written by an older version gains new keys
            d.setdefault(k, v)
        return d

    def on_window(self, plugin, recs, t, info=None):
        with self.lock:
            d = self.day(t)
            d["windows"][plugin] = d["windows"].get(plugin, 0) + 1
            for r in recs:
                key = plugin + "." + str(r.get("k"))
                d["records"][key] = d["records"].get(key, 0) + 1
            if plugin == "dsp":
                d["audio_s"] += 1.0
            elif plugin == "spatial":
                d["spatial_directional"] += sum(1 for r in recs if r.get("k") == "spatial" and r.get("directional"))
            elif plugin == "tags":
                d["tags_speech_suppressed"] += sum(1 for r in recs if r.get("speech_active"))
            elif plugin == "birds" and info is not None:
                d["birds_windows"] += 1
                d["birds_no_beam_over_gate"] += 1 if info.get("beams_classified", 0) == 0 else 0
            self.dirty = True

    def on_error(self, plugin, t):
        with self.lock:
            d = self.day(t)
            d["errors"][plugin] = d["errors"].get(plugin, 0) + 1
            self.dirty = True

    def on_dropped(self, plugin, n, t):
        with self.lock:
            d = self.day(t)
            d["dropped"][plugin] = d["dropped"].get(plugin, 0) + int(n)
            self.dirty = True

    def on_status(self, t, rx, contiguous_s, period_s):
        """rx = cumulative {datagrams,bad,gaps} of this run; deltas since the previous call are added."""
        with self.lock:
            d = self.day(t)
            cur = (rx["datagrams"], rx["bad"], rx["gaps"])
            d["rx_datagrams"] += cur[0] - self._last_rx[0]
            d["rx_bad"] += cur[1] - self._last_rx[1]
            d["rx_gaps"] += cur[2] - self._last_rx[2]
            self._last_rx = cur
            d["uptime_s"] += period_s
            if not contiguous_s:
                d["ring_reset_s"] += period_s
            self.dirty = True

    def flush(self):
        with self.lock:
            for k in sorted(self.days)[:-KEEP_DAYS]:
                del self.days[k]
            if not self.dirty:
                return True
            ok = self.io.write({"k": "daily_summary", "v": 1, "days": self.days})
            if ok:
                self.dirty = False
            return ok

    @property
    def errors(self):
        return self.io.errors
