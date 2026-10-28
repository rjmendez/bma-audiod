contract: ui-http
contract_version: 1.13.0
status: stable
producer: ui/bma_ui.py (read-only dashboard server, Python standard library only)
consumers: ui/static/app.js
schema: schemas/ui_http.schema.json ($defs per payload); examples: fixtures/ui_*.json, fixtures/ui_rollup.jsonl

# ui-http - dashboard JSON API and hourly rollups

Default bind `127.0.0.1:48500`. Reads pipeline files (never writes them), polls dspd with HTTP GET only, persists hourly rollups.

## Transport rules

* GET and HEAD only. Other methods: 405 (`Allow: GET, HEAD`) with a problem body.
* Exact routing. Embedded assets: `/`, `/index.html`, `/static/app.js`, `/static/app.css`, `/static/tip.js`, `/static/spectrum.js`, `/static/session.js`, `/static/login.js`, `/static/login.css`, `/static/review.js`, `/static/review.css`.
* `--ui-next` (1.12.0, default off): also `/next/` and `/next/index.html` (the page), `/next/app.css`, `/next/app.js`, `/next/review.js`, `/next/adapter.js`, `/next/engine/{audition,colormaps,dsp,render,worklets}.js`. Without the flag all of them are 404. Same login gate and Host check as `/static/*`; `Cache-Control: no-cache`; a query is accepted on the two page paths only. CSP of these responses: the base CSP plus `media-src 'self' blob:`. The shell calls only routes already listed here.
* Case, trailing slash, percent-encoded or `..` variants: 404.
* `Host` must be `localhost:PORT` or `127.0.0.1:PORT`, plus `NAME:PORT` per `--allow-host NAME`.
* `--allow-ip-hosts` also accepts any IPv4/IPv6 literal on the listening port. NAMEs still need `--allow-host`.
* Any other `Host` (missing, bare name, wrong port, malformed literal): 403 `host_not_allowed`.
* Headers: `Content-Security-Policy` (`default-src 'self'`, no inline script or style, `frame-ancestors 'none'`), `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`.
* API responses: `Cache-Control: no-store`.
* Errors: `application/problem+json` `{type: "urn:bma-ui:problem:<code>", title, status, code, detail, param?}`.
* Codes: `invalid_parameter` (422), `host_not_allowed` (403), `method_not_allowed` (405), `not_found` (404), `internal_error` (500, no internals).
* Codes: `rate_limited` (429, `Retry-After`), `spectrum_detail_disabled` (403).
* Codes (1.13.0, `--ui-next` route `GET /api/v2/live`, contract `ui-live`): `origin_not_allowed` (403), `upgrade_required` (426, `Upgrade: websocket`, `Sec-WebSocket-Version: 13`), `too_many_connections` (503, `Retry-After: 5`). The route is behind the login of `/api/spectrum`; without `--ui-next` it is 404.
* Login codes: `unauthorized` (401), `too_many_attempts` (429), `invalid_request` (400), `csrf_failed` (403), `login_disabled` (403).
* Board token: read server-side from a file; never in any response, header, log line or error text.
* Board errors are short codes (`unreachable`, `http_401`, `http_500_<code>`).
* Units in field names: `_s` epoch seconds or duration, `_db` mean-square dB re full scale, `_dbfs`, `_hz`, `_pct`, `_deg`, `_kb`, `_c`.
* Board monitor `mean_square_raw` / `peak_raw` are unconverted integers of unknown unit.

## Authentication

Design: `ui/AUTH.md`. Transport only.

