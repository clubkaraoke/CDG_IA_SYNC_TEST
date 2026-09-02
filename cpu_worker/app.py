from __future__ import annotations

import gc
import json
import math
import os
import re
import subprocess
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import psutil
import torch
import torch.nn.functional as F
import torchaudio
import torchaudio.functional as AF
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from faster_whisper import WhisperModel

MODEL_ROOT = Path(os.getenv("MODEL_ROOT", "/models"))
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")
WHISPER_THREADS = int(os.getenv("WHISPER_THREADS", "4"))
LANGUAGE = os.getenv("LANGUAGE", "es")
ALIGN_BUNDLE_NAME = "VOXPOPULI_ASR_BASE_10K_ES"
MAX_AUDIO_SECONDS = int(os.getenv("MAX_AUDIO_SECONDS", "720"))

ANCHOR_CLUSTER_GAP_S = float(os.getenv("ANCHOR_CLUSTER_GAP_S", "3.2"))
LINE_WINDOW_PAD_S = float(os.getenv("LINE_WINDOW_PAD_S", "0.55"))
RMS_HOP_MS = int(os.getenv("RMS_HOP_MS", "10"))
RMS_FRAME_MS = int(os.getenv("RMS_FRAME_MS", "30"))
CTC_SILENCE_PENALTY = float(os.getenv("CTC_SILENCE_PENALTY", "4.5"))
CTC_AMBIGUOUS_PENALTY = float(os.getenv("CTC_AMBIGUOUS_PENALTY", "1.0"))

WHISPER_CACHE = MODEL_ROOT / "faster-whisper"
TORCH_CACHE = MODEL_ROOT / "torch"
WHISPER_CACHE.mkdir(parents=True, exist_ok=True)
TORCH_CACHE.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("TORCH_HOME", str(TORCH_CACHE))

app = FastAPI(title="CDG IA CPU Worker", version="0.2.0")


class ResourceSampler:
    def __init__(self, interval_s: float = 0.2) -> None:
        self.interval_s = interval_s
        self.process = psutil.Process(os.getpid())
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.peak_rss = 0
        self.peak_cpu = 0.0

    def _snapshot(self) -> None:
        procs = [self.process]
        try:
            procs.extend(self.process.children(recursive=True))
        except Exception:
            pass

        rss = 0
        cpu = 0.0
        for proc in procs:
            try:
                rss += int(proc.memory_info().rss)
                cpu += float(proc.cpu_percent(interval=None))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        self.peak_rss = max(self.peak_rss, rss)
        self.peak_cpu = max(self.peak_cpu, cpu)

    def _run(self) -> None:
        try:
            self.process.cpu_percent(interval=None)
        except Exception:
            pass
        while not self.stop_event.wait(self.interval_s):
            self._snapshot()
        self._snapshot()

    def start(self) -> None:
        self._snapshot()
        self.thread = threading.Thread(target=self._run, name="resource-sampler", daemon=True)
        self.thread.start()

    def stop(self) -> dict[str, Any]:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2.0)
        self._snapshot()
        return {
            "peak_rss_mb": round(self.peak_rss / 1024 / 1024, 1),
            "peak_rss_gb": round(self.peak_rss / 1024 / 1024 / 1024, 3),
            "peak_cpu_pct": round(self.peak_cpu, 1),
            "cpu_pct_scale_note": "100%=1 core; worker limit=4 cores",
        }


