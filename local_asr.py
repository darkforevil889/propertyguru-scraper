"""Listening on this PC (Qwen3-ASR 1.7B) for when Groq's free Whisper allowance is used up for the day.

ai_call.py switches to this by itself: the helper starts once (about 30 s to load the model), then every recording
is split into speech pieces (silero VAD) and each piece written out - same shape as Whisper's segments, so answers
still land on the right line. Slower than Groq (about 40 s for a 30 s call) but no daily limit.

The model runs in its own Python (C:\\Users\\PC\\asr\\.venv: torch + qwen-asr) so this project's .venv stays light;
this file is both the client (imported by ai_call.py) and the helper (run by that other Python).

    C:\\Users\\PC\\asr\\.venv\\Scripts\\python.exe local_asr.py      # the helper (started for you)
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
PY = Path.home() / "asr" / ".venv" / "Scripts" / "python.exe"
MODEL = "Qwen/Qwen3-ASR-1.7B"
VAD = ROOT / "models" / "silero_vad.onnx"
LANGS = {"zh": "Chinese", "ms": "Malay", "en": "English"}  # Whisper codes -> Qwen's names
_proc = None


def available():
    return PY.exists() and VAD.exists()


def _start():
    global _proc
    (ROOT / "logs").mkdir(exist_ok=True)
    _proc = subprocess.Popen([str(PY), str(Path(__file__).resolve())], cwd=ROOT, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=open(ROOT / "logs" / "local_asr.log", "a"),
                             text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if not json.loads(_proc.stdout.readline() or "{}").get("ready"):
        _proc = None
        raise RuntimeError("Listening on this PC didn't start - see logs/local_asr.log")


def transcribe(name, data, context="", language=None):
    """Audio bytes (mp3 / wav) -> [{"start", "end", "text"}] like Whisper's segments."""
    if _proc is None or _proc.poll() is not None:
        _start()
    with tempfile.NamedTemporaryFile(suffix=Path(name).suffix or ".wav", delete=False) as f:
        f.write(data)
    try:
        _proc.stdin.write(json.dumps({"path": f.name, "context": context,
                                      "language": LANGS.get(language, language)}) + "\n")
        _proc.stdin.flush()
        reply = json.loads(_proc.stdout.readline() or '{"error": "the helper stopped"}')
    finally:
        Path(f.name).unlink(missing_ok=True)
    if "error" in reply:
        raise RuntimeError("Listening on this PC failed: " + reply["error"])
    return reply["segments"]


# ---------- the helper (runs in the asr Python) ----------
def serve():
    import io
    import numpy as np
    import sherpa_onnx
    import soundfile as sf
    import torch
    from qwen_asr import Qwen3ASRModel

    out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stdout = sys.stderr  # the libraries' own printing must not mix into the replies
    sys.stdin.reconfigure(encoding="utf-8")
    model = Qwen3ASRModel.from_pretrained(MODEL, dtype=torch.float32, device_map="cpu", max_new_tokens=128)
    cfg = sherpa_onnx.VadModelConfig()
    cfg.silero_vad.model, cfg.silero_vad.min_silence_duration, cfg.sample_rate = str(VAD), 0.4, 16000
    out.write(json.dumps({"ready": True}) + "\n")
    out.flush()
    for line in sys.stdin:
        req = json.loads(line)
        try:
            a, sr = sf.read(req["path"], dtype="float32", always_2d=True)
            a = a.mean(axis=1)
            if sr != 16000:
                a = np.interp(np.arange(0, len(a), sr / 16000), np.arange(len(a)), a).astype("float32")
            vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=600)
            for i in range(0, len(a), 512):
                vad.accept_waveform(a[i:i + 512])
            vad.flush()
            pieces = []
            while not vad.empty():  # copy the samples before pop() - they're gone afterwards
                pieces.append((vad.front.start / 16000, np.array(vad.front.samples, dtype="float32")))
                vad.pop()
            res = model.transcribe(audio=[(s, 16000) for _, s in pieces], context=req.get("context") or "",
                                   language=req.get("language")) if pieces else []
            segs = [{"start": round(st, 2), "end": round(st + len(s) / 16000, 2), "text": r.text.strip()}
                    for (st, s), r in zip(pieces, res) if r.text.strip()]
            out.write(json.dumps({"segments": segs}, ensure_ascii=False) + "\n")
        except Exception as e:
            out.write(json.dumps({"error": str(e)[:300]}) + "\n")
        out.flush()


if __name__ == "__main__":
    serve()
