from __future__ import annotations

import mimetypes
import time
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

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="CDG IA Sync Test", version="0.3.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

mvsep = MVSEPClient()
qwen = QwenWorkerClient()
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
    started = time.monotonic()
    if is_transcribe:
        write_event(
            "browser_upload_started",
            request_id=request_id,
            message="El navegador inició el envío hacia OVH.",
            details={
                "content_length": request.headers.get("content-length"),
                "content_type": request.headers.get("content-type"),
            },
        )
    try:
        response = await call_next(request)
    except Exception as exc:
        if is_transcribe:
            write_event(
                "browser_upload_backend_error",
                level="error",
                request_id=request_id,
                message=str(exc),
                details={"elapsed_s": round(time.monotonic() - started, 3)},
            )
        raise
    if is_transcribe:
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
