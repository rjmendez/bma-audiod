contract: artifact-gate
contract_version: 1.0.0
status: stable (offline only)
producer: analysis/artifact_gate.py (the pure gate) and tools/artifact_replay.py (the offline replay)
consumers: offline analysis; the dashboard (ui-http 1.11.0: advisory `artifact_flags`, the `artifact` filter, `status.artifact_gate`; design docs/design/w3-gate-wiring.md)
schema: schemas/artifact_gate.schema.json; example: fixtures/artifact_gate_report.json (regenerate with tools/make_artifact_gate_fixtures.py)
design: docs/design/artifact-gate.md

# artifact_flags - advisory implausible-label verdict

The gate never deletes, hides or rewrites a record. Fail-open = "no flag", never "flag everything".

## Scope

| group (analysis/taxa.py, ui/taxa.json) | result |
|---|---|
| insect | evaluated: `cold_insect`, `burst_coincident`, `temp_unknown` |
| mechanical (not a hidden label) | `mechanical_label` only, level `none` |
| bird, mammal, amphibian, other-bio, unknown (null) | NO verdict: `evaluate` returns None, no `artifact_flags` on the record |
| hidden labels: every label starting with `human` (any case) and `speech` | NO output of any kind; checked on label and scientific name first |

## The object

`{"v":1,"flags":["burst_coincident","cold_insect"],"level":"likely","evidence":{"temp_c":2.0,"temp_age_s":0,"floor_c":5.0,"burst_n":25,"burst_spread_db":1.0,"window_s":30.0}}`

| flag | rule (defaults) |
|---|---|
| `cold_insect` | insect group, usable temperature, `temp_c < 5.0` (strict: exactly 5.0 not flagged) |
| `burst_coincident` | insect group, >= 20 dsp `transient` records within +-30 s of the report (both ends inclusive) and their MEDIAN `beam_spread_db` <= 3.0 |
| `temp_unknown` | insect group and no usable temperature (feed absent, no record, newest record > 3 h old) |
| `mechanical_label` | mechanical group, label not hidden |

* `flags` sorted ascending.
* `level`: `likely` = `cold_insect` present; `suspect` = `burst_coincident` without `cold_insect`; `none` otherwise.
* `evidence` always has the six keys (null where not applicable): `temp_c` and `burst_spread_db` 1 decimal, `temp_age_s` (`h - t_hour_utc`, multiple of 3600), `burst_n` integer (null when no transient feed), `window_s`, `floor_c`.
* Temperature by the reader rule of specs/ambient-temp.md.
* A record that is not an `ambient_temp` v1 object, has an hour off the 3600 grid, or a temperature not a finite number in -90..60 is skipped.
* Readers ignore unknown fields; an unknown `v` = absent.
* Floors, window, count, spread are `GateConfig` fields and replay flags, never environment variables.

## Replay report (`artifact_replay.json`, text twin `artifact_replay.txt`)

`tools/artifact_replay.py --data-dir <snapshot> --out <dir> [--since T] [--until T] [--floor-c 5.0] [--sweep 3,5,7,10] [--taxa PATH] [--timing] [--json]`

* Run on a frozen COPY of the live files (`dsp.jsonl.1` is the only copy of older transients).
* `--taxa` defaults to `ui/taxa.json` of this repository (NOT analysis/taxa.json).
* Writes ONLY the two files into `--out`; refuses an `--out` equal to or inside `<data-dir>/analysis` or `<data-dir>/history` (exit 2).
* Exit: 0 valid, 1 degraded (report still written; `experiment.valid` false), 2 usage error.
* Same inputs give a byte-identical report (`--timing` adds wall and CPU seconds and gives that up).

Top-level keys:

* `k` (`artifact_replay`), `v`, `params`, `experiment.valid`, `coverage`.
* `counts`: `by_group` and `by_label` (temperature bin `lt5` / `5to15` / `ge15` / `unknown` x flagged / unflagged), `flags`, `levels`.
* `confusion`: insect reports, cold / warm / unknown temperature x burst / no burst.
* `flagged_by_hour`: 24 UTC-hour bins per date, and transient rate by temperature bin.
* `transient_hours`: transients per UTC hour with the hour's temperature; survives dsp.jsonl rotation.
* `burst_by_temp_bin`, `floor_sweep` (per floor: per bin and per species n, flagged by floor alone, flagged by floor or burst), `flagged_by_group` (zero outside insect), `out_of_scope_counts`, `excluded_hidden_reports` (one integer).

`coverage`:

* Input identity (name, byte size, sha256 of every file read); report / transient / ambient time ranges; `taxa.ok`; skipped hostile line counts; `transients_cover_reports`; `ambient_covers_reports`; `degraded`; `degraded_reasons`.
* `degraded_reasons` (closed set): `taxa_missing`, `ambient_missing`, `dsp_missing`, `birds_missing`, `no_reports_in_window`, `transients_do_not_cover_reports` (60 s tolerance), `scope_violation` (a flag outside insect).
* A hostile line (not JSON, not an object, non-finite time, > 1 MiB, out-of-range temperature) is skipped and counted, never an abort.

## Privacy

* Only JSON metadata is read.
* Rows with a hidden label are dropped before any counter, range or hash is touched; the report holds the integer count only, and contains neither a hidden label nor its words.
* Schema rejects `lat`, `lon`, `latitude`, `longitude`. No network, audio, environment variable or private path; the fixture is synthetic.
