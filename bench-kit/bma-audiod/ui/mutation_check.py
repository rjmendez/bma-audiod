#!/usr/bin/env python3
"""Mutation check for bma_ui: each mutation edits one behaviour in a COPY of ui/ (and contracts/), runs test_bma_ui.py against
it and requires a non-zero exit (the suite must go red). The unmutated copy must pass first. Run with the contracts venv:

    python mutation_check.py [name-substring]

Exit 0 only if the baseline is green and every mutation was caught."""
import os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "bma_ui.py")

M = [
    ("event gap 3.0 s -> 2.0 s", 'EVENT_GAP_S = 3.0', 'EVENT_GAP_S = 2.0'),
    ("event merge boundary <= -> <", 'ev.t_start - EVENT_GAP_S <= o["t"] <= ev.t_end + EVENT_GAP_S', 'ev.t_start - EVENT_GAP_S <= o["t"] < ev.t_end + EVENT_GAP_S'),
    ("species boundary: all species share one key", 'return (o.get("scientific") or o["common"]).strip().lower()', 'return "x"'),
    ("best beam keeps the lowest confidence", 'if rep["confidence"] > self.best_conf:', 'if rep["confidence"] < self.best_conf:'),
    ("in_region default includes out-of-region", 'return mode == "any" or (e.in_region is not False if mode == "true" else e.in_region is False)', 'return True'),
    ("events sorted oldest first", 'rows.sort(key=lambda e: (-e.t_start, e.key))', 'rows.sort(key=lambda e: (e.t_start, e.key))'),
    ("power mean forgets the /n", '[[rd(p_to_db(self.psum[b][k] / n)) for k in range(8)] for b in range(8)]', '[[rd(p_to_db(self.psum[b][k])) for k in range(8)] for b in range(8)]'),
    ("contrast threshold 3 dB -> 0 dB", 'MIN_CONTRAST_DB = 3.0', 'MIN_CONTRAST_DB = 0.0'),
    ("leader is the quietest beam", 'self.lead[lv.index(max(lv))] += 1', 'self.lead[lv.index(min(lv))] += 1'),
    ("relative-to-ring sign flipped", 'out["beams"].append({"beam": b + 1, "azimuth_deg": az[b], "power_mean_db": r2(p_to_db(ps[b])),\n                                 "max_db": r2(max(r["max_db"][b][band] for r in rows)), "rel_ring_db": r2(p_to_db(ps[b]) - p_to_db(ring)),',
     'out["beams"].append({"beam": b + 1, "azimuth_deg": az[b], "power_mean_db": r2(p_to_db(ps[b])),\n                                 "max_db": r2(max(r["max_db"][b][band] for r in rows)), "rel_ring_db": r2(p_to_db(ring) - p_to_db(ps[b])),'),
    ("rollup written twice (no key filter)", 'if k not in self.recs and k not in seen:', 'if True:'),
    ("rollup written for the open hour", 'if h + HOUR_S + ROLLUP_GRACE_S > now or ("beams", h) in have or h < self.agg_floor_s:', 'if h > now or ("beams", h) in have or h < self.agg_floor_s:'),
    ("torn rollup tail not terminated", 'data = b"\\n" + data                  # terminate a torn tail', 'pass'),
    ("tail ignores truncation", 'elif st.st_size < self.offset:                               # truncated', 'elif False:'),
    ("tail drops the partial line", 'self.buf = parts.pop()', 'parts.pop(); self.buf = b""'),
    ("tail ignores the rotated remainder", 'if self._key(so) == self.key and so.st_size > self.offset:', 'if False:'),
    ("Host allowlist disabled", 'return host in self.allowed_hosts or (self.allow_ip_hosts and is_ip_literal_host(host, self.server_address[1]))', "return True"),
    ("non-GET methods allowed through", 'if self.command not in ("GET", "HEAD"):', 'if False:'),
    ("CSP header dropped", '        self.send_header("Content-Security-Policy", CSP)\n', ''),
    ("API cache header dropped", 'self._send(200, data, "application/json", {"Cache-Control": "no-store"}, head)', 'self._send(200, data, "application/json", {}, head)'),
    ("routing tolerates trailing slash", 'route = ROUTES.get(path)', 'route = ROUTES.get(path.rstrip("/"))'),
    ("limit bound 500 -> 5000", 'p_int("limit", q["limit"], 1, 500)', 'p_int("limit", q["limit"], 1, 5000)'),
    ("band bound 7 -> 9", 'p_int("band", q["band"], 0, 7)', 'p_int("band", q["band"], 0, 9)'),
    ("unknown parameters accepted", 'if k not in allowed:', 'if False:'),
    ("token leaks into the board error", 'return False, None, ("http_%d" % e.code) + (', 'return False, None, ("http_%d_%s" % (e.code, self.token)) + ('),
    ("board error leaks the raw body code (no redaction)", 'code = (str(code).replace(self.token, "") if self.token else str(code)) if code else ""', 'code = str(code) if code else ""\n            return False, None, "http_%d_%s" % (e.code, code)'),
    ("calibration notes exposed", '"measured_at": m, "board_reachable": True, "board_error": None}', '"measured_at": m, "board_reachable": True, "board_error": None, "notes": prof.get("notes")}'),
    ("stale comparison inverted", 'row["age_s"] > stale_after', 'row["age_s"] < stale_after'),
    ("stream loss mean wrong (sum)", '"loss_pct_mean": round(a["sum"] / a["n"], 3)', '"loss_pct_mean": round(a["sum"], 3)'),
    ("beam window validation accepts any version", 'o.get("v") != 1', 'False'),
    ("eviction disabled", 'while self.recs and self.recs[0][0] < cutoff:', 'while False:'),
    # ---- review round 2: validation, seams, boundaries, limits, DST, dead channels, out-of-region records
    ("rollup hour is not validated (inf / float / non-multiple accepted)", 'if not _int(h) or h <= 0 or h % HOUR_S:', 'if h is None:'),
    ("rollup beams payload grid not validated", 'and _grid(r.get("power_mean_db"))', 'and True'),
    ("rollup load failure is fatal", '            self.rollups.load_errors += 1\n', '            raise\n'),
    ("connection cap disabled", 'if not self.slots.acquire(blocking=False):', 'if False:'),
    ("connection slot never released", '        finally:\n            self.slots.release()', '        finally:\n            pass'),
    ("per-connection timeout dropped", 'self.timeout = self.server.conn_timeout', 'self.timeout = None'),
    ("tailer accepts a directory", 'if not stat.S_ISREG(st.st_mode):\n                self.last_error = "not_a_regular_file"\n                return\n            try:', 'if False:\n                self.last_error = "not_a_regular_file"\n                return\n            try:'),
    ("failed backfill restarts from byte 0", 'self.key, self.offset, self.buf = self._key(st), st.st_size, b""', 'self.key, self.offset, self.buf = self._key(st), 0, b""'),
    ("one backfill exception escapes", '        except Exception as ex:                                          # noqa: BLE001 - one source failing must not stop the rest', '        except ZeroDivisionError as ex:                                  # noqa: BLE001 - one source failing must not stop the rest'),
    ("bird_filtered records rejected", 'o.get("k") in ("bird", "bird_filtered")', 'o.get("k") == "bird"'),
    ("bird_filtered keeps the records own in_region", '            o = dict(o, in_region=False)\n', '            pass\n'),
    ("non_species filter ignored", 'return mode == "any" or (bool(non_species) is (mode == "true"))', 'return True'),
    ("in_region unknown excluded from the default view", '(e.in_region is not False if mode == "true"', '(e.in_region is True if mode == "true"'),
    ("beam filter matches the best beam only", '(beam is None or beam in e.beams)', '(beam is None or beam == e.best_beam)'),
    ("events until inclusive", 'if since <= e.t_start < until and self._in_region_ok(', 'if since <= e.t_start <= until and self._in_region_ok('),
    ("events since exclusive", 'if since <= e.t_start < until and self._in_region_ok(', 'if since < e.t_start < until and self._in_region_ok('),
    ("DST repeated hour not resolved", '        return t + HOUR_S\n', '        return t\n'),
    ("DST slack 600 -> 599", '0 < t + HOUR_S - last_t <= FALLBACK_SLACK_S', '0 < t + HOUR_S - last_t < 600'),
    ("dead channel counted as a leader window", 'if ring < 1e-15 or min(pw) < DEAD_BEAM_P:', 'if ring < 1e-15:'),
    ("effective_since not hour-floored", '"effective_since_s": hour_of(since)', '"effective_since_s": int(since)'),
    ("effective_until not hour-ceiled", '"effective_until_s": ceil_hour(until)', '"effective_until_s": hour_of(until)'),
    ("seam: rollup overrides memory", 'if kind == "beams" and h0 <= h < h1 and h not in out:', 'if kind == "beams" and h0 <= h < h1:'),
    ("seam: hourly start not hour-floored", 'h0, h1 = hour_of(since), until', 'h0, h1 = since, until'),
    ("stats power weight ignores n", 'sum(r["n"] * db_to_p(r["power_db"][b][band]) for r in rows) / n', 'sum(db_to_p(r["power_db"][b][band]) for r in rows) / len(rows)'),
    ("stats zero_frac weight ignores n", 'sum(r["zf_mean"] * r["n"] for r in rows) / n', 'sum(r["zf_mean"] for r in rows) / len(rows)'),
    ("hour_of off by one at the boundary", 'return int(t // HOUR_S) * HOUR_S', 'return int((t - 1) // HOUR_S) * HOUR_S'),
    ("ceil_hour adds an hour at the boundary", 'return int(-(-t // HOUR_S)) * HOUR_S', 'return int(t // HOUR_S + 1) * HOUR_S'),
    ("rollup grace 120 -> 0", 'ROLLUP_GRACE_S = 120', 'ROLLUP_GRACE_S = 0'),
    ("rollup grace boundary > -> >=", 'if h + HOUR_S + ROLLUP_GRACE_S > now or ("beams", h) in have', 'if h + HOUR_S + ROLLUP_GRACE_S >= now or ("beams", h) in have'),
    ("contrast threshold 3 dB -> 3.5 dB", 'MIN_CONTRAST_DB = 3.0', 'MIN_CONTRAST_DB = 3.5'),
    ("live ring uses arithmetic dB mean", 'ring = p_to_db(sum(ps) / 8.0)\n                lead', 'ring = sum(pm) / 8.0\n                lead'),
    ("missing Host accepted", 'return host in self.allowed_hosts or (self.allow_ip_hosts and is_ip_literal_host(host, self.server_address[1]))', 'return (not host) or host in self.allowed_hosts or (self.allow_ip_hosts and is_ip_literal_host(host, self.server_address[1]))'),
    ("500 leaks exception text", '"Internal error", "the request could not be served", head)', '"Internal error", str(ex), head)'),
    ("HEAD handled as GET", 'def do_HEAD(self):\n        self._handle(True)', 'def do_HEAD(self):\n        self._handle(False)'),
    ("x-frame-options dropped", '        self.send_header("X-Frame-Options", "DENY")\n', ''),
    ("referrer-policy dropped", '        self.send_header("Referrer-Policy", "no-referrer")\n', ''),
    ("401 keeps the stale token", 'self.token = None                                        # re-read the file on the next call', 'pass'),
    ("asset with a query string served", '            if parts.query:\n                return self._problem(404', '            if False:\n                return self._problem(404'),
    ("problem 422 without no-store", 'h = {"Cache-Control": "no-store"}\n        h.update(extra or {})', 'h = {}\n        h.update(extra or {})'),
    # ---- beam-slot mapping flag, panel frame
    ("slot flag: boundary event counted unreliable (< -> <=)", '"slot_mapping_unreliable": e.t_start < self.slot_fix_s', '"slot_mapping_unreliable": e.t_start <= self.slot_fix_s'),
    ("slot flag: inverted", '"slot_mapping_unreliable": e.t_start < self.slot_fix_s', '"slot_mapping_unreliable": e.t_start >= self.slot_fix_s'),
    ("slot flag: default boundary 21:47Z -> 21:46Z", 'SLOT_FIX_UTC = (2026, 10, 25, 21, 47, 0)', 'SLOT_FIX_UTC = (2026, 10, 25, 21, 46, 0)'),
    ("slot flag: status exposes a shifted boundary", '"slot_mapping_reliable_from_s": self.slot_fix_s, "bridge": bridge,', '"slot_mapping_reliable_from_s": self.slot_fix_s + 1, "bridge": bridge,'),
    ("slot flag: heatmap exposes a shifted boundary", '"basis": basis, "frame": self.calibration()["frame"],\n                "slot_mapping_reliable_from_s": self.slot_fix_s,', '"basis": basis, "frame": self.calibration()["frame"],\n                "slot_mapping_reliable_from_s": self.slot_fix_s + 1,'),
    ("slot flag: events response drops the boundary", '"frame": self.calibration()["frame"], "slot_mapping_reliable_from_s": self.slot_fix_s, "window_start_s": round(win, 3),\n                "truncated_by_window": since < win, "events"', '"frame": self.calibration()["frame"], "slot_mapping_reliable_from_s": 0, "window_start_s": round(win, 3),\n                "truncated_by_window": since < win, "events"'),
    ("slot flag: CLI ISO Z suffix not understood as UTC", 'text.replace("Z", "+00:00")', 'text.replace("Z", "+01:00")'),
    ("panel: DSP azimuth of panel up 15 -> 0", 'PANEL_UP_DSP_AZ_DEG = 15 ', 'PANEL_UP_DSP_AZ_DEG = 0 '),
    ("panel: uncalibrated reports frame_kind panel", '"frame_kind": FRAME_KIND_ARRAY, "provisional": False', '"frame_kind": FRAME_KIND_PANEL, "provisional": False'),
    ("panel: calibrated profile not provisional", '"provisional": True, "north_reference": nref,', '"provisional": False, "north_reference": nref,'),
    ("panel: calibrated frame string loses 'panel'", 'frame="calibrated panel frame " + (m', 'frame="calibrated " + (m'),
    ("panel: live frame_kind always array", '"frame": cal["frame"], "frame_kind": cal["frame_kind"], "calibrated"', '"frame": cal["frame"], "frame_kind": "array", "calibrated"'),
    ("slot flag: build() drops --slot-fix-at", 'window_h=args.window_h, slot_fix_at=args.slot_fix_at)', 'window_h=args.window_h)'),
    ("slot flag: naive CLI time read as UTC", 'return d.timestamp()', 'return (d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)).timestamp()'),
    ("slot flag: --slot-fix-at accepts any text", 'ap.add_argument("--slot-fix-at", type=parse_slot_fix,', 'ap.add_argument("--slot-fix-at", type=str,'),
    ("slot flag: default instant read as host local time", 'return datetime.datetime(*SLOT_FIX_UTC, tzinfo=datetime.timezone.utc).timestamp()', 'return datetime.datetime(*SLOT_FIX_UTC).timestamp()'),
    ("panel: compass profile still labelled panel", 'if nref == "compass":', 'if False:'),
    ("panel: compass profile keeps the panel-up azimuth", 'frame_kind=FRAME_KIND_COMPASS, panel_up_dsp_azimuth_deg=None', 'frame_kind=FRAME_KIND_COMPASS, panel_up_dsp_azimuth_deg=PANEL_UP_DSP_AZ_DEG'),
    ("panel: explicit array north_reference treated as compass", 'if nref == "compass":', 'if nref is not None:'),
    # ---- taxonomic groups, density, steerable bands (ui-http 1.2.0)
    ("taxa: every genus hit is a bird", 'return self.genus[parts[0].lower()], "genus", False', 'return "bird", "genus", False'),
    ("taxa: explicit labels (Dog, Engine) are not consulted", 'if hit:\n                return hit[0], "explicit", hit[1]', 'if False:\n                return hit[0], "explicit", hit[1]'),
    ("taxa: an unmapped label is not reported as unmapped (uncounted)", 'return "other-bio", "unmapped", bool(flag)', 'return "other-bio", "mapped", bool(flag)'),
    ("taxa: genus match is case sensitive", 'parts[0].lower() in self.genus', 'parts[0] in self.genus'),
    ("taxa: a wrong file version is accepted", 'd.get("version") != 1 or', 'False or'),
    ("taxa: status.taxa.loaded always true", '"loaded": self.taxa.error is None and bool(self.taxa.genus)', '"loaded": True'),
    ("event non_species ignores the label class (producer flag only)", 'self.group, self.how, self.non_species = (taxa or Taxa()).classify(self.scientific, self.common, rep.get("non_species"))',
     'self.group, self.how, _n = (taxa or Taxa()).classify(self.scientific, self.common, rep.get("non_species")); self.non_species = bool(rep.get("non_species"))'),
    ("event merge ORs the producer flag into a mapped label", 'if self.how == "unmapped":                                       # a mapped', 'if True:                                       # a mapped'),
    ("rollup rows are classified as bird (history regrouped wrongly)", 'grp, how, non = self.taxa.classify(r.get("scientific") or "", r["common"], r.get("non_species"))',
     'grp, how, non = "bird", "genus", bool(r.get("non_species"))'),
    ("rollup lines gain a group field (format change)", '"max_confidence": 0.0, "beam_events": [0] * 8, "in_region": None,\n                                                           "non_species": False})',
     '"max_confidence": 0.0, "beam_events": [0] * 8, "in_region": None, "group": e.group,\n                                                           "non_species": False})'),
    ("group filter on events ignored", 'and self._group_ok(e.group, gm) and (beam is None', 'and (beam is None'),
    ("group filter on species ignored", 'if not self._group_ok(s["group"], gm):\n                continue', 'if False:\n                continue'),
    ("group=bio includes mechanical", 'group in GROUPS_BIO if mode == "bio"', 'group in GROUP_ORDER if mode == "bio"'),
    ("group=<name> matches by prefix of nothing (always true)", 'else group == mode)', 'else True)'),
    ("species.groups drops the empty groups", '} for g in GROUP_ORDER]     # before the group filter', '} for g in GROUP_ORDER if any(s["group"] == g for s in everyone)]     # before the group filter'),
    ("species.groups counted after the group filter", '"events": sum(s["events"] for s in everyone if s["group"] == g)', '"events": sum(s["events"] for s in everyone if s["group"] == g and self._group_ok(s["group"], gm))'),
    ("unclassified list is empty", 'for s in everyone if s["how"] == "unmapped"]', 'for s in everyone if s["how"] == "unmapped" and False]'),
    ("chorus threshold >= -> >", 's["events_per_active_hour"] >= CHORUS_MIN_PER_ACTIVE_HOUR', 's["events_per_active_hour"] > CHORUS_MIN_PER_ACTIVE_HOUR'),
    ("chorus needs 2 active hours instead of 3", 'CHORUS_MIN_ACTIVE_HOURS = 3 ', 'CHORUS_MIN_ACTIVE_HOURS = 2 '),
    ("chorus threshold 60 -> 50", 'CHORUS_MIN_PER_ACTIVE_HOUR = 60 ', 'CHORUS_MIN_PER_ACTIVE_HOUR = 50 '),
    ("events per active hour not rounded to 1 decimal", 's["events_per_active_hour"] = round(s["events"] / len(hourly), 1)', 's["events_per_active_hour"] = s["events"] / len(hourly)'),
    ("density bucket boundary off by one", 'i = int((e.t_start - since) // bucket)', 'i = int((e.t_start - since - 1) // bucket)'),
    ("density total counts cells not events", '"group": gm, "total": len(rows),\n                "n_rows"', '"group": gm, "total": len(cells),\n                "n_rows"'),
    ("density default bucket range/100 instead of /200", '(until - since) / 200.0', '(until - since) / 100.0'),
    ("density bucket_s lower bound 60 -> 10", 'p_int("bucket_s", q["bucket_s"], 60, 86400) if "bucket_s" in q else min(', 'p_int("bucket_s", q["bucket_s"], 10, 86400) if "bucket_s" in q else min('),
    ("density cells merge species of different beams", 'c = cells.setdefault((i, e.key, e.best_beam),', 'c = cells.setdefault((i, e.key, 0),'),
    ("density drops the slot-mapping boundary", '"frame": self.calibration()["frame"], "slot_mapping_reliable_from_s": self.slot_fix_s, "window_start_s": round(win, 3),\n                "truncated_by_window": since < win, "rows"', '"frame": self.calibration()["frame"], "slot_mapping_reliable_from_s": 0, "window_start_s": round(win, 3),\n                "truncated_by_window": since < win, "rows"'),
    ("density extent not reported", '"data_first_s": round(min(starts), 3) if starts else None,', '"data_first_s": None,'),
    ("steerable threshold 250 -> 125 Hz", 'if hi <= 250)', 'if hi <= 125)'),
    ("steerable threshold 250 -> 500 Hz", 'if hi <= 250)', 'if hi <= 500)'),
    ("beams_stats steerable inverted", '"steerable": band not in NOT_STEERABLE_BANDS, "since_s": since', '"steerable": band in NOT_STEERABLE_BANDS, "since_s": since'),
    ("beams_heatmap steerable inverted", '"steerable": band not in NOT_STEERABLE_BANDS, "bucket_s": bucket', '"steerable": band in NOT_STEERABLE_BANDS, "bucket_s": bucket'),
    ("density accepts a limit parameter it does not have", '"/api/detections/density": ("api_density", ("since", "until", "species", "min_conf", "beam", "in_region", "non_species", "group", "bucket_s")),',
     '"/api/detections/density": ("api_density", ("since", "until", "species", "min_conf", "beam", "in_region", "non_species", "group", "bucket_s", "limit")),'),
    # ---- event detail: GET /api/detections/event (ui-http 1.3.0)
    ("event detail: merged reports are not kept in the trace", 'if len(self.trace) < MAX_TRACE:', 'if False:'),
    ("event detail: first report loses its level", 'self.trace = [(rep["t"], rep["beam"], rep["confidence"], self.max_level)]', 'self.trace = [(rep["t"], rep["beam"], rep["confidence"], None)]'),
    ("event detail: merged reports lose their level", 'rep.get("level_dbfs") if is_num(rep.get("level_dbfs")) else None))', 'None))'),
    ("event detail: trace cap 500 -> 600", 'MAX_TRACE = 500 ', 'MAX_TRACE = 600 '),
    ("event detail: trace_truncated never set", '"trace_truncated": hit.reports > len(hit.trace)', '"trace_truncated": False'),
    ("event detail: unknown id is not a 404", 'if hit is None:\n            raise NotFound', 'if False:\n            raise NotFound'),
    ("event detail: not-found answers 410", 'return self._problem(404, "not_found", "Not found", ex.msg, head)', 'return self._problem(410, "not_found", "Not found", ex.msg, head)'),
    ("event detail: id may be omitted", 'if "id" not in q:', 'if False:'),
    ("event detail: id slug bound 60 -> 61", '[a-z0-9-]{0,60}")', '[a-z0-9-]{0,61}")'),
    ("event detail: reports sorted by beam, not time", 'sorted(hit.trace, key=lambda r: (r[0], r[1]))', 'sorted(hit.trace, key=lambda r: (r[1], r[0]))'),
    ("event detail: level rounded to whole dB", '"level_dbfs": None if lv is None else round(lv, 1)}\n                   for t, b, c, lv in sorted', '"level_dbfs": None if lv is None else round(lv, 0)}\n                   for t, b, c, lv in sorted'),
    ("event detail: azimuth of the next beam", '"beam_azimuth_deg": az[b - 1], "confidence": round(c, 4)', '"beam_azimuth_deg": az[b % 8], "confidence": round(c, 4)'),
    ("event detail: lookup ignores the species slug", 'self._event_id(e) == eid), None)', 'self._event_id(e).split("-")[0] == eid.split("-")[0]), None)'),
    ("event detail: report tiebreak on beam dropped", 'sorted(hit.trace, key=lambda r: (r[0], r[1]))', 'sorted(hit.trace, key=lambda r: (r[0],))'),
    ("event detail: id digits not ASCII-only", 'EVENT_ID_RE = re.compile(r"[0-9]{1,16}-', 'EVENT_ID_RE = re.compile(r"\\d{1,16}-'),
    ("503 drain blocks the accept thread", 'threading.Thread(target=self._reject, args=(request,), daemon=True).start()\n                    return', 'self._reject(request)\n                    return'),
    ("event detail: accepts another parameter", '"/api/detections/event": ("api_event", ("id",)),', '"/api/detections/event": ("api_event", ("id", "limit")),'),
    # ---- spectrum feed (test_bma_ui_spectrum.py): tier enforcement, ring, cursor, limits, latch, loopback-only receiver
    ("spectrum: detail served to any peer", 'if not is_loopback_peer(peer) or not is_loopback_host(host):', 'if False:'),
    ("spectrum: detail checks the Host only, not the peer", 'if not is_loopback_peer(peer) or not is_loopback_host(host):', 'if not is_loopback_host(host):'),
    ("spectrum: detail checks the peer only, not the Host", 'if not is_loopback_peer(peer) or not is_loopback_host(host):', 'if not is_loopback_peer(peer):'),
    ("spectrum: detail served without --spectrum-detail", 'if not feed.detail_enabled:', 'if False:'),
    ("spectrum: detail datagrams stored when the flag is off", 'if tier == "detail" and not self.detail_enabled:', 'if False:'),
    ("spectrum: ring never evicts", 'while len(ring.slots) > t["ring"]:', 'while False:'),
    ("spectrum: no per-stream row rate cap", 'SPEC_BURST_S = 1.0 ', 'SPEC_BURST_S = 100000.0 '),
    ("spectrum: no per-address request limit", 'SPEC_REQ_RATE, SPEC_REQ_BURST, SPEC_REQ_ADDRS = 5.0, 10.0, 256', 'SPEC_REQ_RATE, SPEC_REQ_BURST, SPEC_REQ_ADDRS = 5.0, 100000.0, 256'),
    ("spectrum: request limiter tracks unbounded addresses", 'while len(self.b) > self.max_addrs:', 'while False:'),
    ("spectrum: since cursor is inclusive", 'slots = [s for s in slots if s.idx > since or s.rev_at > rev]', 'slots = [s for s in slots if s.idx >= since or s.rev_at > rev]'),
    ("spectrum: rewritten rows never re-served", 'slots = [s for s in slots if s.idx > since or s.rev_at > rev]', 'slots = [s for s in slots if s.idx > since]'),
    ("spectrum: limit keeps the oldest rows", 'for s in slots[-limit:]:', 'for s in slots[:limit]:'),
    ("spectrum: limit ignored", 'for s in slots[-limit:]:', 'for s in slots:'),
    ("spectrum: stale after 30 s instead of 3 s", 'SPEC_STALE_S = 3.0', 'SPEC_STALE_S = 30.0'),
    ("spectrum: rows from a non-loopback source accepted", 'if not ipaddress.ip_address(src).is_loopback:', 'if False:'),
    ("spectrum: receiver binds all interfaces", 's.bind(("127.0.0.1", port))', 's.bind(("0.0.0.0", port))'),
    ("spectrum: gated rows are not checked for coarseness", '    if flags & 1:\n        g = spec_groups(tier)', '    if False:\n        g = spec_groups(tier)'),
    ("spectrum: latch replaces rows with their first row", 'c = spec_coarsen(tid, [s.bins[beam] for s in fresh])', 'c = fresh[0].bins[beam]'),
    ("spectrum: latch fires on every tag", 'if o.get("speech_active") is True:', 'if True:'),
    ("spectrum: producer restart not detected", 'if s is not None and abs(s.t_us - t_us) > SPEC_SAME_WINDOW_US:', 'if s is not None and False:'),
    ("spectrum: levels take the maximum instead of the power sum", 'sum(spec_power(s.bins[beam][i]) for i in bins_idx)', 'max(spec_power(s.bins[beam][i]) for i in bins_idx)'),
    ("spectrum: 429 without Retry-After", 'extra={"Retry-After": "1"})', 'extra={})'),
    ("spectrum: row idx starts at 0", 's = SpecSlot(ring.run_base + row + 1,', 's = SpecSlot(ring.run_base + row,'),
    ("spectrum: default tier is detail", 'q.get("tier", "standard")', 'q.get("tier", "detail")'),
    ("spectrum: detail cap 500 -> 5000", '"max_limit": 500,', '"max_limit": 5000,'),
    ("spectrum: disabled feed reports no_data", '        if not self.enabled:\n            return "disabled"', '        if not self.enabled:\n            return "no_data"'),
    ("spectrum: duplicate beam row overwrites", 'if s.bins[beam] is not None:\n                self.bad_packets += 1                              # a duplicate', 'if False:\n                self.bad_packets += 1                              # a duplicate'),
    ("spectrum: IPv4-mapped peer is a different client address", 'return str(getattr(a, "ipv4_mapped", None) or a)', 'return str(a)'),
    ("spectrum: tip.js not routed", '"/static/tip.js": ("tip.js", "text/javascript; charset=utf-8"), ', ''),
    ("spectrum: spectrum.js served as text/plain", '"/static/spectrum.js": ("spectrum.js", "text/javascript; charset=utf-8")}', '"/static/spectrum.js": ("spectrum.js", "text/plain")}'),
    ("spectrum: UI_VERSION stays 1.3.0", 'UI_VERSION = "1.13.0"', 'UI_VERSION = "1.3.0"'),
    # ---- ui-http 1.6.0: human voices hidden, spectrum gated by authentication
    ("human: label never matches", 'return any((lab or "").strip().lower() in HUMAN_LABELS for lab in (common, scientific))', 'return False'),
    ("human: label match is case-sensitive", '(lab or "").strip().lower() in HUMAN_LABELS', '(lab or "").strip() in HUMAN_LABELS'),
    ("human: whistle missing from the list", 'HUMAN_LABELS = frozenset(("human vocal", "human non-vocal", "human whistle"))', 'HUMAN_LABELS = frozenset(("human vocal", "human non-vocal"))'),
    ("human: events are stored (filter off)", '        if is_human_label(o["common"], o.get("scientific")):\n            with self.lock:', '        if False:\n            with self.lock:'),
    ("human: old rollup lines not skipped", 'if is_human_label(r["common"], r.get("scientific")):     # a rollup written before the voices were hidden', 'if False:     # a rollup written before the voices were hidden'),
    ("human: status count always 0", '"human_hidden_24h": self.dets.hidden_count(now - 86400)', '"human_hidden_24h": 0'),
    ("human: hidden reports are never merged into one event", 'if cur is not None and cur[0] - EVENT_GAP_S <= o["t"] <= cur[1] + EVENT_GAP_S:', 'if False:'),
    ("spectrum auth: the authenticated flag is ignored", '    if not authenticated:\n        return False, "spectrum_auth_required"', '    if False:\n        return False, "spectrum_auth_required"'),
    ("spectrum auth: detail without the flag allowed", 'if tier == "detail" and not detail_flag:', 'if False:'),
    ("spectrum auth: the network check is back (loopback peers count as authenticated)", 'ok, why = spectrum_client_allowed(authenticated, tier, feed.detail_enabled)', 'ok, why = spectrum_client_allowed(authenticated or norm_peer(peer) in ("127.0.0.1", "::1"), tier, feed.detail_enabled)'),
    ("spectrum auth: server.auth not required", 'getattr(getattr(handler, "server", None), "auth", None) is not None and ', ''),
    ("spectrum auth: a truthy non-True handler.authenticated passes", 'getattr(handler, "authenticated", False) is True', 'bool(getattr(handler, "authenticated", False))'),
    ("spectrum auth: the handler always reports authenticated", 'self.server.app.api_spectrum(q, self.peer_ip(), request_authenticated(self))', 'self.server.app.api_spectrum(q, self.peer_ip(), True)'),
    ("spectrum auth: the refusal state is named disabled", '"state": "auth_required", "tier": tier', '"state": "disabled", "tier": tier'),
    ("spectrum auth: a disabled feed also asks for a login", '        if feed.enabled:\n            ok, why =', '        if True:\n            ok, why ='),
    ("spectrum: the dashboard writes an audio-like file", '            self.rows_received += 1\n', '            self.rows_received += 1\n            open("tap.wav", "wb").write(b"RIFF")\n'),
    ("spectrum: tag latch ignores the age of the tag", 'if t1 < self.clock() - 2 * max(r["ring_s"] for r in SPEC_TIERS.values()):', 'if False:'),
]


