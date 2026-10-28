# Template for pc\bma.local.ps1 (untracked; copy it, then uncomment and edit only what you need). Dot-sourced by
# run_pipeline.ps1 from the data dir it sits in; install_task.ps1 copies it there once and never overwrites an existing
# one. Every line below is commented out, so an unedited copy changes nothing.
#$BMA_PYTHON          = '<path to python.exe>'         # bridge, dashboard, board collector (needs numpy); default: python on PATH
#$BMA_SINK_PYTHON     = '<path to python.exe>'         # cable sink (numpy + sounddevice); the sink starts only when set
#$BMA_BOARD           = '192.0.2.10'                    # the board's IP address, or its MAC (00-00-5E-00-53-01)
#$BMA_DSPD_TOKEN_FILE = '<path to the dspd token file>'         # file holding the dspd bearer token
#$BMA_MODEL_DIR       = '<path to the model dir>'             # analysis models; default <data-dir>\models
#$BMA_BEAM_SHIFT      = '1,0,0,0,0,0,0,-1'              # analyzer per-beam sample shift (8 ints, |k| <= 4; k=+1: beam arrives 1 sample late); default none
#$BMA_WSL_PYTHON      = '/path/in/wsl/bin/python'       # analyzer + classifier interpreter inside WSL; default python3
