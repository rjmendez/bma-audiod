#!/usr/bin/env python3
"""Contract conformance checker for the BMA pipeline interfaces.

    python check_contracts.py                          # schemas + fixtures + versions (hermetic, CI-safe)
    python check_contracts.py --live [--root <data-dir>] [--lines 2000]
                                                       # also the last N lines of every live file

Exit code 0 = no drift, 1 = drift (each finding printed as  DRIFT <file>:<where> [<kind>] <json path>: <message>),
2 = usage / cannot run. Needs `jsonschema` (pip install jsonschema). See README.md for the policy this enforces."""
import argparse, glob, importlib.util, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("BMA_DATA_DIR") or "data"
sys.path.insert(0, HERE)
import ref_binary as R  # noqa: E402

try:
    from jsonschema import Draft202012Validator
except ImportError:                                          # pragma: no cover
    sys.exit("check_contracts.py needs the jsonschema package (pip install jsonschema)")

SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
# interface name -> schema file (binary interfaces have none; their spec files are checked for version only)
SCHEMAS = {"bridge-status": "bridge_status.schema.json", "sink-status": "sink_status.schema.json",
           "board-debug": "board_debug.schema.json", "analysis-records": "analysis_records.schema.json",
           "analysis-status": "analysis_status.schema.json", "beam-bands": "beam_bands.schema.json",
           "ui-http": "ui_http.schema.json", "run-history": "run_history.schema.json", "analysis-history": "analysis_history.schema.json",
           "clips": "clips.schema.json",
           "ambient-temp": "ambient_temp.schema.json",
           "artifact-gate": "artifact_gate.schema.json",
           "loss-log": "loss_log.schema.json",
           "beam-response-map": "beam_response_map.schema.json",
           "burst-scorecard": "burst_scorecard.schema.json",
           "verdict-labels": "verdict_labels.schema.json",
           "beam-agreement": "beam_agreement_report.schema.json",
           "perch-report": "perch_report.schema.json", "active-coherence-report": "active_coherence_report.schema.json",
           "eval-report": "eval_report.schema.json",
           "event-model": "event_model.schema.json",
           "ui-live": "ui_live.schema.json"}
SPECS = {"bmaa": "bmaa.md", "bridge-status": "bridge-status.md", "cable-stream": "cable-stream.md", "board-debug": "board-debug.md",
         "analysis-outputs": "analysis-outputs.md", "beam-bands": "beam-bands.md", "ui-http": "ui-http.md",
         "run-history": "run-history.md", "analysis-history": "analysis-history.md", "clips": "clips.md", "spectrum-packet": "spectrum-packet.md",
         "ambient-temp": "ambient-temp.md", "artifact-gate": "artifact-gate.md",
         "beam-agreement": "beam-agreement.md",
         "verdict-labels": "verdict-labels.md",
         "burst-scorecard": "burst-scorecard.md",
         "beam-response-map": "beam-response-map.md",
         "loss-log": "loss-log.md",
         "perch-report": "perch-report.md", "active-coherence-report": "active-coherence-report.md",
         "eval-report": "eval-report.md",
         "event-model": "event-model.md", "ui-live": "ui-live.md"}
# fixture file -> (schema name, kind of file)
FIXTURES = {"dsp.jsonl": "analysis-records", "spatial.jsonl": "analysis-records", "tags.jsonl": "analysis-records",
            "clap.jsonl": "analysis-records", "birds.jsonl": "analysis-records", "novelty.jsonl": "analysis-records",
            "board_debug.jsonl": "board-debug", "analysis_status.json": "analysis-status", "bridge_status.json": "bridge-status",
            "sink_status.json": "sink-status", "sink_status_metering_only.json": "sink-status", "beam_bands.jsonl": "beam-bands",
            "sink_history.jsonl": "run-history", "analyzer_history.jsonl": "run-history", "board_history.jsonl": "run-history",
            "run_ledger.jsonl": "run-history",
            "config_history.jsonl": "analysis-history", "hourly_rollup.jsonl": "analysis-history", "daily_summary.json": "analysis-history",
            "clips_index.jsonl": "clips", "clip_sidecar.json": "clips", "clip_sidecar_audio_expired.json": "clips",
            "analysis_status_clips.json": "analysis-status",
            "clips_index_visual.jsonl": "clips", "clips_index_lowconf.jsonl": "clips", "clip_sidecar_visual.json": "clips", "clip_sidecar_pcen.json": "clips", "analysis_status_clips_visual.json": "analysis-status",
            "ambient_temp.jsonl": "ambient-temp",
            "artifact_gate_report.json": "artifact-gate",
            "beam_agreement_report.json": "beam-agreement",
            "verdict_labels.jsonl": "verdict-labels",
            "burst_scorecard.json": "burst-scorecard",
            "burst_hours.jsonl": "burst-scorecard",
            "beam_response_map.json": "beam-response-map",
            "loss_log.jsonl": "loss-log",
            "perch_report.json": "perch-report", "active_coherence_report.json": "active-coherence-report",
            "eval_report.json": "eval-report",
            "event_model_report.json": "event-model"}
