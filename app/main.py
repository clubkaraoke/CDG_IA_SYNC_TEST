from __future__ import annotations

import mimetypes
import os
import time

import httpx
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .audio_meta import extract_embedded_lyrics
from .logstore import read_events, write_event
from .mvsep import MVSEPClient, MVSEPError
from .parser import parse_transcription
from .qwen_worker import QwenWorkerClient, QwenWorkerError
from .elevenlabs_scribe import ElevenLabsScribeClient, ElevenLabsScribeError, map_scribe_to_master

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
CPU_WORKER_URL = os.getenv("CPU_WORKER_URL", "http://cdg-ai-sync-cpu:8001").rstrip("/")

app = FastAPI(title="CDG IA Sync Test", version="0.3.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

mvsep = MVSEPClient()
qwen = QwenWorkerClient()
elevenlabs = ElevenLabsScribeClient()
ALLOWED_EXTENSIONS = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg"}
_last_status: dict[str, str] = {}


class TokenConfig(BaseModel):
    token: str


class QwenConfig(BaseModel):
    endpoint: str
    token: str = ""


@app.middleware("http")
async def diagnostic_request_log(request: Request, call_next):
    request_id = request.headers.get("x-debug-request-id")
    is_transcribe = request.method == "POST" and request.url.path.endswith("/api/transcribe")
    is_local_align = request.method == "POST" and request.url.path.endswith("/api/local/align")
    is_elevenlabs = request.method == "POST" and (
        request.url.path.endswith("/api/elevenlabs/transcribe")
        or request.url.path.endswith("/api/elevenlabs/forced-align")
    )
    is_upload = is_transcribe or is_local_align or is_elevenlabs
    started = time.monotonic()
    if is_upload:
        write_event(
            "browser_upload_started",
            request_id=request_id,
            message="El navegador inició el envío hacia OVH.",
            details={
                "content_length": request.headers.get("content-length"),
                "content_type": request.headers.get("content-type"),
                "route": request.url.path,
            },
        )
    try:
        response = await call_next(request)
    except Exception as exc:
        if is_upload:
            write_event(
                "browser_upload_backend_error",
                level="error",
                request_id=request_id,
                message=str(exc),
                details={"elapsed_s": round(time.monotonic() - started, 3)},
            )
        raise
    if is_upload:
        write_event(
            "browser_upload_request_finished",
            level="info" if response.status_code < 400 else "error",
            request_id=request_id,
            message=f"OVH terminó la petición HTTP ({response.status_code}).",
            details={
                "status_code": response.status_code,
                "elapsed_s": round(time.monotonic() - started, 3),
            },
        )
    return response


@app.get("/")
async def home() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "version": "0.3.0",
        "mvsep_configured": mvsep.is_configured(),
        "mvsep_api_base": mvsep.base_url,
        "model": "Parakeet v3",
        "sep_type": 64,
        "add_opt1": 0,
        "add_opt2": 1,
        "qwen_configured": qwen.is_configured(),
        "qwen_endpoint": qwen.endpoint() if qwen.is_configured() else None,
        "elevenlabs_configured": elevenlabs.is_configured(),
        "elevenlabs_model": elevenlabs.model_id,
    }


@app.get("/api/logs")
async def logs(limit: int = 200) -> dict[str, Any]:
    return {"ok": True, "events": read_events(limit)}


@app.get("/api/queue")
async def queue_info() -> dict[str, Any]:
    try:
        payload = await mvsep.get_queue_info()
        return {"ok": True, "mvsep": payload}
    except Exception as exc:
        write_event("mvsep_queue_info_error", level="error", message=str(exc))
        raise HTTPException(502, f"No se pudo consultar la cola de MVSEP: {exc}") from exc


@app.post("/api/config/token")
async def configure_token(config: TokenConfig) -> dict[str, Any]:
    try:
        mvsep.save_token(config.token)
    except MVSEPError as exc:
        raise HTTPException(400, str(exc)) from exc
    write_event("mvsep_token_configured", message="Token de MVSEP configurado en OVH.")
    return {"ok": True, "mvsep_configured": True}


