#!/usr/bin/env python3
"""Regenerate the run-history fixtures (fixtures/sink_history.jsonl, analyzer_history.jsonl, board_history.jsonl,
run_ledger.jsonl) by running the PRODUCTION writers on fixed inputs shaped like live values (nothing is read from
<data-dir>, nothing is invented by hand):

    python tools/make_history_fixtures.py

sink: bma_cable_sink.build_sink_sample (numbers as in fixtures/sink_status.json); analyzer: analysis/analyzer_history;
board: tools/bma_debug_rx.BoardHistory over a scripted health series with one reboot (uptime 130910 s -> 12 s);
ledger: pc/run_ledger.ps1 through powershell.exe (Windows only), including an unknown exit code, a negative exit code
and a crash-loop `suppressed` record. Needs numpy (the sink imports it)."""

import json, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CON = os.path.dirname(HERE)
BMA = os.path.dirname(CON)
for p in (os.path.join(BMA, "pc"), os.path.join(BMA, "tools"), os.path.join(BMA, "analysis")):
    sys.path.insert(0, p)
FIX = os.path.join(CON, "fixtures")


def write(name, rows):
    with open(os.path.join(FIX, name), "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")


def lines(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def main():
    import bma_cable_sink as S
    import analyzer_history as A
    import bma_debug_rx as R

    T = 1792884000.0
    st = {
        "t": T,
        "device": "CABLE In 16ch (VB-Audio Virtual Cable)",
        "audio_output": True,
        "device_error": None,
        "datagrams": 144000,
        "bad": 0,
        "buffered_ms": 151,
        "underruns": 0,
        "slips": 2,
        "slipped_frames": 14400,
        "missing_slot_blocks": 0,
        "holes": 41,
        "playing": True,
        "data_frames": 141,
        "data_dropped": 0,
    }
    rows = [
        S.build_sink_sample(dict(st, t=T), T - 3600.0),
        S.build_sink_sample(dict(st, t=T + 30, datagrams=144000 + 45000, holes=42, buffered_ms=148), T - 3600.0),
        S.build_sink_sample(
            dict(
                st,
                t=T + 60,
                device=None,
                audio_output=False,
                device_error="no CABLE In 16 device",
                playing=False,
                datagrams=0,
                holes=0,
                slips=0,
                slipped_frames=0,
                buffered_ms=0,
                data_frames=0,
            ),
            T + 45.0,
        ),
    ]  # restarted sink, device missing
    write("sink_history.jsonl", rows)

    status = {
        "started": T - 600.0,
        "t": T,
        "uptime_s": 600.0,
        "device": "cuda:0",
        "plugins": {
            "dsp": {"enabled": True, "windows": 600, "records": 612, "errors": 0, "dropped": 0},
            "spatial": {"enabled": True, "windows": 598, "records": 599, "errors": 0, "dropped": 0},
            "tags": {"enabled": True, "windows": 120, "records": 120, "errors": 1, "dropped": 3},
            "clap": {"enabled": False, "reason": "disabled by --no"},
            "novelty": {"enabled": True, "note": "fed by tags embeddings (non-speech only)"},
        },
        "rx": {"datagrams": 1125000, "bad": 0, "gaps": 16},
        "contiguous_s": 412.5,
        "embeddings_stored": 115,
        "audio_written": False,
    }
    later = dict(
        status,
        t=T + 30,
        uptime_s=630.0,
        contiguous_s=442.5,
        rx={"datagrams": 1181250, "bad": 0, "gaps": 16},
        plugins=dict(
            status["plugins"],
            dsp={"enabled": True, "windows": 630, "records": 643, "errors": 0, "dropped": 0},
            spatial={"enabled": True, "windows": 628, "records": 629, "errors": 0, "dropped": 0},
            tags={"enabled": True, "windows": 126, "records": 126, "errors": 1, "dropped": 3},
        ),
    )
    write("analyzer_history.jsonl", [A.build_analyzer_sample(status), A.build_analyzer_sample(later)])

    tmp = tempfile.mkdtemp(prefix="hist_fix_")
    try:
        bp = os.path.join(tmp, "board_history.jsonl")
        h = R.BoardHistory(bp)

        def health(t, up, seq, tl, tr, load="0.32 0.41 0.44", mem=6372):
            return {"type": "health", "up": up, "load": load, "memfree_kb": mem, "temp_local_c": tl, "temp_remote_c": tr, "seq": seq, "host_t": t}

        h.on_health(health(T, 130910.72, 10778, 40.0, 41.75))
        h.on_health(health(T + 60, 130970.72, 10779, 40.0, 41.38))
        h.on_health(health(T + 300, 12.5, 0, None, None, load="1.05 0.30 0.10", mem=9120))  # rebooted; sensors not read yet
        h.on_health(health(T + 360, 72.5, 1, 38.25, 39.0, load="0.55 0.35 0.12", mem=8800))
        shutil.copy(bp, os.path.join(FIX, "board_history.jsonl"))

        led = os.path.join(tmp, "ledger")
        os.makedirs(os.path.join(led, "app"))
        with open(os.path.join(led, "app", "VERSION"), "w", newline="\n") as f:
            f.write("b2aeb0ea feat(rerank): GPU cross-encoder sidecar (llama.cpp /rerank protocol) (#525)\n")
        script = os.path.join(tmp, "gen.ps1")
        with open(script, "w", newline="\n") as f:
            f.write(
                ". '" + os.path.join(BMA, "pc", "run_ledger.ps1") + "'\n"
                "Initialize-RunLedger -Root '" + led + "' -App '" + os.path.join(led, "app") + "' -MaxPerHour 2\n"
                "$t = 1792884000000\n"
                "[void](Write-RunEvent -Proc 'supervisor' -Ev 'supervisor_up' -ProcId 4100 -NowMs $t)\n"
                "[void](Write-RunEvent -Proc 'sink' -Ev 'start' -ProcId 8120 -NowMs ($t + 1000))\n"
                "[void](Write-RunEvent -Proc 'sink' -Ev 'exit' -ProcId 8120 -ExitCode -1073741819 -UptimeS 3600.5 -NowMs ($t + 3601500))\n"
                "[void](Write-RunEvent -Proc 'ui' -Ev 'exit' -ProcId 9001 -NowMs ($t + 3602000))\n"
                "[void](Write-RunEvent -Proc 'analysis' -Ev 'exit' -ProcId 7001 -ExitCode 127 -UptimeS 0.2 -NowMs ($t + 4000))\n"
                "[void](Write-RunEvent -Proc 'analysis' -Ev 'exit' -ProcId 7002 -ExitCode 127 -UptimeS 0.2 -NowMs ($t + 14000))\n"
                "[void](Write-RunEvent -Proc 'analysis' -Ev 'exit' -ProcId 7003 -ExitCode 127 -UptimeS 0.2 -NowMs ($t + 24000))\n"
                "[void](Write-RunEvent -Proc 'analysis' -Ev 'start' -ProcId 7004 -NowMs ($t + 7200000))\n"
            )
        r = subprocess.run(["powershell.exe", "-NoProfile", "-File", script], capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit("run ledger generation failed: " + r.stderr)
        rows = lines(os.path.join(led, "history", "run_ledger.jsonl"))
        write("run_ledger.jsonl", sorted(rows, key=lambda x: x["t"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("wrote sink_history, analyzer_history, board_history, run_ledger fixtures")


if __name__ == "__main__":
    main()
