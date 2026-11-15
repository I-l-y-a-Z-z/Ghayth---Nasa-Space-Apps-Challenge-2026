# Speech development tools

The production speech endpoints are documented in [the service README](../README.md). The tools here prepare/evaluate consented recordings and provide an optional standalone preview. They do not supply production farm facts.

Run from the repository root with `PYTHONPATH=services/speech` or use the normal root test configuration. Install optional model/training requirements only when needed.

## Collect and prepare

The standalone preview includes `/collect.html` for recording and manual transcript review:

```bash
PYTHONPATH=services/speech .venv/bin/python -m uvicorn agrix_voice.app:configured_app --factory --host 127.0.0.1 --port 8001
```

See the [recording guide](../evaluation/RECORDING_GUIDE.md). Keep consented audio outside source control and split speakers between train, validation and test sets.

```bash
PYTHONPATH=services/speech .venv/bin/python -m agrix_voice.corpus data/incoming data/prepared --seed 19
```

Preparation validates PCM audio, rejects silent/clipped files, deduplicates recordings and groups splits by speaker.

## Train and benchmark

```bash
.venv/bin/python -m pip install -r services/speech/requirements-training.txt
PYTHONPATH=services/speech .venv/bin/python -m agrix_voice.train_asr data/prepared runs/whisper-small --model openai/whisper-small
PYTHONPATH=services/speech .venv/bin/python -m agrix_voice.benchmark asr data/prepared/test.jsonl runs/moulsot.json --model atlasia/moulsot.v0.3 --backend qwen
PYTHONPATH=services/speech .venv/bin/python -m agrix_voice.benchmark tts services/speech/evaluation/tts.jsonl runs/kandir.json --backend kandir
```

Keep the held-out corpus unchanged when comparing adapters, decode settings or models. Benchmark transcripts, recordings and checkpoints remain local. These tools do not establish a trained agricultural checkpoint or listener approval. See [verification scope](../../../docs/testing.md).
