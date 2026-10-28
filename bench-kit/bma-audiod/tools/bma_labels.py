#!/usr/bin/env python3
"""bma_labels: the owner's verdict label store and the uncertainty-sampled review queue (contract verdict-labels 1.0.0).

    bma_labels.py queue  [--days 14] [--limit 200] ... [--out FILE]       propose events to review (reads birds, tags, labels; writes only --out / stdout)
    bma_labels.py add    --event-id ID --verdict correct|wrong|unsure|artifact --basis field|clip_visual|metadata_only
                         [--wrong-reason R] [--corrected-key KEY] [--queue FILE]
    bma_labels.py list | fold | stats | verify                            read the label file (never edit it)
    bma_labels.py events                                                  event conservation (exit 1 on failure) and label resolution (exact/superseded/orphaned)

Reads JSONL metadata only; never reads audio, embeddings or novelty; never writes under <data-dir>/analysis or /history; no network.
Exit 0 = ok, 1 = `verify` found problems, 2 = refused / unreadable / usage.  `add` refuses a speech event, an untagged event and an
event it cannot find with the SAME one-word message (`not_labelable`), so the refusal is not an oracle for speech."""
import argparse, json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
for _d in (HERE, os.path.join(HERE, "..", "analysis")):          # flat app dir (a deploy copies the 3 files + taxa.json together) or the repo layout
    sys.path.insert(0, _d)
import labels as L          # noqa: E402
import review_queue as Q    # noqa: E402

DATA_DIR = os.environ.get("BMA_DATA_DIR") or "data"
DEFAULT_TAXA = next((p for p in (os.path.join(HERE, "taxa.json"), os.path.join(HERE, "..", "ui", "taxa.json")) if os.path.exists(p)), None)


def eprint(*a):
    sys.stderr.buffer.write((" ".join(str(x) for x in a) + "\n").encode("utf-8", "replace"))      # bytes: LF stays LF on Windows
    sys.stderr.buffer.flush()


def out_bytes(data, path=None):
    """Write bytes as they are (LF stays LF on Windows)."""
    if path:
        with open(path, "wb") as f:
            f.write(data)
    else:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def paths(a):
    data = a.data_dir
    return {"analysis": os.path.join(data, "analysis"), "history": os.path.join(a.history_dir or os.path.join(data, "history")),
            "labels": a.labels_dir or os.path.join(data, "labels")}


def real(p):
    return os.path.normcase(os.path.realpath(p))


def under(p, root):
    p, root = real(p), real(root)
    return p == root or p.startswith(root.rstrip(os.sep) + os.sep)


def protected_hit(p, pa):
    """-> the name of the protected tree `p` is in (analysis, history), else None.  realpath + normcase: case and symlinks do not bypass it."""
    for sub in ("analysis", "history"):
        if under(p, pa[sub]):
            return sub
    return None


def load_events(a):
    pa = paths(a)
    taxa = L.Taxa(a.taxa or DEFAULT_TAXA)
    rows = L.read_rotated(os.path.join(pa["analysis"], "birds.jsonl"))
    return L.iter_events(rows, taxa), taxa


def load_tags(a):
    base = os.path.join(paths(a)["analysis"], "tags.jsonl")
    present = os.path.exists(base) or os.path.exists(base + ".1")
    return L.Tags(L.read_rotated(base)), present


def cmd_queue(a):
    pa = paths(a)
    events, taxa = load_events(a)
    tags, tags_present = load_tags(a)
    if not tags_present and not a.include_untagged:
        eprint("tags.jsonl not found: every event is speech_unknown and nothing is queued (fail closed); --include-untagged overrides")
    store = L.LabelStore(pa["labels"])
    try:
        recs, bad, partial = store.read()
    except OSError as ex:
        eprint("cannot read the labels file: %s" % type(ex).__name__)
        return 2
    if a.insect_mode == "only" and not taxa.loaded:
        eprint("--insect-mode only needs a usable taxa table (--taxa)")
        return 2
    ambient = None
    if a.insect_mode == "only":
        ambient = L.Ambient(L.read_rotated(os.path.join(pa["history"], "ambient_temp.jsonl")))
    params = {"days": a.days, "seed": a.seed, "per_species_cap": a.per_species_cap, "limit": a.limit, "near_lo": a.near_lo,
              "near_hi": a.near_hi, "audit_frac": a.audit_frac, "audit_per_species_cap": a.audit_per_species_cap,
              "speech_margin_s": a.speech_margin_s, "speech_p_min": a.speech_p_min, "include_untagged": a.include_untagged,
              "include_unjudged_region": a.include_unjudged_region, "insect_mode": a.insect_mode, "relabel": a.relabel}
    now = time.time() if a.now is None else a.now
    try:
        q = Q.build_queue(events, L.fold(recs), tags, params, now, ambient=ambient, taxa_loaded=taxa.loaded)
    except Q.QueueError as ex:
        eprint("usage: %s" % ex)
        return 2
    q["labels"] = {"records": len(recs), "folded": len(L.fold(recs)), "bad_lines": bad, "partial_lines": partial}
    if a.out:
        sub = protected_hit(a.out, pa)
        if sub:
            eprint("refusing to write under %s/" % sub)
            return 2
        if under(a.out, pa["labels"]):
            eprint("refusing to write under the labels dir (the label store is append-only)")
            return 2
    try:
        out_bytes(Q.dumps(q).encode("ascii"), a.out)
    except OSError as ex:
        eprint("cannot write --out: %s" % type(ex).__name__)
        return 2
    return 0


