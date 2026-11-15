# Darija speech

Ghayth mounts `/speech/status`, `/speech/transcribe` and `/speech/synthesize` through the root `speech_api.py`. Recognition defaults to `atlasia/moulsot.v0.3`; synthesis defaults to `KandirResearch/DarijaTTS-v0.1-500M`. These are external pretrained models.

## Install

Install a PyTorch build suitable for the target CPU/GPU first, then the inference dependencies. For the previously verified CPU setup:

```bash
.venv/bin/python -m pip install 'torch>=2.6,<2.8' --index-url https://download.pytorch.org/whl/cpu
.venv/bin/python -m pip install 'torchaudio==2.7.1+cpu' --index-url https://download.pytorch.org/whl/cpu
.venv/bin/python -m pip install -r services/speech/requirements-models.txt
.venv/bin/python -m pip install 'outetts==0.3.3' 'llama-cpp-python==0.3.16' --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
```

Run commands from the repository root. A C++ compiler may be needed without a suitable llama.cpp wheel. Weights download into the Hugging Face cache; `HF_HOME=./data/models` keeps them in the ignored runtime directory.

To download/check selected models before use:

```bash
(set -a; . ./.env; set +a; .venv/bin/python scripts/prepare_voice.py)
```

The app loads models lazily. `/speech/status` reports installed dependencies and loading state; dependency availability does not mean weights are loaded. Normal planning starts without waiting for models. GPU improves latency; typed chat remains available if inference fails.

## Product behavior

Microphone access requires HTTPS or localhost. Record up to 25 seconds, review/edit the transcript, then send it. The product does not persist audio. Speech shares project/session access control, runs one inference at a time and normally evicts the other model to limit memory. Farm questions use the planning engine's `/chat`, not the standalone development adapter.

## Development tools

`agrix_voice` also retains corpus preparation, ASR training/benchmarks and a standalone development preview. These are optional tools, not the production farm service. See [development](docs/development.md), [GPU runbook](docs/voice-gpu.md) and [recording guide](evaluation/RECORDING_GUIDE.md). Production transport tests are in `tests/test_speech_integration.py` at the repository root.

Smoke records are retained in [speech evidence](../../docs/evidence/speech-verification.json). Native-listener and agricultural recognition accuracy evaluation remain separate from transport/model-loading checks.