# verdict-labels: the second shape of that schema (a tool OUTPUT, not a file the pipeline writes, so it is a fixture only and never a LIVE file):
# fixture file -> $defs name of verdict_labels.schema.json (the schema's `x-def-files` must say the same; test_contracts.py checks it)
LABEL_FIXTURES = {"review_queue.json": "review_queue"}
# ui-http: fixture file -> $defs name of ui_http.schema.json (one example response per endpoint; the rollup file holds one line of each kind)
UI_FIXTURES = {"ui_status.json": "status", "ui_events.json": "events", "ui_event.json": "event", "ui_density.json": "density", "ui_species.json": "species", "ui_beams_live.json": "beams_live",
               "ui_beams_stats.json": "beams_stats", "ui_beams_heatmap.json": "beams_heatmap", "ui_system.json": "system",
               "ui_calibration.json": "calibration", "ui_problem.json": "problem", "ui_problem_401.json": "problem", "ui_problem_429.json": "problem", "ui_rollup.jsonl": "rollup", "ui_spectrum.json": "spectrum", "ui_spectrum_auth_required.json": "spectrum", "ui_session.json": "session", "ui_review_next.json": "review_next", "ui_review_verdict.json": "review_verdict",
               "ui_review_stats.json": "review_stats", "ui_healthz.json": "healthz", "ui_problem_422_not_labelable.json": "problem"}
# live file (relative to --root) -> (schema name, optional)
LIVE = {"analysis/dsp.jsonl": ("analysis-records", False), "analysis/spatial.jsonl": ("analysis-records", False),
        "analysis/tags.jsonl": ("analysis-records", False), "analysis/clap.jsonl": ("analysis-records", False),
        "analysis/birds.jsonl": ("analysis-records", False), "analysis/novelty.jsonl": ("analysis-records", False),
        "analysis/board_debug.jsonl": ("board-debug", False), "analysis/status.json": ("analysis-status", False),
        "bridge_status.json": ("bridge-status", False), "sink_status.json": ("sink-status", True),
        # run-history files (<data-dir>/history): optional until the producer that writes each has been deployed
        "history/sink_history.jsonl": ("run-history", True), "history/analyzer_history.jsonl": ("run-history", True),
        "history/board_history.jsonl": ("run-history", True), "history/run_ledger.jsonl": ("run-history", True),
        "history/config_history.jsonl": ("analysis-history", True), "history/hourly_rollup.jsonl": ("analysis-history", True),
        "history/daily_summary.json": ("analysis-history", True),
        # wildlife clips index (analysis/clips/index.jsonl, written only once the analyzer with clip capture runs and stores a clip)
        "analysis/clips/index.jsonl": ("clips", True),

        "history/loss.jsonl": ("loss-log", True),                       # optional until loss_log.py is deployed
        "history/ambient_temp.jsonl": ("ambient-temp", True),          # optional: only written when a geo config exists
        "labels/labels.jsonl": ("verdict-labels", True)}               # optional: created by the first `bma_labels add`