@app.post("/api/qwen/config")
async def configure_qwen(config: QwenConfig) -> dict[str, Any]:
    try:
        qwen.save_config(config.endpoint, config.token)
    except QwenWorkerError as exc:
        raise HTTPException(400, str(exc)) from exc
    write_event(
        "qwen_worker_configured",
        message="Endpoint Qwen GPU configurado en OVH.",
        details={"endpoint": config.endpoint.strip().rstrip("/")},
    )
    return {"ok": True, "qwen_configured": True}


@app.get("/api/qwen/health")
async def qwen_health() -> dict[str, Any]:
    try:
        payload = await qwen.health()
        return {"ok": True, "worker": payload}
    except Exception as exc:
        write_event("qwen_worker_health_error", level="error", message=str(exc))
        raise HTTPException(502, f"Qwen GPU Worker no disponible: {exc}") from exc


@app.post("/api/inspect-audio")
async def inspect_audio(audio: UploadFile = File(...)) -> dict[str, Any]:
    filename = audio.filename or "audio"
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Formato no permitido: {suffix or 'sin extensión'}")
    meta = await extract_embedded_lyrics(audio)
    write_event(
        "audio_metadata_inspected",
        filename=filename,
        message="Metadatos de letra inspeccionados.",
        details={"lyrics_found": meta.get("found"), "source": meta.get("source")},
    )
    return {"ok": True, **meta}


@app.post("/api/qwen/process")
async def qwen_process(
    audio: UploadFile = File(...),
    lyrics: str = Form(""),
    prefer_embedded: bool = Form(True),
    language: str = Form("Spanish"),
) -> dict[str, Any]:
    filename = audio.filename or "audio"
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Formato no permitido: {suffix or 'sin extensión'}")

    embedded = {"found": False, "lyrics": "", "source": None}
    if prefer_embedded and not lyrics.strip():
        embedded = await extract_embedded_lyrics(audio)

    master_lyrics = lyrics.strip()
    lyrics_source = "manual" if master_lyrics else None
    if not master_lyrics and embedded.get("found"):
        master_lyrics = str(embedded.get("lyrics") or "").strip()
        lyrics_source = f"embedded:{embedded.get('source') or 'lyrics'}"

    write_event(
        "qwen_job_started",
        filename=filename,
        message="Enviando audio al Qwen GPU Worker.",
        details={
            "lyrics_source": lyrics_source or "qwen_asr",
            "language": language,
            "forced_alignment_only": bool(master_lyrics),
        },
    )

    started = time.monotonic()
    try:
        payload = await qwen.transcribe_align(
            audio,
            lyrics=master_lyrics,
            language=language,
        )
    except QwenWorkerError as exc:
        write_event(
            "qwen_job_error",
            level="error",
            filename=filename,
            message=str(exc),
            details={"elapsed_s": round(time.monotonic() - started, 3)},
        )
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:
        write_event(
            "qwen_job_error",
            level="error",
            filename=filename,
            message=str(exc),
            details={"elapsed_s": round(time.monotonic() - started, 3)},
        )
        raise HTTPException(502, f"Falló Qwen GPU Worker: {exc}") from exc

    write_event(
        "qwen_job_done",
        filename=filename,
        message="Qwen terminó la transcripción/alineación.",
        details={
            "elapsed_s": round(time.monotonic() - started, 3),
            "word_count": payload.get("word_count"),
            "mode": payload.get("mode"),
            "lyrics_source": lyrics_source or "qwen_asr",
        },
    )
    return {
        "ok": True,
        "lyrics_source": lyrics_source or "qwen_asr",
        "embedded_lyrics_found": bool(embedded.get("found")),
        "embedded_lyrics_source": embedded.get("source"),
        "qwen": payload,
    }


