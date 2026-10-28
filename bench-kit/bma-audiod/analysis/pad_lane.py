"""Pad lane (opt-in, experimental): the `tags` (PANNs) and `clap` windows zero-padded to a 10 s input.

Plugins tags_pad / clap_pad (host.py --tags-pad-s S / --clap-pad-s S, default 0 = off; --no tags_pad / --no clap_pad).
Same model, same weights, same 5 s window as the production plugin; the window audio is followed by zeros up to S seconds.
No pre-cleaning (no high-pass, no gating). Output: tags_pad.jsonl / clap_pad.jsonl (contract pad-lane); tags.jsonl / clap.jsonl never change.
tags_pad: a window with speech probability >= SPEECH_P, padded or unpadded input, writes no record and no embedding. Records carry variant "pad10", pad_s, ms. No audio is stored.
"""
import threading, time
import numpy as np
from scipy import signal

import plugins as P

VARIANT = "pad10"
FS_TAGS = 32000                      # PANNs input rate (the 48 kHz beam is resampled 2:3)


def pad_zeros(x, n):
    """1-D float32 x followed by zeros up to n samples. Longer input is returned unchanged (never trimmed)."""
    x = np.asarray(x, dtype=np.float32)
    if x.ndim != 1:
        raise ValueError("pad_zeros needs a 1-D signal, got %d-D" % x.ndim)
    if len(x) >= n:
        return x
    return np.concatenate([x, np.zeros(n - len(x), dtype=np.float32)])


def shared(plug, name):
    """The running production plugin called `name` in the host plugin list, else None."""
    return next((q for q in plug if q.name == name), None)


def check_pad_s(pad_s, win_s):
    if not pad_s >= win_s:
        raise ValueError("pad seconds %s must be >= the window %s" % (pad_s, win_s))
    return float(pad_s)


class Cost:
    """Wall ms of the padded model call per window (speech-suppressed windows included).
    ref_ms (tags only): wall ms of the unpadded model call on the same window, same process; ms_ref_per_window = its mean."""

    def __init__(self, hop_s):
        self.hop_s, self.n, self.total_ms, self.n_ref, self.ref_ms, self.lock = hop_s, 0, 0.0, 0, 0.0, threading.Lock()

    def add(self, ms, ref_ms=None):
        with self.lock:
            self.n += 1
            self.total_ms += ms
            if ref_ms is not None:
                self.n_ref += 1
                self.ref_ms += ref_ms

    def report(self, pad_s):
        with self.lock:
            if not self.n:
                return {"pad_s": pad_s}
            ms = self.total_ms / self.n
            out = {"pad_s": pad_s, "ms_per_window": round(ms, 1), "ms_per_audio_s": round(ms / self.hop_s, 2)}
            if self.n_ref:
                out["ms_ref_per_window"] = round(self.ref_ms / self.n_ref, 1)
            return out


class PadTagger(P.Tagger):
    """PANNs CNN14 on the 32 kHz window zero-padded to pad_s. `share` = the running production Tagger: its model is reused (no second load)."""
    name = "tags_pad"
    heavy = True                                         # declared here (the manifest checks the class attribute); same value as P.Tagger

    def __init__(self, data_dir, device, pad_s, share=None):
        self.pad_s = check_pad_s(pad_s, self.win_s)
        if share is None:
            super().__init__(data_dir, device)
        else:
            self.labels, self.speech_idx, self.at, self.device = share.labels, share.speech_idx, share.at, share.device
            self.model = share.model                         # the production tagger's own provenance block (same weights, same digest)
        self.model = P.model_block(self.model["name"], self.model["version"], VARIANT, self.model["sha256"])     # variant = the input policy, as `variant` of the record
        self.n_pad = int(round(self.pad_s * FS_TAGS))
        self.cost_ = Cost(self.hop_s)

    def cost(self):
        return self.cost_.report(self.pad_s)

    def _speech_p(self, c):
        return float(c[self.speech_idx].max()) if self.speech_idx else 0.0

    def process(self, x, t_wall):
        b = P.best_beam(x)
        a = signal.resample_poly(x[:, b].astype(np.float32), 2, 3).astype(np.float32)
        padded = pad_zeros(a, self.n_pad)[None, :]
        t0 = time.perf_counter()
        clip, _emb = self.at.inference(padded)
        ms = (time.perf_counter() - t0) * 1000.0
        c = clip[0]
        speech_p = self._speech_p(c)
        ref_ms = None
        if speech_p < self.SPEECH_P:                       # padding dilutes the clip-level speech output: the unpadded verdict (production's) also gates
            t1 = time.perf_counter()
            clip_u, _ = self.at.inference(a[None, :])
            ref_ms = (time.perf_counter() - t1) * 1000.0
            if self._speech_p(clip_u[0]) >= self.SPEECH_P:
                speech_p = self.SPEECH_P
        self.cost_.add(ms, ref_ms)
        if speech_p >= self.SPEECH_P:
            return [], None
        full = np.argsort(c)[::-1]
        top = full[:5]
        speech = set(self.speech_idx)
        top_k, unstored = P.score_tail(c, self.labels, [i for i in full if i not in speech][:P.SCORE_K + 1])      # as the production tagger: human-voice classes not listed
        return [{"k": "tags", "t": t_wall, "beam": b + 1, "speech_p": round(speech_p, 3), "speech_active": False,
                 "top": [[self.labels[i], round(float(c[i]), 3)] for i in top if c[i] >= 0.05],
                 "emb_stored": False, "variant": VARIANT, "pad_s": self.pad_s, "ms": round(ms, 1),
                 "model": dict(self.model), "claim": dict(P.CLAIM), "top_k": top_k, "max_unstored_score": unstored}], None