def cmd_add(a):
    pa = paths(a)
    sub = protected_hit(pa["labels"], pa)
    if sub:
        eprint("refusing to write under %s/" % sub)
        return 2
    events, _taxa = load_events(a)
    tags, _present = load_tags(a)
    by_id = {e.event_id: e for e in events}
    ev = by_id.get(a.event_id)
    if ev is None or L.speech_reason(ev, tags, a.speech_margin_s, a.speech_p_min, a.include_untagged) is not None:
        eprint(L.NOT_LABELABLE)
        return 2
    item = None
    if a.queue:
        try:
            with open(a.queue, "rb") as f:
                q = json.loads(f.read())
            item = next((i for i in q["items"] if i["event_id"] == a.event_id), None)
            qid = q["queue_id"]
        except (OSError, ValueError, KeyError, TypeError):
            eprint("--queue: unreadable or not a review queue")
            return 2
        if item is None:
            eprint("--queue: the event is not in that queue")
            return 2
        item = {"stratum": item["stratum"], "p_include": item["p_include"], "queue_id": qid}
    if a.corrected_key is not None and L.is_speech_label(a.corrected_key):
        eprint(L.NOT_LABELABLE)
        return 2
    if a.corrected_key is not None and a.corrected_key not in {e.key for e in events}:
        eprint("--corrected-key: not a species key seen in birds.jsonl")
        return 2
    try:
        rec = L.make_record(ev, a.verdict, a.basis, wrong_reason=a.wrong_reason, corrected_key=a.corrected_key, source="cli", item=item,
                            t_label=a.now)
        L.LabelStore(pa["labels"]).append(rec)
    except (L.LabelError, OSError) as ex:
        eprint("add failed: %s" % (ex if isinstance(ex, L.LabelError) else type(ex).__name__))
        return 2
    out_bytes((L.dumps(rec) + "\n").encode("ascii"))
    return 0


def read_labels(a):
    try:
        return L.LabelStore(paths(a)["labels"]).read()
    except OSError as ex:
        eprint("cannot read the labels file: %s" % type(ex).__name__)
        return None


def cmd_list(a):
    r = read_labels(a)
    if r is None:
        return 2
    out_bytes(b"".join((L.dumps(x) + "\n").encode("ascii") for x in r[0]))
    return 0


def cmd_fold(a):
    r = read_labels(a)
    if r is None:
        return 2
    f = L.fold(r[0])
    out_bytes(b"".join((L.dumps(f[k]) + "\n").encode("ascii") for k in sorted(f)))
    return 0


def cmd_stats(a):
    r = read_labels(a)
    if r is None:
        return 2
    recs, bad, partial = r
    f = L.fold(recs)
    by = lambda key: dict(sorted(_count(key(x) for x in f.values()).items()))   # noqa: E731
    nulls = sum(1 for x in f.values() if (x.get("model") or {}).get("group") is None)
    s = {"records": len(recs), "folded_events": len(f), "bad_lines": bad, "partial_lines": partial,
         "verdicts": by(lambda x: x.get("verdict")), "by_species": by(lambda x: x.get("species_key")),
         "by_stratum": by(lambda x: x.get("stratum") or "unsolicited"), "audit_items": sum(1 for x in f.values() if x.get("p_include") is not None),
         "model_group_null": nulls, "model_group_null_rate": round(nulls / len(f), 4) if f else None}
    out_bytes((json.dumps(s, separators=(",", ":")) + "\n").encode("ascii"))
    return 0


def _count(it):
    d = {}
    for v in it:
        d[str(v)] = d.get(str(v), 0) + 1
    return d


def cmd_verify(a):
    path = L.LabelStore(paths(a)["labels"]).path
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        data = b""
    except OSError as ex:
        eprint("cannot read the labels file: %s" % type(ex).__name__)
        return 2
    problems, n = [], 0
    if b"\r" in data:
        problems.append("file contains CR bytes (LF only)")
    parts = data.split(b"\n")
    if parts and parts[-1].strip():
        problems.append("last line has no newline (partial)")
    ids = set()
    for i, ln in enumerate(parts[:-1], 1):
        if not ln.strip():
            continue
        n += 1
        try:
            rec = json.loads(ln.decode("utf-8"))
            L.validate_record(rec)
            if rec["label_id"] in ids:
                problems.append("line %d: duplicate label_id" % i)
            ids.add(rec["label_id"])
        except (ValueError, UnicodeDecodeError, L.LabelError) as ex:
            problems.append("line %d: %s" % (i, ex))
    for p in problems:
        eprint(p)
    out_bytes(("verified %d record(s): %d problem(s)\n" % (n, len(problems))).encode("ascii"))
    return 1 if problems else 0


