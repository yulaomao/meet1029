"""Offline Smart Turn endpoint classifier.

It deliberately has no Hub download path: deployments provision the ONNX file
once and meetings continue to work without it if the classifier is absent.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
MAX_SECONDS = 8


@dataclass(frozen=True)
class SmartTurnResult:
    complete: bool
    probability: float
    inference_ms: float


class SmartTurnAnalyzer:
    """Small ONNX wrapper compatible with pipecat-ai/smart-turn-v3.2."""

    def __init__(self, model_path: str, *, threshold: float = 0.5, cpu_count: int = 1):
        path = Path(model_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(path)
        import onnxruntime as ort
        from transformers import WhisperFeatureExtractor

        options = ort.SessionOptions()
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = max(1, int(cpu_count))
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(str(path), sess_options=options, providers=["CPUExecutionProvider"])
        self._input_name = self._session.get_inputs()[0].name
        self._extractor = WhisperFeatureExtractor(chunk_length=MAX_SECONDS)
        self.threshold = min(1.0, max(0.0, float(threshold)))
        log.info("Smart Turn ready model=%s threshold=%.2f", path, self.threshold)

    def predict_pcm16(self, pcm16: bytes) -> SmartTurnResult:
        started = time.perf_counter()
        audio = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
        maximum = SAMPLE_RATE * MAX_SECONDS
        if audio.size > maximum:
            audio = audio[-maximum:]
        elif audio.size < maximum:
            audio = np.pad(audio, (maximum - audio.size, 0))
        inputs = self._extractor(
            audio,
            sampling_rate=SAMPLE_RATE,
            return_tensors="np",
            padding="max_length",
            max_length=maximum,
            truncation=True,
            do_normalize=True,
        )
        output = self._session.run(None, {self._input_name: np.asarray(inputs.input_features, dtype=np.float32)})
        probability = float(np.asarray(output[0]).reshape(-1)[0])
        if not np.isfinite(probability):
            raise RuntimeError("Smart Turn returned a non-finite probability")
        return SmartTurnResult(
            complete=probability > self.threshold,
            probability=probability,
            inference_ms=(time.perf_counter() - started) * 1000,
        )


def get_smart_turn(app) -> SmartTurnAnalyzer | None:
    """Lazily initialize once, with a sticky safe fallback on failure."""
    if not app.config.get("SMART_TURN_ENABLED", False):
        return None
    key = "smart_turn_analyzer"
    if key in app.extensions:
        return app.extensions[key]
    try:
        model_path = str(app.config.get("SMART_TURN_MODEL_PATH") or "")
        analyzer = SmartTurnAnalyzer(
            model_path,
            threshold=float(app.config.get("SMART_TURN_THRESHOLD", 0.5)),
            cpu_count=int(app.config.get("SMART_TURN_CPU_COUNT", 1)),
        )
    except Exception as exc:
        log.warning("Smart Turn unavailable; falling back to VAD endpointing: %s", exc)
        analyzer = None
    app.extensions[key] = analyzer
    return analyzer
