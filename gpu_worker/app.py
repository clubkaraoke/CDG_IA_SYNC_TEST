from __future__ import annotations

import os
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import torch
from fastapi import BackgroundTasks, FastAPI, File, Form, Header, HTTPException, UploadFile
from qwen_asr import Qwen3ASRModel, Qwen3ForcedAligner

APP_TOKEN = os.getenv("WORKER_TOKEN", "").strip()
ASR_MODEL = os.getenv("QWEN_ASR_MODEL", "Qwen/Qwen3-ASR-1.7B")
ALIGNER_MODEL = os.getenv("QWEN_ALIGNER_MODEL", "Qwen/Qwen3-ForcedAligner-0.6B")
DEVICE = os.getenv("QWEN_DEVICE", "cuda:0")
MAX_NEW_TOKENS = int(os.getenv("QWEN_MAX_NEW_TOKENS", "2048"))

app = FastAPI(title="CDG Qwen GPU Worker", version="0.2.0")
_asr: Qwen3ASRModel | None = None
_aligner: Qwen3ForcedAligner | None = None
_model_lock = threading.Lock()
_jobs_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}
_executor = ThreadPoolExecutor(max_workers=1)


def _auth(authorization: str | None) -> None:
    if not APP_TOKEN:
        return
    if authorization != f"Bearer {APP_TOKEN}":
        raise HTTPException(401, "Worker token inválido")


def _load_asr() -> Qwen3ASRModel:
    global _asr
    with _model_lock:
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
    with _model_lock:
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


def _set_job(job_id: str, **updates: Any) -> None:
    with _jobs_lock:
        job = _jobs.setdefault(job_id, {})
        job.update(updates)
        job["updated_at"] = time.time()


def _process_job(job_id: str, path: str, lyrics: str, language: str) -> None:
    started = time.monotonic()
    try:
        _set_job(job_id, status="loading_models", progress=10)

        if lyrics.strip():
            _set_job(job_id, status="aligning", progress=55)
            aligner = _load_aligner()
            aligned = aligner.align(audio=path, text=lyrics.strip(), language=language)
            words = _normalize(aligned[0] if aligned else [])
            result = {
                "ok": True,
                "mode": "lyrics_forced_alignment",
                "language": language,
                "text": lyrics.strip(),
                "words": words,
                "word_count": len(words),
                "elapsed_s": round(time.monotonic() - started, 3),
            }
        else:
            _set_job(job_id, status="transcribing", progress=35)
            asr = _load_asr()
            results = asr.transcribe(audio=path, language=language, return_time_stamps=False)
            if not results:
                raise RuntimeError("Qwen ASR no devolvió resultado")
            detected_text = results[0].text
            detected_language = results[0].language or language

            _set_job(job_id, status="aligning", progress=70, detected_text=detected_text)
            aligner = _load_aligner()
            aligned = aligner.align(audio=path, text=detected_text, language=detected_language)
            words = _normalize(aligned[0] if aligned else [])
            result = {
                "ok": True,
                "mode": "asr_then_forced_alignment",
                "language": detected_language,
                "text": detected_text,
                "words": words,
                "word_count": len(words),
                "elapsed_s": round(time.monotonic() - started, 3),
            }

        _set_job(job_id, status="done", progress=100, result=result)
    except Exception as exc:
        _set_job(job_id, status="error", progress=100, error=str(exc))
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@app.get("/health")
async def health(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _auth(authorization)
    return {
        "ok": True,
        "version": "0.2.0",
        "cuda": torch.cuda.is_available(),
        "device": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "asr_model": ASR_MODEL,
        "aligner_model": ALIGNER_MODEL,
        "asr_loaded": _asr is not None,
        "aligner_loaded": _aligner is not None,
        "queued_jobs": sum(1 for j in _jobs.values() if j.get("status") not in {"done", "error"}),
    }


@app.post("/jobs")
async def create_job(
    audio: UploadFile = File(...),
    lyrics: str = Form(""),
    language: str = Form("Spanish"),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _auth(authorization)

    suffix = Path(audio.filename or "audio.wav").suffix or ".wav"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await audio.read())
        path = tmp.name

    job_id = uuid.uuid4().hex
    _set_job(
        job_id,
        status="queued",
        progress=0,
        filename=audio.filename or "audio",
        created_at=time.time(),
        has_master_lyrics=bool(lyrics.strip()),
        language=language,
    )
    _executor.submit(_process_job, job_id, path, lyrics, language)
    return {"ok": True, "job_id": job_id, "status": "queued"}


@app.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _auth(authorization)
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Trabajo Qwen no encontrado")
        return {"ok": True, "job_id": job_id, **job}