* A token file (`--auth-token-file`, default `<pipeline-root>/dashboard_token.txt`) makes EVERY request need a credential.
* Without a token file the server starts open only on a loopback bind; other binds refuse to start unless `--no-auth`.
* Token: >= 24 printable ASCII characters, no whitespace; else refused at startup.
* Check order: connection cap (503), Host allowlist (403), `/healthz` (200/405, no login), login (401/429), method (405), routing.
* `GET` or `HEAD /healthz` (1.10.0): no credential on any server; Host check applies; `200 application/json` `{"ok":true,"ts":<epoch seconds, int>}`, `Cache-Control: no-store`. Exactly that path. Other methods: 405 (`Allow: GET, HEAD`). Schema `healthz`.
* Credentials (any user name): HTTP Basic `Authorization: Basic base64(<user>:<token>)`, or cookie `bma_auth` set by `GET /?token=<token>`.
* Only exactly `/` is a `?token=` login URL.
* Right token: `302 Location: /`, `Set-Cookie: bma_auth=<64 hex>; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000` (`; Secure` over TLS), `Cache-Control: no-store`, no body.
* Cookie = HMAC-SHA256 under a per-process random key. Secrets compared in constant time, never logged or echoed.
* 401 `unauthorized`: no, wrong or stale credential. Headers `Cache-Control: no-store`, `WWW-Authenticate: Basic realm="BMA dashboard", charset="UTF-8"`.
* The challenge header is omitted only when `Sec-Fetch-Mode` is present and not `navigate`. Example `fixtures/ui_problem_401.json`.
* 429 `too_many_attempts` (`Retry-After: <s>`, `Cache-Control: no-store`): lockout after 5 wrong credentials per client address.
* Counted: wrong Basic password, scheme or encoding; wrong `?token=`. Not counted: missing credential, stale cookie.
* Lock: first 60 s, doubling per further failure to 900 s. A correct token is refused while locked; success clears the count.
* An address idle 1 h is forgotten; at most 1024 addresses tracked (least recently failing evicted). Example `fixtures/ui_problem_429.json`.
* Every response of a login-protected server carries `Cache-Control: no-store`, assets included.
* TLS (optional): `--tls-cert`, `--tls-key`; TLS 1.2+, no client certificates.
* HEAD behaves like GET at every step (same status and headers, no body).

### Sign-in page, sessions, CSRF

On an open server `/login`, `/api/session`, `/api/passkeys*` are 404 `not_found` and POST is 405.

* `GET /login` (also HEAD; no credential; Host check applies): `200 text/html` form (`POST /login`, fields `token`, `csrf`), `Cache-Control: no-store`.
* CSP identical except `form-action 'self'`. `/static/login.css` and `/static/login.js` need no credential. No schema.
* `POST /login` (`application/x-www-form-urlencoded`, body <= 4096 bytes else 400 `invalid_request`): same-origin rule, then valid `csrf`, then `token`.
* `csrf` = stateless token carried by the form, valid 15 min; invalid: the page again with 403.
* Right token: `303 Location: /`, `Set-Cookie: bma_auth=<64 hex>; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000` (`; Secure` per below).
* The cookie value is a random 256 bit in-memory session id (restart ends all sessions), not the HMAC cookie.
* Wrong token: `401 text/html` (page again), counted in the same per-address counter as Basic and `?token=`; locked: 429 `too_many_attempts`. Success clears the counter.
* Browser navigation: a credential-less GET/HEAD of exactly `/` or `/index.html` (no query) with `Sec-Fetch-Mode: navigate` AND `Sec-Fetch-Dest: document` gets `302 Location: /login`. Every other unauthenticated request: 401.
* `GET /?token=<right>` still answers `302 Location: /` and sets the HMAC cookie, revocable only by rotating the token file. Deprecated as primary route.
* `POST /logout` (authenticated; form or empty body <= 4096 bytes): needs CSRF token (`X-CSRF-Token` header or form field `csrf`) and the same-origin rule, else 403 `csrf_failed`.
* Logout forgets the session; `303 Location: /login` with `Set-Cookie: bma_auth=; Max-Age=0; ...`. The `?token=` cookie is only cleared in the browser.
* `GET /api/session` (authenticated; schema `session`, example `fixtures/ui_session.json`): `{authenticated: true, via: session|cookie|basic|tailnet, user, csrf, passkeys: {enabled, count}, tailnet_identity}`.
* `csrf` = `HMAC-SHA256(per-process key, subject)`; subject = session id, legacy cookie, `basic`, or tailnet login. No CORS header.
* Same-origin rule (every state-changing POST: `/login`, `/logout`, passkey routes): `Sec-Fetch-Site`, if present, MUST be `same-origin`.
* `Origin`, if present, MUST have host:port equal to `Host`; `Origin: null` fails unless `Sec-Fetch-Site: same-origin`. Violation: 403 `csrf_failed` (login page with 403).
* Basic is accepted for the read-only API only; state-changing routes need the CSRF token (from `GET /api/session` with the same Basic credential).
* `; Secure` on the cookie when the server serves TLS itself OR the TCP peer is loopback and `X-Forwarded-Proto` is `https`. `X-Forwarded-Proto` from any other peer is ignored.

