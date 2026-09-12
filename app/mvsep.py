from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx


class MVSEPError(RuntimeError):
    pass


class MVSEPClient:
    """Cliente MVSEP usado por los laboratorios DJGABO."""

    KARAOKE_MODEL_MVSEP_TEAM = 6
    CROWD_MODEL_BS_ROFORMER = 2

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

    async def _create_job(self, upload_file: Any, data: dict[str, str]) -> dict[str, Any]:
        token = self.ensure_configured()
        await upload_file.seek(0)
        body = {"api_token": token, **data}
        files = {
            "audiofile": (
                upload_file.filename or "audio.wav",
                upload_file.file,
                upload_file.content_type or "application/octet-stream",
            )
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f"{self.base_url}/separation/create", data=body, files=files)
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

    async def create_parakeet_job(self, upload_file: Any) -> dict[str, Any]:
        return await self._create_job(upload_file, {
            "sep_type": "64",
            "add_opt1": "0",
            "add_opt2": "1",
            "output_format": "0",
            "is_demo": "0",
        })

    async def create_karaoke_job(self, upload_file: Any) -> dict[str, Any]:
        """Función 1: Voz principal + Coros + Instrumental."""
        return await self._create_job(upload_file, {
            "sep_type": "49",
            "add_opt1": str(self.KARAOKE_MODEL_MVSEP_TEAM),
            "add_opt2": "1",
            "output_format": "1",
            "is_demo": "0",
        })

    async def create_crowd_removal_job(self, upload_file: Any) -> dict[str, Any]:
        """Función 2: quitar público / aplausos."""
        return await self._create_job(upload_file, {
            "sep_type": "34",
            "add_opt1": str(self.CROWD_MODEL_BS_ROFORMER),
            "output_format": "1",
            "is_demo": "0",
        })

    async def get_queue_info(self) -> dict[str, Any]:
        token = self.ensure_configured()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                f"{self.base_url}/app/queue",
                params={"api_token": token},
            )
        response.raise_for_status()
        return response.json()

    async def get_history(self, start: int = 0, limit: int = 10) -> dict[str, Any]:
        """Historial real de separaciones de la cuenta MVSEP.

        La API admite start >= 0 y limit <= 20. El token nunca sale al navegador.
        """
        token = self.ensure_configured()
        start = max(0, int(start))
        limit = max(1, min(20, int(limit)))
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                f"{self.base_url}/app/separation_history",
                params={"api_token": token, "start": start, "limit": limit},
            )
        try:
            payload = response.json()
        except Exception as exc:
            raise MVSEPError(f"MVSEP devolvió historial no JSON ({response.status_code})") from exc
        if response.is_error:
            raise MVSEPError(f"MVSEP historial respondió HTTP {response.status_code}")
        if not payload.get("success"):
            raise MVSEPError("MVSEP rechazó la consulta del historial")
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
