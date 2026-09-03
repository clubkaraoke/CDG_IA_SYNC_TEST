from __future__ import annotations

import math
import os
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

import httpx
from fastapi import UploadFile


class ElevenLabsScribeError(RuntimeError):
    pass


@dataclass
class _Token:
    text: str
    norm: str


def _norm(text: str) -> str:
    raw = unicodedata.normalize("NFKD", str(text or ""))
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    return re.sub(r"[^A-Z0-9]+", "", raw.upper())


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _as_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


class ElevenLabsScribeClient:
    def __init__(self) -> None:
        self.base_url = os.getenv("ELEVENLABS_API_BASE", "https://api.elevenlabs.io").rstrip("/")
        self.model_id = os.getenv("ELEVENLABS_SCRIBE_MODEL", "scribe_v2")

    def api_key(self) -> str:
        return os.getenv("ELEVENLABS_API_KEY", "").strip()

    def is_configured(self) -> bool:
        return bool(self.api_key())

    async def transcribe(self, audio: UploadFile, *, language_code: str = "spa") -> dict[str, Any]:
        key = self.api_key()
        if not key:
            raise ElevenLabsScribeError(
                "ElevenLabs no está configurado. Falta ELEVENLABS_API_KEY en el servidor."
            )

        await audio.seek(0)
        files = {
            "file": (
                audio.filename or "audio",
                audio.file,
                audio.content_type or "application/octet-stream",
            )
        }
        data = {
            "model_id": self.model_id,
            "language_code": language_code,
            "tag_audio_events": "false",
            "diarize": "false",
            "timestamps_granularity": "word",
        }
        timeout = httpx.Timeout(connect=30.0, read=1200.0, write=1200.0, pool=30.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{self.base_url}/v1/speech-to-text",
                headers={"xi-api-key": key},
                files=files,
                data=data,
            )

        if response.status_code >= 400:
            detail = response.text[:1200]
            try:
                payload = response.json()
                detail = (
                    payload.get("detail")
                    or payload.get("message")
                    or payload.get("error")
                    or detail
                )
            except Exception:
                pass
            raise ElevenLabsScribeError(
                f"ElevenLabs Scribe respondió HTTP {response.status_code}: {detail}"
            )

        payload = response.json()
        raw_words = payload.get("words") or []
        words: list[dict[str, Any]] = []

        for item in raw_words:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "")
            item_type = str(item.get("type") or "word")
            if item_type not in {"word", ""}:
                continue
            if not text.strip():
                continue

            start = _as_float(item.get("start"))
            if start is None:
                start = _as_float(item.get("start_time"))
            end = _as_float(item.get("end"))
            if end is None:
                end = _as_float(item.get("end_time"))
            if start is None or end is None or end < start:
                continue

            words.append(
                {
                    "text": text,
                    "start": round(start, 6),
                    "end": round(end, 6),
                    "logprob": item.get("logprob"),
                    "speaker_id": item.get("speaker_id"),
                }
            )

        return {
            "engine": "elevenlabs-scribe-v2",
            "model_id": self.model_id,
            "language_code": payload.get("language_code"),
            "language_probability": payload.get("language_probability"),
            "text": str(payload.get("text") or "").strip(),
            "word_count": len(words),
            "words": words,
        }

    async def forced_align(self, audio: UploadFile, *, text: str) -> dict[str, Any]:
        """Alinea un texto AUTORITATIVO contra audio usando ElevenLabs Forced Alignment."""
        key = self.api_key()
        if not key:
            raise ElevenLabsScribeError(
                "ElevenLabs no está configurado. Falta ELEVENLABS_API_KEY en el servidor."
            )
        clean_text = str(text or "").strip()
        if not clean_text:
            raise ElevenLabsScribeError("Falta el texto que se debe alinear.")

        await audio.seek(0)
        files = {
            "file": (
                audio.filename or "audio",
                audio.file,
                audio.content_type or "application/octet-stream",
            )
        }
        data = {"text": clean_text}
        timeout = httpx.Timeout(connect=30.0, read=1200.0, write=1200.0, pool=30.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{self.base_url}/v1/forced-alignment",
                headers={"xi-api-key": key},
                files=files,
                data=data,
            )

        if response.status_code >= 400:
            detail = response.text[:1200]
            try:
                payload = response.json()
                detail = (
                    payload.get("detail")
                    or payload.get("message")
                    or payload.get("error")
                    or detail
                )
            except Exception:
                pass
            raise ElevenLabsScribeError(
                f"ElevenLabs Forced Alignment respondió HTTP {response.status_code}: {detail}"
            )

        payload = response.json()
        words = []
        for item in payload.get("words") or []:
            if not isinstance(item, dict):
                continue
            token = str(item.get("text") or "").strip()
            start = _as_float(item.get("start"))
            end = _as_float(item.get("end"))
            if not token or start is None or end is None or end < start:
                continue
            words.append({
                "text": token,
                "start": round(start, 6),
                "end": round(end, 6),
                "loss": _as_float(item.get("loss")),
            })
        return {
            "engine": "elevenlabs-forced-alignment",
            "text": clean_text,
            "word_count": len(words),
            "words": words,
            "loss": _as_float(payload.get("loss")),
        }


