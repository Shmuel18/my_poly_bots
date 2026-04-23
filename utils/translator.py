"""Hebrew translation cache powered by the Gemini REST API.

Each unique English string is translated at most once and the result is
persisted to ``data/translations.json`` so subsequent scans (and server
restarts) reuse the cached value for free. The cache key is a 16-char
SHA-1 prefix of the stripped source text — collision chance is
effectively zero for the volumes we deal with (hundreds of strings).

Usage pattern:

    tr = GeminiTranslator()
    he = tr.lookup(english_text)     # O(1), None if missing
    tr.queue(english_text)           # enqueue for next flush; noop if cached
    await tr.flush()                 # batch-translate pending + persist

Designed to be throttle-friendly on Gemini's free tier: each ``flush()``
call makes at most ``MAX_BATCHES_PER_FLUSH`` API requests, so the
translator never monopolises the bot's shared daily quota.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Set

import httpx

logger = logging.getLogger(__name__)

TRANSLATIONS_FILE = os.path.join("data", "translations.json")
BATCH_SIZE = 10
MAX_BATCHES_PER_FLUSH = 1  # share daily quota with the discovery LLM
# Use a different model than the discovery agent so the translator
# doesn't contend with it for the same per-minute Gemini quota. Free-tier
# limits are per-model; gemini-2.0-flash-lite has 30 RPM of headroom
# while the discovery agent already saturates gemini-2.5-flash-lite at
# 20 RPM. Override via TRANSLATOR_MODEL env var if needed.
MODEL_NAME = os.getenv("TRANSLATOR_MODEL", "gemini-2.0-flash-lite")
TIMEOUT_SEC = 30.0


def _key(text: str) -> str:
    return hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()[:16]


def _redact_key_from_url(url: str) -> str:
    if "key=" not in url:
        return url
    base, _, qs = url.partition("?")
    parts = ["key=***" if p.startswith("key=") else p for p in qs.split("&")]
    return base + "?" + "&".join(parts)


class GeminiTranslator:
    def __init__(self, api_key: Optional[str] = None, model: str = MODEL_NAME):
        self.api_key = (
            api_key
            or os.getenv("GEMINI_API_KEY")
            or os.getenv("GOOGLE_API_KEY")
        )
        self.enabled = bool(self.api_key)
        self.model = model
        base = os.getenv(
            "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"
        ).rstrip("/")
        self.url = f"{base}/models/{self.model}:generateContent"
        self.cache: Dict[str, str] = self._load()
        self._pending: Set[str] = set()
        if self.enabled:
            logger.info(
                f"🌐 Translator initialized | cached={len(self.cache)} | model={self.model}"
            )
        else:
            logger.info("🌐 Translator disabled (no GEMINI_API_KEY)")

    def _load(self) -> Dict[str, str]:
        p = Path(TRANSLATIONS_FILE)
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"translations.json read failed: {e}")
            return {}

    def _save(self):
        p = Path(TRANSLATIONS_FILE)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                json.dumps(self.cache, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"translations.json write failed: {e}")

    def lookup(self, text: str) -> Optional[str]:
        if not text:
            return None
        return self.cache.get(_key(text))

    def queue(self, text: str):
        if not text or not self.enabled:
            return
        k = _key(text)
        if k in self.cache:
            return
        self._pending.add(text)

    async def flush(self) -> int:
        if not self.enabled or not self._pending:
            return 0
        pending = sorted(self._pending)
        # Only take MAX_BATCHES_PER_FLUSH * BATCH_SIZE this call; the rest
        # stays queued for the next scan so we don't blow the free-tier
        # daily quota in one go.
        take = MAX_BATCHES_PER_FLUSH * BATCH_SIZE
        this_round = pending[:take]
        leftover = pending[take:]
        self._pending = set(leftover)
        done = 0
        for bi in range(0, len(this_round), BATCH_SIZE):
            batch = this_round[bi : bi + BATCH_SIZE]
            try:
                translations = await self._translate_batch(batch)
            except Exception as e:
                logger.warning(f"Translate batch failed: {e}")
                # Re-queue so we try again next scan
                for t in batch:
                    self._pending.add(t)
                break
            for src, tgt in zip(batch, translations):
                if tgt:
                    self.cache[_key(src)] = tgt
                    done += 1
                else:
                    # Mark as translated-to-self so we don't hammer the
                    # API on a string it can't handle.
                    self.cache[_key(src)] = src
        if done:
            self._save()
            logger.info(
                f"🌐 Translated {done} new string(s); cache={len(self.cache)}; pending={len(self._pending)}"
            )
        return done

    async def _translate_batch(self, texts: List[str]) -> List[str]:
        numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(texts))
        prompt = (
            "Translate each of the following English prediction-market strings into Hebrew. "
            "Preserve numbers, dates, ticker symbols (BTC, ETH, NVDA, etc.), proper names, and units. "
            "Do not add commentary. Return ONLY a JSON array of Hebrew strings "
            "in the same order as the inputs, nothing else.\n\n"
            f"{numbered}"
        )
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.0,
                "maxOutputTokens": 2048,
                "responseMimeType": "application/json",
            },
        }
        params = {"key": self.api_key}
        async with httpx.AsyncClient(timeout=TIMEOUT_SEC) as client:
            r = await client.post(self.url, params=params, json=payload)
        if r.status_code != 200:
            safe_url = (
                _redact_key_from_url(str(r.request.url)) if r.request else self.url
            )
            logger.warning(
                f"Translator HTTP {r.status_code} @ {safe_url}: {r.text[:200]}"
            )
            return [""] * len(texts)
        data = r.json()
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception:
            return [""] * len(texts)
        try:
            arr = json.loads(text)
        except Exception:
            return [""] * len(texts)
        if not isinstance(arr, list):
            return [""] * len(texts)
        arr = [str(x) if x is not None else "" for x in arr[: len(texts)]]
        while len(arr) < len(texts):
            arr.append("")
        return arr
