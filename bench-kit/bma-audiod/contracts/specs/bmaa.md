contract: bmaa
contract_version: 1.0.0
status: stable
producer: pc/dante_bridge.py (`bmaa()`)
consumers: classify/bma_classify.py (`_parse_datagram`), analysis/host.py (same parser), pc/bma_cable_sink.py (`parse_datagram`)
reference: contracts/ref_binary.py (`build_bmaa`, `parse_bmaa`), examples in contracts/fixtures/bmaa_examples.json

# BMAA - per-beam audio datagram (UDP)

One datagram carries one block of ONE beam.

* Slots 0-7 go to the `--all-to` targets (analysis host :48300, cable sink 127.0.0.1:48400).
* The single auto-chosen channel, always as slot 0, goes to the `--to` targets (classifier :48200).
* UDP: no ordering, delivery or duplicate guarantee.

## Byte layout (little-endian)

| offset | size | field | rule |
|---|---|---|---|
| 0 | 4 | magic | ASCII `BMAA` |
| 4 | 1 | version | `1`. A receiver MUST reject a different value. |
| 5 | 1 | reserved | `0`. A receiver MUST reject non-zero. |
| 6 | 1 | slot | `0..7` = beam index - 1. `--to` datagrams are always slot 0. |
| 7 | 1 | flags | bit 0 `zero`: every sample of the block is 0. bit 1 `gap_before`: audio was lost immediately before this block. Other bits reserved, ignored. |
| 8 | 4 | seq | u32, per-slot datagram counter, wraps modulo 2^32. Loss accounting only. |
| 12 | 4 | block | u32, ABSOLUTE index on the card's sample clock of the first sample (48 kHz), wraps modulo 2^32. |
| 16 | 4 | n | u32, number of samples that follow. The bridge sends 256. |
| 20 | 4n | samples | n x int32, SIGN-EXTENDED 24-bit: -8388608..8388607 (full scale = 2^23). A value outside that range is malformed. |

* Total length = 20 + 4n bytes; a datagram whose length differs MUST be rejected.
* Float scaling: `sample / 8388608.0`.

## Semantics

* `block` is the time base. The next block of the same slot starts at `block + n`. Align beams by `block` across slots, never by arrival time.
* `gap_before`: the bridge flushes at every hole and never splices audio across a gap. A consumer MUST NOT window or filter across a block flagged `gap_before`.
* `zero` is a hint that the block is silent; samples are still present.
* Beam numbering: slot `s` = Dante Tx channel `s+1` = FPGA matrix input `s+1` = vendor "Beam s+2" = analysis `beam s+1`. Vendor Beam 1 (matrix input 0) is not routed and has no slot.

## Wrap-around

* `block` and `seq` wrap modulo 2^32 (24.85 h at 48 kHz). Consumers MUST compare indices modulo 2^32.
* The cable sink's assembler unwraps `block` to a monotonic 64-bit index.

## Conformance

* `ref_binary.parse_bmaa` is the reference. Rejection codes: `short`, `magic`, `version`, `reserved`, `slot`, `length`, `sample_range`.
* Tests byte-compare `ref_binary.build_bmaa` with `dante_bridge.bmaa` and require the production parsers to agree with the reference on every example in `fixtures/bmaa_examples.json`.
