contract: cable-stream
contract_version: 2.0.0
status: stable
producer: pc/bma_cable_sink.py (playback into VB-Cable "CABLE In 16 Ch", or `--record` to a 24-bit WAV)
consumers: any Windows recorder on "CABLE Output"; contracts/ref_binary.py; pc/bma_cable_decode.py; operator check pc/bma_cable_selftest.py
reference: contracts/ref_binary.py; examples in contracts/fixtures/cable_frames.json, cable_columns.json
related schemas: schemas/sink_status.schema.json (sink-status 1.2.0); data-track payloads: board_debug (kind 2), bridge_status (kind 3), beam_bands (kind 4)

# 16-channel cable stream and the BMD1 data track

48 kHz, 16 channels, 24-bit (or float). Beams 1-8 are signed 24-bit integers `k` carried as the float `k / 2^23`.

* Channels 9-13 are SYMBOL channels: symbol `u` in 0..4095 is carried as the count `(u - 2048) << 8`, the float `(u - 2048) / 2^15` (256-count grid, peak 0.0625 full scale).
* Record at 16-bit PCM or better. A recorder that dithers at 16 bits breaks it. Effects or volume processing on the endpoint break the symbols.

Path (VB-Cable, float32 WASAPI shared):

* The low 5 bits of every 24-bit sample are lost: every value is a multiple of 32 counts (gcd 32).
* Loud content is scaled by 0.985; the 256-count grid at peak 0.0625 FS is exact (750000 of 750000 symbols).
* Beams run at -40 dBFS or lower and are exact; exactness is not guaranteed at very high levels.

## Channel layout

| channel (1-based) | content |
|---|---|
| 1-8 | beams 1-8 (bridge slots 0-7), as sent; zero where audio was lost |
| 9 | card sample index of THIS sample (u32), bits 0-11, as a symbol |
| 10 | card sample index, bits 12-23, as a symbol |
| 11 | card sample index, bits 24-31, as a symbol (`u < 256`); index = `u9 | u10 << 12 | u11 << 24` |
| 12 | data track, first byte of the row's pair: symbol `u` = byte value 0..255 |
| 13 | data track, second byte of the pair. Two bytes per row, 96 kB/s. Zero bytes (`u` = 0, count -524288) when idle |
| 14-16 | reserved, exactly 0; readers ignore them (the decoder counts non-zero samples) |

* The track carries `index mod 2^32` (wraps every 24.9 h; a live row with index 0 has all three columns at -524288). Anchors carry the full 64-bit index.

Symbol rules:

* Writer: `count = (u - 2048) << 8`.
* Reader: `u = ((count + 128) >> 8) + 2048` (arithmetic shift), `residual = count - ((u - 2048) << 8)` in [-128, 127].
* A reader MUST count and report: a sample on channels 9-13 with `|residual|` > 16, a symbol outside 0..4095, an index byte (channel 11) or data byte above 255 on a live row, any non-zero sample on channels 14-16.
* A disturbance in [-128, +127] counts decodes exactly; outside it the symbol moves by one and shows as residual, index jumps or corrupt BMD1 frames.

Silence:

* A row whose three index samples are all exactly zero (u = 2048 on channel 11, which no index can have) marks rows the SINK invented (underrun played as silence): no data; their data-track bytes MUST be skipped.
* No live row has this form, including at the u32 wrap.
* A hole in the card's index is filled with silence whose index keeps counting. A ring overflow drops the oldest frames and shows as an index jump.

## Data track frames (`BMD1`)

| field | size | rule |
|---|---|---|
| magic | 4 | ASCII `BMD1` |
| kind | 1 | 1 anchor, 2 board record, 3 bridge status, 4 beam bands; other kinds ignored |
| length | 2 | big-endian, payload bytes |
| payload | length | see below |
| crc32 | 4 | big-endian CRC-32 (zlib) over `kind | length | payload` |

Between frames the track is zero bytes. A reader scans for `BMD1`; a frame with wrong CRC or impossible length is corrupt: count it and resume scanning one byte after its magic.

| kind | payload |
|---|---|
| 1 anchor | `>QQ`: card sample index at the first sample of the block the anchor was queued with, PC UTC microseconds. Once per second (27 bytes, 14 rows) |
| 2 board record | one JSON line as `board_debug.jsonl` holds it (board-debug 1.0.0), including its `host_t` |
| 3 bridge status | the `bridge_status.json` object (bridge-status 1.0.0) |
| 4 beam bands | one `beam_bands` record (beam-bands 1.0.0): identical compact JSON line as in `beam_bands.jsonl`; once per second; `idx` places it on the index axis |

A data record is queued when READ by the sink: place it by its own timestamp (`host_t` / `t`) through the anchors.

### Delivery guarantees

* Up to 96 KB/s while audio flows, from a bounded 256 KB queue (2.7 s of the track).
* Kinds 1, 3, 4: queued without waiting; a record that does not fit is refused and counted in `data_dropped`.
* Kind 2: queued WITH backpressure; dropped (and counted) only if the queue stays full for 5 s (`push_wait`). The log is read in pieces <= 64 KB. `board_debug.jsonl` itself loses nothing.

## sink_status.json

Written every `--stats-s` seconds while the sink process runs; absent while stopped.

* The sink does not exit when the 16-channel endpoint is not ready: it keeps assembling, metering (kind 4 and `beam_bands.jsonl`) and filling the data track, and retries opening the audio output every 15 s.
* `audio_output` = audio is being played; `device_error` = why not; `device` null until the output has been opened.
* A recording from "CABLE Output" exists only while `audio_output` is true.
* Counters are cumulative since sink start; `buffered_ms`, `playing`, `audio_output`, `device_error` are instantaneous.
* Optional `rx_errors` (1.2.0) counts receive-loop failures (a datagram whose processing raised, or a failed socket read). The receive thread survives them (stderr line for the first 5, then every 1000th; 50 ms pause); after 200 in a row the process exits with code 3.
* Rising `rx_errors` with flat `datagrams` = packet loss. Schema: `schemas/sink_status.schema.json`.

## Conformance

* Tests build rows with the production `Composer` and `encode_frame`, decode with `ref_binary`, encode with `ref_binary` and compare bytes with the production encoder.
* `pc/bma_cable_selftest.py` checks the real Windows path (index track, every data frame, beams within the 5-bit loss); run only with the sink stopped.
* `fixtures/cable_columns.json` holds index <-> column examples.
