import json
import logging
import os
from typing import List, Dict, Any, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

def _redact_key_from_url(url: str) -> str:
    # avoid leaking ?key=... into logs
    if "key=" not in url:
        return url
    base, _, qs = url.partition("?")
    parts = qs.split("&")
    parts = ["key=***" if p.startswith("key=") else p for p in parts]
    return base + "?" + "&".join(parts)

class CalendarArbitrageLLMAgent:
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "gemini-2.0-flash",
        temperature: float = 0.0,
        max_output_tokens: int = 4096,
        timeout_sec: float = 45.0,
    ):
        # Google API key (Gemini). Prefer GEMINI_API_KEY in .env
        self.api_key = (
            api_key
            or os.getenv("GEMINI_API_KEY")
            or os.getenv("GOOGLE_API_KEY")
            or os.getenv("OPENAI_API_KEY")
        )
        if not self.api_key:
            raise ValueError("Missing API key. Set GEMINI_API_KEY in .env")

        self.model = model
        self.temperature = float(temperature)
        self.max_output_tokens = int(max_output_tokens)
        self.timeout_sec = float(timeout_sec)

        self.base_url = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        self.url = f"{self.base_url}/models/{self.model}:generateContent"

        logger.info(f"🤖 LLM Agent Initialized | Model: {self.model} | Provider: Google Gemini")

    async def cluster_markets_debug(
        self,
        markets: List[Dict[str, Any]],
        max_clusters: int = 50,
    ) -> Tuple[List[Tuple[int, int, str]], str]:
        """
        Like cluster_markets, but returns (clusters, raw_llm_text) for debugging/logging.
        """
        if not markets:
            return [], ""

        prompt = self._build_clustering_prompt(markets)

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
                safe_url = _redact_key_from_url(str(r.request.url)) if r.request else self.url
                logger.error(f"❌ Gemini HTTP {r.status_code} @ {safe_url}: {r.text}")
                return [], r.text

            resp_json = r.json()
            text = self._extract_text(resp_json)
            if not text:
                logger.error(f"❌ Gemini returned empty text. Raw response: {resp_json}")
                return [], str(resp_json)

            parsed = self._try_parse_json(text)
            if not parsed:
                logger.error(f"❌ LLM JSON parse failed. Raw text: {text[:500]}")
                return [], text

            clusters = parsed.get("clusters", []) or []
            result: List[Tuple[int, int, str]] = []

            for c in clusters[:max_clusters]:
                early = c.get("early_market_index")
                late = c.get("late_market_index")
                if isinstance(early, int) and isinstance(late, int):
                    # Convert 1-based index from LLM to 0-based for Python
                    result.append((early - 1, late - 1, str(c.get("reasoning", ""))))

            logger.info(f"🤖 LLM found {len(result)} potential arbitrage pairs")
            return result, text

        except Exception as e:
            logger.error(f"❌ LLM Error: {e}")
            return [], str(e)

    async def cluster_markets(
        self,
        markets: List[Dict[str, Any]],
        max_clusters: int = 50,
    ) -> List[Tuple[int, int, str]]:
        clusters, _ = await self.cluster_markets_debug(markets, max_clusters)
        return clusters

    def _build_clustering_prompt(self, markets: List[Dict[str, Any]]) -> str:
        market_descriptions = []
        for idx, market in enumerate(markets):
            question = market.get("question", "Unknown")
            end_date = market.get("end_date_iso", "Unknown")
            market_descriptions.append(f"{idx+1}. \"{question}\" (expires: {end_date})")

        return f"""You are an expert in prediction market arbitrage.
Identify pairs of markets that describe the SAME underlying event but with DIFFERENT expiries.
The early expiry must be a logical SUBSET of the late expiry.

Markets:
{chr(10).join(market_descriptions)}

Return ONLY valid JSON in this exact format:
{{
  "clusters": [
    {{
      "event_description": "short description",
      "early_market_index": 1,
      "late_market_index": 3,
      "reasoning": "why"
    }}
  ]
}}"""

    @staticmethod
    def _extract_text(resp_json: Dict[str, Any]) -> str:
        candidates = resp_json.get("candidates") or []
        if not candidates:
            return ""
        content = (candidates[0] or {}).get("content") or {}
        parts = content.get("parts") or []
        if not parts:
            return ""
        text = (parts[0] or {}).get("text") or ""
        return str(text).strip()

    @staticmethod
    def _strip_markdown_fences(text: str) -> str:
        t = text.strip()
        if "```" not in t:
            return t
        try:
            t2 = t.split("```", 1)[1]
            t2 = t2.split("```", 1)[0].strip()
            if t2.lower().startswith("json"):
                t2 = t2[4:].strip()
            return t2
        except Exception:
            return t

    @staticmethod
    def _try_parse_json(text: str) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(text)
        except Exception:
            pass

        stripped = CalendarArbitrageLLMAgent._strip_markdown_fences(text)
        if stripped != text:
            try:
                return json.loads(stripped)
            except Exception:
                pass

        start = stripped.find("{")
        end = stripped.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(stripped[start : end + 1])
            except Exception:
                pass
        return None

_llm_agent_instance: Optional[CalendarArbitrageLLMAgent] = None

def get_llm_agent(api_key: Optional[str] = None, model: str = "gemini-2.0-flash"):
    global _llm_agent_instance
    if _llm_agent_instance is None:
        try:
            _llm_agent_instance = CalendarArbitrageLLMAgent(api_key=api_key, model=model)
        except Exception as e:
            logger.warning(f"LLM Agent disabled: {e}")
            return None
    return _llm_agent_instance