def run(workdir, only=None):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    code, fails, last = 0, [], ""
    for name in ("test_bma_ui_spectrum.py", "test_bma_ui.py", "test_bma_ui_more.py"):   # all suites; any non-zero exit means the mutant was caught
        p = subprocess.run([sys.executable, os.path.join(workdir, "ui", name)], capture_output=True, text=True, env=env, timeout=600)
        lines = p.stdout.splitlines()
        fails += [ln for ln in lines if ln.startswith("FAIL")]
        tail = lines[-1] if lines else "(no output; stderr: " + (p.stderr.strip().splitlines() or ["-"])[-1][:80] + ")"
        last = last + (" | " if last else "") + tail
        code = code or p.returncode
    return code, fails, last


def main():
    only = sys.argv[1].split("|") if len(sys.argv) > 1 else None          # name substrings separated by |
    root = tempfile.mkdtemp(prefix="bma_ui_mut_")
    try:
        work = os.path.join(root, "w")
        os.makedirs(work)
        shutil.copytree(HERE, os.path.join(work, "ui"), ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(os.path.join(HERE, "..", "contracts"), os.path.join(work, "contracts"), ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(os.path.join(HERE, "..", "classify"), os.path.join(work, "classify"), ignore=shutil.ignore_patterns("__pycache__"))   # birdnet_coverage.json for the taxa test
        code, fails, last = run(work)
        print(f"baseline: exit {code}, {last}")
        if code != 0:
            print("baseline is not green; fix that first")
            return 2
        orig = open(SRC, encoding="utf-8").read()
        missed, ok = [], 0
        part = os.environ.get("MUT_PART")                                 # "i/n": every n-th mutant starting at i, to split the run over parallel processes
        for idx, (name, old, new) in enumerate(M):
            if only and not any(o in name for o in only):
                continue
            if part and idx % int(part.split("/")[1]) != int(part.split("/")[0]):
                continue
            if orig.count(old) != 1:
                print(f"BROKEN MUTATION (pattern occurs {orig.count(old)}x): {name}")
                missed.append(name)
                continue
            with open(os.path.join(work, "ui", "bma_ui.py"), "w", encoding="utf-8", newline="\n") as f:
                f.write(orig.replace(old, new))
            code, fails, last = run(work)
            if code != 0:
                ok += 1
                print(f"RED   {name}: exit {code}, {last}; first failing check: {(fails[0] if fails else last)[:110]}")
            else:
                print(f"GREEN {name}: exit {code}, {last}  <-- NOT CAUGHT")
                missed.append(name)
        print(f"\n{ok} mutation(s) caught, {len(missed)} not caught")
        return 1 if missed else 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
