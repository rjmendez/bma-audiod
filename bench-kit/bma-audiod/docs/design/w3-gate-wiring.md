# Artifact gate wiring: where it runs, what it costs, what the dashboard shows

Date 2026-10-27. Track C of wave 3, branch `w3-gate` (stacked on `w3-manifest`), repo `bench-kit/bma-audiod`. Advice only: nothing is deleted or hidden by default.
Tags: [S] verified by running it, [I] inferred, [U] unverified. Background: docs/design/artifact-gate.md (merged phase 1: the pure gate, the replay tool, contract `artifact-gate` 1.0.0).

## 1. Decision

The gate runs DASHBOARD-SIDE, at read time, and NOT in the analyzer. Contract change: `ui-http` 1.10.0 -> 1.11.0 (minor, additive; master took 1.8.0 for the Review tab, 1.9.0 for its evidence, 1.10.0 for /healthz). `artifact-gate` stays 1.0.0 (its schema did not change).

| option | verdict | why |
|---|---|---|
| A. analyzer-side optional field on `bird` records (analysis-records 1.3.0) | rejected for now | the birds plugin has neither the temperature nor the dsp transients (a different plugin, a different file); wiring them is cross-plugin state in a process that already sits at its 30 ms per audio second budget (indices); it needs an owner deploy and a live analyzer restart; and the verdict would be frozen into the permanent records with today's provisional floor (5 C), while the replay says the floor is the one knob still open (section 6) |
| B. dashboard-side annotation at read time | CHOSEN | zero analyzer cost [S], no analyzer change, thresholds can change without rewriting history, the dashboard already holds the events and tails dsp.jsonl, deploy = the normal pinned dashboard copy (no analyzer restart) |
| C. both | rejected | two sources of truth for one verdict; only worth it when a consumer other than the dashboard needs flags in the stored records (offline tools already import `analysis/artifact_gate.py` directly) |

A temperature-only analyzer stamp stays possible later (phase 3 of the design) if a non-dashboard consumer needs it; nothing here blocks it.

## 2. CPU cost (budget: about 0 ms/s added to the analyzer)

Analyzer: 0 ms/s [S]. `git diff w3-manifest..w3-gate -- bench-kit/bma-audiod/analysis` is empty (no analyzer file changed, `artifact_gate.py` itself is unchanged), so the analyzer image, the golden manifest proof and the 30 ms budget are untouched.

Dashboard process (measured with `bench.py` in the scratch dir, real App over a read-only copy of 2.9 days of birds.jsonl + dsp.jsonl + ambient_temp.jsonl, on the loaded machine, process CPU):

| item | gate off | gate on |
|---|---|---|
| backfill at dashboard start (once) | 0.33 s CPU | 3.3 s CPU (parses about 48 MB of dsp.jsonl to get 46,168 transients; one-time, in the dashboard process, never the analyzer) |
| live ingest per dsp `transient` record | 0 | 9.0 us (46,168 transients are held in two float arrays: 0.74 MB) |
| first verdict of an event | 0 | 9.1 us (4,810 insect + mechanical events: 44 ms once; cached on the event afterwards) |
| `/api/detections/events` (limit 500) | 15-18 ms | 12-17 ms (no measurable difference) |
| `/api/detections/species` | 33 ms | 24-34 ms steady, 74 ms on the first call (the one-time verdicts) |
| `/api/status` | 3-5 ms | 4-10 ms (adds the 24 h scored / flagged count) |

The live ingest cost is 9 us per transient; the transient rate is 0.2 per second on average and a few per second inside a burst (52,259 in 2.9 days), so the steady-state cost is about 0.03 ms/s at the very most [I from the measured rate]. With `--no-artifact-gate` the dsp transients are not read at all and the backfill is back to 0.4 s (test: 0 transients held although the file has one).

## 3. Data and freshness rules