@app.post("/api/transcribe")
async def transcribe(request: Request, audio: UploadFile = File(...)) -> dict[str, Any]:
    request_id = request.headers.get("x-debug-request-id")
    filename = audio.filename or "audio"
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        write_event(
            "upload_rejected",
            level="error",
            request_id=request_id,
            filename=filename,
            message=f"Formato no permitido: {suffix or 'sin extensión'}",
        )
        raise HTTPException(400, f"Formato no permitido: {suffix or 'sin extensión'}")

    write_event(
        "ovh_file_ready",
        request_id=request_id,
        filename=filename,
        message="OVH recibió el archivo completo. Iniciando envío a MVSEP.",
        details={"content_type": audio.content_type},
    )

    started = time.monotonic()
    try:
        payload = await mvsep.create_parakeet_job(audio)
    except MVSEPError as exc:
        write_event(
            "mvsep_create_error",
            level="error",
            request_id=request_id,
            filename=filename,
            message=str(exc),
            details={"elapsed_s": round(time.monotonic() - started, 3)},
        )
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:
        write_event(
            "mvsep_create_error",
            level="error",
            request_id=request_id,
            filename=filename,
            message=str(exc),
            details={"elapsed_s": round(time.monotonic() - started, 3)},
        )
        raise HTTPException(502, f"No se pudo crear el trabajo en MVSEP: {exc}") from exc

    job_hash = payload["data"]["hash"]
    write_event(
        "mvsep_job_created",
        request_id=request_id,
        job_hash=job_hash,
        filename=filename,
        message="MVSEP aceptó el trabajo y devolvió hash.",
        details={"elapsed_s": round(time.monotonic() - started, 3)},
    )
    return {"ok": True, "hash": job_hash, "mvsep": payload}


@app.get("/api/status/{job_hash}")
async def status(job_hash: str) -> dict[str, Any]:
    try:
        payload = await mvsep.get_status(job_hash)
    except Exception as exc:
        write_event(
            "mvsep_status_error",
            level="error",
            job_hash=job_hash,
            message=str(exc),
        )
        raise HTTPException(502, f"No se pudo consultar MVSEP: {exc}") from exc

    status_value = str(payload.get("status"))
    data = payload.get("data") or {}
    signature = f"{status_value}:{data.get('current_order')}:{data.get('queue_count')}"
    if _last_status.get(job_hash) != signature:
        _last_status[job_hash] = signature
        write_event(
            "mvsep_status_changed",
            job_hash=job_hash,
            message=f"Estado MVSEP: {status_value}",
            details={
                "current_order": data.get("current_order"),
                "queue_count": data.get("queue_count"),
                "message": data.get("message"),
            },
        )
    return payload


def _file_name(file_info: dict[str, Any], idx: int) -> str:
    return str(
        file_info.get("download")
        or file_info.get("name")
        or file_info.get("filename")
        or f"resultado_{idx}"
    )


