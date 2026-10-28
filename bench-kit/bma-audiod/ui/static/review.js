"use strict";
/* Review tab (ui-http 1.9.0): one queue item at a time with its evidence (spectrogram + audio), keys space/r play, 1-4 verdict, s = skip.  Global `Review`, driven by app.js (TABS). */
const Review = (function () {
  const VERDICTS = [["correct", "Species is right"], ["wrong", "Species is wrong"], ["unsure", "Cannot tell"], ["artifact", "Not a real sound"]];
  const REASONS = [["other_species", "other species"], ["non_bird_sound", "not a bird"], ["mechanical", "mechanical"], ["insect_or_amphibian", "insect / frog"], ["no_sound_seen", "nothing visible"]];
  const R = { item: null, depth: 0, stats: null, error: null, busy: false, wrong: false, csrf: null, root: null, ui: {}, media: null };

  function el(tag, props, ...kids) {
    const e = document.createElement(tag);
    for (const k of Object.keys(props || {})) { if (k === "class") e.className = props[k]; else if (k.startsWith("on")) e.addEventListener(k.slice(2), props[k]); else e.setAttribute(k, props[k]); }
    for (const c of kids) if (c !== null && c !== undefined && c !== false) e.append(c.nodeType ? c : document.createTextNode(String(c)));
    return e;
  }
  function problem(r, body) { try { const j = JSON.parse(body); return j.detail || j.title || ("HTTP " + r.status); } catch (e) { return "HTTP " + r.status; } }
  async function call(method, path, payload) {
    const opt = { method: method, credentials: "same-origin", headers: {} };
    if (payload) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(payload); }
    let r;
    try { r = await fetch(path, opt); } catch (e) { return { ok: false, error: "network: " + (e && e.message || "failed") }; }
    const body = await r.text();
    if (r.status === 401) return { ok: false, error: "signed out" };
    if (!r.ok) return { ok: false, error: problem(r, body), status: r.status };
    try { return { ok: true, data: JSON.parse(body) }; } catch (e) { return { ok: false, error: "bad response" }; }
  }
  async function token() {
    if (R.csrf) return R.csrf;
    const r = await call("GET", "/api/session");
    if (r.ok && r.data.csrf) R.csrf = r.data.csrf;
    return R.csrf;
  }
  function speciesKey(it) { return ((it.species.scientific || it.species.common) || "").trim().toLowerCase(); }
  function labelsOf(key) { const s = R.stats && R.stats.species.find(x => x.species_key === key); return s ? s.labels : 0; }

  function at(e, lo, w) { e.style.left = (lo * 100).toFixed(2) + "%"; if (w !== undefined) e.style.width = (w * 100).toFixed(2) + "%"; return e; }   // CSSOM, not a style attribute: the CSP forbids inline styles
  function fmtS(x) { return (Math.round(x * 10) / 10) + "s"; }
  /* the evidence block of one item: waterfall with time axis + event span + playhead, then the audio player (or "no audio") */
  function mediaOf(it) {
    const m = { id: it.event_id, audio: null, head: null, node: null };
    const kids = [];
    const sp = it.clip_span;
    if (it.clip_png) {
      const wrap = el("div", { class: "rv-wave" }, el("img", { class: "rv-img", src: it.clip_png, alt: "spectrogram" }));
      const dur = sp ? sp.clip_end_s - sp.clip_start_s : 0;
      if (sp && dur > 0) {
        const a = Math.max(0, (sp.event_start_s - sp.clip_start_s) / dur), b = Math.min(1, (sp.event_end_s - sp.clip_start_s) / dur);
        if (b >= a) wrap.append(at(el("div", { class: "rv-span", title: "event" }), a, Math.max(b - a, 0.01)));   // a one-report event is a 1% marker
        m.head = el("div", { class: "rv-head" });
        wrap.append(m.head);
        const axis = el("div", { class: "rv-axis" });
        const step = Math.max(1, Math.ceil(dur / 8));
        for (let t = 0; t <= dur; t += step) axis.append(at(el("span", { class: "rv-tick" }, fmtS(t)), t / dur));
        kids.push(wrap, axis);
      } else kids.push(wrap);
    } else if (!it.clip_audio) kids.push(el("div", { class: "empty rv-noclip" }, "no clip"));
    if (it.clip_audio) {
      m.audio = el("audio", { class: "rv-audio", controls: "", preload: "none", src: it.clip_audio });
      m.audio.addEventListener("timeupdate", () => {
        const d = m.audio.duration && isFinite(m.audio.duration) ? m.audio.duration : (sp ? sp.clip_end_s - sp.clip_start_s : 0);
        if (m.head && d > 0) m.head.style.left = Math.min(100, m.audio.currentTime / d * 100).toFixed(2) + "%";
      });
      kids.push(m.audio);
    } else if (it.clip_png) kids.push(el("div", { class: "muted rv-noaudio" }, "no audio"));
    m.node = el("div", { class: "rv-media" }, ...kids);
    return m;
  }
  function media(it) {                                  // one block per item: a re-render (busy, reason row) re-inserts the same element, the playback goes on
    if (!R.media || R.media.id !== it.event_id) R.media = mediaOf(it);
    return R.media;
  }
  function togglePlay() { const a = R.media && R.media.audio; if (!a) return; if (a.paused) a.play().catch(() => {}); else a.pause(); }
  function replay() { const a = R.media && R.media.audio; if (!a) return; a.currentTime = 0; a.play().catch(() => {}); }

  function render() {
    const u = R.ui, it = R.item, st = R.stats;
    u.count.textContent = st ? ("labelled " + st.labelled + " | species >= " + st.target + ": " + st.species_at_target + " | queue " + R.depth) : "labelled -";
    u.err.textContent = R.error || "";
    u.err.hidden = !R.error;
    u.card.replaceChildren();
    if (!it) {
      if (!R.error) u.card.append(el("div", { class: "empty" }, "queue empty: " + (st ? st.no_clip : 0) + " candidates without clip"));
    } else {
      const key = speciesKey(it), n = labelsOf(key);
      const med = media(it).node;
      const art = it.artifact_flags ? it.artifact_flags.level + (it.artifact_flags.flags.length ? " " + it.artifact_flags.flags.join(",") : "") : null;
      const rows = [["Species", it.species.common + (it.species.scientific ? " / " + it.species.scientific : "")], ["Conf", it.confidence.toFixed(2)],
        ["Beams", it.beams.join(",") + " (best " + it.best_beam + ")"], ["Time UTC", it.t_utc], ["In region", it.in_region === null ? "unknown" : (it.in_region ? "yes" : "no")],
        ["Group", it.group || "-"], ["Non-species", it.non_species ? "yes" : "no"], art ? ["Artifact", art] : null, ["Queue", it.reason], ["Species labels", n + "/" + (st ? st.target : 20)]].filter(Boolean);
      u.card.append(med, el("table", { class: "wrap facts rv-facts" }, el("tbody", null, ...rows.map(r => el("tr", null, el("th", null, r[0]), el("td", { class: "rv-" + r[0].toLowerCase().replace(/[^a-z]+/g, "-") }, r[1]))))));
      const bar = el("div", { class: "bar rv-btns", role: "group", "aria-label": "verdict" });
      if (R.wrong) {
        bar.append(el("span", { class: "muted" }, "wrong:"));
        REASONS.forEach((x, i) => bar.append(el("button", { type: "button", class: "rv-reason", "data-reason": x[0], disabled: R.busy ? "" : null, onclick: () => send("wrong", x[0]) }, (i + 1) + " " + x[1])));
        bar.append(el("button", { type: "button", class: "rv-cancel", onclick: () => { R.wrong = false; render(); } }, "esc cancel"));
      } else {
        VERDICTS.forEach((v, i) => { const b = el("button", { type: "button", class: "rv-verdict", "data-verdict": v[0], onclick: () => pick(v[0]) }, (i + 1) + " " + v[0]); if (R.busy) b.disabled = true; bar.append(el("span", { class: "rv-v" }, b, Tip.tip(v[1], { label: "About: " + v[0] }))); });
        bar.append(el("button", { type: "button", class: "rv-skip", onclick: skip }, "s skip"));
      }
      u.card.append(bar);
    }
    u.species.replaceChildren();
    if (st && st.species.length) {
      u.species.append(el("table", null, el("thead", null, el("tr", null, el("th", null, "Species"), el("th", { class: "num" }, "Labels"), el("th", null, "To " + st.target))),
        el("tbody", null, ...st.species.map(s => el("tr", { "data-key": s.species_key }, el("td", null, s.species_key), el("td", { class: "num" }, s.labels),
          el("td", null, el("progress", { max: st.target, value: Math.min(s.labels, st.target) })))))));
    } else if (st) u.species.append(el("div", { class: "empty" }, "no labels"));
  }

  function take(d) { R.item = d.item; R.depth = d.queue_depth; R.wrong = false; if (!d.item) R.media = null; }
  async function loadStats() {
    const r = await call("GET", "/api/review/stats");
    if (r.ok) R.stats = r.data; else R.error = r.error;
    return r.ok;
  }
  async function loadNext() {
    const r = await call("GET", "/api/review/next");
    if (r.ok) { take(r.data); R.error = null; } else { R.item = null; R.error = r.error; }
  }
  function pick(v) { if (R.busy || !R.item) return; if (v === "wrong") { R.wrong = true; render(); } else send(v, null); }
  async function send(v, reason) {
    if (R.busy || !R.item) return;
    R.busy = true; render();
    const tk = await token();
    const body = { event_id: R.item.event_id, verdict: v, csrf: tk || "" };
    if (reason) body.wrong_reason = reason;
    const r = await call("POST", "/api/review/verdict", body);
    R.busy = false;
    if (r.ok) { take(r.data); R.error = null; await loadStats(); } else R.error = r.error;
    render();
  }
  async function skip() {
    if (R.busy || !R.item) return;
    R.busy = true; render();
    const tk = await token();
    const r = await call("POST", "/api/review/skip", { event_id: R.item.event_id, csrf: tk || "" });
    R.busy = false;
    if (r.ok) { take(r.data); R.error = null; await loadStats(); } else R.error = r.error;
    render();
  }
  function onKey(ev) {
    if (!R.root || R.root.hidden || ev.ctrlKey || ev.metaKey || ev.altKey) return;
    const t = ev.target && ev.target.tagName;
    if (t === "INPUT" || t === "SELECT" || t === "TEXTAREA" || t === "AUDIO") return;
    const k = ev.key;
    if (R.wrong) {
      if (k === "Escape") { R.wrong = false; render(); ev.preventDefault(); return; }
      const i = "12345".indexOf(k);
      if (i >= 0 && k.length === 1) { ev.preventDefault(); send("wrong", REASONS[i][0]); }
      return;
    }
    if (k === " ") { ev.preventDefault(); togglePlay(); return; }
    if (k === "r" || k === "R") { ev.preventDefault(); replay(); return; }
    const i = "1234".indexOf(k);
    if (i >= 0 && k.length === 1) { ev.preventDefault(); pick(VERDICTS[i][0]); }
    else if (k === "s" || k === "S") { ev.preventDefault(); skip(); }
  }
  function build() {
    R.root = document.getElementById("tab-review");
    R.ui = { count: el("div", { class: "rv-count" }), err: el("div", { class: "err rv-err", hidden: "" }), card: el("div", { class: "rv-card" }), species: el("div", { class: "tablewrap rv-species" }) };
    R.root.append(
      el("div", { class: "panel" }, el("h2", null, "Review", Tip.tip("Keys: space play, r replay, 1-4 verdict, s skip, esc cancel", { label: "About: Review" })), R.ui.count, R.ui.err, R.ui.card),
      el("div", { class: "panel" }, el("h2", null, "Labels per species", Tip.tip("Bar fills at 20 labels", { label: "About: Labels per species" })), R.ui.species));
    document.addEventListener("keydown", onKey);
    document.addEventListener("keyup", ev => { if (ev.key === " " && R.root && !R.root.hidden && ev.target && ev.target.tagName === "BUTTON") ev.preventDefault(); });
  }
  async function refresh() {
    if (R.busy || R.wrong) return;
    R.error = null;
    await loadStats();
    if (!R.item) await loadNext();
    render();
  }
  return { build: build, refresh: refresh, state: R };
})();
if (typeof module !== "undefined") module.exports = { Review };