### Tailnet identity

Flags `--tailnet-identity`, `--allowed-users PATH`, `--require-identity`.

* Only `Tailscale-User-Login` is read. Believed only when ALL hold:
  * TCP peer is loopback (`127.0.0.0/8`, `::1`, `::ffff:127.x`); `X-Forwarded-For` and Host never consulted;
  * the header occurs exactly once;
  * value is printable ASCII <= 254 chars and not an RFC 2047 encoded word (`=?`...);
  * its lower-cased value is a line of the `--allowed-users` file.
* Rule: `auth.identity_login(peer, header_values, allowed)`.
* `--allowed-users` (default `<pipeline-root>/allowed_users.txt`): one login per line, `#` comments and blank lines ignored, case-insensitive. Re-read when size or mtime changes.
* Missing, unreadable, non-UTF-8, > 64 KiB or empty file = nobody.
* Identity-authenticated requests: `via: tailnet`; they do not touch lockout state. Token/Basic attempts through the proxy count against the loopback address.
* Without `--require-identity` the token stays a fallback (Basic, `?token=`, `/login`).
* With `--require-identity`: token, Basic, cookies, `/login` POST (403 `login_disabled`) and passkeys are refused; only an allow-listed identity passes (else 401).
* Startup refused: `--allowed-users` or `--require-identity` without `--tailnet-identity`; `--tailnet-identity` with `--no-auth`.
* Trust model: any local process can forge the header; use identity mode only where local processes are trusted.

### Passkeys

Flags `--passkey-origin ORIGIN` (repeatable, one host), `--passkey-file PATH`.

* Needs a login and an origin `https://host[:port]` (`http` only for `localhost`, `127.0.0.1`, `[::1]`); host = WebAuthn rp id.
* Without `--passkey-origin`, or if `ui/passkeys.py` cannot be imported: every `/api/passkeys*` is 404.
* `ui/passkeys.py`: stdlib only; ES256 (ECDSA P-256 / SHA-256); strict DER.
* Stores public keys only (credential id, point x and y, signature counter, label, times) in `--passkey-file` (default `<pipeline-root>/passkeys.json`, atomic write, mode 0600), at most 20.
* Attestation not verified. User verification required at registration and every login. The signature counter must increase when either side is non-zero.
* `POST /api/passkeys/register/options`, `.../register/verify` (authenticated session only; JSON <= 16 KiB; CSRF and same-origin): challenge bound to the session, single use, 5 min, <= 64 outstanding.
* `GET /api/passkeys` -> `{passkeys: [{id, label, created_s, last_used_s}]}`; `POST /api/passkeys/delete` `{id}` (CSRF).
* `POST /api/passkeys/login/options` -> 200 `{challenge, rpId, timeout, userVerification: "required", allowCredentials: []}`.
* `POST /api/passkeys/login/verify` `{challenge, credential: {id, clientDataJSON, authenticatorData, signature}}` -> 200 `{"ok": true}` with the session cookie, or 401 `unauthorized` (shared lockout, 429 when locked).

## Review (1.8.0, evidence 1.9.0; needs a login, additive)

Label review for the verdict-labels store (specs/verdict-labels.md).

* Without a login: GET routes 404 `not_found`, POST routes 405.
* Library: `analysis/labels.py`, `analysis/review_queue.py`. Flag `--labels-dir` (default `<pipeline-root>/labels`).
* Writes: one lock in this process, the library's file lock across processes.

