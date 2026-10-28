#!/usr/bin/env node
/* Tests for static/adapter.js: the legacy source against a recording fake of the existing bma_ui endpoints (no network, no browser), the v2 source's
   voice filter, the voice-label guard against analysis/labels.py, and the poller's request sequence. Run: node test_adapter.js
   (PASS/FAIL lines and "N checks, M failure(s)"; exit 1 on a failure). */
'use strict';
const path = require('path'), cp = require('child_process');
const A = require(path.join(__dirname, '..', 'static', 'adapter.js'));
let n = 0; const fails = [];
function check(name, ok, detail) { n++; console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok ? '' : '  [' + detail + ']')); if (!ok) fails.push(name); }
const eq = (name, got, want) => check(name, JSON.stringify(got) === JSON.stringify(want), 'got ' + JSON.stringify(got) + ' want ' + JSON.stringify(want));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(cond, ms) { const end = Date.now() + ms; while (Date.now() < end) { if (cond()) return true; await sleep(5); } return cond(); }

/* a fake of the existing endpoints: routes[path] = body | (url, opt) => body | {status, body}; every request is recorded */
function fake(routes) {
  const log = [];
  const fn = async (url, opt) => {
    opt = opt || {}; log.push({ url, method: opt.method || 'GET', body: opt.body ? JSON.parse(opt.body) : null });
    const key = url.split('?')[0], r = routes[key];
    if (r === undefined) return { ok: false, status: 404, headers: { get: () => null }, json: async () => ({ detail: 'no such resource' }), arrayBuffer: async () => new ArrayBuffer(0) };
    const v = typeof r === 'function' ? r(url, opt) : r;
    const status = v && v.status || 200, body = v && v.status ? v.body : v;
    return { ok: status < 400, status, headers: { get: (k) => (k === 'Retry-After' ? '2' : null) }, json: async () => body, arrayBuffer: async () => body };
  };
  return { fn, log };
}
const ev = (id, sp, sci, t, beam, conf, extra) => Object.assign({ id, species: sp, scientific: sci, t_start_s: t, best_beam: beam, best_confidence: conf, in_region: true }, extra);
const legacy = (routes, extra) => { const f = fake(routes); return { f, src: A.create(Object.assign({ kind: 'legacy', fetch: f.fn, base: '', now: () => 1000000 }, extra)) }; };