@app.get("/api/result/{job_hash}")
async def result(job_hash: str) -> dict[str, Any]:
    try:
        payload = await mvsep.get_status(job_hash)
    except Exception as exc:
        write_event("result_status_error", level="error", job_hash=job_hash, message=str(exc))
        raise HTTPException(502, f"No se pudo consultar MVSEP: {exc}") from exc

    if payload.get("status") != "done":
        raise HTTPException(409, f"El trabajo todavía no está listo: {payload.get('status')}")

    data = payload.get("data") or {}
    files = data.get("files") or []
    outputs: list[dict[str, Any]] = []
    best_parse: dict[str, Any] | None = None

    # Los modelos de transcripción de MVSEP (Parakeet/Whisper) pueden
    # devolver TXT/SRT directamente dentro de data.transcription y dejar
    # data.files vacío. Consumimos primero esa salida inline.
    transcription = data.get("transcription")
    if isinstance(transcription, dict):
        inline_txt = transcription.get("txt")
        inline_srt = transcription.get("srt")

        if isinstance(inline_srt, str) and inline_srt.strip():
            parsed = parse_transcription(inline_srt.encode("utf-8"))
            if isinstance(inline_txt, str) and inline_txt.strip():
                parsed["raw_text"] = inline_txt.strip()
            parsed["mvsep_source"] = "data.transcription.srt"
            best_parse = parsed
            outputs.append({
                "name": "transcription.srt",
                "url": None,
                "meta": {"source": "data.transcription.srt"},
                "parsed": parsed,
            })
            write_event(
                "inline_transcription_parsed",
                job_hash=job_hash,
                message="Transcripción SRT inline de MVSEP interpretada.",
                details={
                    "segments": parsed.get("word_count", 0),
                    "format": parsed.get("format"),
                },
            )
        elif isinstance(inline_txt, str) and inline_txt.strip():
            parsed = parse_transcription(inline_txt.encode("utf-8"))
            parsed["mvsep_source"] = "data.transcription.txt"
            best_parse = parsed
            outputs.append({
                "name": "transcription.txt",
                "url": None,
                "meta": {"source": "data.transcription.txt"},
                "parsed": parsed,
            })
            write_event(
                "inline_transcription_parsed",
                job_hash=job_hash,
                message="Transcripción TXT inline de MVSEP interpretada.",
                details={"format": parsed.get("format")},
            )

    for idx, file_info in enumerate(files):
        if not isinstance(file_info, dict):
            continue
        url = file_info.get("url") or file_info.get("link")
        name = _file_name(file_info, idx)
        entry: dict[str, Any] = {"name": name, "url": url, "meta": file_info}

        if url:
            try:
                raw, content_type = await mvsep.download_output(url)
                guessed_type = mimetypes.guess_type(name)[0] or ""
                is_text_candidate = (
                    "text" in content_type.lower()
                    or "json" in content_type.lower()
                    or "text" in guessed_type.lower()
                    or Path(name).suffix.lower() in {".txt", ".json", ".srt", ".vtt", ".lrc", ".csv", ".tsv"}
                )
                if is_text_candidate or len(raw) <= 5_000_000:
                    parsed = parse_transcription(raw)
                    entry["parsed"] = parsed
                    if best_parse is None or parsed["word_count"] > best_parse["word_count"]:
                        best_parse = parsed
                write_event(
                    "result_file_downloaded",
                    job_hash=job_hash,
                    message=f"Resultado descargado: {name}",
                    details={"bytes": len(raw), "content_type": content_type},
                )
            except Exception as exc:
                entry["download_error"] = str(exc)
                write_event(
                    "result_file_error",
                    level="error",
                    job_hash=job_hash,
                    message=f"{name}: {exc}",
                )

        outputs.append(entry)

    write_event(
        "result_ready",
        job_hash=job_hash,
        message="Resultado listo para revisión.",
        details={
            "output_files": len(outputs),
            "word_count": (best_parse or {}).get("word_count", 0),
            "parsed_format": (best_parse or {}).get("format"),
        },
    )

    return {
        "ok": True,
        "hash": job_hash,
        "status": payload.get("status"),
        "algorithm": data.get("algorithm"),
        "algorithm_description": data.get("algorithm_description"),
        "outputs": outputs,
        "best_parse": best_parse,
        "raw_mvsep": payload,
    }



# ---------------------------------------------------------------------------
# ElevenLabs Scribe v2 · API externa · word timestamps
# ---------------------------------------------------------------------------

@app.get("/api/elevenlabs/health")
async def elevenlabs_health() -> dict[str, Any]:
    return {
        "ok": True,
        "configured": elevenlabs.is_configured(),
        "model": elevenlabs.model_id,
        "api_base": elevenlabs.base_url,
    }


