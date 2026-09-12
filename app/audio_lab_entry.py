from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse

from .audio_lab_app import app

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
LAYOUT_FIX = STATIC_DIR / "mvsep_layout_fix.html"


@app.middleware("http")
async def inject_audio_lab_layout_fix(request: Request, call_next):
    response = await call_next(request)
    if request.url.path != "/":
        return response
    content_type = response.headers.get("content-type", "")
    if "text/html" not in content_type.lower():
        return response

    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    body = b"".join(chunks)
    try:
        html = body.decode("utf-8")
        if "djgabo-layout-fix-patch" not in html:
            patch = LAYOUT_FIX.read_text(encoding="utf-8")
            if "</body>" in html:
                html = html.replace("</body>", patch + "\n</body>", 1)
            else:
                html += patch
        headers = dict(response.headers)
        headers.pop("content-length", None)
        return HTMLResponse(
            content=html,
            status_code=response.status_code,
            headers=headers,
        )
    except Exception:
        return HTMLResponse(
            content=body,
            status_code=response.status_code,
            headers={k: v for k, v in response.headers.items() if k.lower() != "content-length"},
        )