| route | method | answer |
|---|---|---|
| `/api/review/next` | GET, HEAD | `review_next` |
| `/api/review/stats` | GET, HEAD | `review_stats` |
| `/api/review/clip?id=<clip id>` | GET, HEAD | `image/png`, the spectrogram `analysis/clips/<day>/<id>.png` |
| `/api/review/audio?id=<clip id>` | GET, HEAD | `audio/flac` or `audio/wav`, `analysis/clips/<day>/<id>.flac|.wav` (1.9.0) |
| `/api/review/verdict` | POST | `review_verdict` |
| `/api/review/skip` | POST | `review_verdict` (`recorded` false) |

* POST body: JSON, at most 16 KiB. `verdict`: `{event_id, verdict, wrong_reason?, csrf}`; `skip`: `{event_id, csrf}`. CSRF token (body `csrf` or `X-CSRF-Token`) and same-origin rule as `POST /logout`, else 403 `csrf_failed`. Malformed body 400 `invalid_request`.
* `verdict` vocabulary: `correct | wrong | unsure | artifact`. `wrong` needs `wrong_reason`: `other_species | non_bird_sound | mechanical | insect_or_amphibian | no_sound_seen`. No free text. Unknown value: 422 `invalid_parameter` with `param`.
* Record: appended through `labels.make_record(source "ui")`; `basis` `clip_visual` when the event has a clip PNG, else `metadata_only`; `stratum`, `p_include`, `queue_id` copied from the queue item.
* Idempotent: same `event_id`, `verdict`, `wrong_reason` within 30 s of the folded record: no new line, `recorded` false, same `label_id`.
* Refused: an event that is unknown, speech, Human-labelled, untagged or tag-speech-flagged: 422 `not_labelable`, detail `not_labelable` (one code for all causes). Such an event is never in `next`.
* `next` item: first queue item not skipped and not labelled. Queue: `review_queue.build_queue` with default params (14 days, speech guard fail closed, in-region only, no insect, no non-species). `reason`: `near | disagree | low | audit`.
* `artifact_flags` only when a report of the event carries it. `clip_png` only when a clip with an existing PNG overlaps the event; `clip_audio` (1.9.0, url of `/api/review/audio`) only when its audio file exists;
* `clip_span` (1.9.0) `{clip_start_s, clip_end_s, event_start_s, event_end_s}` with any clip (the PNG spans clip_start_s..clip_end_s).
* Evidence only (1.9.0): `next` serves only events that have a clip PNG and/or audio overlapping them, items with audio before PNG-only (stable). Without the flag the queue is every eligible event with a clip (no sampling cap: limit, per_species_cap, audit_per_species_cap 10^6; `p_include` as the builder computes it: null for uncertainty picks, 1 for audit picks). Flag `--review-allow-no-clip` also serves the rest, after the same audio-first ordering.
* `stats` gains `no_clip`: candidate events (not skipped, not labelled) without evidence; `queue_depth` counts what `next` serves. Speech / untagged / Human refusals unchanged.
* `skip`: in-memory list, no record, lost on restart.
* `clip`: `id` must match `[0-9]{13}-[a-z0-9]{1,24}-b[1-8]` (else 404); the day directory is computed from the id (UTC), never from the request; a missing file is 404.
* `audio`: same id rule and day derivation as `clip`; 404 when the file is absent, expired, empty or over 16 MiB. `Cache-Control: no-store`, `Accept-Ranges: bytes`.
* `Range: bytes=a-b | a- | -n` (single range): 206 with `Content-Range: bytes lo-hi/size`, at most 4 MiB per answer; start at or past the end (or `-0`): 416 `range_not_satisfiable` with `Content-Range: bytes */size`; a malformed or multi-range header is ignored (200, whole file).
* No Range: 200, whole file. HEAD: headers only.
* Store unreadable or unwritable: 503 `labels_unavailable`, detail `label store unavailable: <exception class>`.
* Queue and stats come from a cache of at most 30 s; a verdict drops it.

## Parameters

Unknown, repeated or malformed: 422.

