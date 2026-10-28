# Licences

Checked 2026-10-27. Weights are never published or redistributed by this repository; they are downloaded by the operator.

| Item | Licence | Use here | Weights redistributed | Status | Source |
|---|---|---|---|---|---|
| BirdNET-Analyzer code | MIT | reference only | n/a | VERIFIED | https://github.com/kahst/BirdNET-Analyzer |
| BirdNET model weights | CC BY-NC-SA 4.0 (non-commercial; README: educational and research use is non-commercial) | species classification, multi-beam | no | VERIFIED | https://github.com/kahst/BirdNET-Analyzer |
| birdnetlib | Apache-2.0 | loads BirdNET models | n/a | VERIFIED | https://github.com/joeweiss/birdnetlib |
| PANNs Cnn14 checkpoint | CC BY 4.0 | tagger, speech gate | no | VERIFIED | https://zenodo.org/records/3987831 |
| panns_inference | MIT | runs the PANNs checkpoint | n/a | VERIFIED | https://github.com/qiuqiangkong/panns_inference |
| PANNs label list (class_labels_indices.csv) | unread | label names | no | UNVERIFIED | https://github.com/qiuqiangkong/audioset_tagging_cnn |
| CLAP laion/clap-htsat-unfused | Apache-2.0 (model card) | zero-shot tags | no | VERIFIED | https://huggingface.co/laion/clap-htsat-unfused |
| Perch 2.0 weights (Google) | unread (Kaggle page not readable) | embeddings + species logits, opt-in | no | UNVERIFIED | https://www.kaggle.com/models/google/bird-vocalization-classifier |
| Perch 2.0 ONNX repack (justinchuby/Perch-onnx) | Apache-2.0 (model card; origin stated as the Kaggle model above) | downloaded by tools/fetch_perch.py | no | UNVERIFIED | https://huggingface.co/justinchuby/Perch-onnx |
| Perch labels.csv (cgeorgiaw/Perch) | Apache-2.0 (model card; provenance not stated) | label names for Perch logits | no | UNVERIFIED | https://huggingface.co/cgeorgiaw/Perch |
| google-research/perch code | Apache-2.0 | reference only | n/a | VERIFIED | https://github.com/google-research/perch |
| Open-Meteo data | CC BY 4.0; free API for non-commercial use, under 10000 calls/day | hourly air temperature, county-level location | n/a | VERIFIED | https://open-meteo.com/en/terms |
| scikit-maad | BSD-3-Clause | acoustic indices | n/a | VERIFIED | https://github.com/scikit-maad/scikit-maad |
| librosa | ISC | audio features | n/a | VERIFIED | https://github.com/librosa/librosa |
| onnxruntime | MIT | runs Perch ONNX | n/a | VERIFIED | https://github.com/microsoft/onnxruntime |
| torch | BSD-style | runs PANNs, CLAP | n/a | VERIFIED | https://github.com/pytorch/pytorch |
| VB-Cable | donationware; redistribution terms unread | virtual audio cable sink | n/a | UNVERIFIED | https://vb-audio.com/Cable/ |
| Dante (Audinate) | unread (page returned 403) | audio network input | n/a | UNVERIFIED | https://www.audinate.com/products/software/dante-virtual-soundcard |
