contract: ui-live
contract_version: 1.0.0
status: stable
producer: ui/bma_ui.py + ui/ws.py (dashboard server, `--ui-next` only)
consumers: ui-next/static/adapter.js
schema: schemas/ui_live.schema.json (text frames); reference: contracts/ref_binary.py (`build_live`, `parse_live`, `ws_encode`, `ws_decode_client`, `ws_accept_key`); examples: contracts/fixtures/ui_live_examples.json

# ui-live - live spectrum rows over a WebSocket

`GET /api/v2/live` on the dashboard, only with `--ui-next` (otherwise 404). Same rows as `GET /api/spectrum` (standard tier, 9 beams x 46 bands, uint8 at 0.5 dB from -100 dB), pushed instead of polled. `/api/spectrum` is unchanged and stays the fallback. Magnitudes only: no audio, phase, sample, label or event anywhere on this socket. Human-voice rules are those of the rows: speech-gated rows arrive already coarse (flag bit 0).

## Opening handshake (RFC 6455, version 13)

* Order of refusals, each a problem+json (`ui-http`): 405 `method_not_allowed` (not GET), 422 `invalid_parameter` (any query), 404 `not_found` (no spectrum feed), 401 `unauthorized` (no login: the same login and cookie as every route; a server without a login is not authenticated either), 403 `origin_not_allowed`, 426 `upgrade_required` (not an upgrade, or a version other than 13; headers `Upgrade: websocket`, `Sec-WebSocket-Version: 13`), 400 `invalid_request` (`Sec-WebSocket-Key` not 16 base64 bytes), 503 `too_many_connections` (`Retry-After: 5`).
* Origin: when the header is present it must be `http(s)://<the Host header>` and nothing else; `null` and any other origin are 403. A missing header is accepted (non-browser clients; the login still applies).
* Answer: `101` with `Upgrade: websocket`, `Connection: Upgrade`, `Sec-WebSocket-Accept` = base64(SHA-1(key + GUID)), `Cache-Control: no-store`. No subprotocol, no extension (no compression).
* At most 8 live sockets at once.

## Server to client

Unmasked, unfragmented frames, in this order:

1. Text `hello` (schema `hello`), once.
2. Binary BMW1 frames: first the backlog (every row of the 60 s ring, oldest first, up to 600), then one frame per new row, in `seq` order.
3. Text `reset` (schema `reset`) when the ring's revision moved (the tag latch gated rows already sent): drop all rows, the whole ring follows as BMW1 frames.
4. Ping every 15 s (payload `bma`); pong to a client ping with the same payload; close.

### BMW1 frame (little endian, 24-byte header + 9 x 46 bytes = 438)

| Offset | Size | Field |
|---|---|---|
| 0 | 4 | magic `BMW1` |
| 4 | 1 | type = 1 (row) |
| 5 | 1 | beams = 9 (beam 0 = power mean of the 8 beams) |
| 6 | 2 | bins = 46 |
| 8 | 4 | seq: the dashboard's row index (`/api/spectrum` `idx`), modulo 2^32 |
| 12 | 8 | t_us: row time, epoch microseconds |
| 20 | 2 | flags |
| 22 | 2 | reserved = 0 |
| 24 | 414 | beam 0..8, 46 uint8 each |

Flags: bit 0 gated (any present beam gated: its bins are the block's coarse value), bit 1 gap, bit 2 zero (bits 0-2: OR over the present beams, as in spectrum-packet), bit 3 a beam is absent (its 46 bytes are 0). Other bits 0. A row missing beams is held back for 0.3 s after it arrived, then sent with bit 3.

## Client to server

Masked, unfragmented control frames only: close, ping, pong, at most 125 bytes. Anything else ends the connection with a close frame: unmasked, RSV bits or FIN clear 1002; a text, binary or continuation frame 1003; a length over 125 1009. A close from the client is answered with a close, then the socket is closed. A client that does not answer pings with a pong for 30 s gets close 1008.

## Limits

A send that takes over 5 s drops the connection (a client that does not read). Nothing is queued per client: a connection holds only its row cursor.

## Compatibility

New route and frames: new interface 1.0.0. `ui-http` 1.13.0 lists the route and the three new problem codes.
