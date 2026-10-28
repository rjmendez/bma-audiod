# bma-audiod

Audio analysis and control stack around a BMA360-class beamforming microphone array with a Dante output: board-side
daemon and bring-up scripts, a PC-side receiver, analyzer (BirdNET, PANNs, CLAP, optional Perch), event classifier and
a web dashboard. Research and bench tooling, not a product.

## Layout
Everything lives under [`bench-kit/bma-audiod/`](bench-kit/bma-audiod/README.md) (kept so CI paths stay identical to the
development repository). Start at [`bench-kit/bma-audiod/README.md`](bench-kit/bma-audiod/README.md) and
[`STATUS.md`](bench-kit/bma-audiod/STATUS.md).

## Models
No model weights, checkpoints or label files are in this repository. BirdNET, PANNs, CLAP and Perch are obtained
separately by the operator under their own licences (BirdNET weights are non-commercial); the code only loads them from
the operator's own install.

## Not included
- No vendor material of any kind: no firmware, firmware images, reverse-engineering output, parameter catalogues, protocol dumps, vendor
  tool screenshots or board-side code that drives vendor binaries. The board side is not published.
- No packet captures, bench logs or dashboard screenshots.
- No history: this repository starts from a single export commit.

## Licence
MIT for this repository's own code and docs ([`LICENSE`](LICENSE)). Downloaded models keep their own licences; BirdNET
weights are non-commercial ([`NOTICE`](NOTICE)). Report vulnerabilities per [`SECURITY.md`](SECURITY.md).
