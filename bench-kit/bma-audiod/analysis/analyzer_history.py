"""Analyzer counter history (contract: run-history, record kind analyzer_sample).

Every `every_s` seconds (30) the analysis host hands its status dict (the object written to status.json every 5 s) to
AnalyzerHistory.tick(); one compact sample goes to <history dir>/analyzer_history.jsonl (hist.RotatingJsonl: 48 MB cap + one
backup, about 1 MB a day). Counters are cumulative since the analyzer start (`started`); a changed `started` is a reset.
`rx.gaps` is the SUM of the 8 per-ring gap counters (about 8 x the bridge's loss events). Never raises."""

import os

import hist

CAP_BYTES = 48 << 20
EVERY_S = 30.0


def build_analyzer_sample(status, now=None):
    """plugins -> {name: [windows, records, errors, dropped]} for the enabled plugins that have counters (arrays, not dicts, to
    keep a sample near 330 B); novelty and disabled plugins have none and are left out."""
    plugins = {}
    for name, p in sorted((status.get("plugins") or {}).items()):
        if isinstance(p, dict) and p.get("enabled") and "windows" in p:
            plugins[name] = [p.get("windows", 0), p.get("records", 0), p.get("errors", 0), p.get("dropped", 0)]
    rx = status.get("rx") or {}
    return {
        "k": "analyzer_sample",
        "v": 1,
        "t": round(status["t"] if now is None else now, 3),
        "started": round(status.get("started", 0.0), 3),
        "uptime_s": status.get("uptime_s"),
        "rx": {"datagrams": rx.get("datagrams", 0), "bad": rx.get("bad", 0), "gaps": rx.get("gaps", 0)},
        "contiguous_s": status.get("contiguous_s"),
        "plugins": plugins,
        "embeddings_stored": status.get("embeddings_stored"),
        "device": status.get("device"),
    }


class AnalyzerHistory:
    def __init__(self, history_dir, cap_bytes=CAP_BYTES, every_s=EVERY_S, replace=os.replace):
        self.out = hist.RotatingJsonl(os.path.join(history_dir, "analyzer_history.jsonl"), cap_bytes, replace=replace)
        self.every_s = every_s
        self.next_t = 0.0
        self.errors = 0  # samples that could not be built (bad status dict)

    def tick(self, status, now):
        """Write a sample when due. Returns True when a line was written."""
        if now < self.next_t:
            return False
        self.next_t = now + self.every_s
        try:
            rec = build_analyzer_sample(status)
        except (KeyError, TypeError, ValueError):
            self.errors += 1
            return False
        return self.out.write(rec)
