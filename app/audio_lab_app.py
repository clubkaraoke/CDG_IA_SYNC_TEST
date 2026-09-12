from __future__ import annotations

import base64
import gzip
from pathlib import Path
from typing import Any, Callable
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
ALLOWED_MVSEP_HOSTS = {
    "mvsep.com",
    "www.mvsep.com",
    "de.mvsep.com",
    "de2.mvsep.com",
    "hk.mvsep.com",
    "mirror.mvsep.com",
}

app = FastAPI(title="DJGABO Audio Lab Web", version="1.0.2")
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
        "version": "1.0.2",
        "browser_editor": True,
        "mvsep_configured": mvsep.is_configured(),
        "progress_ui": True,
        "karaoke": {
            "function": "Voz principal + Coros + Instrumental",
            "algorithm": "MVSep Karaoke (lead/back vocals)",
            "sep_type": 49,
            "model": "BS Roformer — MVSep Team",
            "add_opt1": 6,
            "add_opt2": 1,
            "required_stems": ["voice", "back", "instrumental"],
        },
        "crowd_removal": {
            "function": "Quitar público / aplausos",
            "algorithm": "MVSep Crowd removal (crowd, other)",
            "sep_type": 34,
            "model": "BS Roformer — Crowd Removal",
            "add_opt1": 2,
        },
        "output_format": "wav16",
    }


def _validate_audio(audio: UploadFile) -> None:
    filename = audio.filename or "audio.wav"
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Formato no permitido: {suffix or 'sin extensión'}")


@app.post("/api/mvsep/karaoke")
async def create_karaoke_job(
    audio: UploadFile = File(...),
    model: int = Form(6),  # se acepta por compatibilidad con el frontend, pero se ignora
) -> dict[str, Any]:
    _validate_audio(audio)
    try:
        payload = await mvsep.create_karaoke_job(audio)
    except MVSEPError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"No se pudo crear el trabajo en MVSEP: {exc}") from exc
    return {
        "ok": True,
        "hash": payload.get("data", {}).get("hash"),
        "mvsep": payload,
        "model": 6,
        "model_name": "BS Roformer — MVSep Team",
        "requested_model_ignored": model != 6,
        "expected_stems": ["voice", "back", "instrumental"],
    }


@app.post("/api/mvsep/crowd")
async def create_crowd_job(audio: UploadFile = File(...)) -> dict[str, Any]:
    _validate_audio(audio)
    try:
        payload = await mvsep.create_crowd_removal_job(audio)
    except MVSEPError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"No se pudo crear el trabajo Crowd Removal en MVSEP: {exc}") from exc
    return {
        "ok": True,
        "hash": payload.get("data", {}).get("hash"),
        "mvsep": payload,
        "model": 2,
        "model_name": "BS Roformer — Crowd Removal",
        "expected_stems": ["crowd", "other"],
    }


@app.get("/api/mvsep/status/{job_hash}")
async def separation_status(job_hash: str) -> dict[str, Any]:
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


def _output_files(data: dict[str, Any]) -> list[Any]:
    raw = data.get("files") or []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        return list(raw.values())
    return []


def _display_name(info: Any, idx: int) -> str:
    if isinstance(info, dict):
        for key in ("name", "filename", "title", "label"):
            value = info.get(key)
            if isinstance(value, str) and value and not value.startswith(("http://", "https://")):
                return value
        download = info.get("download")
        if isinstance(download, str) and download and not download.startswith(("http://", "https://")):
            return download
    if isinstance(info, str) and not info.startswith(("http://", "https://")):
        return info
    return f"stem_{idx + 1}.wav"


def _normalized_text(info: Any) -> str:
    return str(info).lower().replace("_", " ").replace("-", " ")


def _classify_karaoke(info: Any, idx: int) -> str:
    text = _normalized_text(info)
    if any(token in text for token in (
        "vocals back",
        "vocal back",
        "back vocals",
        "back vocal",
        "background vocals",
        "background vocal",
        "backing vocals",
        "backing vocal",
        "chorus",
        "choir",
        "coros",
        "coro",
    )):
        return "back"
    if any(token in text for token in (
        "instrumental",
        "instrum",
        "accompaniment",
        "music only",
        "karaoke",
    )):
        return "instrumental"
    if any(token in text for token in (
        "vocals lead",
        "vocal lead",
        "lead vocals",
        "lead vocal",
        "main vocals",
        "main vocal",
        "voice main",
    )):
        return "voice"
    if "vocals" in text or "vocal" in text or "voice" in text:
        return "voice"
    return ("voice", "back", "instrumental")[idx] if idx < 3 else "extra"


