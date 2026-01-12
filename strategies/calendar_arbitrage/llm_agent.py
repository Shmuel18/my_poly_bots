"""
LLM Agent for Semantic Market Clustering

Uses GPT-4 or Claude to identify logical relationships between markets
that are too complex for regex or embeddings alone.

Advantages over SBERT:
- Understands causal relationships (e.g., "Trump wins" → "Republican Senate")
- Detects temporal dependencies (Q1 vs Q2 vs Annual)
- Identifies subset/superset relationships with reasoning
- Handles ambiguous phrasing and implicit context
"""

import asyncio
import json
import logging
import os
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)

try:
    import openai
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False
    logger.warning("OpenAI not installed. LLM agent disabled. Install: pip install openai")


class CalendarArbitrageLLMAgent:
    """LLM-powered agent for advanced market clustering."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "gemini-2.0-flash",  # Google Gemini Flash
        temperature: float = 0.0,
        max_tokens: int = 8000,  # Increased for longer responses
        enable_caching: bool = True,
    ):
        """
        Initialize LLM agent.

        Args:
            api_key: OpenAI API key (defaults to OPENAI_API_KEY env var)
            model: Model to use (gpt-4o-mini, gpt-4o, gpt-4-turbo)
            temperature: Sampling temperature (0 = deterministic)
            max_tokens: Max response length
            enable_caching: Cache responses for identical queries
        """
        if not OPENAI_AVAILABLE:
            raise ImportError("OpenAI package not installed. Run: pip install openai")

        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY not set. Set in .env or pass to constructor.")

        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.enable_caching = enable_caching

        # Initialize OpenAI client
      
        # יצירת הלקוח עם הכתובת החדשה
        # הגדרת הכתובת של גוגל
        base_url = os.getenv("OPENAI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")

        # יצירת הלקוח עם הכתובת החדשה
        self.client = openai.OpenAI(
            api_key=self.api_key,
            base_url=base_url
        )

        # Cache for repeated queries
        self.cache = {}  # {query_hash: response}

        logger.info(f"LLM Agent initialized with model: {model}")

    def _build_clustering_prompt(self, markets: List[Dict[str, Any]]) -> str:
        """Build prompt for market clustering task."""
        # Format markets for LLM
        market_descriptions = []
        for idx, market in enumerate(markets):
            question = market.get("question", "Unknown")
            end_date = market.get("end_date_iso", "Unknown")
            market_descriptions.append(f"{idx+1}. \"{question}\" (expires: {end_date})")

        prompt = f"""You are an expert in prediction markets and logical arbitrage strategies.

**Task:** Identify groups of markets that describe the SAME underlying event but with DIFFERENT time horizons.

**Calendar Arbitrage Logic:**
- Markets are related if they measure the same outcome but one expires EARLIER (subset) and one LATER (superset)
- Example: "Trump wins by March" (early) vs "Trump wins by Election Day" (late)
- The early market is a SUBSET of the late market (if early YES → late YES)

**Markets to analyze:**
{chr(10).join(market_descriptions)}

**Instructions:**
1. Group markets by the SAME underlying event
2. Within each group, identify which market expires EARLIEST (subset) and which expires LATEST (superset)
3. Only include pairs where early expiry is a logical subset of late expiry
4. Ignore markets that don't have clear temporal relationships

**Output Format (JSON):**
```json
{{
  "clusters": [
    {{
      "event_description": "Brief description of the underlying event",
      "early_market_index": 1,
      "late_market_index": 3,
      "reasoning": "Why these markets form a calendar arbitrage pair"
    }}
  ]
}}
```

**Return ONLY valid JSON, no explanatory text.**"""

        return prompt

    async def cluster_markets(
        self,
        markets: List[Dict[str, Any]],
        max_clusters: int = 50,
    ) -> List[Tuple[int, int, str]]:
        """
        Use LLM to identify calendar arbitrage pairs.

        Args:
            markets: List of market dictionaries with 'question' and 'end_date_iso'
            max_clusters: Maximum number of pairs to return

        Returns:
            List of tuples: (early_market_idx, late_market_idx, reasoning)
        """
        if not markets:
            return []

        # Build prompt
        prompt = self._build_clustering_prompt(markets)

        # Check cache
        cache_key = hash(prompt) if self.enable_caching else None
        if cache_key and cache_key in self.cache:
            logger.debug("LLM cache hit")
            response_text = self.cache[cache_key]
        else:
            # Call OpenAI API
            try:
                logger.info(f"Calling LLM ({self.model}) for {len(markets)} markets...")
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": "You are an expert in prediction market arbitrage."},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                )

                response_text = response.choices[0].message.content.strip()

                # Cache response
                if cache_key:
                    self.cache[cache_key] = response_text

                logger.info(f"LLM response received ({response.usage.total_tokens} tokens)")

            except Exception as e:
                logger.error(f"LLM API call failed: {e}")
                return []

        # Parse JSON response
        try:
            # Extract JSON from markdown code blocks if present
            if "```json" in response_text:
                response_text = response_text.split("```json")[1].split("```")[0].strip()
            elif "```" in response_text:
                response_text = response_text.split("```")[1].split("```")[0].strip()

            data = json.loads(response_text)
            clusters = data.get("clusters", [])

            # Convert to tuples
            result = []
            for cluster in clusters[:max_clusters]:
                early_idx = cluster.get("early_market_index")
                late_idx = cluster.get("late_market_index")
                reasoning = cluster.get("reasoning", "")

                if early_idx is not None and late_idx is not None:
                    # Convert to 0-indexed
                    result.append((early_idx - 1, late_idx - 1, reasoning))

            logger.info(f"LLM identified {len(result)} calendar pairs")
            return result

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse LLM JSON response: {e}")
            logger.debug(f"Raw response: {response_text[:500]}")
            return []

    async def explain_relationship(
        self,
        market1_question: str,
        market2_question: str,
    ) -> Optional[str]:
        """
        Ask LLM to explain the logical relationship between two markets.

        Returns:
            Explanation string or None if no clear relationship
        """
        prompt = f"""Analyze the relationship between these two prediction markets:

Market 1: "{market1_question}"
Market 2: "{market2_question}"

**Question:** Is Market 1 a logical subset of Market 2 (or vice versa)? 

**Answer in 1-2 sentences:**"""

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=self.temperature,
                max_tokens=150,
            )

            explanation = response.choices[0].message.content.strip()
            return explanation

        except Exception as e:
            logger.error(f"LLM explanation failed: {e}")
            return None

    def clear_cache(self):
        """Clear the response cache."""
        self.cache.clear()
        logger.info("LLM cache cleared")


# Singleton instance for reuse
_llm_agent_instance: Optional[CalendarArbitrageLLMAgent] = None


def get_llm_agent(
    api_key: Optional[str] = None,
    model: str = "gemini-2.5-flash",  # Google Gemini Flash
) -> Optional[CalendarArbitrageLLMAgent]:
    """
    Get or create singleton LLM agent instance.

    Returns None if OpenAI is not available or API key is not set.
    """
    global _llm_agent_instance

    if not OPENAI_AVAILABLE:
        return None

    if _llm_agent_instance is None:
        try:
            _llm_agent_instance = CalendarArbitrageLLMAgent(
                api_key=api_key,
                model=model,
            )
        except (ImportError, ValueError) as e:
            logger.warning(f"LLM agent disabled: {e}")
            return None

    return _llm_agent_instance
