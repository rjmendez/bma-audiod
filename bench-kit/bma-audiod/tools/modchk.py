import importlib, sys

print("py", sys.version.split()[0])
for m in ["numpy", "soundfile", "librosa", "torch", "torchaudio", "transformers", "birdnetlib", "tensorflow"]:
    try:
        x = importlib.import_module(m)
        print("OK", m, getattr(x, "__version__", "?"))
    except Exception as e:
        print("FAIL", m, type(e).__name__, str(e)[:70])
