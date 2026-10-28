# Data model

## Wire formats (mock server = the proposed `/api/v2` surface)

| Channel | Format |
|---|---|
| `GET /api/v2/live` (WebSocket) | binary frame 10/s: 24-byte `BMW1` header + 9 beams x 46 uint8 levels (beam 0 = mean), 0.5 dB steps from -100 dB (438 B payload, 442 B on the wire). Text frames carry events. 599-frame backlog on connect |
| `GET /api/v2/status` | JSON: strip values + health |
| `GET /api/v2/clips` | JSON list: id, time, species, confidence, best beam, synthetic truth boxes, the claimed box. `/api/v2/clips/<id>/beams.wav`: 8-channel 48 kHz PCM16 |
| `POST /api/v2/review/verdict`, `/skip` | in-memory store in the mock; the verdict record is echoed |
| `GET /api/v2/listen` (WebSocket) | `UILAB1` header frame, interleaved PCM16 chunks (AUDITION.md) |

Transport comparison and render measurements: STREAMING.md.

## Verdict record

The page builds the record the label store already accepts (contract `verdict-labels` 1.1.0, validated in `test/test_verdict_contract.py` against the real `analysis/labels.validate_record`):

```
{"k":"verdict","v":1,"label_id":...,"t_label":...,"event_id":...,"t_start_ms":...,"species_key":...,"verdict":"correct|wrong|unsure|artifact",
 "wrong_reason":null,"corrected_key":null,"basis":"clip_visual","source":"ui","stratum":null,"p_include":null,"queue_id":null,
 "model":{"conf_max":...,"n_beams":...,"best_beam":...,"group":null,"in_region":null},"provenance":"owner_verdict"}
```

Blind by default: the claimed species and confidence stay hidden until a verdict is chosen (`basis` records which views were used).

## Proposed evidence block (contract 1.2.0, not applied)

What the viewer knows and 1.1.0 rejects inside the record: views used, listened seconds, selected box (t0, t1, f0, f1), transform and scale. It travels beside the record (`evidence`), never inside it. `test_verdict_contract.py` asserts that 1.1.0 rejects the block, so the bump is a deliberate contract change, not a quiet field.