| parameter | rule |
|---|---|
| `since`, `until` | epoch seconds or RFC 3339; `until` default now, `since` default `until` - 24 h; `since < until`; range <= 90 days |
| `species` | 1-100 printable chars, matched case-insensitively against common or scientific name |
| `min_conf` | 0..1 |
| `beam` | 1..8 |
| `in_region` | `true` (default: all not marked `false`), `false` (only out-of-region), `any` |
| `non_species` | `false` (default), `true` (only non-species classes such as Engine, Dog), `any`; events and species |
| `group` | `any` (default), `bio` (five biological groups), `bird`, `insect`, `amphibian`, `mammal`, `other-bio`, `mechanical`; events, density, species |
| `artifact` | `any` (default: every event), `none` (omit events flagged `suspect` or `likely`), `only` (just those); events, density, species. Nothing is deleted |
| `tier`, `beam`, `since`, `rev`, `compare`, `band`, `limit` | `/api/spectrum` only; there `since` is a row index |
| `limit` / `offset` | 1..500 (default 100) / 0..100000 |
| `band` | 0..7 (default 5): octave bands 50-125 ... 8000-16000 Hz |
| `bucket_s` | density: 60..86400, default range / 200 rounded up to a minute. Beam endpoints: 10..86400 (default 300) |

* `bucket_s`: <= 2000 buckets; buckets < 3600 s need the fine in-memory window (default 48 h); multiples of 3600 may use rollups.

## Endpoints

`GET /api/status`
* `bridge`, `sink`, `analysis` (plugins), `board` (reachable, API version, temperatures, pattern), `any_stale`, `last_event`, `latest` (tags, CLAP, spatial, dsp), `ingest`.
* `sources[]`: file present/size/age, `stale_after_s`, `stale` (`null` for present event-driven files), `garbage_lines`, `rejected_records`, `resets`, `last_error` (exception class or `null`).

`GET /api/detections/events`
* Grouped events, newest first, with `total`, `limit`, `offset`, `window_start_s`, `truncated_by_window`.
* Individual events only for the in-memory window; older ranges answered from rollups by `species`.

`GET /api/detections/density`
* Same filters as `/events` (`group`, `beam` included, no `limit`).
* `rows[]` of `{t_s, species, scientific, group, best_beam, events, max_confidence}` per (bucket, species, best beam); `t_s` = bucket start; buckets `bucket_s` wide from `since`.
* `total` = sum of `rows[].events`; `n_rows` = `rows.length`; `data_first_s` / `data_last_s` = first/last matching event start (`null` when none).
* In-memory window only. A client MUST say how many events it shows or aggregates.

`GET /api/detections/event?id=<event id>`
* `id` = start in ms, a dash, slug of the species key; required, `[0-9]{1,16}-[a-z0-9-]{0,60}` (else 422); not in memory: 404 `not_found`.
* `reports[]`: `{t_s, beam, beam_azimuth_deg, confidence, level_dbfs}` in time order (`level_dbfs` `null` when absent).
* `trace_truncated` true when > 500 reports (first 500 listed; `event.reports` is the full count).
* Neighbours: `/api/detections/events` with `since = t_start_s - 60`, `until = t_end_s + 60`.

`GET /api/detections/species`
* Per species: `events`, `reports`, `first_seen_s`, `last_seen_s`, `bounds_exact` (false when part came from hourly rollups), `max_confidence`, `beam_events[8]`, `hourly_counts`, `group`, `active_hours`, `events_per_active_hour`, `chorus`.
* `groups[]`: all six, zeros included, counted BEFORE the `group` filter. `unclassified[]`: <= 20 species that fell to `other-bio`, with `events`. `unclassified_total`.
* `chorus` = `events_per_active_hour >= 60` and `active_hours >= 3`; never a filter.