@app.post("/api/elevenlabs/transcribe")
async def elevenlabs_transcribe(
    request: Request,
    audio: UploadFile = File(...),
    lyrics: str = Form(""),
    language_code: str = Form("spa"),
) -> dict[str, Any]:
    request_id = request.headers.get("x-debug-request-id")
    filename = audio.filename or "audio"
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Formato no permitido: {suffix or 'sin extensión'}")
    if not elevenlabs.is_configured():
        raise HTTPException(
            503,
            "ElevenLabs no está configurado en el LAB. Falta ELEVENLABS_API_KEY en el servidor.",
        )

    write_event(
        "elevenlabs_scribe_started",
        request_id=request_id,
        filename=filename,
        message="Enviando acapella a ElevenLabs Scribe v2.",
        details={
            "model": elevenlabs.model_id,
            "language_code": language_code,
            "master_words": len(lyrics.split()) if lyrics.strip() else 0,
            "mode": "compare_master" if lyrics.strip() else "scribe_only",
        },
    )

    started = time.monotonic()
    try:
        scribe = await elevenlabs.transcribe(audio, language_code=language_code)
        if lyrics.strip():
            mapped = map_scribe_to_master(lyrics, scribe.get("words") or [])
            source_mode = "compare_master"
        else:
            raw_words = scribe.get("words") or []
            preview_words = [
                {
                    "text": str(w.get("text") or "").strip(),
                    "start": float(w["start"]),
                    "end": float(w["end"]),
                    "confidence": 1.0,
                    "interpolated": False,
                    "qa_status": "green",
                    "qa_score": 100,
                    "scribe_text": str(w.get("text") or "").strip(),
                    "match_type": "scribe_raw",
                }
                for w in raw_words
                if str(w.get("text") or "").strip()
            ]
            count = len(preview_words)
            mapped = {
                "words": preview_words,
                "metrics": {
                    "master_word_count": count,
                    "scribe_word_count": count,
                    "mapped_words": count,
                    "exact_matches": count,
                    "fuzzy_matches": 0,
                    "grouped_matches": 0,
                    "interpolated_words": 0,
                    "ignored_scribe_words": 0,
                    "coverage_ratio": 1.0 if count else 0.0,
                },
            }
            source_mode = "scribe_only"
    except ElevenLabsScribeError as exc:
        write_event(
            "elevenlabs_scribe_error",
            level="error",
            request_id=request_id,
            filename=filename,
            message=str(exc),
            details={"elapsed_s": round(time.monotonic() - started, 3)},
        )
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:
        write_event(
            "elevenlabs_scribe_error",
            level="error",
            request_id=request_id,
            filename=filename,
            message=str(exc),
            details={"elapsed_s": round(time.monotonic() - started, 3)},
        )
        raise HTTPException(502, f"Falló ElevenLabs Scribe v2: {exc}") from exc

    elapsed = round(time.monotonic() - started, 3)
    metrics = mapped.get("metrics") or {}
    write_event(
        "elevenlabs_scribe_done",
        request_id=request_id,
        filename=filename,
        message="ElevenLabs Scribe v2 terminó y se comparó contra la letra maestra.",
        details={
            "elapsed_s": elapsed,
            "scribe_words": metrics.get("scribe_word_count"),
            "master_words": metrics.get("master_word_count"),
            "coverage_ratio": metrics.get("coverage_ratio"),
            "interpolated_words": metrics.get("interpolated_words"),
        },
    )

    return {
        "ok": True,
        "engine": "elevenlabs-scribe-v2",
        "elapsed_s": elapsed,
        "master_word_count": metrics.get("master_word_count"),
        "source_mode": source_mode,
        "scribe": scribe,
        "words": mapped.get("words") or [],
        "metrics": metrics,
    }