* Temperature: `<data-dir>/history/ambient_temp.jsonl` (+ `.1`), the ambient-temp reader rule applied by `AmbientIndex`: the nearest PAST UTC hour record, at most 3 h old (10,800 s inclusive; 4 h is `temp_unknown`), never a later hour, never interpolated, the last record of an hour wins, a record off the hour grid / non-finite / outside -90..60 is skipped and counted (`status.artifact_gate.ambient.skipped`). The files are re-checked at most every 30 s by (mtime, size) and re-read only when they changed (a few KB; tail of 1 MiB per file); a cached verdict is invalidated when they did (tested with a recording wrapper: 1 gate call for two identical requests, +1 after the file changed).
* Transients: the `transient` records of `analysis/dsp.jsonl` seen by the dashboard's existing tailer; backfill of the last 48 MB (about two days: the live file then the rotated one), then live. Burst evidence is only produced when the event's +-30 s window lies fully inside the span of dsp records read; outside it `burst_n` is null and no burst flag is set (never a false "no burst"; tested at both edges).
* Dashboard cost and its limits: the gate is on by default and raises the dsp startup backfill from 128 KiB to 48 MiB (the 3.3 s CPU one-time figure in section 2). The scoring of in-memory insect and mechanical events is lazy (first artifact-filtered request, first species list or status call) and starts caching only after the backfill is done. Dashboard RSS was not separately measured [U]: the added state is the two transient float arrays (0.74 MB for 46,168 transients) plus one small verdict tuple per scored insect or mechanical event (4,810 events in the measured data), so [I] well under 10 MB; `--no-artifact-gate` removes all of it.
* The dsp tailer behaviour change: with the gate on, the dsp record kinds `transient`, `tonal_state`, `tonal_appeared` and `tonal_gone` are now accepted by the tailer (before, `_latest('dsp')` rejected every kind but `dsp`, and they were counted as rejected lines). `status.latest.dsp` still only uses `k == "dsp"` records, so the status values are unchanged; only the tailer's rejected-line count drops.
* The event is evaluated at its first report (design section 4.1). A verdict is cached on the event only when the window was covered and the backfill is done.
* Hidden labels: human-voice labels never enter the dashboard's stores (`DetStore.add` drops them first, only a count is kept), so they cannot carry a verdict, appear in a list, or be counted per row; the gate itself also returns None for them (belt and braces). The analyzer still writes those records (the clip speech guard depends on it): untouched.

## 4. Failure behaviour (never an error, never hides anything) [S: each tested]

| situation | result |
|---|---|
| no `ambient_temp.jsonl` / unreadable / garbage lines | empty index: insect events read `temp_unknown` (level `none`, not "flagged"), `status.artifact_gate.ambient.records` 0; a file that appears later is used at the next 30 s check |
| newest record older than 3 h | `temp_unknown` |
| no dsp file / dsp file does not cover the window | `burst_n` null, no burst flag |
| `artifact_gate.py` missing next to the dashboard (and not in `../analysis`) or `--no-artifact-gate` | no `artifact_flags` key anywhere, `status.artifact_gate.enabled` false (`available` says which), `artifact=none` matches everything, `artifact=only` nothing, HTTP 200 |
| unknown `artifact` value / repeated parameter | 422 `invalid_parameter` naming `artifact` (same as every other parameter) |
| rollup-sourced events (older than the in-memory window) | carry no flags (rollups have none): never scored, counted in `artifact_unscored_events`; `artifact=none` cannot leave them out and says so |

## 5. What shipped

Contract (ui-http 1.11.0, schema + spec + fixtures + changelog fragment `contracts/changelog.d/ui-http.md`):
* optional `artifact_flags` on events and the event detail, for insect and mechanical events only (bird, mammal, amphibian, other-bio: no key); the object is the `artifact-gate` object, copied into the ui-http schema with a test that the two stay equal;
* query parameter `artifact` = `any` (default, byte-identical to 1.8.0 apart from the echo) | `none` | `only` on events, density and species, echoed as `artifact`;
* species rows `artifact_events`, response `artifact_events_total` (flagged events whatever the mode hides: the number next to the toggle) and `artifact_unscored_events`;
* `status.artifact_gate`: enabled, available, the parameters, ambient feed state, transient feed span, 24 h events scored / flagged;
* server flag `--no-artifact-gate`; no environment variable.
* `artifact_gate.py` is copied next to the dashboard by `pc/install_task.ps1` and `docker/dashboard.Dockerfile` (repo layout falls back to `../analysis`).
* fixtures regenerated by `contracts/tools/make_ui_fixtures.py` (the synthetic tree now has a 2.3 C hour and 25 diffuse transients; the events example is the insect view: three cold katydids, one also in a burst). The generator writes CRLF on Windows: normalised to LF before committing. NOT touched: a pre-existing drift in `ui_rollup.jsonl` (`non_species` of one row) that the generator would also change.

