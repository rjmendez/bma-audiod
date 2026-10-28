# artifact-gate: advisory artifact flags for insect and mechanical classes

Contract: `contracts/specs/artifact-gate.md` 1.0.0. Phase 1 offline; phase 2 UI; phase 3 analyzer.

## 1. Design

### 1.1 Scope
Gate evaluates a report (replay) or event (same species key within EVENT_GAP_S, beams merged, at event start). Group from `analysis/taxa.py`:

| group | evaluated | rules |
|---|---|---|
| insect | yes | cold_insect, burst_coincident, temp_unknown |
| mechanical (not human) | label only | `mechanical_label`, informational |
| bird, mammal, amphibian, other-bio, None | NO: `evaluate` returns None | structural, tested |
| human labels (Human vocal, Human non-vocal, Speech) | NO output at all | policy |

Floors for other groups: config only.

### 1.2 Reason codes (closed vocabulary)

| code | meaning | rule (defaults) |
|---|---|---|
| `cold_insect` | insect label, known ambient below floor | group insect, temp known, temp_c < 5.0 (strict) |
| `burst_coincident` | inside a diffuse DSP transient burst | group insect, n_transients(t +-30 s) >= 20 and median beam_spread_db <= 3.0 |
| `temp_unknown` | no usable temperature (absent, gap, older than 3 h) | group insect, temp unknown |
| `mechanical_label` | Engine, Power tools, Siren, ... | group mechanical, not human |

`level`: `likely` = `cold_insect` present; `suspect` = `burst_coincident` without `cold_insect`;
- `none` otherwise (including `temp_unknown` alone, `mechanical_label` alone). `evidence` always carries `temp_c`, `temp_age_s`, `floor_c`, `burst_n`, `burst_spread_db`, `window_s`; no audio, no coordinates.

Burst setting: event-level 30 s, n>=20, spread<=3 (pinned by a test). Hourly counts are not used.

### 1.3 Files (bench-kit/bma-audiod)

| file | status |
|---|---|
| analysis/artifact_gate.py (pure, stdlib only) | new |
| analysis/test_artifact_gate.py | new |
| tools/artifact_replay.py (read-only inputs, writes only into `--out`) | new |
| tools/test_artifact_replay.py | new |
| contracts/specs/artifact-gate.md, contracts/schemas/artifact_gate.schema.json, contracts/fixtures/artifact_gate_report.json | new |
| contracts/CHANGELOG.md, check_contracts.py, test_contracts.py | changed |
| ui/bma_ui.py, specs/ui-http.md 1.4.0, ui schema, fixtures (`artifact_flags`, `artifact` parameter, `artifact_events`) | phase 2 only |

No environment variable; knobs are CLI flags and `GateConfig` fields; data dir is `--data-dir` (default BMA_DATA_DIR). `scripts/check_private_paths.py` stays clean.

### 1.4 Module API

```python
# analysis/artifact_gate.py  (stdlib only)
REASONS = ("cold_insect", "burst_coincident", "temp_unknown", "mechanical_label")
LEVELS = ("none", "suspect", "likely")

@dataclass(frozen=True)
class GateConfig:
    floors_c: tuple = (("insect", 5.0),)     # group -> floor; groups absent have no temperature rule
    temp_max_age_s: int = 10800
    burst_window_s: float = 30.0
    burst_min_transients: int = 20
    burst_max_spread_db: float = 3.0
    scope_groups: tuple = ("insect",)
    label_groups: tuple = ("mechanical",)
    human_labels: frozenset = frozenset({"human vocal", "human non-vocal", "speech"})

class AmbientIndex:       # AmbientIndex(records); last record wins per t_hour_utc; non-dict/NaN/out-of-range skipped
    def temp_at(self, t): # -> (temp_c, age_s) or (None, None); never a later hour, never interpolated
class TransientIndex:     # TransientIndex(pairs of (t, beam_spread_db))
    def window(self, t, half_s):  # -> (n, median_spread or None)

def evaluate(*, group, label, scientific, t, ambient, transients, cfg=DEFAULT):
    """None when out of scope or a human label (the caller must emit nothing).
    Else {"v":1,"flags":[sorted REASONS],"level":"none|suspect|likely","evidence":{...}}"""
```
Pure: no I/O, no clock, no mutation; byte-identical JSON for identical inputs (flags sorted; temperature and spread rounded to 1 decimal; counts integers). `ambient=None` = `temp_unknown`; `transients=None` leaves `burst_n` null, adds no flag. Floor test is strict `<`.