`GET /api/spectrum`
* Schema `$defs/spectrum`, example `fixtures/ui_spectrum.json`. Rows are magnitude-only band rows from `pc/bma_spectrum.py` over loopback UDP (`--spectrum-port`; `specs/spectrum-packet.md`).
* In-memory ring: 60 s standard, 20 s detail. Nothing persisted.
* `tier` = `standard` (default) | `detail`; `beam` = 0..8 (default 0 = power mean of 8 beams); `compare` = `false` (default) | `true`.
* `since` = `idx` of the client's last row (0 or absent: newest `limit` rows); `rev` = `rev` of the client's last response (absent = 0).
* `band` = 0..7 (default 5), the octave band of `compare`; a spectrum band belongs to the octave band containing its geometric centre.
* `limit` = 1..600 (standard, default 600) or 1..500 (detail, default 500).
* Response: rows with `idx` > `since` plus every row rewritten after `rev`, oldest first; client replaces rows by `idx`.
* `rows_missed` = rows after `since` that left the ring. `next_since` = newest `idx`; `rev` = ring revision.
* `rows[].bins` = base64 of `n_bins` uint8 (`dB = v * db_step + db_floor`); `edges_hz` has `n_bins + 1` contiguous edges.
* `compare=true` adds `levels[8]`: uint8 per beam, power sum of the bins of octave band `band`, same scale.
* `gated` rows are speech-suppressed (constant inside one-third-octave groups); a client MUST show a "speech suppressed" indicator.
* Rows with flag bit 1 (gap) or 2 (zero) have all-zero bins: draw as no data.
* `state` values:
  * `disabled`: no `--spectrum-port`; 200, empty `rows`, `n_bins` 0. Takes precedence.
  * `auth_required`: client not authenticated; 200, empty `rows`, `n_bins` 0, empty `edges_hz`, `rows_missed` 0, `newest_age_s` null, `gated_now` false.
  * `no_data`: enabled, nothing received.
  * `stale`: newest row arrived > 3 s ago by the dashboard's clock; retained rows still listed.
  * `live`.
* `gated_now` = newest row's gate flag. A client shows a "sign in required" badge for `auth_required` and polls slowly.
* Server limits: <= 10 standard and 50 detail rows/s per beam accepted from the socket (excess counted in `status.spectrum.rows_dropped_rate`).
* Per-client-address token bucket: 5 requests/s, burst 10, else 429 `rate_limited` with `Retry-After: 1`; <= 256 addresses tracked. A response never exceeds `limit` rows.
* Access is gated by authentication, not by source network. Both tiers need an authenticated client from any network; peer address and `Host` play no part.
* Unauthenticated client: 200 `state: auth_required`, no rows. Detail tier also needs `--spectrum-detail` (else 403 `spectrum_detail_disabled`).
* Rule: `spectrum_client_allowed(authenticated, tier, detail_flag) -> (allowed, reason)`, fed by `request_authenticated(handler)`, true only when `server.auth` (`ui/auth.py`) set `handler.authenticated = True`.
* An open server (no token file, loopback bind) is not authenticated: it answers `auth_required` to everyone.
* With a login configured, an unauthenticated request gets 401 before reaching the spectrum route. `X-Forwarded-*` is never trusted.
* Tag latch: windows `(t - 5, t)` of the analyzer's `speech_active` tags: rows inside are replaced by their block's `coarsen_block` value, flagged `gated`, and `rev` increments. Delivered rows are not recalled.
* `status.spectrum` (optional): `enabled`, `detail_enabled`, `state` (never `auth_required`), `rows_received`, `rows_dropped_rate`, `bad_packets`, `newest_age_s`, `gated_now`.
* Client polls every 500 ms while the System tab is visible and the document not hidden; else stops.

`GET /api/beams/live`
* Last 60 s: per band the power-mean dB per beam, `rel_ring_db`, `leader_beam`, `contrast_db`; `frame_kind`.

`GET /api/beams/stats`
* Whole hours overlapping the range: per beam `power_mean_db`, `max_db`, `rel_ring_db`, `leader_share`, `mean_rel_ring_db_2_8khz`, plus `hourly[]`.
* `effective_since_s` / `effective_until_s` = hour-aligned bounds actually aggregated (last 60 minutes covers up to two hours); show them, not the requested range.

`GET /api/beams/heatmap`
* `values_power_mean_db[beam][bucket]`, `null` where no window fell in the bucket; `basis` `fine` or `hourly`.

`GET /api/system`
* Stream loss samples (30 s, <= 720), hourly rows, board temperatures (since server start), memory (`memfree_kb` = MemFree), DSP versions, raw monitor cells, plugin status.

