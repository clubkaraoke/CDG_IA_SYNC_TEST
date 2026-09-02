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


def _cluster_line_anchors(
    line: dict[str, Any],
    asr_words: list[dict[str, Any]],
    mapping: dict[int, int],
) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]]]:
    anchors: list[dict[str, Any]] = []
    for local_pos, wi in enumerate(line["word_indices"]):
        if wi not in mapping:
            continue
        j = mapping[wi]
        aw = asr_words[j]
        anchors.append({
            "master_word_index": wi,
            "local_pos": local_pos,
            "asr_word_index": j,
            "start": float(aw["start"]),
            "end": float(aw["end"]),
            "probability": float(aw.get("probability", 0.0) or 0.0),
            "text": aw.get("text", ""),
        })

    clusters: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for anchor in anchors:
        if current:
            gap = float(anchor["start"]) - float(current[-1]["end"])
            if gap > ANCHOR_CLUSTER_GAP_S:
                clusters.append(current)
                current = []
        current.append(anchor)
    if current:
        clusters.append(current)
    return anchors, clusters


def _choose_anchor_cluster(
    clusters: list[list[dict[str, Any]]],
    line_word_count: int,
) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]]]:
    if not clusters:
        return None, []

    scored: list[tuple[float, list[dict[str, Any]]]] = []
    diagnostics: list[dict[str, Any]] = []
    for cluster in clusters:
        first_pos = int(cluster[0]["local_pos"])
        last_pos = int(cluster[-1]["local_pos"])
        count = len(cluster)
        span = max(0.05, float(cluster[-1]["end"]) - float(cluster[0]["start"]))
        avg_prob = sum(float(a["probability"]) for a in cluster) / count
        covered_positions = max(1, last_pos - first_pos + 1)
        coverage = covered_positions / max(1, line_word_count)
        density = count / max(0.5, span)

        score = (
            count * 3.0
            + coverage * 1.8
            + avg_prob * 0.8
            + min(1.5, density * 0.25)
            + (0.8 if first_pos == 0 else 0.0)
            + (0.35 if first_pos <= 1 else 0.0)
            - span * 0.04
        )
        diagnostics.append({
            "count": count,
            "first_local_pos": first_pos,
            "last_local_pos": last_pos,
            "start": round(float(cluster[0]["start"]), 3),
            "end": round(float(cluster[-1]["end"]), 3),
            "span_s": round(span, 3),
            "avg_probability": round(avg_prob, 3),
            "score": round(score, 3),
        })
        scored.append((score, cluster))

    scored.sort(key=lambda item: (-item[0], float(item[1][0]["start"])))
    return scored[0][1], diagnostics