Dashboard (ui/static/app.js; the existing tooltip system):
* a "possible artifact" badge with a tooltip (the numbers, the rule, "advisory only: the detection is kept and counted") on flagged insect events in the events table, the event detail ("Artifact check" row) and a per-species "N of M possible artifact" badge in the species summary;
* a species-list toggle "hide flagged insect detections", OFF by default, with an info tip; the count next to it is always shown ("412 insect events flagged as possible artifacts (still counted)" off, "412 flagged insect events hidden" on, plus "N older insect events were not scored" when rollups are involved); nothing is deleted;
* an honest System-panel note: the rule, the temperature feed (records, newest age, or "no temperature records: every insect reads temperature unknown"), the transient feed, the 24 h tally, and "a heuristic, not ground truth; an unflagged one can still be an artifact";
* human-voice labels stay hidden everywhere (a test asserts no label, scientific name or id of one appears in events, density, species, or the page text).

## 6. Real-data check [S] (read-only copies in the scratch dir; the live pipeline was only read)

`tools/artifact_replay.py` over copies of birds.jsonl, dsp.jsonl(.1) and history/ambient_temp.jsonl (valid=true, degraded=false, transients and ambient cover the reports in all three runs).

Reproduce (re-run on 2026-10-27: the design-window report is byte-identical to the one behind the table). Two private snapshot data dirs, each with `analysis/birds.jsonl`, `analysis/dsp.jsonl`, `history/ambient_temp.jsonl`, taken at 2026-10-27 16:24Z (not committed; the hashes identify them): DIR_LAST (last 24 h, 4,923 rows, birds.jsonl sha256 6e8364cb49b4c84ca4e079efbb629529496ccc418f632ad70cd8c8ebd07cccf8) and DIR_EXT (from 2026-10-24 23:11Z, 13,752 rows, birds.jsonl sha256 002805dc3e7585d2dfa900ff786fe02881b5599e3bf79af99e1072d7d0d7fc26); ambient_temp.jsonl is the same in both (sha256 da9b3b5770ac9af8fa2fe24050a369536f5291b3cf70d5f980054599f76f370f).
* last 24 h row: `artifact_replay.py --data-dir DIR_LAST --out OUT`
* since 10-24 row: `artifact_replay.py --data-dir DIR_EXT --out OUT`
* design window row: `artifact_replay.py --data-dir DIR_EXT --since 1792883460 --until 1793060940 --out OUT` (2026-10-24T23:11Z to 2026-10-27T00:29Z)

Flagged share (level `suspect` or `likely`) per taxa group. Out-of-scope groups get no verdict by construction (the replay has no row for them; their report counts come from the same snapshot through taxa.py):

| window | insect reports | insect flagged | insect <5 C | mechanical reports (informational flag only) | bird / mammal / amphibian reports (never flagged) |
|---|---|---|---|---|---|
| LAST 24 h of birds.jsonl (2026-10-26 16:24Z to 2026-10-27 16:24Z) | 626 | 7 = 1.1 % | 0 | 2,397 (0 flagged) | 1,478 / 19 / 403 (0 flagged) |
| 2026-10-24 23:11Z to the end of the data (2026-10-27 16:24Z) | 5,894 | 5,088 = 86.3 % | 5,080 | 3,688 (0 flagged) | 3,581 / 68 / 521 (0 flagged) |
| the design window (2026-10-24 23:11Z to 2026-10-27 00:29Z) | 5,541 | 5,088 = 91.8 % | 5,080 | 2,237 (0 flagged) | 2,605 / 64 / 125 (0 flagged) |

