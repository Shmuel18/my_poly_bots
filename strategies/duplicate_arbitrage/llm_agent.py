"""Duplicate-market LLM verifier.

Reuses the Gemini client infrastructure from
strategies/calendar_arbitrage/llm_agent.py but asks a different question:
'Are these two markets resolving on IDENTICAL criteria?' — rather than
'Same event with different deadlines?'.

Only pair verification is done via the LLM; candidate generation is
embedding-based in the strategy itself.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)


def _redact_key_from_url(url: str) -> str:
    if "key=" not in url:
        return url
    base, _, qs = url.partition("?")
    parts = ["key=***" if p.startswith("key=") else p for p in qs.split("&")]
    return base + "?" + "&".join(parts)


class DuplicateArbitrageLLMAgent:
    """Sends a pair of market descriptions to Gemini and returns a confidence
    score that the two markets are functionally identical (same event, same
    resolution, same endDate, just listed twice on Polymarket)."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "gemini-2.5-flash-lite",
        temperature: float = 0.0,
        max_output_tokens: int = 1024,
        timeout_sec: float = 30.0,
    ):
        self.api_key = (
            api_key
            or os.getenv("GEMINI_API_KEY")
            or os.getenv("GOOGLE_API_KEY")
        )
        if not self.api_key:
            raise ValueError("Missing GEMINI_API_KEY in .env")

        self.model = model
        self.temperature = float(temperature)
        self.max_output_tokens = int(max_output_tokens)
        self.timeout_sec = float(timeout_sec)

        self.base_url = os.getenv(
            "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"
        ).rstrip("/")
        self.url = f"{self.base_url}/models/{self.model}:generateContent"

        logger.info(f"🤖 Duplicate LLM Agent Initialized | Model: {self.model}")

    def _build_prompt(self, a: Dict[str, Any], b: Dict[str, Any]) -> str:
        def desc(m: Dict[str, Any]) -> str:
            q = m.get("question", "(no question)")
            end = m.get("endDate") or m.get("end_date_iso") or "unknown"
            body = (m.get("description") or "").strip().replace("\n", " ")[:600]
            src = m.get("resolutionSource") or m.get("resolution_source") or ""
            outs = m.get("outcomes", "")
            return (
                f'  Question: "{q}"\n'
                f"  End date: {end}\n"
                f"  Resolution source: {src or '(none)'}\n"
                f"  Outcomes: {outs}\n"
                f"  Description: {body}"
            )

        return (
            "You are verifying whether two Polymarket markets are FUNCTIONALLY IDENTICAL "
            "— i.e. they resolve on the same real-world event, at effectively the same "
            "moment, using the same resolution criteria.\n\n"
            "MARKET A:\n" + desc(a) + "\n\n"
            "MARKET B:\n" + desc(b) + "\n\n"
            "Return ONLY valid JSON in this exact format:\n"
            "{\n"
            '  "identical": true | false,\n'
            '  "confidence": 0.0 to 1.0,\n'
            '  "reasoning": "short sentence"\n'
            "}\n\n"
            "Requirements to score identical=true with confidence ≥ 0.95:\n"
            "1. The questions describe the SAME outcome (same entities, same action/metric, "
            "same threshold if any).\n"
            "2. The end dates match within ±24 hours.\n"
            "3. The resolution criteria (source/method) are the same or trivially compatible.\n"
            "4. Outcome structure is identical (both binary YES/NO, or same multi-outcome).\n\n"
            "REJECT (identical=false) if any of the following are even slightly different:\n"
            "- The threshold, date, or entity named.\n"
            "- The resolution source (e.g. CNN vs Reuters, official announcement vs media).\n"
            "- The granularity (e.g. one resolves daily, the other at end-of-period).\n\n"
            "When uncertain, prefer REJECT. The cost of a false positive is large."
        )

    async def verify(self, a: Dict[str, Any], b: Dict[str, Any]) -> Tuple[bool, float, str]:
        """Return (identical, confidence, reasoning). On error: (False, 0.0, '…')."""
        prompt = self._build_prompt(a, b)
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": self.temperature,
                "maxOutputTokens": self.max_output_tokens,
                "responseMimeType": "application/json",
            },
        }
        params = {"key": self.api_key}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
                r = await client.post(self.url, params=params, json=payload)
            if r.status_code != 200:
                safe = _redact_key_from_url(str(r.request.url)) if r.request else self.url
                logger.error(f"❌ Dup-LLM HTTP {r.status_code} @ {safe}: {r.text[:300]}")
                return False, 0.0, f"HTTP {r.status_code}"
            resp = r.json()
            text = ""
            try:
                text = resp["candidates"][0]["content"]["parts"][0]["text"].strip()
            except Exception:
                return False, 0.0, "empty response"
            parsed = self._try_parse(text)
            if not parsed:
                return False, 0.0, f"parse failed: {text[:120]}"
            identical = bool(parsed.get("identical", False))
            try:
                conf = float(parsed.get("confidence", 0.0))
            except (TypeError, ValueError):
                conf = 0.0
            reason = str(parsed.get("reasoning", ""))[:200]
            return identical, conf, reason
        except Exception as e:
            logger.error(f"❌ Dup-LLM exception: {e}")
            return False, 0.0, str(e)[:120]

    @staticmethod
    def _try_parse(text: str) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(text)
        except Exception:
            pass
        t = text.strip()
        if "```" in t:
            try:
                t = t.split("```", 1)[1].split("```", 1)[0].strip()
                if t.lower().startswith("json"):
                    t = t[4:].strip()
                return json.loads(t)
            except Exception:
                pass
        start, end = t.find("{"), t.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(t[start:end + 1])
            except Exception:
                pass
        return None


def get_duplicate_llm_agent(**kwargs) -> Optional[DuplicateArbitrageLLMAgent]:
    try:
        return DuplicateArbitrageLLMAgent(**kwargs)
    except Exception as e:
        logger.warning(f"Duplicate LLM Agent disabled: {e}")
        return None
