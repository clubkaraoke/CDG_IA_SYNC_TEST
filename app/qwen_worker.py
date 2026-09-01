from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import httpx


class QwenWorkerError(RuntimeError):
    pass


class QwenWorkerClient:
    def __init__(self) -> None:
        self.runtime_dir = Path(os.getenv("QWEN_RUNTIME_DIR", "/runtime"))
        self.endpoint_file = self.runtime_dir / "qwen_endpoint"
        self.token_file = self.runtime_dir / "qwen_worker_token"
        self.timeout = httpx.Timeout(connect=30.0, read=120.0, write=1200.0, pool=30.0)

    def _read(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""

    def endpoint(self) -> str:
        return os.getenv("QWEN_WORKER_URL", "").strip().rstrip("/") or self._read(self.endpoint_file).rstrip("/")

    def token(self) -> str:
        return os.getenv("QWEN_WORKER_TOKEN", "").strip() or self._read(self.token_file)

    def is_configured(self) -> bool:
        return bool(self.endpoint())

    def save_config(self, endpoint: str, token: str = "") -> None:
        endpoint = endpoint.strip().rstrip("/")
        if not endpoint.startswith(("http://", "https://")):
            raise QwenWorkerError("El endpoint Qwen debe empezar con http:// o https://")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.endpoint_file.write_text(endpoint + "\n", encoding="utf-8")
        self.token_file.write_text(token.strip() + "\n", encoding="utf-8")
        try:
            os.chmod(self.endpoint_file, 0o600)
            os.chmod(self.token_file, 0o600)
        except OSError:
            pass

    def headers(self) -> dict[str, str]:
        token = self.token()
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def health(self) -> dict[str, Any]:
        endpoint = self.endpoint()
        if not endpoint:
            raise QwenWorkerError("Qwen GPU Worker todavía no está configurado")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(f"{endpoint}/health", headers=self.headers())
        response.raise_for_status()
        return response.json()

    async def transcribe_align(
        self,
        upload_file: Any,
        *,
        lyrics: str = "",
        language: str = "Spanish",
        poll_interval: float = 5.0,
        max_wait_seconds: int = 3600,
    ) -> dict[str, Any]:
        endpoint = self.endpoint()
        if not endpoint:
            raise QwenWorkerError("Qwen GPU Worker todavía no está configurado")

        await upload_file.seek(0)
        files = {
            "audio": (
                upload_file.filename or "audio.wav",
                upload_file.file,
                upload_file.content_type or "application/octet-stream",
            )
        }
        data = {"lyrics": lyrics, "language": language}

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{endpoint}/jobs",
                files=files,
                data=data,
                headers=self.headers(),
            )
            try:
                created = response.json()
            except Exception as exc:
                raise QwenWorkerError(f"Qwen Worker devolvió respuesta no JSON ({response.status_code})") from exc
            if response.is_error:
                raise QwenWorkerError(created.get("detail") or f"Qwen Worker HTTP {response.status_code}")

            job_id = created.get("job_id")
            if not job_id:
                raise QwenWorkerError("Qwen Worker no devolvió job_id")

            waited = 0.0
            while waited <= max_wait_seconds:
                await asyncio.sleep(poll_interval)
                waited += poll_interval
                status_response = await client.get(
                    f"{endpoint}/jobs/{job_id}",
                    headers=self.headers(),
                )
                try:
                    status = status_response.json()
                except Exception as exc:
                    raise QwenWorkerError(
                        f"Estado Qwen no JSON ({status_response.status_code})"
                    ) from exc
                if status_response.is_error:
                    raise QwenWorkerError(status.get("detail") or f"Qwen status HTTP {status_response.status_code}")

                state = status.get("status")
                if state == "done":
                    result = status.get("result")
                    if not isinstance(result, dict):
                        raise QwenWorkerError("Qwen terminó sin resultado")
                    result["worker_job_id"] = job_id
                    return result
                if state == "error":
                    raise QwenWorkerError(status.get("error") or "Qwen Worker falló")

            raise QwenWorkerError("Timeout esperando resultado de Qwen GPU Worker")
