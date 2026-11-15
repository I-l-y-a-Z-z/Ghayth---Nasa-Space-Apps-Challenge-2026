"""Download/check the selected upstream speech models before a presentation."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/speech"))
os.environ.setdefault("HF_HOME", str(Path(__file__).resolve().parents[1] / "data/models"))
from agrix_voice.models import Recognizer, Synthesizer  # noqa: E402

for model in (Recognizer(), Synthesizer()):
    print(f"Loading {model.model_id}", flush=True)
    model.model = model.load()
    print("Loaded successfully", flush=True)
    del model.model
