#!/usr/bin/env python3
"""Mutation proof for test_clips.py: each mutation below is applied to a TEMP COPY of analysis/ + contracts/ + classify/ + pc/ + docker/ (the repo is never
touched), test_clips.py is run on the copy, and the mutation must turn at least one check red (or hang/crash: a timeout counts as red).
Prints, per mutation, the checks that went red. Exit 0 only when every mutation was caught.
    python -I mutation_clips.py [--jobs 4]
The host mutations run the full 55 s UDP test, serially (each run picks its own free UDP ports anyway); the others pass --no-e2e. Needs the analyzer venv (numpy, scipy, soundfile, PIL)."""
import concurrent.futures as cf, os, re, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BMA = os.path.dirname(HERE)

# (name, file under analysis/, [(old, new), ...], needs_e2e)
M = [
    ("certainty: > 0.85 becomes >= 0.85", "clips.py", [("if not mx > self.cfg.min_conf:", "if not mx >= self.cfg.min_conf:")], False),
    ("certainty: max of the event becomes the mean", "clips.py", [('mx = max(w["conf"] for w in ev["windows"])', 'mx = sum(w["conf"] for w in ev["windows"]) / len(ev["windows"])')], False),
    ("wildlife: group null allowed", "clips.py", [('        if g is None:\n            return "no_group"\n', ""), ("if g not in ALLOWED_GROUPS or", "if (g not in ALLOWED_GROUPS and g is not None) or")], False),
    ("wildlife: non_species check removed", "clips.py", [('g not in ALLOWED_GROUPS or r.get("non_species") is not False:', "g not in ALLOWED_GROUPS:")], False),
    ("wildlife: exclusion list emptied", "clips.py", [("self.names, self.prefixes = load_exclusions(cfg.exclude_path)", "self.names, self.prefixes = set(), ()")], False),
    ("guard: margin 5 s becomes 4 s", "clips.py", [("g = int(self.cfg.guard_s * FS)\n        lo, hi", "g = int((self.cfg.guard_s - 1) * FS)\n        lo, hi")], False),
    ("guard: a hole in the tags timeline counts as a pass", "clips.py", [("        if holes:\n            infl", "        if False:\n            infl")], False),
    ("guard: tagger unavailable fails OPEN", "clips.py", [("if not self.tagger_enabled:", "if False:")], False),
    ("guard: human BirdNET label ignored", "clips.py", [("if h and s < hi and e > lo:", "if False:")], False),
    ("guard: speech threshold 0.10 becomes 0.30 (live guard, _on_tags)", "clips.py", [("or not isinstance(p, (int, float)) or p >= self.cfg.speech_p or self._human_top(", "or not isinstance(p, (int, float)) or p >= 0.30 or self._human_top(")], False),
    ("purge: speech threshold 0.10 becomes 0.30 (human-voice span scan)", "clips.py", [("not isinstance(p, bool) and p >= self.cfg.speech_p)", "not isinstance(p, bool) and p >= 0.30)")], False),
    ("guard: no wait for a tags window past the span", "clips.py", [("if not any(s >= hi for s, _, _, _ in self.tags_tl):\n            return None", "if False:\n            return None")], False),
    ("guard: speech row inside the span ignored", "clips.py", [("if any(f for _, _, _, f in near):", "if False:")], False),
    ("retention: >= becomes > (the 24h00m00s boundary)", "clips.py", [(") >= cfg.audio_retention_s:\n                            self._expire_audio(cid, \"age\")", ") > cfg.audio_retention_s:\n                            self._expire_audio(cid, \"age\")")], False),
    ("retention: age from file mtime instead of the capture time", "clips.py", [('now - (self.table[cid]["t"] if cap_t is None else cap_t) >= cfg.audio_retention_s', 'now - os.path.getmtime(p) >= cfg.audio_retention_s')], False),
    ("retention: sidecars deleted at the AUDIO age", "clips.py", [("if now - e[\"t\"] >= cfg.sidecar_retention_s:", "if now - e[\"t\"] >= cfg.audio_retention_s:")], False),
    ("retention: no tombstone appended when audio expires", "clips.py", [('        self._tomb("clip_audio_expired", cid, reason)', "        pass")], False),
    ("retention: no sweep at start", "clips.py", [("self.store.sweep()                                   # crash leftovers go before the first window", "pass")], False),
    ("retention: orphaned .tmp files never swept", "clips.py", [("if now - mt >= ORPHAN_S or (audioish and now - cap_t >= cfg.audio_retention_s):", "if False:")], False),
    ("retention: sweep interval not capped at 600 s", "clips.py", [("max(1.0, min(float(sweep_s), 600.0))", "max(1.0, float(sweep_s))")], False),
    ("retention: a refused delete is not counted for retry", "clips.py", [('            self.c["audio_expire_retry"] += 1\n            return False', "            return False")], False),
    ("atomic write: audio written straight to the final name", "clips.py", [('self.sf.write(tmp, item["pcm"], FS, subtype="PCM_16", format=self.codec.upper())\n                self.replace(tmp, apath)', 'self.sf.write(apath, item["pcm"], FS, subtype="PCM_16", format=self.codec.upper())')], False),
    ("atomic write: tmp file not removed after a failure", "clips.py", [("            except (OSError, RuntimeError, ValueError):\n                self._rm(tmp)\n", "            except (OSError, RuntimeError, ValueError):\n                pass\n")], False),
    ("queue: put blocks instead of put_nowait", "clips.py", [("self.q.put_nowait(cl)", "self.q.put(cl)")], False),
    ("queue: drops are not counted", "clips.py", [('                    self.c["clips_dropped_queue"] += 1', "                    pass")], False),
    ("cap: keeps the FIRST N (never replaces)", "clips.py", [('if item["conf"] <= lowest[0]:', "if True:")], False),
    ("cap: keeps the newest (a lower-confidence newcomer replaces)", "clips.py", [('if item["conf"] <= lowest[0]:', "if False:")], False),
    ("size cap: eviction order reversed (highest confidence first)", "clips.py", [('key=lambda c: (self.table[c]["conf"], self.table[c]["t"]))', 'key=lambda c: (-self.table[c]["conf"], self.table[c]["t"]))')], False),
    ("audio: wrong beam (first report instead of the best)", "clips.py", [('"beam": int(best["beam"]) - 1,', '"beam": int(rs[0]["beam"]) - 1,')], False),
    ("audio: pre-roll off by one sample", "clips.py", [("self.ring_reader(s - pre, pre, b)", "self.ring_reader(s - pre + 1, pre, b)")], False),
    ("audio: non-contiguous audio merged into one run", "clips.py", [("if runs and st == runs[-1][0] + len(runs[-1][1]):", "if runs:")], False),
    ("audio: 12 s cap not centred on the peak", "clips.py", [("lo = min(max(pc - mxn // 2, st), st + len(arr) - mxn)", "lo = st")], False),
    ("sidecar: the local UTC offset is stored again (reveals the host's timezone)", "clips.py", [('"t_end_utc": iso(cl["t_end"]),', '"t_end_utc": iso(cl["t_end"]), "local_utc_offset_s": int(time.localtime(t0).tm_gmtoff or 0),')], False),
    ("guard: human-sound tagger labels (top) ignored (live guard, _on_tags)", "clips.py", [(' or self._human_top(r.get("top"))\n        self.tags_tl.append', '\n        self.tags_tl.append')], False),
    ("purge: human-sound tagger labels (top) ignored (human-voice span scan)", "clips.py", [('\n                                                 or self._human_top(r.get("top"))):', "):")], False),
    ("guard: human label threshold > instead of >= (the exact 0.05 boundary)", "clips.py", [("float(p) >= self.human_top_p", "float(p) > self.human_top_p")], False),
    ("guard: a malformed top list fails OPEN", "clips.py", [("        except (TypeError, ValueError):\n            return True", "        except (TypeError, ValueError):\n            return False")], False),
    ("guard: speech_p missing or not a number is not flagged", "clips.py", [("or not isinstance(p, (int, float)) or p >= self.cfg.speech_p", "or p >= self.cfg.speech_p if isinstance(p, (int, float)) else False")], False),
    ("wildlife: exclusion matched on the scientific name only", "clips.py", [('for k in ("scientific", "common"):', 'for k in ("scientific",):')], False),
    ("guard: human BirdNET label recognised from the common name only", "clips.py", [('for k in ("common", "scientific"))', 'for k in ("common",))')], False),
    ("exclusion list: Muscovy duck removed from clip_exclude.json", "clip_exclude.json", [('"cairina moschata",', "")], False),
    ("human labels: Laughter removed from clip_exclude.json", "clip_exclude.json", [('"Laughter",', "")], False),
    ("cap: --clip-cap-per-species-hour 0 not handled", "clips.py", [("if self.cfg.cap_per_species_hour <= 0:", "if False:")], False),
    ("config: --clip-max-s not clamped to the 12 s ring", "clips.py", [("min(float(max_s), 12.0)", "float(max_s)")], False),
    ("config: --clip-sweep-s has no lower bound", "clips.py", [("max(1.0, min(float(sweep_s), 600.0))", "min(float(sweep_s), 600.0)")], False),
    ("retention: sidecar boundary > instead of >=", "clips.py", [('if now - e["t"] >= cfg.sidecar_retention_s:', 'if now - e["t"] > cfg.sidecar_retention_s:')], False),
    ("size cap: the sidecar size cap loop removed", "clips.py", [('while sum(e["side_bytes"] for e in self.table.values()) > self.cfg.sidecar_cap and self.table:', "while False:")], False),
    ("retention: the .wav fallback codec is not an audio extension", "clips.py", [('AUDIO_EXT = (".flac", ".wav")', 'AUDIO_EXT = (".flac",)')], False),
    ("retention: a delete that finds the file already gone counts as a failure", "clips.py", [("        except FileNotFoundError:\n            return True\n", "")], False),
    ("retention: orphan PNG / JSON files never swept", "clips.py", [('if mt is None or not n.endswith(AUDIO_EXT + (".png", ".json")):', "if mt is None or not n.endswith(AUDIO_EXT):")], False),
    ("sweep: the store is rebuilt from every JSON on each sweep", "clips.py", [("        for day in days:\n            self._sweep_day(day, now)", "        self.rescan()\n        for day in days:\n            self._sweep_day(day, now)")], False),
    ("status: audio_stats waits for the store lock (the sweep stalls the analyzer)", "clips.py", [("        ents = list(self.table.values())", "        with self.lock:\n            ents = list(self.table.values())")], False),
    ("signature: dB scale 20 log10 becomes 10 log10", "clip_features.py", [("max(floor, 20.0 * math.log10(v))", "max(floor, 10.0 * math.log10(v))")], False),
    ("signature: onset threshold 6 dB becomes 60 dB", "clip_features.py", [("ONSET_DB = 6.0", "ONSET_DB = 60.0")], False),
    ("spectrogram: fixed dB range shifted", "clip_render.py", [("DB_LO, DB_HI = -110.0, -30.0", "DB_LO, DB_HI = -100.0, -20.0")], False),
    ("beams: >= N becomes > N (a 2-beam event needs 3)", "clips.py", [('if len(ev["beam_set"]) < self.cfg.min_beams:', 'if len(ev["beam_set"]) <= self.cfg.min_beams:')], False),
    ("beams: the rule is never applied", "clips.py", [('if len(ev["beam_set"]) < self.cfg.min_beams:', "if False:")], False),
    ("beams: the default becomes 1 (ClipConfig)", "clips.py", [("exclude_path=None, min_beams=2, audio=False, pcen=False):", "exclude_path=None, min_beams=1, audio=False, pcen=False):")], False),
    ("beams: the default becomes 1 (--clip-min-beams flag)", "clips.py", [('"--clip-min-beams", type=int, default=2,', '"--clip-min-beams", type=int, default=1,')], False),
    ("beams: reports are counted instead of distinct beams", "clips.py", [('ev["beam_set"].update(int(r["beam"]) for r in rs)', 'ev["beam_set"].add(len(ev["windows"]))')], False),
    ("beams: only the first window's beams are counted", "clips.py", [('ev["beam_set"].update(int(r["beam"]) for r in rs)', 'if not ev["windows"]: ev["beam_set"].update(int(r["beam"]) for r in rs)')], False),
    ("beams: the second beam may carry a different species (all rows of the window counted)", "clips.py", [('ev["beam_set"].update(int(r["beam"]) for r in rs)', 'ev["beam_set"].update(int(r["beam"]) for r in recs if r.get("k") in ("bird", "bird_filtered"))')], False),
    ("beams: beams_agreeing missing from the index record", "clips.py", [('"beams_agreeing": sc["beams_agreeing"], "in_region"', '"in_region"')], False),
    ("beams: beams_agreeing in the sidecar is a constant", "clips.py", [('"beams_agreeing": len(cl["beams_reported"]),', '"beams_agreeing": 2,')], False),
    ("beams: the rejection is not counted", "clips.py", [('            self.c["clips_skipped_single_beam"] += 1\n            return', "            return")], False),
    ("beams: the beam rule runs before the certainty rule", "clips.py", [('        mx = max(w["conf"] for w in ev["windows"])\n        if not mx > self.cfg.min_conf:\n            self.c["clips_skipped_low_conf"] += 1\n            return\n        if len(ev["beam_set"]) < self.cfg.min_beams:\n            self.c["clips_skipped_single_beam"] += 1\n            return\n', '        mx = max(w["conf"] for w in ev["windows"])\n        if len(ev["beam_set"]) < self.cfg.min_beams:\n            self.c["clips_skipped_single_beam"] += 1\n            return\n        if not mx > self.cfg.min_conf:\n            self.c["clips_skipped_low_conf"] += 1\n            return\n')], False),
    ("host: --clip-min-beams is not passed to the config", "clips.py", [("min_beams=a.clip_min_beams, audio=a.clip_audio,", "min_beams=2, audio=a.clip_audio,")], True),
    ("audio: the default becomes ON", "clips.py", [("exclude_path=None, min_beams=2, audio=False, pcen=False):", "exclude_path=None, min_beams=2, audio=True, pcen=False):")], False),
    ("audio: the flag is ignored (an audio file is written in visual-only mode)", "clips.py", [("        if cfg.audio:                                # visual-only never imports the audio encoder", "        if True:                                # visual-only never imports the audio encoder"), ("        if self.cfg.audio:\n            try:\n                self.sf.write", "        if True:\n            try:\n                self.sf.write")], False),
    ("audio: the launcher script passes --clip-audio", "run_analyzer.sh", [("EXTRA=()", "EXTRA=(--clip-audio)")], False),
    ("visual-only: expiry sweep reconciles clips that never had audio", "clips.py", [('"expired": bool(j.get("audio_expired")) or j.get("audio_stored") is False}', '"expired": bool(j.get("audio_expired"))}'), ('"expired": not self.cfg.audio}', '"expired": False}')], False),
    ("visual: the floor is lowered from 3500 Hz to 100 Hz", "clip_features.py", [("FLOOR_HZ = 3500.0", "FLOOR_HZ = 100.0"), ("HP_TAPS, HP_CUTOFF_HZ, HP_BETA = 4097, 3550.0, 12.0", "HP_TAPS, HP_CUTOFF_HZ, HP_BETA = 4097, 100.0, 12.0")], False),
    ("visual: the high-pass is bypassed (full band stored)", "clip_features.py", [('    return signal.oaconvolve(x, h, mode="valid")', "    return x[TRIM // 2:len(x) - TRIM // 2]")], False),
    ("visual: PNG time resolution 10 ms per column", "clip_render.py", [("COL_S = 0.050", "COL_S = 0.010")], False),
    ("visual: PNG has 300 rows", "clip_render.py", [("H = 96", "H = 300")], False),
    ("visual: the level envelope runs at audio rate (1 ms frames)", "clip_features.py", [("FRAME_S = 0.050 ", "FRAME_S = 0.001 ")], False),
    ("visual: the dominant-frequency track at 10 ms per value", "clip_features.py", [("DOM_FRAME_S = 0.100 ", "DOM_FRAME_S = 0.010 ")], False),
    ("purge: rule inverted (a miss deletes, a hit keeps)", "clips.py", [("if (a < hi and b > lo) if a < b else (lo <= a <= hi):", "if not ((a < hi and b > lo) if a < b else (lo <= a <= hi)):")], False),
    ("purge: the sweeper never calls it", "clips.py", [("                self.store.sweep()\n                self.purge_human()\n", "                self.store.sweep()\n")], False),
    ("purge: speech_p threshold ignored", "clips.py", [("and not isinstance(p, bool) and p >= self.cfg.speech_p", "and not isinstance(p, bool) and p >= 1.0")], False),
    ("purge: guard margin ignored", "clips.py", [("lo, hi = t0 - g, t1 + g", "lo, hi = t0, t1")], False),
    ("purge: reads the logs although no audio clip is stored", "clips.py", [('if not any(e.get("audio") for _, e in ents):', "if False:")], False),
    ("purge: human BirdNET rows ignored", "clips.py", [("elif is_human(r):", "elif False:")], False),
    ("min-conf: staging ignores the configured threshold (fixed 0.5 floor)", "clips.py", [('if best["confidence"] < self.cfg.stage_floor:', "if best[\"confidence\"] < STAGE_FLOOR:")], False),
    ("min-conf: the staging floor is always the configured value (default 0.85 stages from 0.85)", "clips.py", [("self.stage_floor = min(STAGE_FLOOR, self.min_conf)", "self.stage_floor = self.min_conf")], False),
    ("min-conf: the range check is removed", "clips.py", [("    if not (MIN_CONF_LO <= v <= MIN_CONF_HI):", "    if False:")], False),
    ("min-conf: the lower bound is 0 instead of 0.05", "clips.py", [("MIN_CONF_LO, MIN_CONF_HI = 0.05, 1.0", "MIN_CONF_LO, MIN_CONF_HI = 0.0, 1.0")], False),
    ("min-conf: the CLI flag skips the range check", "clips.py", [("type=_min_conf_arg, default=0.85", "type=float, default=0.85")], False),
    ("pcen: the PNG writer ignores the flag", "clips.py", [('clip_render.write_png(ptmp, item["pcm"], pcen=self.cfg.pcen)', 'clip_render.write_png(ptmp, item["pcm"])')], False),
    ("pcen: the sidecar always says log", "clips.py", [("        if self.cfg.pcen:\n            import clip_render", "        if False:\n            import clip_render")], False),
    ("pcen: the flag is not passed to the config", "clips.py", [('pcen=getattr(a, "clip_pcen", False))', "pcen=False)")], False),
    ("pcen: the flag defaults to on", "clips.py", [('ap.add_argument("--clip-pcen", action="store_true"', 'ap.add_argument("--clip-pcen", action="store_false"')], False),
    ("pcen: time constant 0.4 s becomes 0.2 s", "clip_render.py", [("PCEN_TIME_CONSTANT_S = 0.4", "PCEN_TIME_CONSTANT_S = 0.2")], False),
    ("pcen: vmax 4 becomes 8", "clip_render.py", [("PCEN_VMAX = 4.0", "PCEN_VMAX = 8.0")], False),
    ("pcen: init first becomes librosa", "clip_render.py", [('"init": "first"', '"init": "librosa"')], False),
    ("pcen: input is amplitude instead of power", "clip_render.py", [("(amp[:, a:b] ** 2).max(axis=1)", "amp[:, a:b].max(axis=1)")], False),
    ("pcen: power scale 1 instead of 32768**2", "clip_render.py", [('"power_scale": pcen.INT16_POWER', '"power_scale": 1.0')], False),
    ("host: the on_plugin hook removed", "wiring.py", [("            self.clips.on_plugin(p.name, recs, x, t_wall)       # every window, also with zero records; never raises", "            pass")], True),
    ("host: clips.tick() removed from the main loop", "host.py", [("            clips.tick(now)", "            pass")], True),
    ("host: audio_written stuck false", "host.py", [("status[\"audio_written\"] = bool(clips.audio_written()) if clips else False", "status[\"audio_written\"] = False")], True),
]


