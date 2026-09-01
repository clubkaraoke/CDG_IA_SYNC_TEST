from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx


class MVSEPError(RuntimeError):
    pass


class MVSEPClient:
    """Cliente mínimo para la prueba Parakeet v3 de MVSEP."""

    def __init__(self) -> None:
        self.base_url = os.getenv("MVSEP_API_BASE", "https://mvsep.com/api").rstrip("/")
        self.token_file = Path(os.getenv("MVSEP_TOKEN_FILE", "/runtime/mvsep_token"))
        self.timeout = httpx.Timeout(connect=30.0, read=1200.0, write=1200.0, pool=30.0)

    def get_token(self) -> str:
        env_token = os.getenv("MVSEP_API_TOKEN", "").strip()
        if env_token:
            return env_token
        try:
            return self.token_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""

    def is_configured(self) -> bool:
        return bool(self.get_token())

    def save_token(self, token: str) -> None:
        token = token.strip()
        if len(token) < 8:
            raise MVSEPError("El token parece demasiado corto.")
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(token + "\n", encoding="utf-8")
        try:
            os.chmod(self.token_file, 0o600)
        except OSError:
            pass

    def ensure_configured(self) -> str:
        token = self.get_token()
        if not token:
            raise MVSEPError("Falta configurar el API token de MVSEP.")
        return token

    async def create_parakeet_job(self, upload_file: Any) -> dict[str, Any]:
        token = self.ensure_configured()
        await upload_file.seek(0)

        data = {
            "api_token": token,
            "sep_type": "64",
            "add_opt1": "0",
            "add_opt2": "1",
            "output_format": "0",
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

    async def get_queue_info(self) -> dict[str, Any]:
        token = self.ensure_configured()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                f"{self.base_url}/app/queue",
                params={"api_token": token},
            )
        response.raise_for_status()
        return response.json()

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