### 1.5 Field `artifact_flags`
`{"v":1,"flags":["cold_insect","burst_coincident"],"level":"likely","evidence":{"temp_c":2.1,"temp_age_s":1800,"floor_c":5.0,"burst_n":214,"burst_spread_db":1.6,"window_s":30.0}}`
- Clean in-scope events: `flags: []`, `level: "none"`. Out-of-scope: NO `artifact_flags` key (replay counts them under `out_of_scope`). Readers ignore unknown fields; unknown `v` = absent.
- Phase 2 (ui-http 1.4.0, MINOR): events gain optional `artifact_flags`; query parameter `artifact` = `any` (default) | `none` | `only`; `species` and `density` rows gain `artifact_events` (`events` unchanged).

### 1.6 Replay `tools/artifact_replay.py`
`artifact_replay.py --data-dir DIR --since T --until T [--floor-c 5.0] [--sweep 3,5,7,10] [--taxa PATH] --out DIR [--json]`
- Reads analysis/birds.jsonl, dsp.jsonl(.1), history/ambient_temp.jsonl(.1), taxa table (`--taxa` default `ui/taxa.json`). Writes only `<out>/artifact_replay.json` and `.txt`; refuses `--out` equal to or inside `analysis/` or `history/`. Single streaming pass.
- Report: `counts` (label x temperature bin (<5, 5-15, >=15, unknown) x flagged, per group); `confusion` (in-scope reports, temperature rule vs burst evidence);
  - `flagged_by_hour` (24 UTC bins, per date x hour; transient rate by temperature bin); `floor_sweep` (per floor, flagged fraction of katydid at <5 / 5-15 / >=15 C, per species);
  - `out_of_scope_counts` (per label; flagged-by-group zero outside insect); `excluded_hidden_reports` (one integer; no row, time, beam, confidence);
  - `coverage` (ambient range vs birds range, taxa status, skipped line counts, `degraded`, wall time, CPU seconds, `inputs` byte size + sha256, `transients` time range); `transient_hours` (transients per UTC hour with the hour's temperature).
- `degraded` set on: `taxa_missing` (exit 1), `transients_do_not_cover_reports`; a degraded run sets `experiment.valid=false`.
- Hidden labels: `artifact_gate.is_hidden_label` hides every label starting with `human` (any case) and `speech`, on label and scientific name;
  - replay drops those rows before any counter, range or time is touched (`human whistle` included). The words "human" and "speech" occur nowhere in the output (test scans the json and txt); positive twin: Engine present.
- Events are not evaluated in phase 1, only reports.

### 1.7 Degradation (fail-open = no flag)

| failure | behaviour |
|---|---|
| ambient absent/empty | `temp_unknown`; no `cold_insect`; burst evidence still computed; `suspect` only with burst, else `none` |
| nearest record older than 3 h | same as absent for that report |
| dsp.jsonl missing/rotated | burst evidence null; temperature rule unaffected |
| taxa table missing | group None: no flags, replay prints `taxa_missing` |
| hostile line (non-JSON, huge, NaN, out-of-range temp) | skipped and counted |
| future events / clock skew | a later temperature hour is never used |

### 1.8 Budget
`evaluate`: two bisects + median over k transients. Replay: < 60 s on one core; report < 200 KB; nothing written to analysis/ or history/. No GPU, no network.

## 2. Contract changes

- `contracts/specs/artifact-gate.md` 1.0.0: scope table, reason vocabulary, level rule, evidence keys, reader rules, ambient-temp reader rule reference, privacy, "advisory, never deletes".
- `artifact_gate.schema.json`: root = replay report (`k` const `artifact_replay`); `$defs.artifact_flags` (v const 1; flags enum, unique; level enum; evidence additionalProperties false; `not` rule rejecting lat/lon/latitude/longitude); `$defs.replay_report`.
- Registered through SCHEMAS, SPECS, FIXTURES in check_contracts.py, version string in test_contracts.py, CHANGELOG entry, `contracts/tools/make_artifact_gate_fixtures.py` (runs the real replay on invented data).
- analysis-outputs stays 1.2.0 in phases 1-2; phase 3 = 1.3.0. ui-http 1.3.0 -> 1.4.0 in phase 2 only. ambient-temp unchanged.

## 3. Tests

Script style: `check(name, ok, detail)`, exit 1 on failure, `python -I test_x.py`; `raises(exc, fn, match)` helper.

`analysis/test_artifact_gate.py`
1. AmbientIndex vs spec: record at h-10800 usable; h-10801 -> (None, None); later hour never used; duplicate hour last wins; garbage skipped; empty -> (None, None).
2. Floor: 4.9 flagged, 5.0 not, 5.1 not; 4.9 C with bird label -> None.
3. 2.0 C insect -> `likely`; 16.0 C -> flags [], none.
4. Burst: 19 no, 20 yes; spread 3.0 yes, 3.1 no; transient at t+30 inside, t+30.001 outside; even-count median = mean of the two middle values.
5. Level matrix: (cold,burst) `likely`; (cold,no burst) `likely`; (warm,burst) `suspect`; (warm,no burst) none; (unknown,burst) suspect + `temp_unknown`; (unknown,no burst) none + `temp_unknown`.
6. Scope: over every label of `ui/taxa.json`, groups other than insect/mechanical -> None at any temperature/transient count; woodpecker, nuthatch, owl at -10 C with 500 transients -> None.
7. Human labels -> None; Engine -> `mechanical_label`, none.
8. Determinism: byte-identical (`sort_keys`); arguments unchanged.
9. Hostile: temp NaN, inf, 1e400, -100 unusable without exception; non-finite `t` raises `ValueError` matching "t must be finite".
10. Config: floor 10 C flips the 7 C case; group without a floor has no temperature rule.

`tools/test_artifact_replay.py` (synthetic data in tmp)
- Crafted 40-report set, every confusion cell non-zero and distinct: exact confusion, flagged-by-hour, sweep, `out_of_scope_counts`.
- `--out` inside analysis/ exits 2 with a specific message; valid out dir writes exactly two files.
- N hidden rows -> `excluded_hidden_reports == N`, their timestamps nowhere in output.
- Missing ambient / dsp / taxa: documented degraded numbers, `degraded: true`, `experiment.valid: false`.

Contract tests: fixture validates; record with `lat` rejected; unknown flag rejected; each known flag accepted.

Mutations (`scripts/run_artifact_gate_mutations.py`: copy to scratch, one textual edit, run test script, assert exit 1 plus the named failing checks, assert restored copy green): M1 `<` to `<=` at floor; M2 `>= burst_min` to `>`;
- M3 window 30 to 60; M4 remove scope check; M5 remove human-label guard; M6 age cutoff 10800 to 14400; M7 allow later hour in `temp_at`; M8 last-wins to first-wins; M9 median to mean; M10 replay writes into analysis/.

## 4. Acceptance (phase 1)

Replay birds.jsonl at floor 5 C, default burst evidence:
- >= 90% of cold (<5 C) katydid reports flagged.
- <= 5% of katydid reports at >= 15 C flagged (`cold_insect` 0; `burst_coincident` at 30 s, spread<=3 only).
- Bird, mammal, amphibian reports flagged: 0.
- Floor table (3, 5, 7, 10 C) published.
- Two replay runs byte-identical, < 60 s on one core.
- M1-M10 red; `check_private_paths` clean; contract checker passes.
