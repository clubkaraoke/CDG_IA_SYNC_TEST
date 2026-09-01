from __future__ import annotations

import json
import re
from typing import Any


TIME_ARROW_RE = re.compile(
    r"(?P<start>\d{1,2}:\d{2}(?::\d{2})?[,\.]\d{1,3})\s*-->\s*"
    r"(?P<end>\d{1,2}:\d{2}(?::\d{2})?[,\.]\d{1,3})"
)
NUMERIC_LINE_RE = re.compile(
    r"^\s*(?P<start>\d+(?:\.\d+)?)\s*[,;\t ]+\s*"
    r"(?P<end>\d+(?:\.\d+)?)\s*[,;\t ]+\s*(?P<word>.+?)\s*$"
)


def _to_seconds(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", ".")
    try:
        return float(text)
    except ValueError:
        pass
    parts = text.split(":")
    try:
        if len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    except ValueError:
        return None
    return None


def _find_words_json(node: Any, out: list[dict[str, Any]]) -> None:
    if isinstance(node, dict):
        token = node.get("word", node.get("text", node.get("token")))
        start = node.get("start", node.get("start_time", node.get("begin")))
        end = node.get("end", node.get("end_time", node.get("finish")))
        start_s = _to_seconds(start)
        end_s = _to_seconds(end)
        if token is not None and start_s is not None:
            out.append({
                "word": str(token).strip(),
                "start": start_s,
                "end": end_s,
                "source": "json",
            })
        for value in node.values():
            _find_words_json(value, out)
    elif isinstance(node, list):
        for item in node:
            _find_words_json(item, out)


def _parse_srt_or_vtt(text: str) -> list[dict[str, Any]]:
    lines = text.splitlines()
    result: list[dict[str, Any]] = []
    i = 0
    while i < len(lines):
        match = TIME_ARROW_RE.search(lines[i])
        if not match:
            i += 1
            continue
        start = _to_seconds(match.group("start"))
        end = _to_seconds(match.group("end"))
        i += 1
        content: list[str] = []
        while i < len(lines) and lines[i].strip() and not TIME_ARROW_RE.search(lines[i]):
            if not lines[i].strip().isdigit():
                content.append(lines[i].strip())
            i += 1
        phrase = " ".join(content).strip()
        if phrase and start is not None:
            result.append({
                "word": phrase,
                "start": start,
                "end": end,
                "source": "subtitle_segment",
            })
    return result


def _parse_numeric_lines(text: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = NUMERIC_LINE_RE.match(line)
        if not match:
            continue
        result.append({
            "word": match.group("word").strip(),
            "start": float(match.group("start")),
            "end": float(match.group("end")),
            "source": "numeric_line",
        })
    return result


def parse_transcription(raw: bytes) -> dict[str, Any]:
    """Parser deliberadamente tolerante.

    La primera prueba real nos dirá el formato exacto que entrega Parakeet en MVSEP.
    Hasta entonces conserva SIEMPRE el contenido crudo y prueba formatos comunes.
    """
    text = raw.decode("utf-8", errors="replace").strip()
    words: list[dict[str, Any]] = []
    parsed_format = "plain_text"
    json_data: Any = None

    try:
        json_data = json.loads(text)
        _find_words_json(json_data, words)
        if words:
            parsed_format = "json_word_timestamps"
    except json.JSONDecodeError:
        pass

    if not words:
        words = _parse_srt_or_vtt(text)
        if words:
            parsed_format = "subtitle_segments"

    if not words:
        words = _parse_numeric_lines(text)
        if words:
            parsed_format = "numeric_word_timestamps"

    # Completa end usando el inicio de la siguiente palabra cuando falte.
    for idx, item in enumerate(words):
        if item.get("end") is None and idx + 1 < len(words):
            item["end"] = words[idx + 1].get("start")

    clean_words = [w for w in words if w.get("word")]
    return {
        "format": parsed_format,
        "word_count": len(clean_words),
        "words": clean_words,
        "raw_text": text,
        "json": json_data,
    }
