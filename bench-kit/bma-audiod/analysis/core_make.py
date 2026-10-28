"""Factories of the five core plugins (analysis/plugins.manifest.json `make` entries): make(ctx) -> plugin instance.

They live next to, not inside, plugins.py on purpose: the classes are looked up as attributes of the plugins module at call time
(`P.Tagger`), so a test that swaps a class on the module, or replaces plugins.py with a stand-in, still takes effect. ctx is
manifest.Ctx (args, taxa, dev, classify_dir, ...)."""

import os

import plugins as P


def make_dsp(ctx):
    return P.DSPMonitor(ctx.args.tonal_mask, ctx.args.tonal_mask_tol)


def make_spatial(ctx):
    return P.Spatial()


def make_tags(ctx):
    return P.Tagger(ctx.args.panns_dir, ctx.dev)


def make_clap(ctx):
    return P.ClapZeroShot(ctx.dev)


def make_birds(ctx):
    return P.MultiBeamBirds(os.path.abspath(ctx.classify_dir), geo_config=ctx.args.geo_config, taxa=ctx.taxa)
