#!/usr/bin/env python3
"""Synthetic data for tools/burst_source_report.py: never live data, a seeded RNG, a known truth.

    python -I make_burst_source_fixtures.py [out_dir]     (default ../contracts/fixtures: regenerates burst_scorecard.json and burst_hours.jsonl
                                                            by running the REAL tool on a synthetic data dir)

Library use (tests, --self-check): write_data_dir(dir, hours, ...) writes dsp/spatial/ambient/board/... files in the layout the pipeline uses;
raw_hour(...) builds a raw hour record (the feature record before the selection thresholds) for logic tests without any file."""

import json, os, shutil, sys, tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
H0 = 1792800000  # a synthetic hour boundary (multiple of 3600)
CONTRACT_NOW = 1793000000  # fixed generated_utc of the contract fixture


def dumps(o):
    return json.dumps(o, separators=(",", ":"))


# ------------------------------------------------------------------ event time generators (seconds from the hour start)
def periodic(n, period, jitter, rng, on_s=200.0, off_s=100.0, start=5.0):
    """n events every `period` s (+- `jitter` s uniform) inside on-windows of a slow on/off envelope (a rate envelope the surrogates keep)."""
    out, t = [], start
    cyc = on_s + off_s
    while len(out) < n:
        if (t - start) % cyc < on_s:
            out.append(t + rng.uniform(-jitter, jitter))
            t += period
        else:
            t += cyc - ((t - start) % cyc)
    return np.sort(np.asarray(out))


def poisson_env(n, rate, rng, on_s=200.0, off_s=100.0, start=5.0):
    """n Poisson events (mean gap 1/rate) inside the same on/off envelope."""
    out, t = [], start
    cyc = on_s + off_s
    while len(out) < n:
        t += rng.exponential(1.0 / rate)
        ph = (t - start) % cyc
        if ph >= on_s:
            t += cyc - ph
        out.append(t)
    return np.asarray(out)


def exponential(n, rate, rng, start=1.0):
    return start + np.cumsum(rng.exponential(1.0 / rate, n))


def uniform_events(n, covered_s, rng):
    return np.sort(rng.uniform(1.0, covered_s - 1.0, n))


# ------------------------------------------------------------------ files
def write_jsonl(path, recs):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        for r in recs:
            f.write((r if isinstance(r, str) else dumps(r)).encode() + b"\n")


def dsp_lines(hours, rng):
    """hours: list of dict(hour, covered_s, events (seconds from the hour start), dsp_records=None, level=-50.0, beam=3, spread=2.0)."""
    out = []
    for h in hours:
        h0, cov = h["hour"], h["covered_s"]
        nrec = h.get("dsp_records") or cov
        for i in range(nrec):
            out.append(
                dumps(
                    {
                        "k": "dsp",
                        "t": h0 + (i % cov) + 0.3 + (0.4 if i >= cov else 0.0),
                        "rms_dbfs": [-67.8] * 8,
                        "peak_dbfs": -54.9,
                        "band_pct": [1.8, 94.9, 1.3, 1.9, 0.1],
                        "centroid_hz": 323.0,
                        "flatness": 0.0028,
                    }
                )
            )
        for j, e in enumerate(h["events"]):
            beam = h.get("beam", 3) if not callable(h.get("beam", 3)) else h["beam"](j)
            out.append(
                dumps(
                    {
                        "k": "transient",
                        "t": h0 + float(e),
                        "excess_db": 9.0,
                        "level_db": h.get("level", -50.0),
                        "loudest_beam": beam,
                        "beam_spread_db": h.get("spread", 2.0),
                    }
                )
            )
    return out


