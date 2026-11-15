# GPU speech setup

Install the PyTorch build appropriate to the machine before the optional [speech dependencies](../README.md). Check the GPU/driver with `nvidia-smi`; requirements do not provision a GPU driver.

Set the following in the root `.env` to use the selected Darija models:

```bash
AGRIX_DEVICE=cuda
AGRIX_ASR_MODEL=atlasia/moulsot.v0.3
AGRIX_ASR_BACKEND=qwen
AGRIX_TTS_MODEL=KandirResearch/DarijaTTS-v0.1-500M
AGRIX_TTS_BACKEND=kandir
HF_HOME=./data/models
```

`AGRIX_DEVICE=auto` chooses an available device. Keep one API worker. The product normally evicts the other speech model before inference to reduce memory use; `GHAYTH_VOICE_KEEP_MODELS=1` retains both only when the host has enough memory.

```bash
(set -a; . ./.env; set +a; .venv/bin/python scripts/prepare_voice.py)
./scripts/start.sh
```

`GET /speech/status` reports dependency availability and loading state. Initial downloads/loading differ from warm inference latency. Review recognized transcripts before sending. [Development tools](development.md) cover corpus preparation, training and benchmarks; accuracy evaluation requires independent recordings and native listeners.
