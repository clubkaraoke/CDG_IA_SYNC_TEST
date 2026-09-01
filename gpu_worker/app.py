from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import Any

import torch
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from qwen_asr import Qwen3ASRModel, Qwen3ForcedAligner

APP_TOKEN = os.getenv("WORKER_TOKEN", "").strip()
ASR_MODEL = os.getenv("QWEN_ASR_MODEL", "Qwen/Qwen3-ASR-1.7B")
ALIGNER_MODEL = os.getenv("QWEN_ALIGNER_MODEL", "Qwen/Qwen3-ForcedAligner-0.6B")
DEVICE = os.getenv("QWEN_DEVICE", "cuda:0")
MAX_NEW_TOKENS = int(os.getenv("QWEN_MAX_NEW_TOKENS", "2048"))

app = FastAPI(title="CDG Qwen GPU Worker", version="0.1.0")
_asr: Qwen3ASRModel | None = None
_aligner: Qwen3ForcedAligner | None = None


def _auth(authorization: str | None) -> None:
    if not APP_TOKEN:
        return
    if authorization != f"Bearer {APP_TOKEN}":
        raise HTTPException(401, "Worker token inválido")


def _load_asr() -> Qwen3ASRModel:
    global _asr
    if _asr is None:
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA no está disponible en este worker")
        _asr = Qwen3ASRModel.from_pretrained(
            ASR_MODEL,
            dtype=torch.bfloat16,
            device_map=DEVICE,
            max_inference_batch_size=1,
            max_new_tokens=MAX_NEW_TOKENS,
        )
    return _asr


def _load_aligner() -> Qwen3ForcedAligner:
    global _aligner
    if _aligner is None:
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA no está disponible en este worker")
        _aligner = Qwen3ForcedAligner.from_pretrained(
            ALIGNER_MODEL,
            dtype=torch.bfloat16,
            device_map=DEVICE,
        )
    return _aligner


def _normalize(items: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if items is None:
        return out
    for item in items:
        text = getattr(item, "text", None)
        start = getattr(item, "start_time", None)
        end = getattr(item, "end_time", None)
        if text is None and isinstance(item, dict):
            text = item.get("text") or item.get("word")
            start = item.get("start_time", item.get("start"))
            end = item.get("end_time", item.get("end"))
        if text is None:
            continue
        out.append({
            "word": str(text),
            "start": None if start is None else float(start),
            "end": None if end is None else float(end),
        })
    return out


@app.get("/health")
async def health(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _auth(authorization)
    return {
        "ok": True,
        "cuda": torch.cuda.is_available(),
        "device": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "asr_model": ASR_MODEL,
        "aligner_model": ALIGNER_MODEL,
        "asr_loaded": _asr is not None,
        "aligner_loaded": _aligner is not None,
    }


@app.post("/align")
async def align(
    audio: UploadFile = File(...),
    lyrics: str = Form(...),
    language: str = Form("Spanish"),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _auth(authorization)
    if not lyrics.strip():
        raise HTTPException(400, "Falta la letra para alinear")

    suffix = Path(audio.filename or "audio.wav").suffix or ".wav"
    started = time.monotonic()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await audio.read())
        path = tmp.name

    try:
        model = _load_aligner()
        results = model.align(audio=path, text=lyrics.strip(), language=language)
        words = _normalize(results[0] if results else [])
        return {
            "ok": True,
            "mode": "forced_alignment",
            "language": language,
            "text": lyrics.strip(),
            "words": words,
            "word_count": len(words),
            "elapsed_s": round(time.monotonic() - started, 3),
        }
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@app.post("/transcribe-align")
async def transcribe_align(
    audio: UploadFile = File(...),
    lyrics: str = Form(""),
    language: str = Form("Spanish"),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _auth(authorization)

    suffix = Path(audio.filename or "audio.wav").suffix or ".wav"
    started = time.monotonic()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await audio.read())
        path = tmp.name

    try:
        if lyrics.strip():
            aligner = _load_aligner()
            aligned = aligner.align(audio=path, text=lyrics.strip(), language=language)
            words = _normalize(aligned[0] if aligned else [])
            return {
                "ok": True,
                "mode": "lyrics_forced_alignment",
                "language": language,
                "text": lyrics.strip(),
                "words": words,
                "word_count": len(words),
                "elapsed_s": round(time.monotonic() - started, 3),
            }

        asr = _load_asr()
        results = asr.transcribe(audio=path, language=language, return_time_stamps=False)
        if not results:
            raise HTTPException(502, "Qwen ASR no devolvió resultado")
        detected_text = results[0].text
        detected_language = results[0].language or language

        aligner = _load_aligner()
        aligned = aligner.align(audio=path, text=detected_text, language=detected_language)
        words = _normalize(aligned[0] if aligned else [])
        return {
            "ok": True,
            "mode": "asr_then_forced_alignment",
            "language": detected_language,
            "text": detected_text,
            "words": words,
            "word_count": len(words),
            "elapsed_s": round(time.monotonic() - started, 3),
        }
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