def build_line_windows(
    lines: list[dict[str, Any]],
    master_words: list[dict[str, Any]],
    asr_words: list[dict[str, Any]],
    mapping: dict[int, int],
    duration: float,
) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []

    for line in lines:
        all_anchors, clusters = _cluster_line_anchors(line, asr_words, mapping)
        chosen, cluster_diagnostics = _choose_anchor_cluster(clusters, len(line["word_indices"]))

        if chosen:
            first_pos = int(chosen[0]["local_pos"])
            last_pos = int(chosen[-1]["local_pos"])
            line_word_count = len(line["word_indices"])
            anchor_start = min(float(a["start"]) for a in chosen)
            anchor_end = max(float(a["end"]) for a in chosen)

            left_missing = max(0, first_pos)
            right_missing = max(0, line_word_count - 1 - last_pos)

            start = anchor_start - LINE_WINDOW_PAD_S - min(2.4, left_missing * 0.68)
            end = anchor_end + LINE_WINDOW_PAD_S + min(3.2, right_missing * 0.78)

            expected_min = min(10.0, max(1.6, line_word_count * 0.58 + 0.9))
            span = end - start
            if span < expected_min:
                missing = expected_min - span
                if left_missing > right_missing:
                    start -= missing * 0.7
                    end += missing * 0.3
                elif right_missing > left_missing:
                    start -= missing * 0.3
                    end += missing * 0.7
                else:
                    start -= missing * 0.5
                    end += missing * 0.5

            chosen_ids = {int(a["asr_word_index"]) for a in chosen}
            rejected = [a for a in all_anchors if int(a["asr_word_index"]) not in chosen_ids]
            windows.append({
                "known": True,
                "start": max(0.0, start),
                "end": min(duration, end),
                "anchors": len(chosen),
                "anchors_total": len(all_anchors),
                "anchors_rejected": len(rejected),
                "anchor_cluster_count": len(clusters),
                "anchor_start": round(anchor_start, 3),
                "anchor_end": round(anchor_end, 3),
                "anchor_local_first": first_pos,
                "anchor_local_last": last_pos,
                "rejected_anchor_times": [
                    round(float(a["start"]), 3) for a in rejected[:12]
                ],
                "anchor_clusters": cluster_diagnostics,
            })
        else:
            windows.append({
                "known": False,
                "start": None,
                "end": None,
                "anchors": 0,
                "anchors_total": 0,
                "anchors_rejected": 0,
                "anchor_cluster_count": 0,
                "anchor_start": None,
                "anchor_end": None,
                "rejected_anchor_times": [],
                "anchor_clusters": [],
            })

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

    # Prevent adjacent anchored lines from owning a large overlapping region.
    for a, b in zip(known_ids, known_ids[1:]):
        wa = windows[a]
        wb = windows[b]
        if b == a + 1 and float(wa["end"]) - float(wb["start"]) > 0.35:
            a_anchor_end = float(wa.get("anchor_end") or wa["end"])
            b_anchor_start = float(wb.get("anchor_start") or wb["start"])
            if b_anchor_start >= a_anchor_end:
                boundary = (a_anchor_end + b_anchor_start) / 2.0
                wa["end"] = min(float(wa["end"]), boundary + 0.18)
                wb["start"] = max(float(wb["start"]), boundary - 0.18)

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
        if e - s < 0.7:
            mid = (s + e) / 2
            s = max(0.0, mid - 0.45)
            e = min(duration, mid + 0.45)
        windows[i]["start"] = round(s, 3)
        windows[i]["end"] = round(e, 3)
        windows[i]["window_span_s"] = round(max(0.0, e - s), 3)
    return windows

def build_rms_activity(
    waveform: torch.Tensor,
    sample_rate: int,
) -> tuple[torch.Tensor, float, dict[str, Any]]:
    frame = max(160, int(sample_rate * RMS_FRAME_MS / 1000))
    hop = max(80, int(sample_rate * RMS_HOP_MS / 1000))
    x = waveform.float().unsqueeze(0)
    if x.shape[-1] < frame:
        states = torch.full((1,), 2, dtype=torch.uint8)
        return states, hop / sample_rate, {
            "frame_ms": RMS_FRAME_MS,
            "hop_ms": RMS_HOP_MS,
            "threshold_db": None,
            "active_ratio": 1.0,
            "ambiguous_ratio": 0.0,
            "silent_ratio": 0.0,
        }

    power = F.avg_pool1d(x.pow(2), kernel_size=frame, stride=hop).squeeze(0).squeeze(0)
    rms = torch.sqrt(torch.clamp(power, min=1e-12))
    db = 20.0 * torch.log10(torch.clamp(rms, min=1e-8))

    noise_p20 = float(torch.quantile(db, 0.20).item())
    signal_p90 = float(torch.quantile(db, 0.90).item())
    threshold_db = max(noise_p20 + 10.0, signal_p90 - 30.0)
    threshold_db = max(-75.0, min(-35.0, threshold_db))
    ambiguous_db = threshold_db - 8.0

    states = torch.zeros(db.shape[0], dtype=torch.uint8)
    states[db >= ambiguous_db] = 1
    states[db >= threshold_db] = 2

    # Bridge very short dropouts so consonants/breaths do not split one sung phrase.
    vals = states.tolist()
    max_gap = max(1, int(0.18 / (hop / sample_rate)))
    i = 0
    while i < len(vals):
        if vals[i] != 0:
            i += 1
            continue
        j = i
        while j < len(vals) and vals[j] == 0:
            j += 1
        if i > 0 and j < len(vals) and (j - i) <= max_gap:
            for k in range(i, j):
                vals[k] = 1
        i = j
    states = torch.tensor(vals, dtype=torch.uint8)

    total = max(1, states.numel())
    active = int((states == 2).sum().item())
    ambiguous = int((states == 1).sum().item())
    silent = total - active - ambiguous
    return states, hop / sample_rate, {
        "frame_ms": RMS_FRAME_MS,
        "hop_ms": RMS_HOP_MS,
        "noise_p20_db": round(noise_p20, 2),
        "signal_p90_db": round(signal_p90, 2),
        "threshold_db": round(threshold_db, 2),
        "ambiguous_threshold_db": round(ambiguous_db, 2),
        "active_ratio": round(active / total, 3),
        "ambiguous_ratio": round(ambiguous / total, 3),
        "silent_ratio": round(silent / total, 3),
    }