def _master_tokens(lyrics: str) -> list[_Token]:
    return [
        _Token(text=part, norm=_norm(part))
        for part in re.findall(r"\S+", lyrics.replace("\r", ""))
    ]


def _score_11(master: _Token, scribe: dict[str, Any]) -> tuple[float, float]:
    sim = _similarity(master.norm, _norm(str(scribe.get("text") or "")))
    score = -1.2 + (3.7 * sim)
    if sim == 1.0:
        score += 1.0
    return score, sim


def _score_21(master_a: _Token, master_b: _Token, scribe: dict[str, Any]) -> tuple[float, float]:
    target = master_a.norm + master_b.norm
    sim = _similarity(target, _norm(str(scribe.get("text") or "")))
    return -1.75 + (3.6 * sim), sim


def _score_12(master: _Token, scribe_a: dict[str, Any], scribe_b: dict[str, Any]) -> tuple[float, float]:
    source = _norm(str(scribe_a.get("text") or "")) + _norm(str(scribe_b.get("text") or ""))
    sim = _similarity(master.norm, source)
    return -1.75 + (3.6 * sim), sim


def map_scribe_to_master(lyrics: str, scribe_words: list[dict[str, Any]]) -> dict[str, Any]:
    master = _master_tokens(lyrics)
    n = len(master)
    m = len(scribe_words)
    if not n:
        raise ElevenLabsScribeError("Falta la letra maestra exacta.")
    if not m:
        raise ElevenLabsScribeError("Scribe no devolvió palabras con timestamps.")

    gap_master = -1.0
    gap_scribe = -0.85
    neg = -10**9

    dp = [[neg] * (m + 1) for _ in range(n + 1)]
    back: list[list[tuple[int, int, str, float] | None]] = [
        [None] * (m + 1) for _ in range(n + 1)
    ]
    dp[0][0] = 0.0

    for i in range(n + 1):
        for j in range(m + 1):
            base = dp[i][j]
            if base <= neg / 2:
                continue

            if i < n:
                candidate = base + gap_master
                if candidate > dp[i + 1][j]:
                    dp[i + 1][j] = candidate
                    back[i + 1][j] = (i, j, "master_gap", 0.0)

            if j < m:
                candidate = base + gap_scribe
                if candidate > dp[i][j + 1]:
                    dp[i][j + 1] = candidate
                    back[i][j + 1] = (i, j, "scribe_gap", 0.0)

            if i < n and j < m:
                score, sim = _score_11(master[i], scribe_words[j])
                candidate = base + score
                if candidate > dp[i + 1][j + 1]:
                    dp[i + 1][j + 1] = candidate
                    back[i + 1][j + 1] = (i, j, "11", sim)

            if i + 1 < n and j < m:
                score, sim = _score_21(master[i], master[i + 1], scribe_words[j])
                candidate = base + score
                if candidate > dp[i + 2][j + 1]:
                    dp[i + 2][j + 1] = candidate
                    back[i + 2][j + 1] = (i, j, "21", sim)

            if i < n and j + 1 < m:
                score, sim = _score_12(master[i], scribe_words[j], scribe_words[j + 1])
                candidate = base + score
                if candidate > dp[i + 1][j + 2]:
                    dp[i + 1][j + 2] = candidate
                    back[i + 1][j + 2] = (i, j, "12", sim)

    ops: list[tuple[int, int, str, float, int, int]] = []
    i, j = n, m
    while i or j:
        step = back[i][j]
        if step is None:
            if i:
                ops.append((i - 1, j, "master_gap", 0.0, i, j))
                i -= 1
                continue
            ops.append((i, j - 1, "scribe_gap", 0.0, i, j))
            j -= 1
            continue
        pi, pj, kind, sim = step
        ops.append((pi, pj, kind, sim, i, j))
        i, j = pi, pj
    ops.reverse()

    mapped: list[dict[str, Any] | None] = [None] * n
    exact = 0
    fuzzy = 0
    grouped = 0
    ignored_scribe = 0

    for mi, sj, kind, sim, ni, nj in ops:
        if kind == "scribe_gap":
            ignored_scribe += 1
            continue
        if kind == "master_gap":
            continue

        if kind == "11":
            sw = scribe_words[sj]
            if sim < 0.42:
                continue
            qa = "green" if sim >= 0.92 else "yellow"
            if sim >= 0.999:
                exact += 1
            else:
                fuzzy += 1
            mapped[mi] = {
                "text": master[mi].text,
                "start": float(sw["start"]),
                "end": float(sw["end"]),
                "confidence": round(sim, 3),
                "interpolated": False,
                "qa_status": qa,
                "qa_score": round(sim * 100),
                "scribe_text": sw.get("text"),
                "match_type": "exact" if sim >= 0.999 else "fuzzy",
            }
            continue

        if kind == "21":
            sw = scribe_words[sj]
            if sim < 0.55:
                continue
            grouped += 1
            start = float(sw["start"])
            end = float(sw["end"])
            span = max(0.04, end - start)
            len_a = max(1, len(master[mi].norm))
            len_b = max(1, len(master[mi + 1].norm))
            cut = start + span * (len_a / (len_a + len_b))
            cut = min(max(cut, start + 0.02), end - 0.02) if span >= 0.05 else start + span / 2
            for idx, a, b in ((mi, start, cut), (mi + 1, cut, end)):
                mapped[idx] = {
                    "text": master[idx].text,
                    "start": round(a, 6),
                    "end": round(max(a + 0.001, b), 6),
                    "confidence": round(sim, 3),
                    "interpolated": False,
                    "qa_status": "yellow",
                    "qa_score": round(sim * 100),
                    "scribe_text": sw.get("text"),
                    "match_type": "split_2_master_to_1_scribe",
                }
            continue

        if kind == "12":
            if sim < 0.55:
                continue
            grouped += 1
            a = scribe_words[sj]
            b = scribe_words[sj + 1]
            mapped[mi] = {
                "text": master[mi].text,
                "start": float(a["start"]),
                "end": float(b["end"]),
                "confidence": round(sim, 3),
                "interpolated": False,
                "qa_status": "yellow",
                "qa_score": round(sim * 100),
                "scribe_text": f'{a.get("text", "")} {b.get("text", "")}'.strip(),
                "match_type": "merge_1_master_to_2_scribe",
            }

    # Rellena solo los huecos que el matching no pudo resolver. Se marcan en rojo
    # para que el LAB nunca los confunda con una coincidencia real de Scribe.
    interpolated = 0
    for idx in range(n):
        if mapped[idx] is not None:
            continue

        prev_idx = next((k for k in range(idx - 1, -1, -1) if mapped[k] is not None), None)
        next_idx = next((k for k in range(idx + 1, n) if mapped[k] is not None), None)

        if prev_idx is not None and next_idx is not None:
            gap_indices = [k for k in range(prev_idx + 1, next_idx) if mapped[k] is None]
            pos = gap_indices.index(idx)
            left = float(mapped[prev_idx]["end"])
            right = float(mapped[next_idx]["start"])
            usable = max(0.03 * len(gap_indices), right - left)
            step = usable / max(1, len(gap_indices))
            start = left + step * pos
            end = min(right, start + max(0.03, step * 0.72))
            if end <= start:
                end = start + 0.03
        elif prev_idx is not None:
            start = float(mapped[prev_idx]["end"]) + 0.04 * (idx - prev_idx)
            end = start + 0.18
        elif next_idx is not None:
            end = max(0.03, float(mapped[next_idx]["start"]) - 0.04 * (next_idx - idx))
            start = max(0.0, end - 0.18)
        else:
            start = idx * 0.2
            end = start + 0.18

        mapped[idx] = {
            "text": master[idx].text,
            "start": round(start, 6),
            "end": round(end, 6),
            "confidence": 0.0,
            "interpolated": True,
            "qa_status": "red",
            "qa_score": 0,
            "scribe_text": None,
            "match_type": "interpolated",
        }
        interpolated += 1

    result_words = [item for item in mapped if item is not None]
    matched = n - interpolated
    return {
        "words": result_words,
        "metrics": {
            "master_word_count": n,
            "scribe_word_count": m,
            "mapped_words": matched,
            "exact_matches": exact,
            "fuzzy_matches": fuzzy,
            "grouped_matches": grouped,
            "interpolated_words": interpolated,
            "ignored_scribe_words": ignored_scribe,
            "coverage_ratio": round(matched / n, 4) if n else 0.0,
        },
    }