`Common Virtuoso Katydid` alone: last 24 h 564 reports, 0 below 5 C, 7 flagged (7 of 564 = 1.2 % of the katydid reports; the same 7 are 1.1 % of all 626 insect reports in the table; all burst-only); design window 5,055 reports, 4,706 below 5 C = 93.1 % (exactly the design document's 4,706 / 5,055), 4,714 flagged (93.3 %); since 10-24: 5,380, 4,706 below 5 C (87.5 %), 4,714 flagged.

Reading: the ~92 % cold-insect finding REPRODUCES exactly on the design window (91.8 % of insect reports, 93.1 % of the katydid), and it does NOT hold for the literal last day: the weather warmed (the last 24 h have 430 insect reports at 5-15 C and 196 at 15 C or more, none below 5 C), so the gate correctly flags almost nothing there (7 burst-only). That is the intended behaviour (a temperature rule, not a label blacklist) but it means the dashboard's top "species" is an artifact only while it is cold; on a warm day the katydid counts are plausible and the top of the list is the Engine class. The floor sweep on the last day shows the one open question: a 10 C floor would flag 311 of the 430 reports at 5-15 C (no primary katydid floor was found: design section 3, [U]); the shipped floor stays the conservative 5 C.

Other facts from the same data: the 24 h mechanical rows are mostly Engine (2,393 of 2,397); 7 bursts in the last day are all warm-night `suspect`, none `likely`.

## 7. Tests (all run, each with positive twins and a mutation that goes red)

* `ui/test_artifact_ui.py`, 53 exact-value checks: cold 2.0 C -> exactly `["cold_insect"]` with the full evidence object; warm 12.0 C and exactly 5.0 C unflagged, 4.9 C flagged; 3 h (10,800 s) used, 4 h `temp_unknown`; no ambient / corrupt ambient / late ambient; 20 transients at 1.0 dB burst, 19 not, median 3.1 dB not, exactly 3.0 dB yes, covered-quiet 0 vs uncovered null; `any` = default, `none` / `only` partition; species / density / event detail; unscored rollups; gate off; 422s over real HTTP; human labels hidden; no coordinates or paths; the verdict cache with a recording wrapper.
* `ui/test_app_js.js` (+17 checks, 176 total), `ui/test_dom_artifact.py` (9 checks in real Edge: badge, tooltip text, toggle default off, count text, toggle on/off, System note, warm positive twin, human labels).
* contracts: schema equality of the copied object, fixtures validate, three negative twins (artifact reason outside the closed set, a `lat` in the evidence, a species response without `artifact_events_total`).
* Mutation run `ui/mutation_artifact.py` (--dom adds two Edge mutants): 13 mutants, all KILLED (none survived): `none` inverted; default mode `none`; suspect not flagged; burst for an uncovered window; cache ignoring the ambient file (this one SURVIVED the first test version: the cache path had no test, fixed by the recording-wrapper test, then killed); hidden count dropped; unscored dropped; floor `<=` (artifact_gate.py); missing ambient raising; JS badge for level none; JS count text inverted; DOM always asking `artifact=none`; DOM badge removed.
* Suites, run one at a time after the last change: check_contracts 0 drift; test_contracts 791 passed 0 failures; test_bma_ui 237, _config 22, _lan 50, _more 308, _spectrum 165, test_auth 86, test_auth2 92, test_auth_spectrum 37, test_passkeys 94, test_artifact_ui 53, test_app_js 176, docker install lists 0 failures, check_private_paths clean, Edge DOM: test_dom_artifact 9, test_dom_groups 24, test_dom_tips 15. (The two UI_VERSION assertions in test_bma_ui_spectrum and test_auth_spectrum were bumped to 1.8.0.)

## 8. Owner steps (not done by this track: no deploy, no restart, no push)

1. Merge the branch (after `w3-manifest`).
2. Re-run `pc/install_task.ps1` (it copies `artifact_gate.py` next to the dashboard) and restart ONLY the dashboard; the analyzer does not change and is not restarted. Until then the pinned dashboard simply shows no flags.
3. Decide the floor (5 C shipped) once the label store (verdict `artifact` labels) has data; the replay's floor sweep is the tool.
