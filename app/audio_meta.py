from __future__ import annotations

import html
import tempfile
from pathlib import Path
from typing import Any

from mutagen import File as MutagenFile


LYRIC_KEYS = (
    "lyrics",
    "unsyncedlyrics",
    "©lyr",
    "uslt",
    "lyrics-eng",
)


def _flatten(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "\n".join(_flatten(v) for v in value if v is not None).strip()
    text = getattr(value, "text", None)
    if text is not None:
        return _flatten(text)
    return str(value).strip()


async def extract_embedded_lyrics(upload_file: Any) -> dict[str, Any]:
    suffix = Path(upload_file.filename or "audio.bin").suffix or ".bin"
    await upload_file.seek(0)
    data = await upload_file.read()
    await upload_file.seek(0)

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
        tmp.write(data)
        tmp.flush()
        try:
            audio = MutagenFile(tmp.name, easy=False)
        except Exception:
            return {"found": False, "lyrics": "", "source": None}

        if audio is None or getattr(audio, "tags", None) is None:
            return {"found": False, "lyrics": "", "source": None}

        tags = audio.tags

        for key in list(tags.keys()):
            lower = str(key).lower()
            if any(candidate in lower for candidate in LYRIC_KEYS):
                raw = _flatten(tags.get(key))
                if raw:
                    return {
                        "found": True,
                        "lyrics": html.unescape(raw).replace("\r\n", "\n").strip(),
                        "source": str(key),
                    }

        # FLAC/Vorbis suele guardar exactamente "lyrics".
        try:
            raw = _flatten(tags.get("lyrics"))
            if raw:
                return {
                    "found": True,
                    "lyrics": html.unescape(raw).replace("\r\n", "\n").strip(),
                    "source": "lyrics",
                }
        except Exception:
            pass

    return {"found": False, "lyrics": "", "source": None}