class PadClap(P.ClapZeroShot):
    """LAION-CLAP on the 48 kHz window zero-padded to pad_s (the stock processor repeats a short clip instead; here it gets a full-length input).
    `share` = the running production ClapZeroShot: model, processor and text embeddings are reused."""
    name = "clap_pad"
    heavy = True

    def __init__(self, device, pad_s, share=None):
        self.pad_s = check_pad_s(pad_s, self.win_s)
        if share is None:
            super().__init__(device)
        else:
            self.torch, self.device, self.proc, self.model = share.torch, share.device, share.proc, share.model
            self.prompts, self.text, self.scale = share.prompts, share.text, share.scale
            self.model_meta = share.model_meta               # the production clap's own provenance block (same weights, same digest)
        self.model_meta = P.model_block(self.model_meta["name"], self.model_meta["version"], VARIANT, self.model_meta["sha256"])
        self.n_pad = int(round(self.pad_s * P.FS))
        self.cost_ = Cost(self.hop_s)

    def cost(self):
        return self.cost_.report(self.pad_s)

    def process(self, x, t_wall):
        torch = self.torch
        b = P.best_beam(x)
        wav = [pad_zeros(x[:, b], self.n_pad)]
        t0 = time.perf_counter()
        with torch.no_grad():
            try:                                   # newer transformers: `audio`; older: `audios`
                ai = self.proc(audio=wav, sampling_rate=P.FS, return_tensors="pt")
            except (TypeError, ValueError):
                ai = self.proc(audios=wav, sampling_rate=P.FS, return_tensors="pt")
            ai = {k: v.to(self.device) for k, v in ai.items()}
            a = torch.nn.functional.normalize(P._feat(self.model.get_audio_features(**ai)), dim=-1)
            p = torch.softmax(self.scale * (a @ self.text.T), dim=-1)[0].cpu().numpy()
        ms = (time.perf_counter() - t0) * 1000.0
        self.cost_.add(ms)
        order = np.argsort(p)[::-1][:P.SCORE_K + 1]
        top_k, unstored = P.score_tail(p, self.prompts, order)
        return [{"k": "clap", "t": t_wall, "beam": b + 1,
                 "top": [[self.prompts[i], round(float(p[i]), 3)] for i in order[:3]],
                 "variant": VARIANT, "pad_s": self.pad_s, "ms": round(ms, 1),
                 "model": dict(self.model_meta), "claim": dict(P.CLAIM), "top_k": top_k, "max_unstored_score": unstored}], None


def make_tags_pad(ctx):
    """Plugin factory named by analysis/plugins.manifest.json: the running production tagger (if any) lends its model."""
    return PadTagger(ctx.args.panns_dir, ctx.dev, ctx.args.tags_pad_s, share=shared(ctx.plug, "tags"))


def make_clap_pad(ctx):
    return PadClap(ctx.dev, ctx.args.clap_pad_s, share=shared(ctx.plug, "clap"))
