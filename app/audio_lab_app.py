from __future__ import annotations

import base64
import gzip
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response

from .mvsep import MVSEPClient, MVSEPError

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
AUDIO_LAB_BUNDLE = STATIC_DIR / "audio_lab.html.gz.b64"
AUDIO_LAB_PROGRESS_PATCH = STATIC_DIR / "mvsep_progress_patch.html"
ALLOWED_EXTENSIONS = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus"}
ALLOWED_MVSEP_HOSTS = {"mvsep.com", "www.mvsep.com", "de.mvsep.com", "de2.mvsep.com", "hk.mvsep.com", "mirror.mvsep.com"}

app = FastAPI(title="DJGABO Audio Lab Web", version="1.0.1")
mvsep = MVSEPClient()


def _audio_lab_html() -> str:
    try:
        packed = base64.b64decode(AUDIO_LAB_BUNDLE.read_text(encoding="ascii").strip())
        html = gzip.decompress(packed).decode("utf-8")
        patch = AUDIO_LAB_PROGRESS_PATCH.read_text(encoding="utf-8")
        if "djgabo-mvsep-progress-patch" not in html:
            if "</body>" in html:
                html = html.replace("</body>", patch + "\n</body>", 1)
            else:
                html += patch
        return html
    except Exception as exc:
        raise RuntimeError(f"No se pudo cargar el frontend Audio Lab: {exc}") from exc


@app.get("/", response_class=HTMLResponse)
async def home() -> HTMLResponse:
    return HTMLResponse(_audio_lab_html(), headers={"Cache-Control": "no-cache"})


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "DJGABO_AUDIO_LAB_WEB",
        "version": "1.0.1",
        "browser_editor": True,
        "mvsep_configured": mvsep.is_configured(),
        "mvsep_algorithm": "MVSep Karaoke (lead/back vocals)",
        "sep_type": 49,
        "output_format": "wav16",
        "progress_ui": True,
    }


@app.post("/api/mvsep/karaoke")
async def create_karaoke_job(
    audio: UploadFile = File(...),
    model: int = Form(6),
) -> dict[str, Any]:
    filename = audio.filename or "audio.wav"
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Formato no permitido: {suffix or 'sin extensión'}")
    if model not in range(0, 8):
        model = 6
    try:
        payload = await mvsep.create_karaoke_job(audio, model=model)
    except MVSEPError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"No se pudo crear el trabajo en MVSEP: {exc}") from exc
    return {
        "ok": True,
        "hash": payload.get("data", {}).get("hash"),
        "mvsep": payload,
        "model": model,
    }


@app.get("/api/mvsep/status/{job_hash}")
async def karaoke_status(job_hash: str) -> dict[str, Any]:
    try:
        return await mvsep.get_status(job_hash)
    except Exception as exc:
        raise HTTPException(502, f"No se pudo consultar MVSEP: {exc}") from exc


def _collect_http_strings(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, str):
        if value.startswith(("http://", "https://")):
            out.append(value)
    elif isinstance(value, dict):
        preferred = ["url", "download_url", "download", "link", "href", "file"]
        for key in preferred:
            if key in value:
                out.extend(_collect_http_strings(value[key]))
        for key, item in value.items():
            if key not in preferred:
                out.extend(_collect_http_strings(item))
    elif isinstance(value, list):
        for item in value:
            out.extend(_collect_http_strings(item))
    return out


def _display_name(info: Any, idx: int) -> str:
    if isinstance(info, dict):
        for key in ("name", "filename", "title", "label", "download"):
            value = info.get(key)
            if isinstance(value, str) and value and not value.startswith(("http://", "https://")):
                return value
    return f"stem_{idx + 1}.wav"


def _classify(info: Any, idx: int) -> str:
    text = str(info).lower()
    if any(word in text for word in ("back vocal", "back_vocal", "back-vocal", "background", "backing", "bv", "chorus", "choir", "coro")):
        return "back"
    if any(word in text for word in ("instrumental", "instrum", "music", "accompaniment", "karaoke", "other")):
        return "instrumental"
    if any(word in text for word in ("lead vocal", "lead_vocal", "lead-vocal", "main vocal", "vocals", "vocal")):
        return "voice"
    return ("voice", "back", "instrumental")[idx] if idx < 3 else "extra"


def _remote_url(info: Any) -> str:
    urls = _collect_http_strings(info)
    for url in urls:
        host = (urlparse(url).hostname or "").lower()
        if host in ALLOWED_MVSEP_HOSTS or host.endswith(".mvsep.com"):
            return url
    return ""


@app.get("/api/mvsep/result/{job_hash}")
async def karaoke_result(job_hash: str) -> dict[str, Any]:
    try:
        payload = await mvsep.get_status(job_hash)
    except Exception as exc:
        raise HTTPException(502, f"No se pudo consultar MVSEP: {exc}") from exc
    if payload.get("status") != "done":
        raise HTTPException(409, f"El trabajo todavía no está listo: {payload.get('status')}")
    data = payload.get("data") or {}
    raw_files = data.get("files") or []
    files: list[dict[str, Any]] = []
    for idx, info in enumerate(raw_files):
        if not _remote_url(info):
            continue
        files.append({
            "index": idx,
            "name": _display_name(info, idx),
            "role": _classify(info, idx),
            "url": f"api/mvsep/file/{job_hash}/{idx}",
        })
    return {
        "ok": True,
        "hash": job_hash,
        "algorithm": data.get("algorithm"),
        "algorithm_description": data.get("algorithm_description"),
        "files": files,
        "file_count": len(files),
    }


@app.get("/api/mvsep/file/{job_hash}/{index}")
async def karaoke_file(job_hash: str, index: int) -> Response:
    try:
        payload = await mvsep.get_status(job_hash)
    except Exception as exc:
        raise HTTPException(502, f"No se pudo consultar MVSEP: {exc}") from exc
    if payload.get("status") != "done":
        raise HTTPException(409, "El trabajo todavía no está listo")
    raw_files = (payload.get("data") or {}).get("files") or []
    if index < 0 or index >= len(raw_files):
        raise HTTPException(404, "Stem no encontrado")
    info = raw_files[index]
    url = _remote_url(info)
    if not url:
        raise HTTPException(502, "MVSEP no devolvió una URL de descarga válida para este stem")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=1200.0, write=1200.0, pool=30.0), follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
    except Exception as exc:
        raise HTTPException(502, f"No se pudo descargar el stem desde MVSEP: {exc}") from exc
    name = _display_name(info, index)
    headers = {
        "Content-Disposition": f'inline; filename="{name.replace(chr(34), "_")}"',
        "Cache-Control": "private, max-age=300",
    }
    return Response(content=r.content, media_type=r.headers.get("content-type", "audio/wav"), headers=headers)
