contract: event-model
contract_version: 1.0.0
status: stable
producer: analysis/events.py (library); tools/bma_labels.py events (report)
consumers: analysis/beam_agreement.py, analysis/labels.py, the owner
schema: schemas/event_model.schema.json; example: fixtures/event_model_report.json (regenerate with tools/make_event_fixtures.py)

# event-model

Per-beam `bird` / `bird_filtered` reports -> events. Pure, stdlib, no I/O.

## Clustering

* Species key: `(scientific or common).strip().lower()`.
* Rows in the order given. Last event per key. Report joins it when `t_start - gap <= t <= t_end + gap`, else starts an event.
* `gap` = 3.0 s (labels, dashboard) or `--gap-s` (beam_agreement tables).
* Event = key + member rows in arrival order.

## Rejections

Each input row ends in exactly one place. Check order:

| name | rule |
|---|---|
| other_kind | not an object, or `k` not `bird` / `bird_filtered` |
| out_of_region | `k` is `bird_filtered` and `bird_filtered` is not an accepted kind |
| invalid | fails `valid_report`: `t` > 0, `beam` int 1..8, `confidence` 0..1, `common` non-empty string, `scientific` null or string |
| hidden_human | `common` or `scientific` is `human vocal`, `human non-vocal`, `human whistle` (case-insensitive); only when human rows are dropped |
| duplicate_window | same (key, t_ms, beam) already seen; rejected only with `dedup`, otherwise kept and counted in `duplicate_windows` |

## Conservation

`input == sum(len(event.rows)) + sum(rejected)`. Every `rejected` key present. Violation raises `ConservationError`; `bma_labels events` and `beam_agreement.py` exit 1.

## Event id (`ev1`)

* `t_ms = round(t * 1000)`; member = `(t_ms, beam)`; members sorted ascending, duplicates collapsed.
* text = `"ev1\n" + key + "\n" + first_t_ms + "\n" + ";".join("<t_ms>:<beam>")`.
* id = `"ev1-" + sha256(text, utf-8).hexdigest()[:24]`; regex `^ev1-[0-9a-f]{24}$`.
* Same input -> same id. Any member added or removed, or a different key -> new id.
* Vector: key `melanerpes carolinus`, members `(1792940400000,1) (1792940400000,2) (1792940402000,1)` -> `ev1-6a216aaaba6133f1325fc813`.

## Supersedes

`diff(prev_snapshots, current_events)`; snapshot = `{id, key, members: [[t_ms, beam], ...]}`.

* `kept`: id still current.
* `superseded {old: new}`: all members of the old event are in one current event of the same key.
* `orphaned`: members spread over several current events, or none.
* `new`: current ids neither kept nor a supersede target.
* `membership_changed = len(superseded)`.
* `merge_aliases(table, diff)` keeps `{old_id: current_id}` one hop. `resolve(id, table)`.

## Label ids

`verdict-labels` keeps its event id `<int(t_start * 1000)>-<slug>`; unchanged. Events gain `content_id` (`ev1-...`).
A folded verdict resolves as: `exact` (its `event_id` is current), `superseded` (a current event of the same `species_key` spans its `t_start_ms`; earliest wins; a second candidate counts in `ambiguous`), `orphaned` (none).
Label files are never rewritten.

## Dashboard mapping (DetStore, not changed)

| dashboard (ui/bma_ui.py) | event-model |
|---|---|
| `DetStore.add`, `EVENT_GAP_S` 3.0, `valid_bird`, per-key last event | `build(rows, 3.0, kinds=("bird","bird_filtered"), drop_human=False)` |
| event id `<start ms>-<slug>` | `labels.Event.event_id`; `content_id` = `ev1-...` |
| human rows hidden at query time | `drop_human=True` |

## Report (`bma_labels.py events`)

`{"k":"event_model_report","v":1,"id_version":"ev1","gap_s":3.0,"conservation":{input,events,reports,rejected{5 names},duplicate_windows},"labels":{exact,superseded,orphaned,ambiguous}}`

## Consumers

* `beam_agreement.py`: `build(rows, gap, kinds=("bird",), drop_human=True)`; report bytes unchanged; conservation line on stderr.
* `labels.py`: `build(rows, 3.0, kinds=("bird","bird_filtered"), drop_human=False)`; event ids unchanged.