def cmd_events(a):
    pa = paths(a)
    taxa = L.Taxa(a.taxa or DEFAULT_TAXA)
    events, res = L.iter_events_counted(L.read_rotated(os.path.join(pa["analysis"], "birds.jsonl")), taxa)
    r = read_labels(a)
    if r is None:
        return 2
    rl = L.resolve_labels(r[0], events)
    out = {"k": "event_model_report", "v": 1, "id_version": L.E.ID_VERSION, "gap_s": L.EVENT_GAP_S, "conservation": res.counts(), "labels": {"exact": rl["exact"], "superseded": len(rl["superseded"]), "orphaned": len(rl["orphaned"]),
                                                    "ambiguous": rl["ambiguous"]}}
    out_bytes((json.dumps(out, separators=(",", ":"), sort_keys=True) + "\n").encode("ascii"))
    try:
        res.check()
    except L.E.ConservationError as ex:
        eprint("conservation FAILED: %s" % ex)
        return 1
    return 0


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", default=DATA_DIR, help="the data dir (default $BMA_DATA_DIR or ./data)")
    ap.add_argument("--labels-dir", default=None, help="default <data-dir>/labels")
    ap.add_argument("--history-dir", default=None, help="default <data-dir>/history (ambient_temp.jsonl, artifact probe only)")
    ap.add_argument("--taxa", default=None, help="the dashboard taxa table (default taxa.json next to this tool, else ../ui/taxa.json)")
    ap.add_argument("--now", type=float, default=None, help="the clock, Unix seconds (default wall clock; makes a run reproducible)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("queue", help="propose events to review")
    q.add_argument("--days", type=float, default=Q.DEFAULTS["days"])
    q.add_argument("--seed", type=int, default=Q.DEFAULTS["seed"])
    q.add_argument("--limit", type=int, default=Q.DEFAULTS["limit"])
    q.add_argument("--per-species-cap", type=int, default=Q.DEFAULTS["per_species_cap"])
    q.add_argument("--audit-frac", type=float, default=Q.DEFAULTS["audit_frac"])
    q.add_argument("--audit-per-species-cap", type=int, default=Q.DEFAULTS["audit_per_species_cap"])
    q.add_argument("--near-lo", type=float, default=Q.NEAR_LO, help="ARBITRARY default 0.5 (nobody has measured a gate); see conf_quantiles in the output")
    q.add_argument("--near-hi", type=float, default=Q.NEAR_HI, help="ARBITRARY default 0.85")
    q.add_argument("--speech-margin-s", type=float, default=L.SPEECH_MARGIN_S)
    q.add_argument("--speech-p-min", type=float, default=L.SPEECH_P_MIN)
    q.add_argument("--include-untagged", action="store_true", help="lift the fail-closed rule for events with no tags row nearby")
    q.add_argument("--include-unjudged-region", action="store_true", help="include events whose reports predate the geo filter (in_region absent)")
    q.add_argument("--insect-mode", choices=Q.INSECT_MODES, default="exclude",
                   help="exclude (default): insect events are not queued; only: the artifact-probe queue (insect events only, spread over hour and temperature)")
    q.add_argument("--relabel", action="store_true")
    q.add_argument("--out", default=None, help="write the queue JSON here (default stdout)")
    q.set_defaults(fn=cmd_queue)
    ad = sub.add_parser("add", help="append one verdict")
    ad.add_argument("--event-id", required=True)
    ad.add_argument("--verdict", required=True, choices=L.VERDICTS)
    ad.add_argument("--basis", required=True, choices=L.BASES)
    ad.add_argument("--wrong-reason", choices=L.WRONG_REASONS, default=None)
    ad.add_argument("--corrected-key", default=None)
    ad.add_argument("--queue", default=None, help="a queue file: copies stratum, p_include and queue_id of the event's item")
    ad.add_argument("--speech-margin-s", type=float, default=L.SPEECH_MARGIN_S)
    ad.add_argument("--speech-p-min", type=float, default=L.SPEECH_P_MIN)
    ad.add_argument("--include-untagged", action="store_true")
    ad.set_defaults(fn=cmd_add)
    for name, fn in (("list", cmd_list), ("fold", cmd_fold), ("stats", cmd_stats), ("verify", cmd_verify), ("events", cmd_events)):
        sub.add_parser(name).set_defaults(fn=fn)
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