(async () => {
  // ---- the voice guard: a copy of labels.SPEECH_LABELS + the "human" prefix
  let py = null;
  for (const exe of [process.env.PYTHON, 'python3', 'python']) {
    if (!exe) continue;
    const r = cp.spawnSync(exe, ['-I', '-c', 'import sys, json; sys.path.insert(0, sys.argv[1]); import labels; print(json.dumps(sorted(labels.SPEECH_LABELS)))', path.join(__dirname, '..', '..', 'analysis')], { encoding: 'utf8' });
    if (r.status === 0) { py = JSON.parse(r.stdout); break; }
  }
  if (py) eq('SPEECH_LABELS equals analysis/labels.py SPEECH_LABELS (14 labels)', A.SPEECH_LABELS.slice().sort(), py);
  else check('SPEECH_LABELS equals analysis/labels.py SPEECH_LABELS: python with analysis/labels.py importable is required', false, 'no python on PATH');
  eq('14 speech labels', A.SPEECH_LABELS.length, 14);
  eq('voice labels: human vocal / human non-vocal / human whistle / padded Speech / PANNs Singing / Shout are voices', ['Human vocal', 'human non-vocal', 'HUMAN WHISTLE', '  Speech ', 'Singing', 'Shout'].map(A.isVoiceLabel), [true, true, true, true, true, true]);
  eq('voice labels: positive twin - a chickadee, a katydid, an empty and a missing label are not voices', ['Carolina Chickadee', 'Common True Katydid', '', null, undefined].map(A.isVoiceLabel), [false, false, false, false, false]);
  eq('kindFor: /next/ is legacy, / is v2, ?src= overrides both ways', [A.kindFor('/next/', ''), A.kindFor('/', ''), A.kindFor('/next/', '?src=v2'), A.kindFor('/', '?src=legacy'), A.kindFor('/next', '')], ['legacy', 'v2', 'v2', 'legacy', 'legacy']);

  // ---- legacy detections: voices dropped, the rest mapped exactly
  {
    const { f, src } = legacy({ '/api/detections/events': { events: [
      ev('e1', 'Carolina Chickadee', 'Poecile carolinensis', 999000, 3, 0.78), ev('e2', 'Human vocal', 'Human vocal', 999100, 1, 0.9), ev('e3', 'Common True Katydid', 'Pterophylla camellifolia', 999200, 6, 0.41, { in_region: false }),
      ev('e4', 'Northern Cardinal', 'Speech', 999300, 2, 0.6), ev('e5', 'Whispering', 'x', 999400, 2, 0.6)] } });
    const out = await src.clips();
    eq('legacy clips: the Human vocal, a voice scientific name and Whispering are dropped; the two animals stay, in server order', out.map((c) => c.id), ['e1', 'e3']);
    eq('legacy clips: exact mapping of the first row', out[0], { id: 'e1', title: 'Carolina Chickadee | beam 3 | 78%', test: false, truth: [], t: 999000, beam: 3, common: 'Carolina Chickadee', scientific: 'Poecile carolinensis', confidence: 0.78, in_region: true, audio: false });
    eq('legacy clips: out_of_region is kept as in_region false', out[1].in_region, false);
    eq('legacy clips: one GET, 24 h back from now, limit 200', f.log.map((r) => [r.method, r.url]), [['GET', '/api/detections/events?since=913600&limit=200']]);
  }
  // ---- the v2 source applies the same guard
  {
    const f = fake({ '/api/v2/clips': { clips: [{ id: 'scene-a', test: true, truth: [] }, { id: 'c001', test: false, common: 'Human whistle', scientific: 'x' }, { id: 'c002', test: false, common: 'Alpha Wren', scientific: 'Aus a' }] } });
    const src = A.create({ kind: 'v2', fetch: f.fn, base: '' });
    eq('v2 clips: a voice clip is dropped, a test signal and an animal stay', (await src.clips()).map((c) => c.id), ['scene-a', 'c002']);
  }
  // ---- legacy review queue
  const ITEM = { event_id: '1791997838000-poecile-carolinensis', species: { common: 'Carolina Chickadee', scientific: 'Poecile carolinensis' }, confidence: 0.78, beams: [1, 2], best_beam: 2, t_start_s: 1791997838, in_region: true,
    group: 'bird', non_species: false, reason: 'near', clip_png: '/api/review/clip?id=C1', clip_audio: '/api/review/audio?id=C1', clip_span: { clip_start_s: 1791997837, clip_end_s: 1791997843, event_start_s: 1791997838.5, event_end_s: 1791997841 } };
  {
    const { src } = legacy({ '/api/review/next': { item: ITEM, queue_depth: 37 } });
    const items = await src.reviewItems();
    eq('legacy review: one item, event span relative to the clip start, full band, claim kept for the blind list', items, [{ id: ITEM.event_id, clip: ITEM.event_id, audio: '/api/review/audio?id=C1', png: '/api/review/clip?id=C1', t0: 1.5, t1: 4, f0: 100, f1: 15500, span: true,
      model: { species: 'Carolina Chickadee', conf: 0.78, beam: 2 }, reason: 'near', artifact: null }]);
    eq('legacy review: the queue depth is the server one', src.depth, 37);
  }
  {
    const { src } = legacy({ '/api/review/next': { item: Object.assign({}, ITEM, { species: { common: 'Human vocal', scientific: 'Human vocal' } }), queue_depth: 5 } });
    eq('legacy review: a voice item is never offered (empty list), depth still reported', [await src.reviewItems(), src.depth], [[], 5]);
  }
  {
    const { src } = legacy({ '/api/review/next': { item: null, queue_depth: 0 } });
    eq('legacy review: an empty queue is an empty list', [await src.reviewItems(), src.depth], [[], 0]);
  }
  // ---- verdict / skip: exact request bodies, csrf from /api/session once, the next item comes back
  {
    const NEXT = Object.assign({}, ITEM, { event_id: '1791997900000-cyanocitta-cristata', species: { common: 'Blue Jay', scientific: 'Cyanocitta cristata' }, clip_audio: undefined, clip_span: undefined });
    const { f, src } = legacy({ '/api/session': { csrf: 'tok123' }, '/api/review/verdict': { item: NEXT, queue_depth: 36, recorded: true }, '/api/review/skip': { item: null, queue_depth: 35 } });
    const item = { id: ITEM.event_id };
    const next = await src.verdict(item, 'wrong', 'mechanical', { ignored: true }, { ignored: true });
    eq('legacy verdict: POST {event_id, verdict, csrf, wrong_reason}; the proposal record and evidence are not sent (verdict-labels 1.1.0 has no evidence block yet)', f.log.filter((r) => r.method === 'POST').map((r) => [r.url, r.body]),
      [['/api/review/verdict', { event_id: ITEM.event_id, verdict: 'wrong', csrf: 'tok123', wrong_reason: 'mechanical' }]]);
    eq('legacy verdict: the answer is the next item (no audio, no span) and depth 36', [next.map((x) => [x.id, x.audio, x.png, x.span, x.t1]), src.depth], [[['1791997900000-cyanocitta-cristata', null, '/api/review/clip?id=C1', false, 0]], 36]);
    await src.verdict(item, 'correct', null);
    eq('legacy verdict: a verdict that is not wrong sends no wrong_reason key; the csrf token was read once (one /api/session GET)', [f.log[f.log.length - 1].body, f.log.filter((r) => r.url === '/api/session').length],
      [{ event_id: ITEM.event_id, verdict: 'correct', csrf: 'tok123' }, 1]);
    const after = await src.skip(item);
    eq('legacy skip: POST {event_id, csrf}, an empty queue answers []', [f.log[f.log.length - 1].url, f.log[f.log.length - 1].body, after], ['/api/review/skip', { event_id: ITEM.event_id, csrf: 'tok123' }, []]);
  }
  {
    const { src } = legacy({ '/api/session': { csrf: 'c' }, '/api/review/verdict': { status: 422, body: { detail: 'not_labelable' } } });
    let msg = '';
    try { await src.verdict({ id: 'x' }, 'correct', null); } catch (e) { msg = e.message + '|' + e.status; }
    eq('legacy verdict: a refusal rejects with the problem detail and status (nothing is treated as saved)', msg, '/api/review/verdict 422: not_labelable|422');
  }
  // ---- legacy audio
  {
    const buf = new ArrayBuffer(8);
    const { f, src } = legacy({ '/api/review/audio': buf });
    const got = await src.clipAudio('x', { audio: '/api/review/audio?id=C1', model: { beam: 4 } });
    eq('legacy clipAudio: the item audio URL is fetched and the beam of the clip is reported', [f.log[0].url, got.bytes === buf, got.beam], ['/api/review/audio?id=C1', true, 4]);
    let m = ''; try { await src.clipAudio('x', { audio: null, model: { beam: 1 } }); } catch (e) { m = e.message; }
    eq('legacy clipAudio: an item without audio is refused', m, 'no audio for x');
  }
  // ---- status
  {
    const STATUS = { any_stale: false, bridge: { state: 'ok', loss_pct: 0.31, uptime_s: 1234 }, analysis: { uptime_s: 3600, plugins: { dsp: { enabled: true }, perch: { enabled: false }, tags: { enabled: true } } },
      board: { reachable: true, enabled: true, temp_local_c: 41.5 }, spectrum: { gated_now: true } };
    const { f, src } = legacy({ '/api/status': STATUS, '/api/detections/events': { total: 17, events: [] } });
    eq('legacy status: exact normalisation (loss, uptime from the analyzer, plugins on/off, board, detections last hour, clips unknown = null)', await src.status(),
      { t: 1000000, state: 'live', uptime_s: 3600, rx_loss_pct: 0.31, plugins: { dsp: 'on', perch: 'off', tags: 'on' }, detections_1h: 17, clips: { stored_24h: null, audio: null }, board: { state: 'ok', temp_c: 41.5 }, listening: false, gated_now: true });
    eq('legacy status: detections_1h asks for one hour back, limit 1', f.log.map((r) => r.url).sort(), ['/api/detections/events?since=996400&limit=1', '/api/status']);
  }
  {
    const { src } = legacy({ '/api/status': { any_stale: true, bridge: null, analysis: null, board: { reachable: false, enabled: false, temp_local_c: null } }, '/api/detections/events': { status: 500, body: {} } });
    eq('legacy status: missing sources give nulls (never zeros), a stale source is stale, a disabled board is off, a failing events call leaves detections_1h null', await src.status(),
      { t: 1000000, state: 'stale', uptime_s: null, rx_loss_pct: null, plugins: {}, detections_1h: null, clips: { stored_24h: null, audio: null }, board: { state: 'off', temp_c: null }, listening: false, gated_now: false });
  }
  // ---- the poller
  const b64 = (u8) => Buffer.from(u8).toString('base64');
  const row = (idx, t, v) => ({ idx, t_s: t, flags: 0, gated: false, bins: b64(new Uint8Array(46).fill(v)), levels: [100, 101, 102, 103, 104, 105, 106, 107] });
  const spec = (rows, o) => Object.assign({ state: 'live', n_bins: 46, rev: 1, next_since: rows.length ? rows[rows.length - 1].idx : 0, gated_now: false, rows }, o);
  function handlers() { const h = { rows: [], states: [], gates: [], resets: 0, modes: [] }; h.mode = (m) => h.modes.push(m); h.row = (t, cols, lv) => h.rows.push([t, cols.map((c) => (c ? c[0] : null)), lv]); h.state = (s) => h.states.push(s); h.gate = (g) => h.gates.push(g); h.reset = () => { h.resets++; }; h.event = () => {}; return h; }
  {
    let call = 0;
    const { f, src } = legacy({ '/api/spectrum': () => (++call === 1 ? spec([row(1, 10.0, 60), row(2, 10.1, 62)]) : spec([row(3, 10.2, 64)])) }, { pollMs: 5 });
    const h = handlers(), live = src.live(h);
    await until(() => h.rows.length >= 3, 2000); live.stop();
    eq('poller: first request is beam 0 with levels and no cursor; the second continues from next_since with the rev', f.log.slice(0, 2).map((r) => r.url), ['/api/spectrum?beam=0&compare=true', '/api/spectrum?beam=0&compare=true&since=2&rev=1']);
    eq('poller: three rows in order, bins only in slot 0 (the polled beam), t exact, levels = -100 + 0.5 * quantised value', h.rows, [[10.0, [60, ...new Array(8).fill(null)], [-50, -49.5, -49, -48.5, -48, -47.5, -47, -46.5]],
      [10.1, [62, ...new Array(8).fill(null)], [-50, -49.5, -49, -48.5, -48, -47.5, -47, -46.5]], [10.2, [64, ...new Array(8).fill(null)], [-50, -49.5, -49, -48.5, -48, -47.5, -47, -46.5]]]);
    eq('poller: state live reported, gate false, no reset', [h.states[0], h.gates[0], h.resets], ['live', false, 0]);
  }
  {
    const { f, src } = legacy({ '/api/spectrum': (url) => spec([row(1, 5.0, 70)], { rev: 1 }) }, { pollMs: 5 });
    const h = handlers(), live = src.live(h);
    await until(() => h.rows.length >= 1, 2000);
    live.setBeam(3);
    await until(() => f.log.some((r) => r.url.includes('beam=3')), 2000); live.stop();
    const b3 = f.log.find((r) => r.url.includes('beam=3'));
    eq('poller: switching beam resets the shell once and asks beam 3 from the start of the ring (no since)', [h.resets, b3.url], [1, '/api/spectrum?beam=3&compare=true']);
    live.setBeam(3);
    eq('poller: positive twin - asking for the beam already polled does not reset again', h.resets, 1);
  }
  {
    let call = 0;
    const { f, src } = legacy({ '/api/spectrum': () => { call++; return call === 1 ? spec([row(1, 1.0, 50)], { rev: 1 }) : call === 2 ? spec([], { rev: 2, next_since: 1 }) : spec([row(1, 1.0, 51)], { rev: 2 }); } }, { pollMs: 5 });
    const h = handlers(), live = src.live(h);
    await until(() => h.rows.length >= 2, 2000); live.stop();
    eq('poller: a revision change (rows re-gated) resets the shell and re-reads the ring from the start', [h.resets, h.rows.map((r) => r[1][0]), f.log[2].url], [1, [50, 51], '/api/spectrum?beam=0&compare=true']);
  }
  {
    const { src } = legacy({ '/api/spectrum': { state: 'auth_required', n_bins: 0, rev: 0, next_since: 0, rows: [] } }, { pollMs: 5, slowMs: 5 });
    const h = handlers(), live = src.live(h);
    await until(() => h.states.length >= 2, 2000); live.stop();
    eq('poller: auth_required is reported as the state and produces no rows', [h.states[0], h.rows.length], ['auth_required', 0]);
  }
  {
    const { src } = legacy({ '/api/spectrum': { status: 429, body: { detail: 'slow down' } } }, { pollMs: 5, slowMs: 5 });
    const h = handlers(), live = src.live(h);
    await until(() => h.states.length >= 1, 2000); live.stop();
    eq('poller: a 429 is the state unavailable', h.states[0], 'unavailable');
  }
  {
    const { src } = legacy({ '/api/spectrum': spec([Object.assign(row(1, 1.0, 50), { gated: true })], { gated_now: true }) }, { pollMs: 5 });
    const h = handlers(), live = src.live(h);
    await until(() => h.gates.length >= 1, 2000); live.stop();
    eq('poller: gated_now is passed on (the shell shows "speech suppressed")', h.gates[0], true);
  }
  {
    const { src } = legacy({ '/api/spectrum': spec([Object.assign(row(1, 1.0, 50), { bins: b64(new Uint8Array(45).fill(50)) }), row(2, 1.1, 51)]) }, { pollMs: 5 });
    const h = handlers(), live = src.live(h);
    await until(() => h.rows.length >= 1, 2000); live.stop();
    eq('poller: a row with the wrong bin count is skipped, the next one is kept', h.rows.map((r) => r[0]).slice(0, 1), [1.1]);
  }
  // ---- the live socket (ui-live 1.0.0) first, polling as the fallback
  class FakeWS {
    constructor(url) { this.url = url; FakeWS.all.push(this); this.closed = false; }
    close() { this.closed = true; }
  }
  FakeWS.all = [];
  const frame = (seq, t, flags, fills, extra) => { const u = new Uint8Array(24 + 9 * 46), dv = new DataView(u.buffer); u.set([0x42, 0x4d, 0x57, 0x31, 1, 9]); dv.setUint16(6, 46, true); dv.setUint32(8, seq, true); dv.setBigUint64(12, BigInt(Math.round(t * 1e6)), true); dv.setUint16(20, flags, true);
    for (let b = 0; b < 9; b++) u.fill(fills[b], 24 + b * 46, 24 + (b + 1) * 46); return extra ? extra(u) : u; };
  const hello = JSON.stringify({ k: 'hello', v: 1, tier: 'standard', beams: 9, n_bins: 46, rows_per_s: 10 });
  const FILLS = [10, 11, 12, 13, 14, 15, 16, 17, 18];
  {
    FakeWS.all.length = 0;
    const { f, src } = legacy({ '/api/spectrum': spec([row(1, 5.0, 70)]) }, { pollMs: 5, WebSocket: FakeWS, wsUrl: (p) => 'ws://h' + p });
    const h = handlers(), live = src.live(h);
    const w = FakeWS.all[0];
    eq('socket first: one WebSocket to /api/v2/live is opened and no /api/spectrum request is made before the hello', [FakeWS.all.length, w.url, f.log.length, src.caps.tiles, src.caps.push], [1, 'ws://h/api/v2/live', 0, false, false]);
    w.onmessage({ data: hello });
    eq('socket: the hello turns the tiles on (9 beams), resets the shell once, reports live and mode ws', [src.caps.tiles, src.caps.push, h.resets, h.states, h.modes], [true, true, 1, ['live'], ['ws']]);
    w.onmessage({ data: frame(7, 12.5, 0, FILLS).buffer });
    eq('socket: a BMW1 frame gives one row, t in seconds, all 9 beams present with their own bytes, no levels', h.rows, [[12.5, FILLS.map((v) => v), null]]);
    w.onmessage({ data: frame(8, 12.6, 1, FILLS).buffer });
    eq('socket: every frame reports its gate: flags 0 is not gated, flag bit 0 (the next frame) is gated', h.gates, [false, true]);
    w.onmessage({ data: frame(9, 12.7, 0, FILLS).buffer });
    eq('socket: positive twin - the frame after it, flags 0 again, reports the gate off', h.gates, [false, true, false]);
    const bad = frame(10, 12.8, 0, FILLS); bad[4] = 2;
    const short = frame(11, 12.9, 0, FILLS).slice(0, 400);
    const before = h.rows.length;
    w.onmessage({ data: bad.buffer }); w.onmessage({ data: short.buffer }); w.onmessage({ data: new Uint8Array(0).buffer });
    eq('socket: a frame of type 2, a short frame and an empty frame are ignored (no row)', h.rows.length - before, 0);
    w.onmessage({ data: JSON.stringify({ k: 'reset', rev: 4 }) });
    eq('socket: a reset text frame resets the shell (twice now: hello, reset)', h.resets, 2);
    w.onmessage({ data: 'not json' });
    eq('socket: a text frame that is not JSON is ignored', h.resets, 2);
    live.setBeam(3);
    eq('socket: choosing a beam changes nothing (all beams arrive): no reset, no request', [h.resets, f.log.length], [2, 0]);
    w.onclose();
    await sleep(30);
    eq('socket dropped after the hello: the poller takes over (mode poll, tiles off, a /api/spectrum request for beam 3) and a new socket is scheduled', [h.modes, src.caps.tiles, f.log.some((r) => r.url.startsWith('/api/spectrum?beam=3')), FakeWS.all.length], [['ws', 'poll'], false, true, 1]);
    live.stop();
    eq('stop closes the socket and the poller (no new socket after stop)', [w.closed, FakeWS.all.length], [true, 1]);
  }
  {
    FakeWS.all.length = 0;
    const { f, src } = legacy({ '/api/spectrum': spec([row(1, 5.0, 70)]) }, { pollMs: 5, WebSocket: FakeWS, wsUrl: (p) => 'ws://h' + p });
    const h = handlers(), live = src.live(h);
    FakeWS.all[0].onclose();                                  // the upgrade was refused (401, 403, 503, 404): the socket closes without a hello
    await until(() => h.rows.length >= 1, 2000); live.stop();
    eq('socket refused (closed before any hello): mode poll, tiles stay off, the page polls beam 0 and gets its rows', [h.modes, src.caps.tiles, f.log[0].url, h.rows[0][1][0]], [['poll'], false, '/api/spectrum?beam=0&compare=true', 70]);
  }
  {
    FakeWS.all.length = 0;
    const { f, src } = legacy({ '/api/spectrum': spec([row(1, 5.0, 70)]) }, { pollMs: 5, WebSocket: FakeWS, wsUrl: (p) => 'ws://h' + p, noSocket: true });
    const h = handlers(), live = src.live(h);
    await until(() => h.rows.length >= 1, 2000); live.stop();
    eq('noSocket (page option ?ws=0): no WebSocket is created, the page polls', [FakeWS.all.length, h.modes, h.rows.length >= 1], [0, ['poll'], true]);
  }
  console.log('\n' + n + ' checks, ' + fails.length + ' failure(s)');
  process.exit(fails.length ? 1 : 0);
})().catch((e) => { console.log('FAIL exception ' + (e && e.stack || e)); process.exit(1); });