`GET /api/calibration`
* `calibrated`, `frame`, `frame_kind`, `provisional`, `panel_up_dsp_azimuth_deg`, `beam_azimuth_deg`.
* Uncalibrated: `frame` = `array frame (uncalibrated)`, fallback azimuths 90,45,0,315,270,225,180,135 (audio beam k at entry k, DSP frame).
* Stored dspd profile: `calibrated panel frame <measured_at>` (or `calibrated compass frame <measured_at>` when `north_reference` is `compass`).
* `frame` is a label: match on `frame_kind`: `panel` (calibrated, `north_reference` `array` or absent), `compass`, else `array`.
* `provisional` true for a stored profile. `panel_up_dsp_azimuth_deg`: 15 for `panel`, `null` for `compass` and uncalibrated.
* Free-text profile fields (notes, source, method) and coordinates are never exposed. Values are not converted.
* Panel frame: 0 = panel up, clockwise from the front (90 right, 180 down, 270 left); panel azimuth = DSP azimuth - 15.

### Taxonomic groups

Every event and species carries `group`: `bird`, `insect`, `amphibian`, `mammal`, `other-bio` (counted in `unclassified`, never dropped), `mechanical`. Group comes from the label, in order:

1. Whole label in `ui/taxa.json` `explicit_labels` (Dog = `mammal`; Engine, Power tools, Siren, Fireworks, Gun, Environmental, Noise, Human vocal / non-vocal / whistle = `mechanical`).
2. First word of the scientific name in `taxa.json` `genera` (built by `ui/make_taxa.py`): 11 amphibian, 19 insect, 6 mammal genera; every other genus is a bird.
3. Else `other-bio`.

* Event `non_species` follows the class of a mapped label (Dog and mechanical true; genus hit false); the producer flag applies only to unmapped labels.
* Dog is hidden by default (`non_species=any` shows it, under `mammal`); `group=mechanical` needs `non_species=any` or `true`.
* Rollup lines carry no group (derived at read time).
* `status.taxa`: `loaded`, `n_genera`, `error` (missing or malformed `taxa.json` loads empty: every label `other-bio`), `unclassified_events_24h`.

### Human voices hidden

* Events whose label (common or scientific, case-insensitive) is `Human vocal`, `Human non-vocal` or `Human whistle` are dropped when the detection stream is read.
* They appear in no list, count, chart, density cell, scatter, timeline, species summary, group chip, heatmap, event detail or rollup line.
* `non_species=any`, `group=mechanical`, `species=` do not bring them back. `Engine`, `Siren` and other mechanical labels are unaffected.
* Earlier rollup lines naming one are skipped on read.
* Optional `status.human_hidden_24h` (integer): such events (merged by the 3 s gap) started in the last 24 h, clipped to the in-memory window.

### Steerable bands

* Bands 0 and 1 (50-125, 125-250 Hz) are identical on every beam. `beams_stats` and `beams_heatmap` carry `steerable` (false for bands 0 and 1). Values are not altered.

### Applied beam pattern

* No endpoint reads the pattern back from the DSPs.
* `status.board.beams_applied_by_dspd` and `status.board.pattern_ref` come from dspd `/v1/state`: `true` with `pattern_ref` null = applied, no name; `false` = none; `null` = not readable.
* Per-beam directions = `/api/calibration` `beam_azimuth_deg`: stored metadata, not applied to any DSP.
* A client MUST label the sources separately ("reported by dspd", "from the stored calibration profile", "not readable") and MUST NOT infer gains or a pattern shape.

### Beam-slot mapping flag

* `slot_mapping_reliable_from_s` (epoch s; default 2026-10-25T21:47:00Z; `--slot-fix-at ISO8601`) is carried by `/api/status`, `/api/detections/events`, `/api/beams/heatmap`.
* Every event has `slot_mapping_unreliable` = `t_start_s < slot_mapping_reliable_from_s`.
* A client treats an hour as partly unreliable when it ends after the boundary.

## Artifact flags (1.11.0; additive, advisory)