# The analyzer plugins own their contract rows: interface, schema, spec, fixtures and live files are declared in the plugin's entry of
# analysis/plugins.manifest.json and added here (docs/design/w3-plugin-manifest.md). A name present in a hand row and in the manifest is an error.
def _load_manifest():
    spec = importlib.util.spec_from_file_location("bma_plugin_manifest", os.path.join(HERE, "..", "analysis", "manifest.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, mod.load(os.path.join(HERE, "..", "analysis", "plugins.manifest.json"))


MF, PLUGIN_MANIFEST = _load_manifest()
PLUGIN_CONTRACTS = MF.contracts(PLUGIN_MANIFEST)             # interface -> manifest contract dict (name, version, schema, spec, fixtures, live, tests, plugin)
for _tbl, _rows in zip((SCHEMAS, SPECS, FIXTURES, LIVE), MF.contract_tables(PLUGIN_MANIFEST)):
    _dup = sorted(set(_tbl) & set(_rows))
    if _dup:
        raise SystemExit(f"plugins.manifest.json declares {_dup}, which check_contracts.py also lists by hand")
    _tbl.update(_rows)
CHANGELOG_FRAGMENTS = "changelog.d"                            # contracts/changelog.d/<interface>.md: one file per new entry, so two PRs never touch the same line


class Drift:
    def __init__(self, file, where, kind, path, message):
        self.file, self.where, self.kind, self.path, self.message = file, where, kind, path, message

    def __str__(self):
        return f"DRIFT {self.file}:{self.where} [{self.kind}] {self.path}: {self.message}"


def load_schemas(schema_dir):
    out = {}
    for name, fn in SCHEMAS.items():
        with open(os.path.join(schema_dir, fn), encoding="utf-8") as f:
            out[name] = json.load(f)
    return out


def kind_validator(schema, kind):
    """Validator for one record kind: $defs/<kind> resolved inside the schema's own $defs."""
    disc = schema["x-discriminator"]["map"][kind]
    return Draft202012Validator({"$schema": schema["$schema"], "$ref": f"#/$defs/{disc}", "$defs": schema["$defs"]})


def whole_validator(schema):
    return Draft202012Validator(schema)


def errors(validator, rec):
    return sorted(validator.iter_errors(rec), key=lambda e: [str(p) for p in e.absolute_path])


def check_record(file, where, rec, schema, allowed_kinds, validators):
    """-> [Drift] for one decoded JSON object of a discriminated JSONL file."""
    disc = schema["x-discriminator"]
    field = disc["field"]
    if not isinstance(rec, dict):
        return [Drift(file, where, "?", "$", f"record is {type(rec).__name__}, expected an object")]
    kind = rec.get(field)
    if kind not in disc["map"]:
        return [Drift(file, where, str(kind), f"$.{field}", f"unknown record kind {kind!r}; the contract knows {sorted(disc['map'])} "
                      "(a producer change needs a contract change in the same commit)")]
    if allowed_kinds is not None and kind not in allowed_kinds:
        return [Drift(file, where, kind, f"$.{field}", f"kind {kind!r} is not allowed in {os.path.basename(file)} (allowed: {allowed_kinds})")]
    v = validators.setdefault(kind, kind_validator(schema, kind))
    out = [Drift(file, where, kind, e.json_path, e.message) for e in errors(v, rec)]
    if kind == "status" and field == "type" and isinstance(rec.get("json"), str):     # board_debug status: double-encoded inner JSON
        try:
            inner = json.loads(rec["json"])
        except ValueError as ex:
            out.append(Drift(file, where, kind, "$.json", f"inner JSON string does not parse: {ex}"))
        else:
            iv = Draft202012Validator({"$schema": schema["$schema"], "$ref": "#/$defs/status_inner", "$defs": schema["$defs"]})
            out += [Drift(file, where, kind, "$.json" + e.json_path[1:], e.message) for e in errors(iv, inner)]
    return out


def tail_lines(path, n):
    """The last n complete lines of a file that another process may be appending to: a leading fragment (we started
    reading mid-line) and a trailing line without its newline (mid-write) are dropped."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        pos, data, block = f.tell(), b"", 1 << 20
        while pos > 0 and data.count(b"\n") <= n + 1:
            step = min(block, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + data
    lines = data.split(b"\n")
    if pos > 0:
        lines = lines[1:]
    if lines and lines[-1] != b"":
        lines = lines[:-1]
    return [l for l in lines if l][-n:]


def check_jsonl(path, schema, label, tail=None):
    """-> (n_records, [Drift]). `tail` = only the last N lines (live files); a trailing partial line is ignored then."""
    fname = os.path.basename(path)
    allowed = schema.get("x-files", {}).get(fname)
    if tail:
        lines = tail_lines(path, tail)
    else:
        with open(path, "rb") as f:
            lines = [l for l in f.read().split(b"\n") if l]
    out, validators = [], {}
    for i, l in enumerate(lines, 1):
        where = f"tail[{i}/{len(lines)}]" if tail else str(i)
        try:
            rec = json.loads(l.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as ex:
            out.append(Drift(label, where, "?", "$", f"line is not valid JSON: {ex}"))
            continue
        out += check_record(label, where, rec, schema, allowed, validators)
    return len(lines), out


def check_json_file(path, schema, label):
    try:
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)
    except (ValueError, UnicodeDecodeError) as ex:
        return 1, [Drift(label, "1", "?", "$", f"file is not valid JSON: {ex}")]
    return 1, [Drift(label, "1", "status", e.json_path, e.message) for e in errors(whole_validator(schema), rec)]


def check_schema_files(schema_dir, spec_dir, changelog):
    """Every schema is a valid Draft 2020-12 schema with $id + semver x-contract-version; specs carry the same version; the
    CHANGELOG names every interface at its current version."""
    out, versions = [], {}
    for name, fn in SCHEMAS.items():
        p = os.path.join(schema_dir, fn)
        with open(p, encoding="utf-8") as f:
            s = json.load(f)
        try:
            Draft202012Validator.check_schema(s)
        except Exception as ex:                                      # noqa: BLE001
            out.append(Drift(fn, "-", "schema", "$", f"not a valid JSON Schema: {ex}"))
        if s.get("$id") != f"urn:bma:contract:{name}":
            out.append(Drift(fn, "-", "schema", "$id", f"expected urn:bma:contract:{name}, found {s.get('$id')!r}"))
        v = s.get("x-contract-version", "")
        if not SEMVER.match(v):
            out.append(Drift(fn, "-", "schema", "x-contract-version", f"{v!r} is not MAJOR.MINOR.PATCH"))
        versions[name] = v
    for name, fn in SPECS.items():
        p = os.path.join(spec_dir, fn)
        if not os.path.exists(p):
            out.append(Drift(fn, "-", "spec", "-", "spec file is missing"))
            continue
        head = open(p, encoding="utf-8").read(600)
        m = re.search(r"^contract_version:\s*(\S+)\s*$", head, re.M)
        if not m or not SEMVER.match(m.group(1)):
            out.append(Drift(fn, "-", "spec", "contract_version", "spec has no `contract_version: X.Y.Z` line in its header"))
            continue
        versions.setdefault(name, m.group(1))
        if name in SCHEMAS and m.group(1) != versions[name]:
            out.append(Drift(fn, "-", "spec", "contract_version", f"spec says {m.group(1)}, schema says {versions[name]}"))
    cl = open(changelog, encoding="utf-8").read() if os.path.exists(changelog) else ""
    frag_dir = os.path.join(os.path.dirname(changelog), CHANGELOG_FRAGMENTS)
    if os.path.isdir(frag_dir):
        for fn in sorted(os.listdir(frag_dir)):
            if fn.endswith(".md"):
                with open(os.path.join(frag_dir, fn), encoding="utf-8") as ff:
                    cl += "\n" + ff.read()
    for name, c in PLUGIN_CONTRACTS.items():                       # the manifest's version is a third copy of the version: it must equal the schema's
        if versions.get(name) != c["version"]:
            out.append(Drift("plugins.manifest.json", "-", "manifest", f"plugins.{c['plugin']}.contract.version",
                             f"manifest says {c['version']} for {name}, the schema says {versions.get(name)!r}"))
    for name, v in versions.items():
        if not re.search(rf"\b{re.escape(name)}\s+{re.escape(v)}\b", cl):
            out.append(Drift("CHANGELOG.md", "-", "changelog", name, f"no entry `{name} {v}` (every version bump needs a changelog line)"))
    return out, versions


def check_live_fixtures(fixture_dir, schemas=None):
    """specs/ui-live.md: every valid BMW1 frame parses and is rebuilt byte for byte, every invalid one is rejected with its code, the handshake vector and
    the server / client WebSocket frames reproduce, every invalid client frame is rejected with its code, the text frames satisfy the schema."""
    out, F = [], "ui_live_examples.json"
    d = json.load(open(os.path.join(fixture_dir, F), encoding="utf-8"))
    for ex in d["frames"]["valid"]:
        raw = bytes.fromhex(ex["hex"])
        try:
            got = R.parse_live(raw)
        except R.ContractError as e:
            out.append(Drift(F, ex["name"], "ui-live", "$", f"valid frame rejected: {e}"))
            continue
        for k in ("seq", "t_us", "flags"):
            if got[k] != ex[k]:
                out.append(Drift(F, ex["name"], "ui-live", k, f"parsed {got[k]!r}, example says {ex[k]!r}"))
        if R.build_live(ex["seq"], ex["t_us"], ex["flags"], [[first] + [fill] * (R.LIVE_BINS - 1) for fill, first in ex["beams"]]) != raw:
            out.append(Drift(F, ex["name"], "ui-live", "hex", "reference builder does not reproduce the example bytes"))
    for ex in d["frames"]["invalid"]:
        try:
            R.parse_live(bytes.fromhex(ex["hex"]))
            out.append(Drift(F, ex["name"], "ui-live", "$", f"invalid frame was accepted (expected code {ex['code']})"))
        except R.ContractError as e:
            if e.code != ex["code"]:
                out.append(Drift(F, ex["name"], "ui-live", "$", f"rejected with code {e.code!r}, expected {ex['code']!r}"))
    if R.ws_accept_key(d["handshake"]["key"]) != d["handshake"]["accept"]:
        out.append(Drift(F, "handshake", "ui-live", "accept", "reference accept key differs from the example"))
    for ex in d["server_frames"]:
        if R.ws_encode(ex["opcode"], bytes.fromhex(ex["payload_hex"])) != bytes.fromhex(ex["hex"]):
            out.append(Drift(F, ex["name"], "ui-live", "hex", "reference encoder does not reproduce the server frame"))
    for ex in d["client_frames"]["valid"]:
        raw = bytes.fromhex(ex["hex"])
        if R.ws_encode(ex["opcode"], bytes.fromhex(ex["payload_hex"]), bytes.fromhex(ex["mask_hex"])) != raw or R.ws_decode_client(raw) != (ex["opcode"], bytes.fromhex(ex["payload_hex"]), len(raw)):
            out.append(Drift(F, ex["name"], "ui-live", "hex", "client frame does not round trip through the reference"))
    for ex in d["client_frames"]["invalid"]:
        try:
            R.ws_decode_client(bytes.fromhex(ex["hex"]))
            out.append(Drift(F, ex["name"], "ui-live", "$", f"invalid client frame was accepted (expected code {ex['code']})"))
        except R.ContractError as e:
            if e.code != ex["code"]:
                out.append(Drift(F, ex["name"], "ui-live", "$", f"rejected with code {e.code!r}, expected {ex['code']!r}"))
    if schemas is not None:
        for ex in d["text"]:
            out += [Drift(F, ex["name"], "ui-live", "json" + e.json_path[1:], e.message) for e in errors(whole_validator(schemas["ui-live"]), ex["json"])]
    return out


def check_binary_fixtures(fixture_dir, schemas=None):
    out = []
    p = os.path.join(fixture_dir, "bmaa_examples.json")
    d = json.load(open(p, encoding="utf-8"))
    for ex in d["valid"]:
        raw = bytes.fromhex(ex["hex"])
        try:
            got = R.parse_bmaa(raw)
        except R.ContractError as e:
            out.append(Drift("bmaa_examples.json", ex["name"], "bmaa", "$", f"valid example rejected: {e}"))
            continue
        for k in ("slot", "flags", "seq", "block"):
            if got[k] != ex[k]:
                out.append(Drift("bmaa_examples.json", ex["name"], "bmaa", k, f"parsed {got[k]!r}, example says {ex[k]!r}"))
        if got["samples"] != ex["samples"]:
            out.append(Drift("bmaa_examples.json", ex["name"], "bmaa", "samples", "parsed samples differ from the example"))
        if R.build_bmaa(ex["slot"], ex["flags"], ex["seq"], ex["block"], ex["samples"]) != raw:
            out.append(Drift("bmaa_examples.json", ex["name"], "bmaa", "hex", "reference builder does not reproduce the example bytes"))
    for ex in d["invalid"]:
        try:
            R.parse_bmaa(bytes.fromhex(ex["hex"]))
            out.append(Drift("bmaa_examples.json", ex["name"], "bmaa", "$", f"invalid example was accepted (expected code {ex['code']})"))
        except R.ContractError as e:
            if e.code != ex["code"]:
                out.append(Drift("bmaa_examples.json", ex["name"], "bmaa", "$", f"rejected with code {e.code!r}, expected {ex['code']!r}"))
    out += check_spectrum_fixtures(fixture_dir)
    out += check_live_fixtures(fixture_dir, schemas)
    c = json.load(open(os.path.join(fixture_dir, "cable_frames.json"), encoding="utf-8"))
    for fr in c["frames"]:
        frame = bytes.fromhex(fr["frame_hex"])
        if R.encode_frame(fr["kind"], bytes.fromhex(fr["payload_hex"])) != frame:
            out.append(Drift("cable_frames.json", fr["name"], "bmd1", "frame_hex", "reference encoder does not reproduce the frame bytes"))
        frames, bad = R.parse_frames(b"\x00\x00\x00" + frame + b"\x00\x00\x00")
        if bad or frames != [(fr["kind"], bytes.fromhex(fr["payload_hex"]))]:
            out.append(Drift("cable_frames.json", fr["name"], "bmd1", "frame_hex", f"parse gave {frames!r} with {bad} corrupt"))
        payload_schema = {R.K_BOARD: "board-debug", R.K_BRIDGE: "bridge-status", R.K_BANDS: "beam-bands"}.get(fr["kind"])
        if schemas is not None and payload_schema:                  # JSON payload kinds must satisfy their interface's schema
            try:
                rec = json.loads(bytes.fromhex(fr["payload_hex"]))
            except ValueError as ex:
                out.append(Drift("cable_frames.json", fr["name"], "bmd1", "payload_hex", f"kind {fr['kind']} payload is not JSON: {ex}"))
                continue
            sch = schemas[payload_schema]
            if "x-discriminator" in sch:
                out += [Drift("cable_frames.json", fr["name"], "bmd1", "payload" + d.path[1:], d.message)
                        for d in check_record("cable_frames.json", fr["name"], rec, sch, None, {})]
            else:
                out += [Drift("cable_frames.json", fr["name"], "bmd1", "payload" + e.json_path[1:], e.message)
                        for e in errors(whole_validator(sch), rec)]
        if fr["kind"] == R.K_BANDS and fr["payload_hex"] != json.dumps(json.loads(bytes.fromhex(fr["payload_hex"])), separators=(",", ":")).encode().hex():
            out.append(Drift("cable_frames.json", fr["name"], "bmd1", "payload_hex", "kind 4 payload is not the compact JSON form (separators , and :)"))
    cc = json.load(open(os.path.join(fixture_dir, "cable_columns.json"), encoding="utf-8"))
    for i, case in enumerate(cc["cases"]):
        cols = tuple(case["columns"])
        if R.index_columns(case["index"]) != cols or R.index_from_columns(*cols) != case["index"]:
            out.append(Drift("cable_columns.json", str(i), "cable", "index", "index <-> columns do not round trip"))
        if any(c & 0xFF for c in cols) or not all(-(2048 << 8) <= c <= (2047 << 8) for c in cols) or cols[2] > (-2048 + 255) << 8 \
                or case["from_columns"] != case["index"]:
            out.append(Drift("cable_columns.json", str(i), "cable", "columns", "a column count must be a multiple of 256 inside the 12-bit range, "
                                                                                 "channel 11 an index byte (u < 256), and from_columns must equal index"))
    return out


def check_spectrum_fixtures(fixture_dir):
    """specs/spectrum-packet.md: every valid example parses and is rebuilt byte for byte, every invalid one is rejected with its code,
    the coarsen example reproduces, and the ui-http spectrum example's rows decode to n_bins values (no samples anywhere)."""
    import base64
    out, F = [], "spectrum_examples.json"
    d = json.load(open(os.path.join(fixture_dir, F), encoding="utf-8"))
    for ex in d["valid"]:
        raw = bytes.fromhex(ex["hex"])
        try:
            got = R.parse_spectrum(raw)
        except R.ContractError as e:
            out.append(Drift(F, ex["name"], "spectrum", "$", f"valid example rejected: {e}"))
            continue
        for k in ("tier", "flags", "beam", "row", "t_us", "bins"):
            if got[k] != ex[k]:
                out.append(Drift(F, ex["name"], "spectrum", k, f"parsed {got[k]!r}, example says {ex[k]!r}"))
        if R.build_spectrum(ex["tier"], ex["flags"], ex["beam"], ex["row"], ex["t_us"], ex["bins"]) != raw:
            out.append(Drift(F, ex["name"], "spectrum", "hex", "reference builder does not reproduce the example bytes"))
    for ex in d["invalid"]:
        try:
            R.parse_spectrum(bytes.fromhex(ex["hex"]))
            out.append(Drift(F, ex["name"], "spectrum", "$", f"invalid example was accepted (expected code {ex['code']})"))
        except R.ContractError as e:
            if e.code != ex["code"]:
                out.append(Drift(F, ex["name"], "spectrum", "$", f"rejected with code {e.code!r}, expected {ex['code']!r}"))
    ce = d["coarsen_example"]
    if R.coarsen_block(ce["tier"], ce["rows"]) != ce["result"]:
        out.append(Drift(F, "coarsen_example", "spectrum", "result", "reference coarsen_block does not reproduce the example"))
    u = json.load(open(os.path.join(fixture_dir, "ui_spectrum.json"), encoding="utf-8"))
    tier = {"standard": 0, "detail": 1}[u["tier"]]
    if u["n_bins"] != R.SPEC_TIERS[tier]["n_bins"] or len(u["edges_hz"]) != u["n_bins"] + 1:
        out.append(Drift("ui_spectrum.json", "-", "spectrum", "n_bins", "n_bins / edges_hz do not match the tier"))
    for i, r in enumerate(u["rows"]):
        if len(base64.b64decode(r["bins"])) != u["n_bins"]:
            out.append(Drift("ui_spectrum.json", str(i), "spectrum", "rows.bins", "a row does not decode to n_bins bytes"))
        if r["gated"] != bool(r["flags"] & R.SPEC_FLAG_GATED):
            out.append(Drift("ui_spectrum.json", str(i), "spectrum", "rows.gated", "gated disagrees with flags bit 0"))
    return out


def check_ui_fixtures(fixture_dir, schemas):
    """ui-http example responses and rollup lines, each against its $defs entry; the rollup fixture must hold every kind."""
    out, n = [], 0
    sch = schemas["ui-http"]
    for fn, name in UI_FIXTURES.items():
        p = os.path.join(fixture_dir, fn)
        if not os.path.exists(p):
            out.append(Drift(fn, "-", "fixture", "-", "fixture file is missing"))
            continue
        v = Draft202012Validator({"$schema": sch["$schema"], "$ref": f"#/$defs/{name}", "$defs": sch["$defs"]})
        try:
            with open(p, "rb") as f:
                recs = [json.loads(l) for l in f.read().split(b"\n") if l.strip()] if fn.endswith(".jsonl") else [json.loads(f.read())]
        except ValueError as ex:
            out.append(Drift(fn, "1", "?", "$", f"not valid JSON: {ex}"))
            continue
        for i, rec in enumerate(recs, 1):
            n += 1
            out += [Drift(fn, str(i), name, e.json_path, e.message) for e in errors(v, rec)]
        if fn.endswith(".jsonl"):
            kinds = {r.get("kind") for r in recs}
            out += [Drift(fn, "-", k, "-", f"fixture has no `{k}` rollup line (fixtures must cover every kind)") for k in ("detections", "beams", "stream") if k not in kinds]
    return n, out


def check_label_fixtures(fixture_dir, schemas):
    """verdict-labels def-based fixtures (LABEL_FIXTURES), each against its own $defs entry, never against the schema root."""
    out, n = [], 0
    sch = schemas["verdict-labels"]
    if sch.get("x-def-files") != LABEL_FIXTURES:
        out.append(Drift("verdict_labels.schema.json", "-", "schema", "x-def-files", f"the schema says {sch.get('x-def-files')!r}, the checker table says {LABEL_FIXTURES!r}"))
    for fn, name in LABEL_FIXTURES.items():
        p = os.path.join(fixture_dir, fn)
        if not os.path.exists(p):
            out.append(Drift(fn, "-", "fixture", "-", "fixture file is missing"))
            continue
        v = Draft202012Validator({"$schema": sch["$schema"], "$ref": f"#/$defs/{name}", "$defs": sch["$defs"]})
        try:
            with open(p, "rb") as f:
                rec = json.loads(f.read())
        except ValueError as ex:
            out.append(Drift(fn, "1", "?", "$", f"not valid JSON: {ex}"))
            continue
        n += 1
        out += [Drift(fn, "1", name, e.json_path, e.message) for e in errors(v, rec)]
    return n, out


def check_fixtures(fixture_dir, schemas):
    out, n = [], 0
    for fn, name in FIXTURES.items():
        p = os.path.join(fixture_dir, fn)
        if not os.path.exists(p):
            out.append(Drift(fn, "-", "fixture", "-", "fixture file is missing"))
            continue
        schema = schemas[name]
        if fn.endswith(".jsonl"):
            cnt, d = check_jsonl(p, schema, fn)
            n += cnt
            out += d
            seen = set()                                              # every kind the file may hold must appear in the fixture
            for l in open(p, encoding="utf-8"):
                try:
                    seen.add(json.loads(l).get(schema["x-discriminator"]["field"]))
                except ValueError:
                    pass
            for kind in schema["x-files"][fn] if fn in schema.get("x-files", {}) else []:
                if kind not in seen:
                    out.append(Drift(fn, "-", kind, "-", f"fixture has no `{kind}` record (fixtures must cover every kind)"))
        else:
            cnt, d = check_json_file(p, schema, fn)
            n += cnt
            out += d
    out += check_binary_fixtures(fixture_dir, schemas)
    n2, d2 = check_ui_fixtures(fixture_dir, schemas)
    n3, d3 = check_label_fixtures(fixture_dir, schemas)
    return n + n2 + n3, out + d2 + d3


def check_live(root, schemas, lines, bands_log=None, brm_report=None):
    """Tail of every live file under `root`, plus the beam-bands log (it lives on D:, outside `root`) when `bands_log` is given and the
    beam-response-map report (`reports/` is a link to a shared folder, so the path is passed, not assumed) when `brm_report` is given."""
    out, n, notes = [], 0, []
    entries = [(rel, os.path.join(root, *rel.split("/")), name, optional) for rel, (name, optional) in LIVE.items()]
    if bands_log:
        entries.append(("beam_bands.jsonl", bands_log, "beam-bands", True))
    if brm_report:
        entries.append(("beam_response_map.json", brm_report, "beam-response-map", True))      # optional: written only by a manual run of the tool
    for rel, p, name, optional in entries:
        if not os.path.exists(p):
            if optional:
                notes.append(f"{rel}: not present (optional, the producer is not running)")
            else:
                out.append(Drift(rel, "-", "live", "-", "required live file is missing"))
            continue
        cnt, d = (check_jsonl(p, schemas[name], rel, tail=lines) if rel.endswith(".jsonl") else check_json_file(p, schemas[name], rel))
        n += cnt
        out += d
    return n, out, notes


def run(contracts_dir=HERE, live_root=None, lines=2000, bands_log=None, brm_report=None):
    """-> (records_checked, [Drift], notes). Hermetic part always; live part when live_root is given."""
    sd, spd, fd = (os.path.join(contracts_dir, x) for x in ("schemas", "specs", "fixtures"))
    drift, notes = [], []
    d, versions = check_schema_files(sd, spd, os.path.join(contracts_dir, "CHANGELOG.md"))
    drift += d
    schemas = load_schemas(sd)
    n, d = check_fixtures(fd, schemas)
    drift += d
    if live_root:
        n2, d2, notes = check_live(live_root, schemas, lines, bands_log, brm_report)
        n += n2
        drift += d2
    return n, drift, notes


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--live", action="store_true", help="also check the tail of the live pipeline files")
    ap.add_argument("--root", default=DATA_DIR, help="the data dir: analysis/, history/, status files (default $BMA_DATA_DIR or ./data)")
    ap.add_argument("--lines", type=int, default=2000, help="tail length per live JSONL file")
    ap.add_argument("--bands-log", default=os.path.join(DATA_DIR, "reports", "beam_bands.jsonl"), help="the sink's beam-bands log (optional, absent until the sink writes it)")
    ap.add_argument("--brm-report", default=os.path.join(DATA_DIR, "reports", "beam_response_map.json"), help="the beam-response-map report (optional, written by a manual run of analysis/beam_response_map.py --out)")
    ap.add_argument("--contracts", default=HERE, help="contracts directory (schemas/, specs/, fixtures/, CHANGELOG.md)")
    a = ap.parse_args(argv)
    try:
        n, drift, notes = run(a.contracts, a.root if a.live else None, a.lines, a.bands_log, a.brm_report)
    except (OSError, ValueError, KeyError) as ex:
        print(f"cannot run: {type(ex).__name__}: {ex}", file=sys.stderr)
        return 2
    for note in notes:
        print("note:", note)
    shown = drift[:200]
    for d in shown:
        print(d)
    if len(drift) > len(shown):
        print(f"... {len(drift) - len(shown)} more")
    print(f"checked {n} record(s){' (fixtures + live tail)' if a.live else ' (fixtures)'}: {len(drift)} drift finding(s)")
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