@app.post("/api/elevenlabs/forced-align")
async def elevenlabs_forced_align(
    request: Request,
    audio: UploadFile = File(...),
    text: str = Form(...),
) -> dict[str, Any]:
    request_id = request.headers.get("x-debug-request-id")
    filename = audio.filename or "audio"
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Formato no permitido: {suffix or 'sin extensión'}")
    if not text.strip():
        raise HTTPException(400, "Falta el texto del bloque.")
    if not elevenlabs.is_configured():
        raise HTTPException(503, "ElevenLabs no está configurado en el LAB.")

    write_event(
        "elevenlabs_forced_alignment_started",
        request_id=request_id,
        filename=filename,
        message="Alineando bloque con ElevenLabs Forced Alignment.",
        details={"words": len(text.split())},
    )
    started = time.monotonic()
    try:
        result = await elevenlabs.forced_align(audio, text=text)
    except ElevenLabsScribeError as exc:
        write_event(
            "elevenlabs_forced_alignment_error",
            level="error",
            request_id=request_id,
            filename=filename,
            message=str(exc),
            details={"elapsed_s": round(time.monotonic() - started, 3)},
        )
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:
        write_event(
            "elevenlabs_forced_alignment_error",
            level="error",
            request_id=request_id,
            filename=filename,
            message=str(exc),
            details={"elapsed_s": round(time.monotonic() - started, 3)},
        )
        raise HTTPException(502, f"Falló ElevenLabs Forced Alignment: {exc}") from exc

    elapsed = round(time.monotonic() - started, 3)
    write_event(
        "elevenlabs_forced_alignment_done",
        request_id=request_id,
        filename=filename,
        message="ElevenLabs Forced Alignment terminó.",
        details={
            "elapsed_s": elapsed,
            "word_count": result.get("word_count"),
            "loss": result.get("loss"),
        },
    )
    return {"ok": True, "elapsed_s": elapsed, **result}


# ---------------------------------------------------------------------------
# Motor local CPU · Whisper + CTC español
# ---------------------------------------------------------------------------

@app.get("/api/local/health")
async def local_cpu_health() -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(f"{CPU_WORKER_URL}/health")
        response.raise_for_status()
        payload = response.json()
        return {"ok": True, "worker": payload}
    except Exception as exc:
        raise HTTPException(502, f"Worker IA local no disponible: {exc}") from exc


@app.post("/api/local/align")
async def local_cpu_align(
    request: Request,
    audio: UploadFile = File(...),
    lyrics: str = Form(...),
) -> dict[str, Any]:
    request_id = request.headers.get("x-debug-request-id")
    filename = audio.filename or "audio"
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Formato no permitido: {suffix or 'sin extensión'}")
    if not lyrics.strip():
        raise HTTPException(400, "Falta la letra maestra exacta.")

    write_event(
        "local_cpu_align_started",
        request_id=request_id,
        filename=filename,
        message="Enviando audio + letra maestra al worker IA local.",
        details={"engine": "whisperx-style-local"},
    )

    started = time.monotonic()
    try:
        await audio.seek(0)
        files = {
            "audio": (
                filename,
                audio.file,
                audio.content_type or "application/octet-stream",
            )
        }
        data = {"lyrics": lyrics}
        timeout = httpx.Timeout(connect=30.0, read=1200.0, write=1200.0, pool=30.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{CPU_WORKER_URL}/align",
                files=files,
                data=data,
            )
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail")
            except Exception:
                detail = response.text[:1000]
            raise HTTPException(response.status_code, detail or "Falló el worker IA local.")

        payload = response.json()
        write_event(
            "local_cpu_align_done",
            request_id=request_id,
            filename=filename,
            message="Worker IA local terminó la sincronización.",
            details={
                "elapsed_s": round(time.monotonic() - started, 3),
                "word_count": payload.get("master_word_count"),
                "aligned_words": (payload.get("metrics") or {}).get("aligned_words"),
                "interpolated_words": (payload.get("metrics") or {}).get("interpolated_words"),
            },
        )
        return payload
    except HTTPException:
        raise
    except Exception as exc:
        write_event(
            "local_cpu_align_error",
            level="error",
            request_id=request_id,
            filename=filename,
            message=str(exc),
            details={"elapsed_s": round(time.monotonic() - started, 3)},
        )
        raise HTTPException(502, f"Falló el worker IA local: {exc}") from exc
