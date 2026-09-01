from __future__ import annotations

import os
from typing import Any

import httpx


class MVSEPError(RuntimeError):
    pass


class MVSEPClient:
    """Cliente mínimo para la prueba Parakeet v3 de MVSEP."""

    def __init__(self) -> None:
        self.token = os.getenv("MVSEP_API_TOKEN", "").strip()
        self.base_url = os.getenv("MVSEP_API_BASE", "https://de.mvsep.com/api").rstrip("/")
        self.timeout = httpx.Timeout(connect=30.0, read=1200.0, write=1200.0, pool=30.0)

    def ensure_configured(self) -> None:
        if not self.token:
            raise MVSEPError("Falta MVSEP_API_TOKEN en el archivo .env")

    async def create_parakeet_job(self, upload_file: Any) -> dict[str, Any]:
        self.ensure_configured()
        await upload_file.seek(0)

        data = {
            "api_token": self.token,
            "sep_type": "64",      # Parakeet
            "add_opt1": "0",       # usar el archivo tal cual (ya es Voces)
            "add_opt2": "1",       # Parakeet v3
            "output_format": "0",  # no es relevante para la transcripción, se deja explícito
            "is_demo": "0",
        }
        files = {
            "audiofile": (
                upload_file.filename or "audio.wav",
                upload_file.file,
                upload_file.content_type or "application/octet-stream",
            )
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f"{self.base_url}/separation/create", data=data, files=files)

        try:
            payload = response.json()
        except Exception as exc:
            raise MVSEPError(f"MVSEP devolvió una respuesta no JSON ({response.status_code})") from exc

        if response.is_error:
            message = payload.get("data", {}).get("message") if isinstance(payload, dict) else None
            raise MVSEPError(message or f"Error HTTP {response.status_code} al crear el trabajo")
        if not payload.get("success"):
            raise MVSEPError(payload.get("data", {}).get("message", "MVSEP rechazó el trabajo"))
        return payload

    async def get_status(self, job_hash: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                f"{self.base_url}/separation/get",
                params={"hash": job_hash},
            )
        response.raise_for_status()
        return response.json()

    async def download_output(self, url: str) -> tuple[bytes, str]:
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            response = await client.get(url)
        response.raise_for_status()
        return response.content, response.headers.get("content-type", "")