def normalize_match(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return "".join(c for c in text if c.isalnum())


def split_master(lyrics: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    lines: list[dict[str, Any]] = []
    words: list[dict[str, Any]] = []
    for source_index, raw in enumerate(lyrics.replace("\r", "").split("\n")):
        text = raw.strip()
        if not text:
            continue
        line_words = re.findall(r"\S+", text)
        line = {
            "index": len(lines),
            "source_index": source_index,
            "text": text,
            "word_indices": [],
        }
        for token in line_words:
            wi = len(words)
            line["word_indices"].append(wi)
            words.append({
                "index": wi,
                "line_index": line["index"],
                "text": token,
                "norm": normalize_match(token),
            })
        lines.append(line)
    return lines, words


def ffprobe_duration(path: str) -> float:
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", path,
    ]
    try:
        out = subprocess.check_output(cmd, text=True, timeout=30).strip()
        return float(out)
    except Exception as exc:
        raise RuntimeError(f"No pude leer duración del audio: {exc}") from exc


def decode_mono_16k(path: str) -> torch.Tensor:
    cmd = [
        "ffmpeg", "-v", "error", "-nostdin", "-i", path,
        "-f", "s16le", "-ac", "1", "-ar", "16000", "-",
    ]
    try:
        raw = subprocess.check_output(cmd, timeout=180)
    except Exception as exc:
        raise RuntimeError(f"FFmpeg no pudo decodificar el audio: {exc}") from exc
    import numpy as np
    arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return torch.from_numpy(arr).unsqueeze(0)


def token_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def monotonic_word_match(master: list[dict[str, Any]], asr_words: list[dict[str, Any]]) -> dict[int, int]:
    n, m = len(master), len(asr_words)
    if not n or not m:
        return {}

    gap = -0.72
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    bt = [[""] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dp[i][0] = i * gap
        bt[i][0] = "U"
    for j in range(1, m + 1):
        dp[0][j] = j * gap
        bt[0][j] = "L"

    for i in range(1, n + 1):
        a = master[i - 1]["norm"]
        for j in range(1, m + 1):
            b = asr_words[j - 1]["norm"]
            sim = token_similarity(a, b)
            subst = 2.4 * sim - 1.05
            if a == b:
                subst += 0.9
            diag = dp[i - 1][j - 1] + subst
            up = dp[i - 1][j] + gap
            left = dp[i][j - 1] + gap
            best = max(diag, up, left)
            dp[i][j] = best
            bt[i][j] = "D" if best == diag else ("U" if best == up else "L")

    mapping: dict[int, int] = {}
    i, j = n, m
    while i > 0 or j > 0:
        move = bt[i][j]
        if move == "D":
            sim = token_similarity(master[i - 1]["norm"], asr_words[j - 1]["norm"])
            if sim >= 0.52:
                mapping[i - 1] = j - 1
            i -= 1
            j -= 1
        elif move == "U":
            i -= 1
        elif move == "L":
            j -= 1
        else:
            break
    return mapping


def run_rough_asr(audio_path: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    started = time.monotonic()
    model = WhisperModel(
        WHISPER_MODEL,
        device="cpu",
        compute_type="int8",
        cpu_threads=WHISPER_THREADS,
        download_root=str(WHISPER_CACHE),
    )
    try:
        segments, info = model.transcribe(
            audio_path,
            language=LANGUAGE,
            task="transcribe",
            beam_size=3,
            word_timestamps=True,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        words: list[dict[str, Any]] = []
        for seg in segments:
            for word in seg.words or []:
                text = (word.word or "").strip()
                norm = normalize_match(text)
                if not norm:
                    continue
                words.append({
                    "text": text,
                    "norm": norm,
                    "start": float(word.start),
                    "end": float(word.end),
                    "probability": float(word.probability or 0.0),
                })
        return words, {
            "elapsed_s": round(time.monotonic() - started, 3),
            "language": getattr(info, "language", LANGUAGE),
            "language_probability": float(getattr(info, "language_probability", 0.0) or 0.0),
            "word_count": len(words),
        }
    finally:
        del model
        gc.collect()


def build_line_windows(
    lines: list[dict[str, Any]],
    master_words: list[dict[str, Any]],
    asr_words: list[dict[str, Any]],
    mapping: dict[int, int],
    duration: float,
) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    for line in lines:
        matched = [mapping[i] for i in line["word_indices"] if i in mapping]
        if matched:
            starts = [asr_words[j]["start"] for j in matched]
            ends = [asr_words[j]["end"] for j in matched]
            windows.append({
                "known": True,
                "start": max(0.0, min(starts) - 0.45),
                "end": min(duration, max(ends) + 0.45),
                "anchors": len(matched),
            })
        else:
            windows.append({"known": False, "start": None, "end": None, "anchors": 0})

    known_ids = [i for i, w in enumerate(windows) if w["known"]]
    if not known_ids:
        total_weight = sum(max(1, len(line["word_indices"])) for line in lines) or 1
        cursor = 0.0
        for i, line in enumerate(lines):
            weight = max(1, len(line["word_indices"]))
            next_cursor = duration if i == len(lines) - 1 else cursor + duration * weight / total_weight
            windows[i].update(start=cursor, end=next_cursor)
            cursor = next_cursor
        return windows

    first = known_ids[0]
    if first > 0:
        right = max(0.4, float(windows[first]["start"]))
        weights = [max(1, len(lines[i]["word_indices"])) for i in range(first)]
        total = sum(weights)
        cursor = 0.0
        for idx, weight in zip(range(first), weights):
            nxt = cursor + right * weight / total
            windows[idx].update(start=max(0.0, cursor - 0.2), end=min(duration, nxt + 0.2))
            cursor = nxt

    for a, b in zip(known_ids, known_ids[1:]):
        if b <= a + 1:
            continue
        left = float(windows[a]["end"])
        right = float(windows[b]["start"])
        if right <= left:
            right = min(duration, left + max(1.0, (b - a - 1) * 1.2))
        ids = list(range(a + 1, b))
        weights = [max(1, len(lines[i]["word_indices"])) for i in ids]
        total = sum(weights)
        cursor = left
        for idx, weight in zip(ids, weights):
            nxt = cursor + (right - left) * weight / total
            windows[idx].update(start=max(0.0, cursor - 0.25), end=min(duration, nxt + 0.25))
            cursor = nxt

    last = known_ids[-1]
    if last < len(lines) - 1:
        left = min(duration, float(windows[last]["end"]))
        ids = list(range(last + 1, len(lines)))
        weights = [max(1, len(lines[i]["word_indices"])) for i in ids]
        total = sum(weights)
        cursor = left
        for pos, (idx, weight) in enumerate(zip(ids, weights)):
            nxt = duration if pos == len(ids) - 1 else cursor + (duration - left) * weight / total
            windows[idx].update(start=max(0.0, cursor - 0.25), end=min(duration, nxt))
            cursor = nxt

    for i, w in enumerate(windows):
        s = float(w["start"] if w["start"] is not None else 0.0)
        e = float(w["end"] if w["end"] is not None else duration)
        if e - s < 0.55:
            mid = (s + e) / 2
            s = max(0.0, mid - 0.35)
            e = min(duration, mid + 0.35)
        windows[i]["start"] = s
        windows[i]["end"] = e
    return windows


@dataclass
class AlignmentResources:
    model: torch.nn.Module
    labels: tuple[str, ...]
    dictionary: dict[str, int]
    sample_rate: int


def load_alignment_resources() -> AlignmentResources:
    bundle = torchaudio.pipelines.VOXPOPULI_ASR_BASE_10K_ES
    model = bundle.get_model(dl_kwargs={"model_dir": str(TORCH_CACHE)})
    model.eval()
    labels = tuple(bundle.get_labels())
    dictionary = {label.lower(): i for i, label in enumerate(labels)}
    return AlignmentResources(model=model, labels=labels, dictionary=dictionary, sample_rate=bundle.sample_rate)


def target_tokens_for_line(
    line: dict[str, Any],
    master_words: list[dict[str, Any]],
    dictionary: dict[str, int],
) -> tuple[list[int], list[int | None], list[str]]:
    token_ids: list[int] = []
    token_word_map: list[int | None] = []
    warnings: list[str] = []

    for pos, wi in enumerate(line["word_indices"]):
        word = master_words[wi]["text"]
        usable = 0
        for char in word.lower():
            candidates = [char]
            decomposed = unicodedata.normalize("NFKD", char)
            base = "".join(c for c in decomposed if not unicodedata.combining(c))
            if base and base != char:
                candidates.append(base)
            chosen = None
            for candidate in candidates:
                if candidate in dictionary:
                    chosen = candidate
                    break
            if chosen is not None:
                token_ids.append(dictionary[chosen])
                token_word_map.append(wi)
                usable += 1
        if usable == 0:
            warnings.append(f'Palabra sin caracteres alineables: "{word}"')

        if pos < len(line["word_indices"]) - 1 and "|" in dictionary:
            token_ids.append(dictionary["|"])
            token_word_map.append(None)

    return token_ids, token_word_map, warnings


def align_line(
    resources: AlignmentResources,
    waveform: torch.Tensor,
    window: dict[str, Any],
    line: dict[str, Any],
    master_words: list[dict[str, Any]],
) -> tuple[dict[int, dict[str, Any]], list[str]]:
    s = float(window["start"])
    e = float(window["end"])
    f1 = max(0, int(s * resources.sample_rate))
    f2 = min(waveform.shape[-1], int(e * resources.sample_rate))
    chunk = waveform[:, f1:f2]

    token_ids, word_map, warnings = target_tokens_for_line(line, master_words, resources.dictionary)
    if not token_ids:
        return {}, warnings + ["Línea sin tokens alineables."]
    if chunk.shape[-1] < 800:
        return {}, warnings + ["Ventana de audio demasiado corta."]

    with torch.inference_mode():
        emissions, _ = resources.model(chunk)
        log_probs = torch.log_softmax(emissions, dim=-1)

    targets = torch.tensor([token_ids], dtype=torch.int32)
    try:
        alignment, scores = AF.forced_align(log_probs, targets, blank=0)
    except Exception as exc:
        return {}, warnings + [f"CTC no pudo alinear la línea: {exc}"]

    alignment = alignment[0]
    scores = scores[0].exp()
    spans = AF.merge_tokens(alignment, scores, blank=0)
    if len(spans) != len(token_ids):
        return {}, warnings + [f"CTC devolvió {len(spans)} spans para {len(token_ids)} tokens."]

    frame_seconds = (e - s) / max(1, log_probs.shape[1])
    per_word: dict[int, list[Any]] = {}
    for token_pos, span in enumerate(spans):
        wi = word_map[token_pos]
        if wi is None:
            continue
        per_word.setdefault(wi, []).append(span)

    result: dict[int, dict[str, Any]] = {}
    for wi, wspans in per_word.items():
        start = s + min(span.start for span in wspans) * frame_seconds
        end = s + max(span.end for span in wspans) * frame_seconds
        score_values = [float(span.score) for span in wspans]
        result[wi] = {
            "start": round(start, 3),
            "end": round(max(start + 0.04, end), 3),
            "confidence": round(sum(score_values) / max(1, len(score_values)), 3),
        }
    return result, warnings


def interpolate_missing(words: list[dict[str, Any]], duration: float) -> list[str]:
    warnings: list[str] = []
    n = len(words)
    known = [i for i, w in enumerate(words) if w.get("start") is not None and w.get("end") is not None]
    if not known:
        return ["No hubo palabras alineadas."]

    for i in range(n):
        if words[i].get("start") is not None:
            continue
        prev = max((j for j in known if j < i), default=None)
        nxt = min((j for j in known if j > i), default=None)
        if prev is not None and nxt is not None:
            gap_count = nxt - prev
            left = float(words[prev]["end"])
            right = float(words[nxt]["start"])
            frac0 = (i - prev - 1) / gap_count
            frac1 = (i - prev) / gap_count
            start = left + max(0.0, right - left) * frac0
            end = left + max(0.0, right - left) * frac1
        elif prev is not None:
            start = float(words[prev]["end"]) + 0.05 * (i - prev - 1)
            end = min(duration, start + 0.18)
        elif nxt is not None:
            end = max(0.0, float(words[nxt]["start"]) - 0.05 * (nxt - i - 1))
            start = max(0.0, end - 0.18)
        else:
            continue
        words[i]["start"] = round(start, 3)
        words[i]["end"] = round(max(start + 0.04, end), 3)
        words[i]["confidence"] = 0.0
        words[i]["interpolated"] = True
        warnings.append(f'Interpolada palabra #{i + 1}: "{words[i]["text"]}"')
    return warnings


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "engine": "whisperx-style-local",
        "device": "cpu",
        "compute_type": "int8",
        "whisper_model": WHISPER_MODEL,
        "align_model": ALIGN_BUNDLE_NAME,
        "threads": WHISPER_THREADS,
        "model_root": str(MODEL_ROOT),
        "torch": torch.__version__,
        "torchaudio": torchaudio.__version__,
    }


@app.post("/warmup")
async def warmup() -> dict[str, Any]:
    started = time.monotonic()
    phases: dict[str, float] = {}

    t0 = time.monotonic()
    wm = WhisperModel(
        WHISPER_MODEL,
        device="cpu",
        compute_type="int8",
        cpu_threads=WHISPER_THREADS,
        download_root=str(WHISPER_CACHE),
    )
    phases["faster_whisper_s"] = round(time.monotonic() - t0, 3)
    del wm
    gc.collect()

    t0 = time.monotonic()
    resources = load_alignment_resources()
    phases["spanish_aligner_s"] = round(time.monotonic() - t0, 3)
    label_preview = list(resources.labels[:12])
    del resources
    gc.collect()

    return {
        "ok": True,
        "elapsed_s": round(time.monotonic() - started, 3),
        "phases": phases,
        "labels_preview": label_preview,
    }


@app.post("/align")
async def align(
    audio: UploadFile = File(...),
    lyrics: str = Form(...),
) -> dict[str, Any]:
    lyrics = lyrics.strip()
    if not lyrics:
        raise HTTPException(400, "Falta la letra maestra.")

    lines, master_words = split_master(lyrics)
    if not master_words:
        raise HTTPException(400, "La letra maestra no contiene palabras.")

    suffix = Path(audio.filename or "audio.wav").suffix or ".wav"
    started = time.monotonic()
    warnings: list[str] = []

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fh:
        temp_path = fh.name
        while True:
            chunk = await audio.read(1024 * 1024)
            if not chunk:
                break
            fh.write(chunk)

    try:
        duration = ffprobe_duration(temp_path)
        if duration <= 0:
            raise HTTPException(400, "Audio sin duración válida.")
        if duration > MAX_AUDIO_SECONDS:
            raise HTTPException(400, f"Audio demasiado largo para LAB: {duration:.1f}s > {MAX_AUDIO_SECONDS}s.")

        asr_words, asr_meta = run_rough_asr(temp_path)
        mapping = monotonic_word_match(master_words, asr_words)
        match_ratio = len(mapping) / max(1, len(master_words))
        if match_ratio < 0.45:
            warnings.append(f"ASR encontró pocos anclajes con la letra maestra: {match_ratio:.1%}.")

        windows = build_line_windows(lines, master_words, asr_words, mapping, duration)

        waveform = decode_mono_16k(temp_path)
        resources = load_alignment_resources()
        try:
            output_words = [
                {
                    "text": w["text"],
                    "line": w["line_index"],
                    "start": None,
                    "end": None,
                    "confidence": None,
                    "interpolated": False,
                }
                for w in master_words
            ]

            for line, window in zip(lines, windows):
                line_result, line_warnings = align_line(resources, waveform, window, line, master_words)
                warnings.extend([f"Línea {line['index'] + 1}: {msg}" for msg in line_warnings])
                for wi, timing in line_result.items():
                    output_words[wi].update(timing)
        finally:
            del resources
            del waveform
            gc.collect()

        warnings.extend(interpolate_missing(output_words, duration))

        for i in range(len(output_words) - 1):
            cur = output_words[i]
            nxt = output_words[i + 1]
            if cur["end"] is not None and nxt["start"] is not None and cur["end"] > nxt["start"]:
                cur["end"] = round(max(float(cur["start"]) + 0.04, float(nxt["start"]) - 0.01), 3)

        aligned = sum(1 for w in output_words if not w.get("interpolated") and w.get("start") is not None)
        interpolated = sum(1 for w in output_words if w.get("interpolated"))

        return {
            "ok": True,
            "engine": "whisperx-style-local",
            "model": {
                "rough_asr": f"faster-whisper/{WHISPER_MODEL}",
                "aligner": ALIGN_BUNDLE_NAME,
                "device": "cpu",
                "compute_type": "int8",
            },
            "elapsed_s": round(time.monotonic() - started, 3),
            "audio_duration_s": round(duration, 3),
            "master_word_count": len(master_words),
            "rough_asr": {
                **asr_meta,
                "master_anchor_count": len(mapping),
                "master_anchor_ratio": round(match_ratio, 3),
            },
            "metrics": {
                "aligned_words": aligned,
                "interpolated_words": interpolated,
            },
            "words": output_words,
            "warnings": warnings[:200],
            "line_windows": windows,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Falló el worker local: {exc}") from exc
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        gc.collect()