def write_data_dir(d, hours, ambient=None, board=None, coherence=None, spatial=False, bands=False, bursts=None, seed=1, dsp_name="dsp.jsonl"):
    """Write the input files of a data dir. ambient: {hour: temp}; board: list of (t, temp_local_c); coherence: list of (t0, t);
    spatial: write one `spatial` window per covered second of each hour (spread 1.5 dB when the second holds a transient, 4.0 otherwise);
    bands: a dir-relative path under d for beam_bands.jsonl (True = reports/beam_bands.jsonl); bursts: list of burst_stats record dicts."""
    rng = np.random.default_rng(seed)
    write_jsonl(os.path.join(d, "analysis", dsp_name), dsp_lines(hours, rng))
    if ambient is not None:
        write_jsonl(
            os.path.join(d, "history", "ambient_temp.jsonl"),
            [{"k": "ambient_temp", "v": 1, "t_hour_utc": h, "temp_c": t, "source": "open-meteo", "model_grid_km": None} for h, t in sorted(ambient.items())],
        )
    if board is not None:
        write_jsonl(
            os.path.join(d, "history", "board_history.jsonl"),
            [
                {
                    "k": "board_sample",
                    "v": 1,
                    "t": t,
                    "up": 1.0,
                    "boot_t": 1.0,
                    "boot_id": 1,
                    "temp_local_c": c,
                    "temp_remote_c": c,
                    "load1": 0.3,
                    "memfree_kb": 1,
                    "seq": i,
                }
                for i, (t, c) in enumerate(board)
            ],
        )
    if coherence is not None:
        write_jsonl(os.path.join(d, "analysis", "coherence.jsonl"), [{"k": "beam_coherence", "v": 1, "t0": a, "t": b} for a, b in coherence])
    if spatial:
        recs = []
        for h in hours:
            ts = {int(e) for e in h["events"]}
            for i in range(h["covered_s"]):
                recs.append(
                    {
                        "k": "spatial",
                        "t": h["hour"] + i + 0.5,
                        "az_deg": 10.0,
                        "R": 0.05,
                        "dominant_beam": 3,
                        "spread_db": 1.5 if i in ts else 4.0,
                        "e_rel_db": [0.0] * 8,
                        "directional": False,
                        "calibrated": False,
                    }
                )
        write_jsonl(os.path.join(d, "analysis", "spatial.jsonl"), recs)
    if bands:
        recs = []
        for h in hours:
            ts = {int(e) for e in h["events"]}
            for i in range(0, h["covered_s"], 5):
                lv = -45.0 if i in ts else -90.0
                recs.append(
                    {
                        "k": "beam_bands",
                        "v": 1,
                        "idx": i,
                        "t_us": int((h["hour"] + i + 0.5) * 1e6),
                        "n": 48000,
                        "zero_frac": 0.0,
                        "db": [[-80.0] * 7 + [lv] for _ in range(8)],
                    }
                )
        write_jsonl(os.path.join(d, "reports", "beam_bands.jsonl") if bands is True else os.path.join(d, bands), recs)
    if bursts is not None:
        write_jsonl(os.path.join(d, "analysis", "bursts.jsonl"), bursts)
    return d