def vocal_support_for_range(
    start: float,
    end: float,
    states: torch.Tensor,
    hop_s: float,
) -> float:
    if states.numel() == 0 or hop_s <= 0:
        return 1.0
    i1 = max(0, min(states.numel() - 1, int(start / hop_s)))
    i2 = max(i1 + 1, min(states.numel(), int(math.ceil(max(start + 0.02, end) / hop_s))))
    seg = states[i1:i2].float()
    if seg.numel() == 0:
        return 1.0
    # 0=silence, 1=ambiguous, 2=active.
    return float((seg / 2.0).mean().item())


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
    vocal_states: torch.Tensor | None = None,
    vocal_hop_s: float | None = None,
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

    # PyTorch 2.8 marks tensors created inside inference_mode as inference tensors.
    # BASE v2 applies soft RMS penalties in-place afterwards, so make a normal
    # tensor copy first; otherwise PyTorch raises:
    # "Inplace update to inference tensor outside InferenceMode is not allowed."
    log_probs = log_probs.clone()

    if vocal_states is not None and vocal_hop_s and vocal_states.numel() > 0:
        frame_count = log_probs.shape[1]
        frame_times = s + (torch.arange(frame_count, dtype=torch.float32) + 0.5) * ((e - s) / max(1, frame_count))
        mask_idx = torch.clamp((frame_times / vocal_hop_s).long(), 0, vocal_states.numel() - 1)
        frame_states = vocal_states[mask_idx]
        silent = frame_states == 0
        ambiguous = frame_states == 1
        if silent.any():
            log_probs[0, silent, 1:] -= CTC_SILENCE_PENALTY
        if ambiguous.any():
            log_probs[0, ambiguous, 1:] -= CTC_AMBIGUOUS_PENALTY

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
        final_end = max(start + 0.04, end)
        result[wi] = {
            "start": round(start, 3),
            "end": round(final_end, 3),
            "confidence": round(sum(score_values) / max(1, len(score_values)), 3),
        }
        if vocal_states is not None and vocal_hop_s:
            result[wi]["vocal_support"] = round(
                vocal_support_for_range(start, final_end, vocal_states, vocal_hop_s),
                3,
            )
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


