"""Bounded and opt-in local speech recognition for short clips and previews."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from ..config import DATA_ROOT

PREVIEW_SECONDS = 18
PCM_BYTES = PREVIEW_SECONDS * 16000 * 2
MODEL = Path(os.environ.get("KF_WHISPER_MODEL", DATA_ROOT / "vendor" / "ggml-tiny-q5_1.bin")).expanduser()


def _timestamp(ticks):
    seconds, centiseconds = divmod(max(0, ticks), 100)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}.{centiseconds:02}0"


def recognize(data):
    if not MODEL.is_file():
        raise RuntimeError("local transcription model is missing")
    pcm = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-threads", "1", "-protocol_whitelist",
         "pipe", "-i", "pipe:0", "-vn", "-t", str(PREVIEW_SECONDS), "-ar", "16000",
         "-ac", "1", "-f", "s16le", "pipe:1"],
        input=data, capture_output=True, timeout=9,
    )
    audio = pcm.stdout[:PCM_BYTES]
    if len(audio) < 16000:
        raise RuntimeError("the preview has no decodable audio")
    worker = subprocess.run(
        [sys.executable, "-B", "-m", "knowledge_forge.capture.speech"],
        input=audio, capture_output=True, timeout=34,
        env={**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"},
    )
    if worker.returncode:
        raise RuntimeError("local speech recognition did not complete within its resource limit")
    segments = json.loads(worker.stdout)
    lines = ["WEBVTT", ""]
    for start, end, value in segments:
        text = " ".join(value.split())
        if text:
            lines.extend((f"{_timestamp(start)} --> {_timestamp(end)}", text, ""))
    if len(lines) <= 2:
        raise RuntimeError("no speech detected in the preview")
    return "\n".join(lines).encode(), len(audio) / 32000


def _decode():
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (24, 25))
    os.nice(8)
    import numpy as np
    from pywhispercpp.model import Model
    audio = np.frombuffer(sys.stdin.buffer.read(PCM_BYTES), np.int16).astype(np.float32) / 32768
    model = Model(str(MODEL), n_threads=1, language="auto", print_progress=False,
                  print_realtime=False, print_timestamps=False, print_special=False)
    segments = model.transcribe(audio)
    print(json.dumps([(s.t0, s.t1, s.text) for s in segments], ensure_ascii=False))


if __name__ == "__main__":
    _decode()