def _classify_crowd(info: Any, idx: int) -> str:
    text = _normalized_text(info)
    if any(token in text for token in ("crowd", "audience", "applause", "publico", "público", "aplaus")):
        return "crowd"
    if "other" in text or "music" in text or "no crowd" in text:
        return "other"
    return ("crowd", "other")[idx] if idx < 2 else "extra"


def _remote_url(info: Any) -> str:
    urls = _collect_http_strings(info)
    for url in urls:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme == "https" and (host in ALLOWED_MVSEP_HOSTS or host.endswith(".mvsep.com")):
            return url
    return ""


def _result_files(
    job_hash: str,
    raw_files: list[Any],
    classifier: Callable[[Any, int], str],
) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for idx, info in enumerate(raw_files):
        if not _remote_url(info):
            continue
        files.append({
            "index": idx,
            "name": _display_name(info, idx),
            "role": classifier(info, idx),
            "url": f"api/mvsep/file/{job_hash}/{idx}",
        })
    return files


def _diagnostic_names(raw_files: list[Any], classifier: Callable[[Any, int], str]) -> list[str]:
    return [f"{_display_name(info, idx)} => {classifier(info, idx)}" for idx, info in enumerate(raw_files)]


@app.get("/api/mvsep/result/{job_hash}")
async def karaoke_result(job_hash: str) -> dict[str, Any]:
    try:
        payload = await mvsep.get_status(job_hash)
    except Exception as exc:
        raise HTTPException(502, f"No se pudo consultar MVSEP: {exc}") from exc
    if payload.get("status") != "done":
        raise HTTPException(409, f"El trabajo todavía no está listo: {payload.get('status')}")

    data = payload.get("data") or {}
    raw_files = _output_files(data)
    files = _result_files(job_hash, raw_files, _classify_karaoke)
    roles = {item["role"] for item in files}
    required = {"voice", "back", "instrumental"}
    missing = sorted(required - roles)

    if missing:
        returned = _diagnostic_names(raw_files, _classify_karaoke)
        raise HTTPException(
            502,
            detail=(
                "MVSEP terminó, pero no devolvió los 3 stems obligatorios. "
                f"Faltan: {', '.join(missing)}. "
                f"Devueltos ({len(raw_files)}): {returned or ['ninguno']}. "
                "No se marca el trabajo como correcto."
            ),
        )

    return {
        "ok": True,
        "hash": job_hash,
        "algorithm": data.get("algorithm"),
        "algorithm_description": data.get("algorithm_description"),
        "model_name": "BS Roformer — MVSep Team",
        "files": files,
        "file_count": len(files),
        "roles": sorted(roles),
        "validated_three_stems": True,
    }


@app.get("/api/mvsep/crowd/result/{job_hash}")
async def crowd_result(job_hash: str) -> dict[str, Any]:
    try:
        payload = await mvsep.get_status(job_hash)
    except Exception as exc:
        raise HTTPException(502, f"No se pudo consultar MVSEP: {exc}") from exc
    if payload.get("status") != "done":
        raise HTTPException(409, f"El trabajo todavía no está listo: {payload.get('status')}")

    data = payload.get("data") or {}
    raw_files = _output_files(data)
    files = _result_files(job_hash, raw_files, _classify_crowd)
    roles = {item["role"] for item in files}
    missing = sorted({"crowd", "other"} - roles)
    if missing:
        returned = _diagnostic_names(raw_files, _classify_crowd)
        raise HTTPException(
            502,
            detail=f"Crowd Removal terminó incompleto. Faltan: {', '.join(missing)}. Devueltos: {returned or ['ninguno']}",
        )
    return {
        "ok": True,
        "hash": job_hash,
        "algorithm": data.get("algorithm"),
        "algorithm_description": data.get("algorithm_description"),
        "model_name": "BS Roformer — Crowd Removal",
        "files": files,
        "file_count": len(files),
        "roles": sorted(roles),
    }


@app.get("/api/mvsep/file/{job_hash}/{index}")
async def separation_file(job_hash: str, index: int) -> Response:
    try:
        payload = await mvsep.get_status(job_hash)
    except Exception as exc:
        raise HTTPException(502, f"No se pudo consultar MVSEP: {exc}") from exc
    if payload.get("status") != "done":
        raise HTTPException(409, "El trabajo todavía no está listo")

    raw_files = _output_files(payload.get("data") or {})
    if index < 0 or index >= len(raw_files):
        raise HTTPException(404, "Stem no encontrado")
    info = raw_files[index]
    url = _remote_url(info)
    if not url:
        raise HTTPException(502, "MVSEP no devolvió una URL de descarga válida para este stem")
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, read=1200.0, write=1200.0, pool=30.0),
            follow_redirects=True,
        ) as client:
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