def build_line_quality(
    lines: list[dict[str, Any]],
    output_words: list[dict[str, Any]],
    windows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []

    for line, window in zip(lines, windows):
        ws = [output_words[i] for i in line["word_indices"]]
        timed = [w for w in ws if w.get("start") is not None and w.get("end") is not None]
        confs = [float(w.get("confidence") or 0.0) for w in timed]
        supports = [float(w.get("vocal_support") or 0.0) for w in timed]

        gaps: list[float] = []
        non_monotonic = False
        for a, b in zip(timed, timed[1:]):
            if float(b["start"]) < float(a["start"]):
                non_monotonic = True
            gaps.append(max(0.0, float(b["start"]) - float(a["end"])))

        max_gap = max(gaps, default=0.0)
        avg_conf = sum(confs) / max(1, len(confs))
        avg_support = sum(supports) / max(1, len(supports))
        low_support_ratio = (
            sum(1 for v in supports if v < 0.25) / max(1, len(supports))
        )
        interpolated = sum(1 for w in ws if w.get("interpolated"))
        anchors_used = int(window.get("anchors") or 0)
        anchors_rejected = int(window.get("anchors_rejected") or 0)

        diagnostics.append({
            "line": int(line["index"]),
            "text": line["text"],
            "confidence_avg": round(avg_conf, 3),
            "vocal_support_avg": round(avg_support, 3),
            "low_vocal_support_ratio": round(low_support_ratio, 3),
            "max_intra_gap_s": round(max_gap, 3),
            "non_monotonic": non_monotonic,
            "overlap_next_s": 0.0,
            "anchors_used": anchors_used,
            "anchors_total": int(window.get("anchors_total") or 0),
            "anchors_rejected": anchors_rejected,
            "anchor_clusters": int(window.get("anchor_cluster_count") or 0),
            "window_span_s": round(float(window.get("window_span_s") or 0.0), 3),
            "interpolated_words": interpolated,
            "status": "green",
            "score": 100,
            "reasons": [],
            "realign_recommended": False,
        })

    for i in range(len(diagnostics) - 1):
        cur_ids = lines[i]["word_indices"]
        nxt_ids = lines[i + 1]["word_indices"]
        cur_timed = [output_words[j] for j in cur_ids if output_words[j].get("end") is not None]
        nxt_timed = [output_words[j] for j in nxt_ids if output_words[j].get("start") is not None]
        if cur_timed and nxt_timed:
            overlap = max(0.0, float(cur_timed[-1]["end"]) - float(nxt_timed[0]["start"]))
            diagnostics[i]["overlap_next_s"] = round(overlap, 3)

    for diag in diagnostics:
        reasons: list[str] = []
        score = 100

        if diag["anchors_rejected"] > 0:
            reasons.append(f'{diag["anchors_rejected"]} anchor(s) repetido/sospechoso(s) descartado(s)')
            score -= min(15, 4 * diag["anchors_rejected"])
        if diag["max_intra_gap_s"] > 2.5:
            reasons.append(f'hueco interno extremo {diag["max_intra_gap_s"]:.2f}s')
            score -= 35
        elif diag["max_intra_gap_s"] > 1.2:
            reasons.append(f'hueco interno alto {diag["max_intra_gap_s"]:.2f}s')
            score -= 15
        if diag["non_monotonic"]:
            reasons.append("orden temporal no monótono")
            score -= 35
        if diag["overlap_next_s"] > 0.15:
            reasons.append(f'solape con línea siguiente {diag["overlap_next_s"]:.2f}s')
            score -= 30
        elif diag["overlap_next_s"] > 0.05:
            reasons.append(f'solape leve {diag["overlap_next_s"]:.2f}s')
            score -= 10
        if diag["low_vocal_support_ratio"] >= 0.35:
            reasons.append("muchas palabras sobre zona de baja actividad vocal")
            score -= 30
        elif diag["low_vocal_support_ratio"] >= 0.15:
            reasons.append("actividad vocal débil en parte de la línea")
            score -= 12
        if diag["confidence_avg"] < 0.18:
            reasons.append(f'confidence muy baja {diag["confidence_avg"]:.2f}')
            score -= 30
        elif diag["confidence_avg"] < 0.42:
            reasons.append(f'confidence moderada/baja {diag["confidence_avg"]:.2f}')
            score -= 12
        if diag["anchors_used"] == 0:
            reasons.append("sin anchors ASR directos")
            score -= 12
        if diag["interpolated_words"] > 0:
            reasons.append(f'{diag["interpolated_words"]} palabra(s) interpolada(s)')
            score -= 20

        hard_red = (
            diag["max_intra_gap_s"] > 2.5
            or diag["non_monotonic"]
            or diag["overlap_next_s"] > 0.15
            or diag["low_vocal_support_ratio"] >= 0.35
            or diag["confidence_avg"] < 0.18
        )
        yellow = (
            reasons
            or diag["confidence_avg"] < 0.55
            or diag["anchors_used"] < 2
        )

        status = "red" if hard_red else ("yellow" if yellow else "green")
        diag["status"] = status
        diag["score"] = max(0, min(100, score))
        diag["reasons"] = reasons
        diag["realign_recommended"] = status == "red"

        for wi in lines[diag["line"]]["word_indices"]:
            output_words[wi]["qa_status"] = status
            output_words[wi]["qa_score"] = diag["score"]

    counts = {
        "green": sum(1 for d in diagnostics if d["status"] == "green"),
        "yellow": sum(1 for d in diagnostics if d["status"] == "yellow"),
        "red": sum(1 for d in diagnostics if d["status"] == "red"),
    }
    summary = {
        "line_status_counts": counts,
        "suspicious_gaps": sum(1 for d in diagnostics if d["max_intra_gap_s"] > 1.2),
        "extreme_gaps": sum(1 for d in diagnostics if d["max_intra_gap_s"] > 2.5),
        "non_monotonic_lines": sum(1 for d in diagnostics if d["non_monotonic"]),
        "overlap_lines": sum(1 for d in diagnostics if d["overlap_next_s"] > 0.05),
        "realign_recommended_lines": sum(1 for d in diagnostics if d["realign_recommended"]),
    }
    return diagnostics, summary


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "engine": "whisperx-style-local-base-v2",
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
    phases: dict[str, float] = {}
    temp_path: str | None = None
    sampler = ResourceSampler()
    sampler.start()
    sampler_stopped = False
    resource_metrics: dict[str, Any] = {}

    try:
        t0 = time.monotonic()
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fh:
            temp_path = fh.name
            while True:
                chunk = await audio.read(1024 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
        phases["receive_audio_s"] = round(time.monotonic() - t0, 3)

        t0 = time.monotonic()
        duration = ffprobe_duration(temp_path)
        phases["probe_s"] = round(time.monotonic() - t0, 3)
        if duration <= 0:
            raise HTTPException(400, "Audio sin duración válida.")
        if duration > MAX_AUDIO_SECONDS:
            raise HTTPException(400, f"Audio demasiado largo para LAB: {duration:.1f}s > {MAX_AUDIO_SECONDS}s.")

        t0 = time.monotonic()
        asr_words, asr_meta = run_rough_asr(temp_path)
        phases["rough_asr_s"] = round(time.monotonic() - t0, 3)

        t0 = time.monotonic()
        mapping = monotonic_word_match(master_words, asr_words)
        match_ratio = len(mapping) / max(1, len(master_words))
        if match_ratio < 0.45:
            warnings.append(f"ASR encontró pocos anclajes con la letra maestra: {match_ratio:.1%}.")
        windows = build_line_windows(lines, master_words, asr_words, mapping, duration)
        phases["anchors_windows_s"] = round(time.monotonic() - t0, 3)

        t0 = time.monotonic()
        waveform = decode_mono_16k(temp_path)
        vocal_states, vocal_hop_s, rms_meta = build_rms_activity(waveform, 16000)
        phases["decode_rms_mask_s"] = round(time.monotonic() - t0, 3)

        t0 = time.monotonic()
        resources = load_alignment_resources()
        phases["load_aligner_s"] = round(time.monotonic() - t0, 3)

        output_words = [
            {
                "text": w["text"],
                "line": w["line_index"],
                "start": None,
                "end": None,
                "confidence": None,
                "vocal_support": None,
                "interpolated": False,
                "qa_status": None,
                "qa_score": None,
            }
            for w in master_words
        ]

        t0 = time.monotonic()
        try:
            for line, window in zip(lines, windows):
                line_result, line_warnings = align_line(
                    resources,
                    waveform,
                    window,
                    line,
                    master_words,
                    vocal_states=vocal_states,
                    vocal_hop_s=vocal_hop_s,
                )
                warnings.extend([f"Línea {line['index'] + 1}: {msg}" for msg in line_warnings])
                for wi, timing in line_result.items():
                    output_words[wi].update(timing)
        finally:
            del resources
            del waveform
            gc.collect()
        phases["ctc_align_s"] = round(time.monotonic() - t0, 3)

        t0 = time.monotonic()
        warnings.extend(interpolate_missing(output_words, duration))

        # Preserve problematic chronology for QA instead of hiding it.
        # Only trim a previous word when the next word genuinely starts later.
        for i in range(len(output_words) - 1):
            cur = output_words[i]
            nxt = output_words[i + 1]
            if (
                cur["end"] is not None
                and cur["start"] is not None
                and nxt["start"] is not None
                and float(cur["end"]) > float(nxt["start"])
                and float(nxt["start"]) >= float(cur["start"]) + 0.05
            ):
                cur["end"] = round(max(float(cur["start"]) + 0.04, float(nxt["start"]) - 0.01), 3)

        for w in output_words:
            if w.get("start") is not None and w.get("end") is not None and w.get("vocal_support") is None:
                w["vocal_support"] = round(
                    vocal_support_for_range(
                        float(w["start"]),
                        float(w["end"]),
                        vocal_states,
                        vocal_hop_s,
                    ),
                    3,
                )

        line_quality, quality_summary = build_line_quality(lines, output_words, windows)

        aligned = sum(
            1 for w in output_words
            if not w.get("interpolated") and w.get("start") is not None
        )
        interpolated = sum(1 for w in output_words if w.get("interpolated"))
        low_conf_020 = sum(
            1 for w in output_words
            if w.get("confidence") is not None and float(w["confidence"]) < 0.20
        )
        low_conf_010 = sum(
            1 for w in output_words
            if w.get("confidence") is not None and float(w["confidence"]) < 0.10
        )
        anchors_rejected = sum(int(w.get("anchors_rejected") or 0) for w in windows)
        split_anchor_lines = sum(1 for w in windows if int(w.get("anchor_cluster_count") or 0) > 1)
        phases["postprocess_qa_s"] = round(time.monotonic() - t0, 3)

        resource_metrics = sampler.stop()
        sampler_stopped = True

        return {
            "ok": True,
            "engine": "whisperx-style-local-base-v2",
            "model": {
                "rough_asr": f"faster-whisper/{WHISPER_MODEL}",
                "aligner": ALIGN_BUNDLE_NAME,
                "device": "cpu",
                "compute_type": "int8",
                "vocal_mask": "adaptive-rms-soft-ctc",
            },
            "elapsed_s": round(time.monotonic() - started, 3),
            "audio_duration_s": round(duration, 3),
            "master_word_count": len(master_words),
            "phases": phases,
            "rough_asr": {
                **asr_meta,
                "master_anchor_count": len(mapping),
                "master_anchor_ratio": round(match_ratio, 3),
                "anchors_rejected": anchors_rejected,
                "split_anchor_lines": split_anchor_lines,
            },
            "vocal_mask": rms_meta,
            "metrics": {
                "aligned_words": aligned,
                "interpolated_words": interpolated,
                "low_confidence_lt_020": low_conf_020,
                "low_confidence_lt_010": low_conf_010,
                "anchors_rejected": anchors_rejected,
                "split_anchor_lines": split_anchor_lines,
                **quality_summary,
                **resource_metrics,
            },
            "words": output_words,
            "line_quality": line_quality,
            "warnings": warnings[:300],
            "line_windows": windows,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Falló el worker local BASE v2: {exc}") from exc
    finally:
        if not sampler_stopped:
            try:
                sampler.stop()
            except Exception:
                pass
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        gc.collect()
