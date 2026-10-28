#!/usr/bin/env python3
"""Regenerate contracts/fixtures/{clips_index.jsonl, clip_sidecar.json, clip_sidecar_audio_expired.json, analysis_status_clips.json, clips_index_lowconf.jsonl (--clip-min-conf 0.3) and the visual-only (default) clips_index_visual.jsonl, clip_sidecar_visual.json, analysis_status_clips_visual.json} by running the REAL clip
writer (analysis/clips.py via analysis/clips_sim.py) on a deterministic synthetic signal with a fixed clock in a temp directory, so
the fixtures are what the code writes. Needs numpy, scipy, soundfile and PIL (the analyzer venv).
    python -I make_clips_fixtures.py [out_dir]      (default: ../fixtures)
analysis/test_clips.py regenerates them in a temp dir and requires the same records (audio_bytes, signatures and envelope aside:
those depend on the FLAC encoder and the scipy version)."""

import json, os, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
AN = os.path.abspath(os.path.join(HERE, "..", "..", "analysis"))
sys.path.insert(0, AN)
import clips_sim as S  # noqa: E402
import clips as CL  # noqa: E402


def build(out_dir):
    work = tempfile.mkdtemp(prefix="cfix_")
    try:
        s = S.Sim(os.path.join(work, "clips"), cap_per_species_hour=1, audio=True)  # audio mode (--clip-audio): the audio fixtures

        # Strix varia 0.90, then a better Strix varia in the same hour (evicts the first), then a Poecile (kept)
        def two(sci, conf, common, **kw):  # the default --clip-min-beams 2: two distinct beams report the species
            return [S.row(sci, conf, beam=1, common=common, **kw), S.row(sci, conf - 0.02, beam=2, common=common, **kw)]

        s.play(
            {
                5: two("Strix varia", 0.90, "Barred Owl"),
                9: two("Strix varia", 0.95, "Barred Owl", in_region=True),
                13: two("Poecile carolinensis", 0.91, "Carolina Chickadee"),
                22: two("Turdus migratorius", 0.93, "American Robin"),
            },
            k_end=26,
        )
        robin = [c for c in s.clip_ids() if "turdus" in c[1]][0]
        s.log("tags.jsonl", [S.tag_row(0.9, True, CL.id_time(robin[1]) + 1.0)])  # a later tags row proves a voice in the robin's span
        assert s.svc.purge_human() == 1  # the robin clip is deleted whole: clip_evicted, reason human_voice
        ids = s.clip_ids()
        poecile = [c for c in ids if "poecile" in c[1]][0]
        os.makedirs(out_dir, exist_ok=True)
        tc = CL.id_time(poecile[1])
        sc = s.sidecar(*poecile)
        with open(os.path.join(HERE, "..", "fixtures", "analysis_status.json"), encoding="utf-8") as f:
            base = json.load(f)  # a status.json of an analyzer without clips, plus what the real service reports
        status = dict(base, audio_written=s.svc.audio_written(), clips=s.svc.status_block())
        status["meta"] = dict(base["meta"], **s.svc.counters())
        s.now = tc + 25 * 3600
        s.svc.store.sweep(s.now)  # audio older than 24 h: audio_expired tombstones
        sc2 = s.sidecar(*poecile)
        s.now = tc + 15 * 86400
        s.svc.store.sweep(s.now)  # sidecars older than 14 d: clip_sidecar_expired tombstones
        with open(os.path.join(s.root, "index.jsonl"), "rb") as f:
            idx = f.read()
        with open(os.path.join(out_dir, "clips_index.jsonl"), "wb") as f:
            f.write(idx)
        # the DEFAULT mode: visual-only, no audio file is ever written (the analyzer without --clip-audio)
        v = S.Sim(os.path.join(work, "clips_visual"), cap_per_species_hour=1)
        v.play({13: two("Poecile carolinensis", 0.91, "Carolina Chickadee")}, k_end=20)
        vid = v.clip_ids()[0]
        vsc = v.sidecar(*vid)
        vstatus = dict(base, audio_written=v.svc.audio_written(), clips=v.svc.status_block())
        vstatus["meta"] = dict(base["meta"], **v.svc.counters())
        assert v.audio_files() == [] and vsc["audio_stored"] is False
        with open(os.path.join(v.root, "index.jsonl"), "rb") as f:
            vidx = f.read()
        with open(os.path.join(out_dir, "clips_index_visual.jsonl"), "wb") as f:
            f.write(vidx)
        lc = S.Sim(os.path.join(work, "clips_lowconf"), cap_per_species_hour=1, min_conf=0.3)  # --clip-min-conf 0.3: an event at 0.31 is stored
        lc.play({13: two("Poecile carolinensis", 0.31, "Carolina Chickadee")}, k_end=20)
        assert len(lc.clip_ids()) == 1
        with open(os.path.join(lc.root, "index.jsonl"), "rb") as f:
            lidx = f.read()
        with open(os.path.join(out_dir, "clips_index_lowconf.jsonl"), "wb") as f:
            f.write(lidx)
        pc_ = S.Sim(os.path.join(work, "clips_pcen"), cap_per_species_hour=1, pcen=True)  # --clip-pcen: spectrogram.scale pcen
        pc_.play({13: two("Poecile carolinensis", 0.91, "Carolina Chickadee")}, k_end=20)
        psc = pc_.sidecar(*pc_.clip_ids()[0])
        assert psc["spectrogram"]["scale"] == "pcen"
        for name, obj in (("clip_sidecar_pcen.json", psc), ("clip_sidecar_visual.json", vsc), ("analysis_status_clips_visual.json", vstatus)):
            with open(os.path.join(out_dir, name), "wb") as f:
                f.write((json.dumps(obj, indent=1) + "\n").encode())
        for name, obj in (("clip_sidecar.json", sc), ("clip_sidecar_audio_expired.json", sc2), ("analysis_status_clips.json", status)):
            with open(os.path.join(out_dir, name), "wb") as f:
                f.write((json.dumps(obj, indent=1) + "\n").encode())
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return out_dir


if __name__ == "__main__":
    print("wrote", build(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "fixtures")))
