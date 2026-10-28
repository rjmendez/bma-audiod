# Migration plan

Rule for every phase: the ui-http contract tests, the auth and passkey surface (`ui/auth.py`, `ui/passkeys.py`) and the label store (`analysis/labels.py`) stay as they are until a phase says otherwise. Each phase ships behind a flag that is a registry row (`scripts/test_features_registry.py`).

| Phase | Ships | Touches | Contract | Tests |
|---|---|---|---|---|
| 0 | this change: `ui-next/` shell, engine, mock server, tests, docs. No serving change | `ui-next/`, `docs/ui-next/`, CI | none | the suites in README of ui-next, registered in `ci.yml` |
| 1 (built, ui-http 1.12.0) | the shell under `/next/` behind `--ui-next`, reading EXISTING endpoints through a data-source adapter (`/api/spectrum` polled, `/api/detections/*`, `/api/review/*`, `/api/status`, `/api/system`) | `ui/bma_ui.py` (asset routes), new `ui-next/static/adapter.js`, feature registry | ui-http minor: new static paths only | asset list and CSP tests, `test_browser.py` per view against the adapter, screenshot diff |
| 2 (built, ui-http 1.13.0, ui-live 1.0.0) | `GET /api/v2/live` WebSocket: stdlib RFC 6455 on the existing Handler, binary `BMW1` frames, ping/pong, 60 s backlog from the spectrum ring, auth on upgrade, Origin check, connection cap, write timeout. `/api/spectrum` stays | `ui/bma_ui.py`, new `ui/ws.py`, `contracts/` (new `ui-live` 1.0.0 with reference builder and fixtures), `pc/bma_spectrum.py` ring access | new interface `ui-live`, ui-http minor | handshake, framing, masking, oversize, slow client, unauthenticated upgrade 401, wrong Origin 403, byte-exact frame fixture, mutation per rule |
| 3 | event push (text frames) and incremental `/api/v2/events?since=`; the 7 s refetch of 500 events goes away | `ui/bma_ui.py`, `analysis/events.py` cursor | ui-live minor, ui-http minor | cursor correctness, no gap after reconnect, no duplicate rows |
| 4 | review parity and cut-over: old tabs behind `--ui-classic`, then removed after the owner accepts; `ui/app.js` and its DOM suites retired | `ui/` (assets removed), `ui/test_dom_*.py` replaced by per-view suites | ui-http major | per-view suites replace the old ones before deletion |
| 5 | verdict `evidence` block: contract `verdict-labels` 1.2.0 (DATA-MODEL.md) and the writer in `analysis/labels.py` | `contracts/specs/verdict-labels.md`, schema, `analysis/labels.py`, `ui-next/static/review.js` | verdict-labels minor | `test_verdict_contract.py` flips from "1.1.0 rejects" to "1.2.0 accepts", `analysis/test_labels.py` |
| 6 | live listening: flag `--live-listen` (default OFF), only after the owner decides OPEN-CHOICES.md | new listen route in `ui/`, `pc/` PCM source, audit log, speech mute | new interface and a privacy review (today BMAA audio goes only to `pc/bma_spectrum.py`) | consent, token expiry, one listener, no server copy, mute on speech |

Phase 1 as built:

* Flag `--ui-next`, registry row `Next dashboard`, default OFF; the supervisor adds it when `<root>\enable_ui_next.txt` exists (the same switch-file mechanism as passkeys), the installer copies the assets to `<ui dir>
ext`, the image to `/app/ui/next`. Without it every `/next/` path is 404. With it and without the assets the server refuses to start. Assets are read from `<ui dir>/next` or `../ui-next/static`.
* One data seam, `ui-next/static/adapter.js`: `v2` (mock server) and `legacy` (this server, chosen by the `/next/` path). Legacy reads `/api/spectrum` (the chosen beam, polled every 500 ms, level chips from `compare=true`), `/api/detections/events`, `/api/review/next|verdict|skip`, `/api/status`, `/api/session` (csrf). Nothing server-side was added.
* What legacy does not have, and the shell shows instead: no per-beam strips (one beam is polled; a beam switch re-reads the 60 s ring), no listening (button hidden, reason shown), no clip list (Clips tab hidden), detections have no Open button, review clips are mono (one beam enabled) or PNG only, the verdict goes through the existing 1.1.0 writer (the evidence block waits for phase 5), unknown figures read `n/a`.
* Voice rule: the adapter drops any label `labels.is_speech_label` would refuse (list pinned to `analysis/labels.py` by `test_adapter.js`), whatever the server sent; `speech suppressed` shows while rows are gated.
* CSP: base CSP plus `media-src 'self' blob:` on `/next/` only (Keep-pitch playback). Auth, passkeys and labels untouched; `/next/` needs the same login (sign in at `/` first, the cookie covers it).

Phase 2 as built:

* `ui/ws.py` (stdlib RFC 6455) and the route `GET /api/v2/live` in `bma_ui.py`, only with `--ui-next` (no new flag). Contract `ui-live` 1.0.0: `contracts/specs/ui-live.md`, `schemas/ui_live.schema.json`, reference in `ref_binary.py`, fixtures `ui_live_examples.json`. `/api/spectrum` unchanged.
* Order of refusals: 405, 422 (query), 404 (no feed), 401 (login, same cookie; an open server is not authenticated), 403 Origin, 426 / 400 (upgrade), 503 (8 sockets). Client frames: masked close / ping / pong only (1002, 1003, 1009). Ping every 15 s, 30 s without a pong = close 1008, 5 s write timeout drops a client that does not read. No per-client queue: a connection holds a row cursor.
* Frames: `hello`, the 60 s backlog, then one 438-byte `BMW1` frame per row; text `reset` when the tag latch re-gated rows. Rows only, no audio, no label, no event; gated rows arrive coarse (flag bit 0).
* The shell tries the socket first and polls `/api/spectrum` when it is refused or dropped (`?ws=0` forces polling); with the socket the nine per-beam strips and all nine chips are live.
* The installer and the image copy `ui/ws.py`; without it `--ui-next` refuses to start.

What phases 2 and 3 remove (measured on the prototype, one client): one socket at 4.6 KB/s for nine beams instead of 3.7 KB/s for one beam polled; repeated fetches of 28.2 KB/s (Overview) and 81.7 KB/s (Detections) become deltas. Current-dashboard baseline: AUDIT.md.

Not planned: WebTransport (needs HTTP/3 and a non-stdlib server), a JS framework or bundler (the measurements do not need one), any change to the spectrum producer or the analyzer in phases 1 to 4.