def burst_stats(t0, n=60, analysed_s=60.0, regularity=0.9, spread_p50=1.5, gaps_n=59):
    return {
        "k": "burst_stats",
        "v": 1,
        "t0": t0,
        "interval_s": 60,
        "analysed_s": analysed_s,
        "impulses": {"n": n, "spread_db": {"p10": 1.0, "p50": spread_p50, "p90": 2.0}, "coincident_all_n": n // 2},
        "gaps": {"n": gaps_n, "regularity": regularity, "jitter_cv": 0.03, "mode_s": 0.75},
        "bands_db": {"p50": [-90.0, -90.0] + [-45.0] * 8},
    }


# ------------------------------------------------------------------ raw hour records (logic tests without files)
def raw_hour(
    hour,
    covered_s=1800,
    n=400,
    cv=1.0,
    peak_z=None,
    peak_lag=None,
    peak_ratio=3.0,
    level=-50.0,
    spread=2.0,
    entropy=0.95,
    board=None,
    coherence=None,
    bursts=None,
    ioi_n=None,
):
    """A raw hour record. peak_z None = no periodicity test. Everything threshold dependent (is_burst, ambient, ...) is added by derive()."""
    return {
        "k": "burst_hour",
        "v": 1,
        "hour_utc": hour,
        "covered_s": covered_s,
        "n_transients": n,
        "ioi": {
            "n": max(n - 1, 0) if ioi_n is None else ioi_n,
            "median_s": 0.5,
            "p10_s": 0.1,
            "p90_s": 2.0,
            "cv_trim": cv,
            "serial_corr_lag1": 0.0,
            "fano_1s": 1.0,
            "fano_10s": 1.0,
            "trim_max_gap_s": 10.0,
            "min_observable_s": 0.04,
        },
        "acorr": {
            "bin_s": 0.02,
            "max_lag_s": 5.0,
            "jitter_window_s": 10.0,
            "n_surrogates": 0 if peak_z is None else 200,
            "peak_lag_s": peak_lag,
            "peak_z": peak_z,
            "peak_ratio": None if peak_z is None else peak_ratio,
        },
        "level_db": {"median": level, "iqr": 1.0},
        "excess_db_median": 9.0,
        "beam": {"counts": [n // 8] * 8, "top_share": 0.125, "entropy_norm": entropy, "spread_db_median": spread},
        "spatial": None,
        "band_8_16k": None,
        "bursts": bursts,
        "board": board,
        "coherence": coherence,
    }


# ------------------------------------------------------------------ self-check scenarios (each builds a data dir; expected verdicts)
def scenario_plan(rng, kind):
    """-> (rates, amb): the transient rate per covered second of 72 consecutive hours (3 nights) and the ambient series {hour: temp} over hours -3..74.
    'cold': bursts (rate 0.3-0.9) at hour-of-day 4..10 UTC every night, 0.02 otherwise, temperature a daily cosine plus noise (rate independent of it);
    'warm': rate 0.0143 * (T - 4) rising with the temperature of days with mean 8, 10 and 13 C."""
    amb, rates = {}, []
    daymean = [8.0, 10.0, 13.0]
    for i in range(-3, 75):
        hod = (H0 // 3600 + i) % 24
        d = min(max(i, 0), 71) // 24
        if kind == "cold":
            temp = 2.0 + 12.0 * np.cos(2 * np.pi * (hod - 16) / 24) + rng.normal(0, 1.5)
        else:
            temp = daymean[d] + 6.0 * np.cos(2 * np.pi * (hod - 16) / 24)
        amb[H0 + 3600 * i] = round(float(temp), 1)
    for i in range(72):
        hod = (H0 // 3600 + i) % 24
        if kind == "cold":
            rates.append(rng.uniform(0.3, 0.9) if 4 <= hod <= 10 else 0.02)
        else:
            rates.append(max(0.01, 0.0143 * (amb[H0 + 3600 * i] - 4.0)))
    return rates, amb


def _scenario_hours(rng, kind):
    rates, amb = scenario_plan(rng, kind)
    return [{"hour": H0 + 3600 * i, "covered_s": 1800, "events": uniform_events(int(r * 1800), 1800, rng)} for i, r in enumerate(rates)], amb


def build_cold(d):
    hours, amb = _scenario_hours(np.random.default_rng(11), "cold")
    write_data_dir(d, hours, ambient=amb)


def build_warm(d):
    hours, amb = _scenario_hours(np.random.default_rng(12), "warm")
    write_data_dir(d, hours, ambient=amb)


def build_pulser(d):
    """1.2 s fence-like bursts: 2 hours on each of 3 nights, plus a quiet hour; no ambient."""
    rng = np.random.default_rng(13)
    hours = []
    for day in range(3):
        for k in (4, 5):
            hours.append({"hour": H0 + 3600 * (24 * day + k), "covered_s": 1800, "events": periodic(900, 1.2, 0.004, rng, on_s=400, off_s=200)})
    write_data_dir(d, hours)


def scenarios():
    return [
        ("cold bursts independent of temperature", build_cold, {"insect": "refuted"}),
        ("afternoon bursts rising with temperature", build_warm, {"insect": "supported"}),
        (
            "1.2 s fence-like bursts on 3 nights, no ambient",
            build_pulser,
            {"insect": "not-measurable", "pulser": "supported", "thermal": "not-measurable", "electronics": "not-measurable"},
        ),
    ]


def build_contract_fixtures(out_dir):
    """30 consecutive hours: bursts at hour-of-day 4..9 and a warm evening pair, ambient and board temperature, one hour with a bursts.jsonl, a digest hour is
    not part of it. The REAL tool writes burst_scorecard.json and burst_hours.jsonl into out_dir."""
    sys.path.insert(0, HERE)
    import burst_source_report as T

    rng = np.random.default_rng(2026)
    base = H0 - (H0 // 3600) % 24 * 3600 + 24 * 3600  # a midnight UTC
    hours, amb = [], {}
    for i in range(-2, 33):
        hod = i % 24
        amb[base + 3600 * i] = round(float(2.0 + 12.0 * np.cos(2 * np.pi * (hod - 16) / 24)), 1)
    for i in range(30):
        hod = i % 24
        hot = 4 <= hod <= 9 or hod in (20, 21)
        n = int(rng.uniform(450, 900)) if hot else int(rng.uniform(5, 40))
        ev = periodic(n, 0.75, 0.2, rng, on_s=500, off_s=120) if 4 <= hod <= 6 else uniform_events(n, 1800, rng)
        hours.append(
            {
                "hour": base + 3600 * i,
                "covered_s": 1800 + 40 * (i % 3),
                "events": ev,
                "level": -48.0 - (i % 3),
                "beam": lambda j: 1 + j % 8,
                "spread": 1.8 + 0.1 * (i % 4),
            }
        )
    board = [(base + 3600 * h + 60 * k, 41.0 - (0.3 + 0.1 * (h % 4)) * k / 60.0) for h in range(4, 10) for k in range(60)]
    bursts = [burst_stats(base + 3600 * 5 + 60 * k, analysed_s=60.0) for k in range(60)] + [burst_stats(base + 3600 * 6 + 60 * k) for k in range(60)]
    d = tempfile.mkdtemp(prefix="bsr_contract_")
    try:
        write_data_dir(d, hours, ambient=amb, board=board, coherence=[(base + 3600 * 4, base + 3600 * 4 + 60)], spatial=True, bands=True, bursts=bursts)
        os.makedirs(out_dir, exist_ok=True)
        code = T.main(["--data-dir", d, "--out", os.path.join(d, "reports", "out"), "--now", str(CONTRACT_NOW)])
        assert code == 0, code
        for fn in ("burst_scorecard.json", "burst_hours.jsonl"):
            shutil.copyfile(os.path.join(d, "reports", "out", fn), os.path.join(out_dir, fn))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return out_dir


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "contracts", "fixtures")
    build_contract_fixtures(out)
    print("wrote burst_scorecard.json and burst_hours.jsonl to", os.path.abspath(out))
