/* Data-source adapter: the one place the dashboard shell learns where its data comes from.
   "v2"     the mock server's proposed surface (ui-next/mock_server.py): WebSocket frames, clip list, 8-channel clip audio, in-memory verdicts.
   "legacy" the EXISTING bma_ui endpoints (ui-http 1.11.0, served under /next/ by --ui-next): /api/spectrum polled, /api/detections/events, /api/review/*,
            /api/status, /api/system. Nothing here needs a server change.
   Human voices never reach the shell: every label that labels.is_speech_label would refuse (a copy of SPEECH_LABELS plus the "human" prefix, pinned by
   test_adapter.py) is dropped here, whatever the server sent. Runs in the browser and in Node (tests). Original code. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LabAdapter = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';
  const SPEECH_LABELS = ['Speech', 'Male speech, man speaking', 'Female speech, woman speaking', 'Child speech, kid speaking', 'Conversation', 'Narration, monologue', 'Babbling',
    'Speech synthesizer', 'Shout', 'Yell', 'Whispering', 'Chatter', 'Hubbub, speech noise, speech babble', 'Singing'];
  const SPEECH_SET = new Set(SPEECH_LABELS.map((s) => s.toLowerCase()));
  const HUMAN_PREFIX = 'human';
  const isVoiceLabel = (name) => { const n = String(name == null ? '' : name).trim().toLowerCase(); return n.startsWith(HUMAN_PREFIX) || SPEECH_SET.has(n); };
  const voiceFree = (...names) => !names.some(isVoiceLabel);
  const EDGES_HZ = [50.0, 250.0, 315.0, 396.9, 500.0, 630.0, 793.7, 1000.0, 1259.9, 1587.4, 2000.0, 2118.9, 2244.9, 2378.4, 2519.8, 2669.7, 2828.4, 2996.6, 3174.8, 3363.6, 3563.6,
    3775.5, 4000.0, 4237.9, 4489.8, 4756.8, 5039.7, 5339.4, 5656.9, 5993.2, 6349.6, 6727.2, 7127.2, 7551.0, 8000.0, 8475.7, 8979.7, 9513.7, 10079.4, 10678.7, 11313.7, 11986.5,
    12699.2, 13454.3, 14254.4, 15102.0, 16000.0];
  const BINS = 46, DB_FLOOR = -100, DB_STEP = 0.5, POLL_MS = 500, POLL_SLOW_MS = 5000, EVENT_LIMIT = 200, REVIEW_F0 = 100, REVIEW_F1 = 15500;

  const b64ToU8 = (s) => { if (typeof atob === 'function') { const bin = atob(s), u = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i); return u; } return new Uint8Array(Buffer.from(s, 'base64')); };
  const num = (v) => (typeof v === 'number' && isFinite(v) ? v : null);

  /* one BMW1 binary frame (contract ui-live 1.0.0) -> {t (s), seq, flags, cols[9] of Uint8Array(46)} or null when it is not a valid frame */
  function parseBmw1(u8) {
    if (u8.length < 24) return null;
    const dv = new DataView(u8.buffer, u8.byteOffset, u8.byteLength);
    if (dv.getUint32(0, true) !== 0x31574d42 || u8[4] !== 1 || u8[5] !== 9 || dv.getUint16(6, true) !== BINS || u8.length !== 24 + 9 * BINS) return null;   // 'BMW1', row, 9 beams, 46 bins
    const cols = []; for (let b = 0; b < 9; b++) cols.push(u8.subarray(24 + b * BINS, 24 + (b + 1) * BINS));
    return { t: Number(dv.getBigUint64(12, true)) / 1e6, seq: dv.getUint32(8, true), flags: dv.getUint16(20, true), cols };
  }

  /* ------------------------------------------------------------------ transport: JSON over fetch, problem+json errors, no cookies of our own */
  function http(fetchFn, base) {
    const url = (p) => base + p;
    async function raw(path, opt) {
      let r;
      try { r = await fetchFn(url(path), Object.assign({ cache: 'no-store', credentials: 'same-origin' }, opt)); } catch (e) { const x = new Error('network: ' + (e && e.message || 'failed')); x.status = 0; throw x; }
      return r;
    }
    async function json(path, opt) {
      const r = await raw(path, opt);
      if (!r.ok) { let d = ''; try { const b = await r.json(); d = b.detail || b.title || ''; } catch (e) { /* not json */ } const x = new Error(path.split('?')[0] + ' ' + r.status + (d ? ': ' + d : '')); x.status = r.status; x.retryAfter = +(r.headers && r.headers.get && r.headers.get('Retry-After')) || 0; throw x; }
      return r.json();
    }
    return { raw, json };
  }

  /* ------------------------------------------------------------------ v2: the mock server's surface */
  function createV2(o) {
    const h = http(o.fetch, o.base), WS = o.WebSocket, wsUrl = o.wsUrl;
    return {
      kind: 'v2', caps: { push: true, tiles: true, listen: true, clipList: true, audio8: true, skip: true },
      async status() { return h.json('/api/v2/status'); },
      async clips() { return (await h.json('/api/v2/clips')).clips.filter((c) => voiceFree(c.common, c.scientific)); },
      async clipAudio(id) { const r = await h.raw('/api/v2/clips/' + id + '/beams.wav'); if (!r.ok) throw new Error('clip ' + id + ' ' + r.status); return { bytes: await r.arrayBuffer(), beam: null }; },
      async reviewItems(clips) { return clips.filter((c) => c.claim).map((c) => ({ id: c.id, clip: c.id, t0: c.claim.t0, t1: c.claim.t1, f0: c.claim.f0, f1: c.claim.f1, model: { species: c.common, conf: c.confidence, beam: c.beam } })); },
      async verdict(item, v, wrongReason, record, evidence) { await h.json('/api/v2/review/verdict', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: item.id, verdict: v, record, proposed_evidence: evidence }) }); return null; },
      async skip(item) { await h.json('/api/v2/review/skip', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: item.id }) }); return null; },
      /* h: {row(tSec, cols[9] of Uint8Array(46)), event(ev), state(s)}; WebSocket 'BMW1' frames, 9 beams each */
      live(hd) {
        let ws = null, backoff = 500, closed = false;
        const connect = () => {
          ws = new WS(wsUrl('/api/v2/live')); ws.binaryType = 'arraybuffer';
          ws.onopen = () => { backoff = 500; };
          ws.onmessage = (e) => {
            if (typeof e.data === 'string') { const ev = JSON.parse(e.data); if (ev.k === 'event' && voiceFree(ev.common)) hd.event({ t: Date.now() / 1000, common: ev.common, beam: ev.beam, confidence: ev.confidence }); return; }
            const f = parseBmw1(new Uint8Array(e.data)); if (f) hd.row(f.t, f.cols, null);
          };
          ws.onclose = () => { if (!closed) { setTimeout(connect, backoff); backoff = Math.min(8000, backoff * 2); } };
        };
        connect();
        return { setBeam() {}, stop() { closed = true; if (ws) ws.close(); } };
      },
    };
  }

  /* ------------------------------------------------------------------ legacy: the existing endpoints */
  function createLegacy(o) {
    const h = http(o.fetch, o.base), now = o.now || (() => Date.now() / 1000), pollMs = o.pollMs || POLL_MS, slowMs = o.slowMs || POLL_SLOW_MS;
    let csrf = null;
    const token = async () => { if (csrf) return csrf; try { csrf = (await h.json('/api/session')).csrf || ''; } catch (e) { csrf = ''; } return csrf; };
    const eventRow = (e) => ({ id: e.id, title: e.species + ' | beam ' + e.best_beam + ' | ' + Math.round(100 * e.best_confidence) + '%', test: false, truth: [], t: e.t_start_s, beam: e.best_beam,
      common: e.species, scientific: e.scientific, confidence: e.best_confidence, in_region: e.in_region !== false, audio: false });
    const reviewItem = (it) => {
      if (!it || !voiceFree(it.species && it.species.common, it.species && it.species.scientific)) return null;
      const sp = it.clip_span, t0 = sp ? Math.max(0, sp.event_start_s - sp.clip_start_s) : 0, t1 = sp ? Math.max(t0, sp.event_end_s - sp.clip_start_s) : 0;
      return { id: it.event_id, clip: it.event_id, audio: it.clip_audio || null, png: it.clip_png || null, t0, t1: sp ? t1 : 0, f0: REVIEW_F0, f1: REVIEW_F1, span: !!sp,
        model: { species: it.species.common, conf: it.confidence, beam: it.best_beam }, reason: it.reason, artifact: it.artifact_flags || null };
    };
    return {
      kind: 'legacy', caps: { push: false, tiles: false, listen: false, clipList: false, audio8: false, skip: true },   // push and tiles turn true while the live socket carries the rows
      async status() {
        const t = now(), [st, ev] = await Promise.all([h.json('/api/status'), h.json('/api/detections/events?since=' + Math.floor(t - 3600) + '&limit=1').catch(() => null)]);
        const br = st.bridge || {}, an = st.analysis || {}, bd = st.board || {};
        const plugins = {}; for (const [k, p] of Object.entries(an.plugins || {})) plugins[k] = p && p.enabled === false ? 'off' : 'on';
        return { t, state: st.any_stale ? 'stale' : 'live', uptime_s: num(an.uptime_s) ?? num(br.uptime_s), rx_loss_pct: num(br.loss_pct), plugins,
          detections_1h: ev ? num(ev.total) : null, clips: { stored_24h: null, audio: null }, board: { state: bd.reachable ? 'ok' : bd.enabled === false ? 'off' : 'down', temp_c: num(bd.temp_local_c) },
          listening: false, gated_now: !!(st.spectrum && st.spectrum.gated_now) };
      },
      async clips() { const r = await h.json('/api/detections/events?since=' + Math.floor(now() - 86400) + '&limit=' + EVENT_LIMIT); return r.events.filter((e) => voiceFree(e.species, e.scientific)).map(eventRow); },
      async clipAudio(id, item) { if (!item || !item.audio) throw new Error('no audio for ' + id); const r = await h.raw(item.audio); if (!r.ok) throw new Error('audio ' + r.status); return { bytes: await r.arrayBuffer(), beam: item.model.beam }; },
      /* one item at a time, the server decides the order; the queue depth rides along */
      async reviewItems() { const d = await h.json('/api/review/next'); const it = reviewItem(d.item); this.depth = d.queue_depth; return it ? [it] : []; },
      async verdict(item, v, wrongReason) {
        const body = { event_id: item.id, verdict: v, csrf: await token() }; if (wrongReason) body.wrong_reason = wrongReason;
        const d = await h.json('/api/review/verdict', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        this.depth = d.queue_depth; const nx = reviewItem(d.item); return nx ? [nx] : [];
      },
      async skip(item) {
        const d = await h.json('/api/review/skip', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ event_id: item.id, csrf: await token() }) });
        this.depth = d.queue_depth; const nx = reviewItem(d.item); return nx ? [nx] : [];
      },
      /* hd: {row(tSec, cols[9]: Uint8Array(46) per beam, or only the polled beam with the rest null; levelsDb: 8 dB values or null), event(ev), state(s), gate(b), reset(), mode('ws'|'poll')}
         The socket (/api/v2/live, ui-live 1.0.0) is tried first; a refused or dropped socket falls back to polling /api/spectrum and the socket is retried with a growing delay. */
      live(hd) {
        const src = this, poller = pollLive(hd);
        let ws = null, up = false, closed = false, retry = 2000, retryTimer = 0;
        const setMode = (m) => { src.caps.push = src.caps.tiles = m === 'ws'; hd.mode(m); };
        const startPoll = () => { setMode('poll'); poller.start(); };
        const connect = () => {
          if (closed) return;
          up = false; ws = new o.WebSocket(o.wsUrl('/api/v2/live')); ws.binaryType = 'arraybuffer';
          ws.onmessage = (e) => {
            if (typeof e.data === 'string') {
              let m; try { m = JSON.parse(e.data); } catch (x) { return; }
              if (m.k === 'hello') { up = true; retry = 2000; poller.stop(); hd.reset(); setMode('ws'); hd.state('live'); } else if (m.k === 'reset') hd.reset();
              return;
            }
            const f = parseBmw1(new Uint8Array(e.data)); if (!f) return;
            hd.gate(!!(f.flags & 1)); hd.row(f.t, f.cols, null);
          };
          ws.onclose = () => { if (closed) return; startPoll(); retryTimer = setTimeout(connect, retry); retry = Math.min(60000, retry * 2); };
        };
        if (o.WebSocket && o.wsUrl && !o.noSocket) { poller.hold(); connect(); } else startPoll();
        return { setBeam(b) { poller.setBeam(b); }, stop() { closed = true; clearTimeout(retryTimer); poller.stop(); if (ws) ws.close(); } };
      },
    };
    function pollLive(hd) {
      {
        let beam = 0, since = 0, rev = 0, gen = 0, timer = 0, busy = false, stopped = true, delay = pollMs, fresh = true;
        const reset = () => { gen++; since = 0; rev = 0; hd.reset(); };
        async function tick() {
          if (stopped || busy) return; busy = true; const my = gen;
          try {
            const q = '/api/spectrum?beam=' + beam + '&compare=true' + (since > 0 ? '&since=' + since + '&rev=' + rev : '');
            const b = await h.json(q); if (my !== gen) return;
            hd.state(b.state); hd.gate(!!b.gated_now); delay = pollMs;
            if (b.state === 'auth_required' || b.state === 'disabled' || !b.n_bins) { delay = slowMs; return; }
            if (b.rev !== rev && since > 0) { reset(); delay = 0; return; }              // rows already shown were re-gated: draw them again from the start of the ring
            rev = b.rev;
            for (const r of b.rows) {
              const bins = b64ToU8(r.bins); if (bins.length !== BINS) continue;
              const cols = new Array(9).fill(null); cols[beam] = bins;
              hd.row(r.t_s, cols, r.levels ? r.levels.map((v) => DB_FLOOR + DB_STEP * v) : null);
            }
            if (b.next_since > 0) since = b.next_since;
          } catch (e) {
            hd.state('unavailable'); delay = e.status === 429 ? Math.max(1000, 1000 * (e.retryAfter || 1)) : slowMs;
          } finally { busy = false; if (!stopped) timer = setTimeout(tick, delay); }
        }
        return { hold() { stopped = true; }, start() { if (!stopped) return; stopped = false; if (fresh) { fresh = false; gen++; since = 0; rev = 0; } else reset(); clearTimeout(timer); timer = setTimeout(tick, 0); },
          setBeam(b) { if (b === beam) return; beam = b; if (stopped) return; reset(); clearTimeout(timer); timer = setTimeout(tick, 0); }, stop() { stopped = true; clearTimeout(timer); } };
      }
    }
  }

  /* the kind follows where the page was served from: /next/ is bma_ui (legacy), any other path is the mock server; ?src=legacy|v2 overrides */
  function kindFor(pathname, search) { const q = new URLSearchParams(search || ''); const s = q.get('src'); return s === 'v2' || s === 'legacy' ? s : /^\/next(\/|$)/.test(pathname || '') ? 'legacy' : 'v2'; }
  function create(o) { return (o.kind || kindFor(o.pathname, o.search)) === 'legacy' ? createLegacy(o) : createV2(o); }

  return { create, kindFor, isVoiceLabel, SPEECH_LABELS, EDGES_HZ, BINS };
});
