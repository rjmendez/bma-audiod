"""The explicit wiring between plugins and the host (analysis/plugins.manifest.json `hooks`).

Until the manifest, host.py found the plugins that take part in a hand-off with `hasattr` and `p.name == "..."` branches. Now a
plugin's participation is DECLARED in its manifest entry (hooks.consumes_speech / consumes_tag / observes / cost / flush /
stats_in_status / window_info_to_daily and the named hooks.feeds below) and the host dispatches only on the declaration;
manifest.validate() and test_manifest.py prove declaration == implementation in both directions.

Named feeds (the plugin-specific hand-offs, deliberately not generalised):
  tags_feed       the tagger's verdict: speech consumers, tag consumers, speech_fraction, embedding store and novelty
  speech_guard    tags only: every tagger verdict also goes to the Perch speech guard (ctx.shared["speech_guard"], built by perch:make)
  birds_to_perch  birds only: its records and window info go to the running perch plugin
  clips           birds and tags: every window, also with zero records, goes to the wildlife clip service

Everything is resolved ONCE at start (the dispatch lists); the per-window path does no manifest lookup and no getattr."""
import json

import manifest as M


def feed_speech(consumers, tag_plugin, recs, t_wall):
    """Hand one tagger verdict (recs = the tags plugin's records of the window ending at t_wall) to every plugin that declared
    consumes_speech (note_speech(t_start, t_end, speech_active)): they exclude the windows the tagger flagged as speech."""
    if not recs:
        return
    active = bool(recs[0].get("speech_active"))
    for q in consumers:
        q.note_speech(t_wall - tag_plugin.win_s, t_wall, active)


def consumers(m, plug, hook):
    """The instances in `plug` whose manifest entry declares `hook`, in start order."""
    ent = M.by_name(m)
    return [p for p in plug if p.name in ent and M.hooks(ent[p.name]).get(hook)]


class Wiring:
    def __init__(self, m, plug, *, out, status, clips, store, novelty, shared):
        self.m, self.plug, self.out, self.status = m, plug, out, status
        self.clips, self.store, self.novelty, self.shared = clips, store, novelty, shared
        self.ent = M.by_name(m)
        self.speech = consumers(m, plug, "consumes_speech")
        self.tag = consumers(m, plug, "consumes_tag")
        self.observers = consumers(m, plug, "observes")
        self.cost_plugins = consumers(m, plug, "cost")
        self.flush_plugins = consumers(m, plug, "flush")
        self.perch = next((q for q in plug if q.name == "perch"), None)
        self.speech_recent = 0.0
        # resolved once: per plugin name (stats_in_status, feeds, window_info_to_daily); the per-window path does dict lookups only
        self.plan = {p.name: (bool(self.hooks(p).get("stats_in_status")), tuple(self.hooks(p).get("feeds", ())), bool(self.hooks(p).get("window_info_to_daily")))
                     for p in plug}

    def hooks(self, p):
        e = self.ent.get(p.name)
        return M.hooks(e) if e else {}

    def window_info(self, p):
        """What the daily summary gets next to the records of this plugin (the birds plugin's window_info), else None."""
        return getattr(p, "window_info", None) if self.plan[p.name][2] else None

    def fan_out(self, src, recs, t_wall):
        """Hand a plugin's records (also none) to every plugin that observes the others in-process (mixscore). Never raises."""
        for q in self.observers:
            if q is src:
                continue
            try:
                rr = q.observe(src.name, recs, t_wall)
                for r in rr:
                    self.out.write(q.name, r)
                self.status["plugins"][q.name]["records"] += len(rr)
            except Exception as e:
                self.status["plugins"][q.name]["errors"] += 1
                self.status["plugins"][q.name]["last_error"] = f"{type(e).__name__}: {e}"[:300]

    def after_process(self, p, recs, emb, x, t_wall, st):
        """Everything the host does for plugin p after its records are written and counted, in the order the old run_plugin had:
        status stats, birds to perch, speech guard, fan-out to observers, clip service, tagger feed. May raise (the caller counts it)."""
        stats_in_status, feeds, _wi = self.plan[p.name]
        if stats_in_status:
            st.update(p.stats())
        if "birds_to_perch" in feeds and self.perch is not None:
            self.perch.feed_birds(t_wall, recs, getattr(p, "window_info", None))
        if "speech_guard" in feeds and recs and self.shared.get("speech_guard") is not None:
            self.shared["speech_guard"].note(t_wall - p.win_s, t_wall, bool(recs[0].get("speech_active")))
        self.fan_out(p, recs, t_wall)
        if "clips" in feeds and self.clips is not None:
            self.clips.on_plugin(p.name, recs, x, t_wall)       # every window, also with zero records; never raises
        if "tags_feed" in feeds and recs:
            feed_speech(self.speech, p, recs, t_wall)
            for q in self.tag:                                  # the indices plugin excludes the windows the tagger flagged as speech
                q.note_tag(recs[0]["t"], bool(recs[0].get("speech_active")), p.win_s)
            self.speech_recent = 0.9 * self.speech_recent + 0.1 * (1.0 if recs[0].get("speech_active") else 0.0)
            st["speech_fraction"] = round(self.speech_recent, 3)
            if emb is not None:
                top1 = json.dumps(recs[0]["top"][0][0]) if recs[0]["top"] else "null"
                self.store.add(emb, {"t": t_wall, "top1_json": top1})
                for r in self.novelty.add(emb, t_wall):
                    self.out.write("novelty", r)