def summary(n, bad, napply):
    """-> (summary line, exit code): 0 all caught, 1 some not caught, 2 some anchors did not apply (checked first: those proved nothing)."""
    return f"{n} mutations, {bad} not caught, {napply} did not apply", 2 if napply else (1 if bad else 0)


def anchor_problem(text, old):
    """None when `old` occurs exactly once in `text`; else the reason (a mutation that does not apply proves nothing)."""
    n = text.count(old)
    return None if n == 1 else f"mutation does not apply: {old[:60]!r} found {n} times"


def run_one(m):
    name, fn, edits, e2e = m
    root = tempfile.mkdtemp(prefix="mutclips_")
    try:
        for d in ("analysis", "contracts", "classify", "pc", "docker"):
            shutil.copytree(os.path.join(BMA, d), os.path.join(root, d), ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pkl"))
        p = os.path.join(root, "analysis", fn)
        s = open(p, encoding="utf-8").read()
        for old, new in edits:
            why = anchor_problem(s, old)
            if why:
                return name, None, [why]
            s = s.replace(old, new)
        try:
            if fn.endswith(".py"):
                compile(s, p, "exec")
        except SyntaxError as e:
            return name, None, [f"the mutant is not valid Python ({e.msg}); a syntax error proves nothing"]
        open(p, "w", encoding="utf-8", newline="\n").write(s)
        cmd = [sys.executable, "-I", os.path.join(root, "analysis", "test_clips.py")] + ([] if e2e else ["--no-e2e"])
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            return name, True, ["(timeout: the mutated code hung, counted as red)"]
        red = re.findall(r"^FAIL (.*?)(?:  \[|$)", r.stdout, re.M)
        crashed = r.returncode != 0 and not red
        tail = r.stderr.strip().splitlines()[-1][:200] if r.stderr.strip() else ""
        if crashed and "status.json" in tail:                   # the host under test never wrote status.json (e.g. a lost port): says nothing about the test
            return name, None, [f"inconclusive, not counted as caught: {tail}"]
        return name, bool(red) or crashed, red or ([f"(crash) {tail}"] if crashed else [])
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    jobs = int(sys.argv[sys.argv.index("--jobs") + 1]) if "--jobs" in sys.argv else 4
    base = run_one(("baseline (no mutation)", "clips.py", [], False))
    if base[1]:
        sys.exit(f"baseline is not green, a red mutation would prove nothing: {base[2][:3]}")
    print("baseline: test_clips.py is green on an unmutated copy")
    bad = napply = 0
    def show(name, caught, red):
        nonlocal bad, napply
        if caught is None:
            if red[0].startswith("mutation does not apply"):
                napply += 1
                print(f"DID-NOT-APPLY  {name}: {red[0]}")
            else:
                bad += 1
                print(f"ERROR  {name}: {red[0]}")
            return
        print(f"{'RED   ' if caught else 'GREEN '} {name}  ->  {len(red)} check(s) red" + (f": {red[0][:110]}" if red else ""))
        bad += 0 if caught else 1
    only = sys.argv[sys.argv.index("--only") + 1].lower() if "--only" in sys.argv else None      # run just the mutations whose name contains this
    sel = [m for m in M if only is None or only in m[0].lower()]
    unit = [m for m in sel if not m[3]]
    with cf.ThreadPoolExecutor(jobs) as ex:
        for res in ex.map(run_one, unit):
            show(*res)
    for m in (m for m in sel if m[3]):                           # the host mutations run one at a time (each starts three hosts and a UDP sender)
        show(*run_one(m))
    line, code = summary(len(sel), bad, napply)
    print("\n" + line)
    sys.exit(code)


if __name__ == "__main__":
    main()
