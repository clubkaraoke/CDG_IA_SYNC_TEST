from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .mvsep import MVSEPClient, MVSEPError
from .parser import parse_transcription

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="CDG IA Sync Test", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

mvsep = MVSEPClient()

ALLOWED_EXTENSIONS = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg"}


@app.get("/")
async def home() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "mvsep_configured": bool(os.getenv("MVSEP_API_TOKEN", "").strip()),
        "model": "Parakeet v3",
        "sep_type": 64,
        "add_opt1": 0,
        "add_opt2": 1,
    }


@app.post("/api/transcribe")
async def transcribe(audio: UploadFile = File(...)) -> dict[str, Any]:
    suffix = Path(audio.filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Formato no permitido: {suffix or 'sin extensión'}")

    try:
        payload = await mvsep.create_parakeet_job(audio)
    except MVSEPError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"No se pudo crear el trabajo en MVSEP: {exc}") from exc

    return {
        "ok": True,
        "hash": payload["data"]["hash"],
        "mvsep": payload,
    }


@app.get("/api/status/{job_hash}")
async def status(job_hash: str) -> dict[str, Any]:
    try:
        payload = await mvsep.get_status(job_hash)
    except Exception as exc:
        raise HTTPException(502, f"No se pudo consultar MVSEP: {exc}") from exc
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
        raise HTTPException(502, f"No se pudo consultar MVSEP: {exc}") from exc

    if payload.get("status") != "done":
        raise HTTPException(409, f"El trabajo todavía no está listo: {payload.get('status')}")

    data = payload.get("data") or {}
    files = data.get("files") or []
    outputs: list[dict[str, Any]] = []
    best_parse: dict[str, Any] | None = None

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
                # Parakeet puede entregar un archivo textual con extensión poco obvia;
                # si es pequeño también intentamos parsearlo de forma segura.
                if is_text_candidate or len(raw) <= 5_000_000:
                    parsed = parse_transcription(raw)
                    entry["parsed"] = parsed
                    if best_parse is None or parsed["word_count"] > best_parse["word_count"]:
                        best_parse = parsed
            except Exception as exc:
                entry["download_error"] = str(exc)

        outputs.append(entry)

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
