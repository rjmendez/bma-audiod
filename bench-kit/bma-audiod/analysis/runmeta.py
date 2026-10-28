"""Configuration snapshot records ("analysis_config") for <history>/config_history.jsonl.

One record at analyzer start and another whenever the CONTENT fingerprint changes (mask file, calibration file, geo
file, model versions). The record never carries a location: only whether the geo filter is on, its week and threshold.
The calibration the board stores is not reachable from WSL (no board access), so the analyzer reads an optional file
(host.py --calibration, default <analysis dir>/calibration.json) shaped like the dspd GET /v1/calibration answer
({"profile": {"beam_azimuth_deg": [8], "north_reference", "measured_at"}} or that profile itself); absent -> source "absent".
"""

import hashlib, json, os, time
from hist import RotatingJsonl


def sha12(b):
    return hashlib.sha256(b).hexdigest()[:12]


def _file_sha(path):
    with open(path, "rb") as f:
        return sha12(f.read())


def _ver(pkg):
    try:
        from importlib import metadata

        return metadata.version(pkg)
    except Exception:
        return None


def mask_info(path, tol_override=None):
    if not path:
        return {"path_set": False, "sha12": None, "n_lines": 0, "tol_hz": None, "version": None}
    try:
        with open(path, "r", encoding="utf-8") as f:
            m = json.load(f)
        lines = sorted(float(h) for h in m["lines_hz"])
        tol = float(tol_override if tol_override is not None else m.get("tolerance_hz", 4.0))
        # hash of the CONTENT that changes the analyzer's behaviour (lines + tolerance), not of the path or the notes
        return {"path_set": True, "sha12": sha12(json.dumps([lines, tol]).encode()), "n_lines": len(lines), "tol_hz": tol, "version": m.get("version")}
    except (OSError, ValueError, KeyError, TypeError):
        return {"path_set": True, "sha12": None, "n_lines": 0, "tol_hz": None, "version": None, "error": "unreadable"}


def calibration_info(path):
    if not path or not os.path.exists(path):
        return {"source": "absent"}
    try:
        with open(path, "rb") as f:
            raw = f.read()
        d = json.loads(raw.decode("utf-8"))
        prof = d.get("profile", d) if isinstance(d, dict) else {}
        az = prof.get("beam_azimuth_deg")
        if not (isinstance(az, list) and len(az) == 8):
            return {"source": "file", "sha12": sha12(raw), "azimuth_deg": None, "error": "no 8-entry beam_azimuth_deg"}
        return {
            "source": "file",
            "sha12": sha12(raw),
            "azimuth_deg": [round(float(a), 2) for a in az],
            "north_reference": prof.get("north_reference"),
            "measured_at": prof.get("measured_at"),
        }
    except (OSError, ValueError, TypeError):
        return {"source": "file", "sha12": None, "azimuth_deg": None, "error": "unreadable"}


def geo_info(geo_path, load_geo):
    """(public info, private fingerprint). The fingerprint hashes the location so a change is detected; it is never written."""
    if not geo_path:
        return {"on": False, "week": None, "threshold": None}, None
    try:
        g = load_geo(geo_path)
        return ({"on": True, "week": g.get("week"), "threshold": g.get("threshold")}, sha12(json.dumps([g.get("lat"), g.get("lon")]).encode()))
    except Exception:
        return {"on": True, "week": None, "threshold": None, "error": "unreadable"}, None


def models_info(panns_dir, novelty_k, novelty_mv):
    ck = os.path.join(panns_dir or "", "Cnn14_mAP=0.431.pth")
    try:
        st = os.stat(ck)
        panns = {"ckpt": os.path.basename(ck), "bytes": st.st_size, "mtime": int(st.st_mtime)}
    except OSError:
        panns = {"ckpt": None}
    return {
        "birdnetlib": _ver("birdnetlib"),
        "panns": panns,
        "panns_inference": _ver("panns-inference"),
        "clap": "laion/clap-htsat-unfused",
        "transformers": _ver("transformers"),
        "torch": _ver("torch"),
        "sklearn": _ver("scikit-learn"),
        "novelty": {"k": novelty_k, "mv": novelty_mv},
    }


def build_config(here, mask_path, mask_tol, calib_path, geo_path, load_geo, panns_dir, device, novelty_k, novelty_mv, beam_shift=None):
    """-> (record body without k/v/t/run_id, private fingerprint string)."""
    ver = None
    try:
        with open(os.path.join(here, "VERSION"), "r", encoding="utf-8") as f:
            ver = f.readline().strip() or None
    except OSError:
        pass
    geo, geo_fp = geo_info(geo_path, load_geo)
    body = {
        "app_version": ver,
        "mask": mask_info(mask_path, mask_tol),
        "calibration": calibration_info(calib_path),
        "geo": geo,
        "models": models_info(panns_dir, novelty_k, novelty_mv),
        "device": device,
    }
    if beam_shift is not None and any(beam_shift):
        body["beam_shift"] = [int(k) for k in beam_shift]  # optional: absent = no correction (older records, default run)
    return body, sha12(json.dumps([body, geo_fp], sort_keys=True).encode())


class ConfigWatcher:
    def __init__(self, hist_dir, build, run_id, clock=time.time, cap_bytes=4 << 20):
        self.out = RotatingJsonl(os.path.join(hist_dir, "config_history.jsonl"), cap_bytes)
        self.build, self.run_id, self.clock = build, run_id, clock
        self.fp, self.errors, self.written = None, 0, 0

    def poll(self):
        """Write a record when nothing was written yet in this run or the fingerprint changed. Never raises."""
        try:
            body, fp = self.build()
        except Exception:
            self.errors += 1
            return False
        if fp == self.fp:
            return False
        rec = {"k": "analysis_config", "v": 1, "t": self.clock(), "run_id": self.run_id, **body}
        if self.out.write(rec):
            self.fp = fp
            self.written += 1
            return True
        self.errors += 1
        return False