* Insect and mechanical events carry optional `artifact_flags` (`$defs/artifact_flags`, contract `artifact-gate`) on `/api/detections/events` and `/event`. Computed at read time by the dashboard (`analysis/artifact_gate.py`, evaluated at the event's first report); the analyzer is not involved.
* Advisory: the event is listed and counted as before. Other groups have no `artifact_flags` key. Human-voice labels never reach the dashboard.
* `level`: `likely` = `cold_insect` (insect label, ambient temperature known and below 5 C); `suspect` = `burst_coincident` only (>= 20 dsp transients within +-30 s, median beam spread <= 3 dB); `none` otherwise (`temp_unknown`, `mechanical_label` alone are information). `artifact` filter "flagged" = `suspect` or `likely`.
* Temperature: `<data-dir>/history/ambient_temp.jsonl` (+ `.1`), `ambient-temp` reader rule (nearest past UTC hour, <= 3 h old, no interpolation); re-read when the files change (checked every 30 s).
* Transients: `transient` records of `analysis/dsp.jsonl` seen by the dashboard's tailer (about two days backfill, then live). Burst evidence only when the +-30 s window lies inside the span of dsp records, else `burst_n` null and no burst flag.
* Failure: no ambient file, unreadable file or record older than 3 h gives `temp_unknown`; no dsp file gives null burst evidence; missing `artifact_gate.py` or `--no-artifact-gate` removes the field everywhere (`status.artifact_gate.enabled` false). None is an error.
* `events`, `density`, `species` echo the mode as `artifact`. `species` adds per row `artifact_events`, and `artifact_events_total` (flagged in-memory events matching the other filters, whatever the mode omits) and `artifact_unscored_events` (insect and mechanical events from hourly rollups: never scored, `artifact=none` cannot omit them).
* `GET /api/status` adds `artifact_gate`: `enabled`, `available`, parameters (`floor_c`, `temp_max_age_s`, `burst_window_s`, `burst_min_transients`), ambient feed (`records`, `first_hour_s`, `newest_hour_s`, `newest_age_s`, `skipped`), transient feed (`in_memory`, `first_t_s`, `last_t_s`), `events_scored_24h`, `events_flagged_24h`. Server flag `--no-artifact-gate`.

## Definitions

* Event: reports of the same species (scientific name, else common name) on any beams, each at most 3.0 s after the previous (exactly 3.0 s joins, 3.01 s splits).
* Event keeps the best-confidence beam, beam set, report count, maximum confidence and level. `provenance` always `model_opinion`.
* Event `in_region`: `false` only when every report said false, `true` when any said true, else `null`.
* Power mean: mean over windows of linear mean-square power, converted back to dB.
* Contrast window: over the 2-8 kHz bands (index 5 and 6 summed), loudest beam exceeds quietest by >= 3 dB.
* A window in which any beam's 2-8 kHz power is at the -200 dB floor (below -160 dB) is not a contrast window.
* Leader share: fraction of contrast windows each beam led. Relative to ring: a beam's dB minus the dB of the mean power of the 8 beams.
* Out-of-region records: `k: "bird_filtered"` with `in_region: false`; grouped like `bird` records (`in_region=false|any` shows them).
* Beams are 1..8; `power_mean_db[0]` is beam 1.

## Rollups

`<data-dir>/reports/ui/rollup_YYYYMMDD.jsonl` (UTC day of the hour), one line per `(kind, hour_s)`, schema `$defs/rollup`:

* `detections`: per species `scientific, common, events, reports, max_confidence, beam_events[8], in_region, non_species`.
* `beams`: `windows`, `zero_frac_mean`, `power_mean_db[beam][band]`, `max_db[beam][band]`, `contrast{windows, leader_count[8], mean_rel_ring_db_2_8khz[8]|null}`.
* `stream`: `samples`, `loss_pct_mean/min/max`, `gaps_delta`, `state_counts`.

* Append-only: written once, only after the hour closed (+120 s grace) and only for hours the in-memory data covered from the window start.
* An existing `(kind, hour_s)` is never rewritten (first line wins on load). One `write` per file per batch.
* A torn last line is newline-terminated before the next batch and skipped (counted) on load. `hour_s` is a multiple of 3600.
* A line whose payload does not match its kind is skipped and counted in `ingest.rollup_garbage_lines`; a failed load is counted in `ingest.rollup_load_errors` and the live tailers still start.

## Versioning

Optional fields and endpoints bump minor; a removed or re-typed field or changed definition bumps major.
