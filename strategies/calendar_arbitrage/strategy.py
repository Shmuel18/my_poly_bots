"""
Calendar (Logical) Arbitrage Strategy

קונה NO בשוק מוקדם (תת-קבוצה) ו-YES בשוק המאוחר (על-קבוצה),
כאשר סכום העלויות (ASK_NO מוקדם + ASK_YES מאוחר) קטן מ-1 פחות סף רווח ועמלות.

Real-time WebSocket integration for sub-second early exit triggers.
"""

import asyncio
import logging
import os
import re
import json
from typing import Dict, List, Any, Optional

from strategies.base_strategy import BaseStrategy
from strategies.calendar_arbitrage.websocket import CalendarArbitrageWebSocketManager
from strategies.calendar_arbitrage.llm_agent import get_llm_agent, CalendarArbitrageLLMAgent
from core.database import get_database, DatabaseManager

logger = logging.getLogger(__name__)


MONTH_WORDS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december"
]


class CalendarArbitrageStrategy(BaseStrategy):
    """ארביטראז' לוגי בין שווקים עם טווחי זמן שונים לאותו אירוע."""


    def __init__(self, *args, **kwargs):
        # ...existing code...
        self.market_offset = 0
        self.discovered_pairs_file = "data/discovered_pairs.json"
        self.discovered_pairs = self._load_discovered_pairs()
        # ...existing code...

    def _load_discovered_pairs(self):
        if os.path.exists(self.discovered_pairs_file):
            try:
                with open(self.discovered_pairs_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load discovered pairs: {e}")
                return []
        return []

    def _save_discovered_pairs(self):
        try:
            os.makedirs(os.path.dirname(self.discovered_pairs_file), exist_ok=True)
            with open(self.discovered_pairs_file, 'w', encoding='utf-8') as f:
                json.dump(self.discovered_pairs, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"❌ Failed to save discovered pairs: {e}")

    def __init__(
        self,
        strategy_name: str = "CalendarArbitrageStrategy",
        scan_interval: int = 10,
        log_level: str = "INFO",
        min_profit_threshold: float = 0.01,
        max_pairs: int = 1000,
        dry_run: bool = False,
        early_exit_threshold: float = 0.20,
        min_annualized_roi: float = 0.15,
        check_invalid_risk: bool = True,
        use_embeddings: bool = True,
        similarity_threshold: float = 0.85,
        use_llm: bool = True,
        llm_model: str = "gemini-2.5-flash-lite",
        use_database: bool = False,
        min_resolution_match_confidence: float = 0.9,
        probe_usd: float = 5.0,
        confirmed_usd: float = 20.0,
        escalation_minutes: float = 30.0,
        use_telegram: bool = True,
        **kwargs,
    ):
        super().__init__(
            strategy_name=strategy_name,
            scan_interval=scan_interval,
            log_level=log_level,
            connection=kwargs.get("connection"),
            dry_run=dry_run,
        )

        # 1. קבצים וזיכרון (חובה להגדיר כאן כדי למנוע AttributeError)
        self.PAIRS_FILE = os.path.join("data", "discovered_pairs.json")
        self.market_offset = 0
        self.llm_batch_size = 100
        
        # 2. הגדרות אסטרטגיה
        self.min_profit_threshold = float(min_profit_threshold)
        self.max_pairs = max_pairs
        self.estimated_fee = float(os.getenv("DEFAULT_SLIPPAGE", "0.01"))
        self.early_exit_threshold = float(early_exit_threshold)
        self.min_annualized_roi = float(min_annualized_roi)
        self.check_invalid_risk = check_invalid_risk
        self.use_embeddings = use_embeddings
        self.similarity_threshold = float(similarity_threshold)
        self.use_llm = use_llm
        self.llm_model = llm_model
        self.min_resolution_match_confidence = float(min_resolution_match_confidence)

        # Human-in-the-loop tiered sizing
        self.probe_usd = float(probe_usd)
        self.confirmed_usd = float(confirmed_usd)
        self.escalation_seconds = float(escalation_minutes) * 60.0
        self.CONFIRMED_FILE = os.path.join("data", "confirmed_pairs.json")
        self.PENDING_FILE = os.path.join("data", "pending_confirmation.json")
        self.REJECTED_FILE = os.path.join("data", "rejected_pairs.json")
        # Live price snapshot: dashboard reads this file to render pair cards
        # with current ask/bid and running profit %.
        self.PRICE_SNAPSHOT_FILE = os.path.join("data", "price_snapshot.json")
        self.confirmed_pairs: Dict[str, Dict[str, Any]] = self._load_json_state(self.CONFIRMED_FILE)
        self.pending_pairs: Dict[str, Dict[str, Any]] = self._load_json_state(self.PENDING_FILE)
        self.rejected_pairs: Dict[str, Dict[str, Any]] = self._load_json_state(self.REJECTED_FILE)

        # Telegram notifier (no-op if TELEGRAM_BOT_TOKEN / CHAT_ID not set)
        self.telegram = None
        if use_telegram:
            try:
                from utils.telegram_notifier import TelegramNotifier
                self.telegram = TelegramNotifier()
            except Exception as e:
                self.logger.warning(f"Telegram init failed: {e}")
                self.telegram = None

        # Hebrew translator (shares GEMINI_API_KEY with the discovery LLM,
        # but throttled to one batch/scan so it never starves discovery).
        # Disabled automatically if no GEMINI_API_KEY is set.
        self.translator = None
        try:
            from utils.translator import GeminiTranslator
            self.translator = GeminiTranslator()
        except Exception as e:
            self.logger.warning(f"Translator init failed: {e}")
            self.translator = None

        # 3. טעינת זוגות שנשמרו מהעבר
        self.discovered_pairs = self._load_discovered_pairs()

        # 4. אתחול רכיבים (Embedding, LLM, WS)
        self._embedding_model = None
        self._embedding_cache = {}
        self._llm_agent = None
        if self.use_llm:
            try:
                self._llm_agent = get_llm_agent(model=llm_model)
                if self._llm_agent:
                    self.logger.info(f"🤖 LLM Agent enabled: {llm_model}")
            except Exception as e:
                self.logger.warning(f"⚠️ LLM Agent initialization failed: {e}")
                self.use_llm = False

        self.ws_manager = CalendarArbitrageWebSocketManager()
        self.ws_running = False
        self.price_updates = {}
        self.use_database = use_database
        self.db = None
        
        self.logger.info(f"✅ Strategy Initialized | {len(self.discovered_pairs)} monitored pairs")

    def _load_discovered_pairs(self) -> List[Dict]:
        """טעינת זוגות מהדיסק."""
        if os.path.exists(self.PAIRS_FILE):
            try:
                with open(self.PAIRS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data if isinstance(data, list) else []
            except Exception as e:
                self.logger.warning(f"Failed to load discovered pairs: {e}")
                return []
        return []

    def _save_discovered_pairs(self):
        """שמירה לדיסק."""
        try:
            os.makedirs(os.path.dirname(self.PAIRS_FILE), exist_ok=True)
            with open(self.PAIRS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.discovered_pairs, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.logger.error(f"❌ Failed to save discovered pairs: {e}")

    def _load_json_state(self, path: str) -> Dict[str, Any]:
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception as e:
            self.logger.warning(f"Failed to load {path}: {e}")
            return {}

    def _save_json_state(self, path: str, data: Dict[str, Any]):
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.logger.error(f"Failed to save {path}: {e}")

    @staticmethod
    def _pair_key(early_id: str, late_id: str) -> str:
        """Stable key regardless of insertion order."""
        a, b = sorted((str(early_id), str(late_id)))
        return f"{a[:12]}__{b[:12]}"

    @staticmethod
    def _event_slug(market: Dict[str, Any]) -> Optional[str]:
        """Extract the parent event's slug from a Gamma market dict.

        Gamma markets carry an ``events`` array; the first event's ``slug``
        powers the canonical URL ``https://polymarket.com/event/<slug>``.
        Returns None if the shape is unexpected so the dashboard can hide
        the link gracefully.
        """
        events = market.get("events") or []
        if isinstance(events, list) and events:
            first = events[0]
            if isinstance(first, dict):
                slug = first.get("slug")
                if isinstance(slug, str) and slug:
                    return slug
        return None

    def _get_tier_status(self, early_id: str, late_id: str) -> str:
        """Returns one of: 'rejected', 'confirmed', 'pending', 'probe'."""
        key = self._pair_key(early_id, late_id)
        if key in self.rejected_pairs:
            return "rejected"
        if key in self.confirmed_pairs:
            return "confirmed"
        if key in self.pending_pairs:
            return "pending"
        return "probe"

    def _size_for_tier(self, tier: str, combined_ask: float) -> float:
        """Convert tier label + combined ask price into shares."""
        if tier == "confirmed":
            usd = self.confirmed_usd
        elif tier == "probe":
            usd = self.probe_usd
        else:
            return 0.0  # pending / rejected → no new position
        if combined_ask <= 0:
            return 0.0
        return max(1.0, round(usd / combined_ask, 2))

    # How many consecutive scans a pair's market must be absent from Gamma
    # before we purge the pair. ~5 scans of grace guards against transient
    # Gamma pagination hiccups / 5xx responses wiping out live pairs.
    PURGE_AFTER_MISSING_SCANS = 5

    def _cleanup_expired_pairs(self, healthy_pair_keys: set):
        """Prune pairs that consistently fail to produce a price snapshot.

        A pair is "healthy" on a given scan if the monitoring phase
        managed to build a snap_entry for it — meaning both markets
        were present in Gamma, passed ``_validate_temporal_containment``,
        and had two-outcome token IDs. Anything else bumps the pair's
        ``missing_scans`` counter; once it hits
        ``PURGE_AFTER_MISSING_SCANS`` consecutive unhealthy scans the
        pair is dropped along with any confirmed/pending/rejected
        references so the dashboard and Telegram flow stop showing
        ghost cards. The grace window guards against transient Gamma
        pagination hiccups that would otherwise flash-purge every pair.
        """
        threshold = self.PURGE_AFTER_MISSING_SCANS
        kept: List[Dict[str, Any]] = []
        dropped: List[Dict[str, Any]] = []
        counter_changed = False

        for p in self.discovered_pairs:
            key = self._pair_key(p.get('early_id', ''), p.get('late_id', ''))
            prev = p.get('missing_scans', 0)
            if key in healthy_pair_keys:
                if prev != 0:
                    p['missing_scans'] = 0
                    counter_changed = True
                kept.append(p)
                continue
            new_count = prev + 1
            p['missing_scans'] = new_count
            counter_changed = True
            if new_count >= threshold:
                dropped.append(p)
            else:
                kept.append(p)

        if not dropped and not counter_changed:
            return

        self.discovered_pairs = kept

        if dropped:
            dropped_keys = {
                self._pair_key(p['early_id'], p['late_id']) for p in dropped
            }
            self.confirmed_pairs = {
                k: v for k, v in self.confirmed_pairs.items() if k not in dropped_keys
            }
            self.pending_pairs = {
                k: v for k, v in self.pending_pairs.items() if k not in dropped_keys
            }
            self.rejected_pairs = {
                k: v for k, v in self.rejected_pairs.items() if k not in dropped_keys
            }
            self._save_json_state(self.CONFIRMED_FILE, self.confirmed_pairs)
            self._save_json_state(self.PENDING_FILE, self.pending_pairs)
            self._save_json_state(self.REJECTED_FILE, self.rejected_pairs)
            sample = ", ".join(
                (p.get('early_question') or p['early_id'])[:40] for p in dropped[:3]
            )
            self.logger.info(
                f"🧹 Cleanup: purged {len(dropped)} stale pair(s) absent "
                f"≥{threshold} scans. Sample: {sample}"
            )

        self._save_discovered_pairs()

    def _save_discovered_pairs(self):
        try:
            with open(self.PAIRS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.discovered_pairs, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.logger.warning(f"Failed to save discovered pairs: {e}")

        self.logger.info("⚙️ Configuration:")
        self.logger.info(f"   Min profit threshold: {self.min_profit_threshold:.3f}")
        self.logger.info(f"   Early exit threshold: {self.early_exit_threshold:.3f}")
        self.logger.info(f"   Min annualized ROI: {self.min_annualized_roi:.1%}")
        self.logger.info(f"   Estimated fee/slippage per leg: {self.estimated_fee:.3f}")
        self.logger.info(f"   Check invalid risk: {self.check_invalid_risk}")
        self.logger.info(f"   Use embeddings: {self.use_embeddings}")
        if self.use_embeddings:
            self.logger.info(f"   Similarity threshold: {self.similarity_threshold:.2f}")
        self.logger.info(f"   Use LLM: {self.use_llm}")
        if self.use_llm:
            self.logger.info(f"   LLM model: {self.llm_model}")
        # התיקון בשורה למטה: הוספת self.
        self.logger.info(f"   Scan interval: {self.scan_interval}s")

    def _get_embedding_model(self):
        """Lazy load sentence transformer model."""
        if self._embedding_model is None:
            try:
                from sentence_transformers import SentenceTransformer
                self.logger.info("📦 Loading sentence embedding model (all-MiniLM-L6-v2)...")
                self._embedding_model = SentenceTransformer('all-MiniLM-L6-v2')
                self.logger.info("✅ Embedding model loaded successfully")
            except ImportError:
                self.logger.warning("⚠️ sentence-transformers not installed. Install with: pip install sentence-transformers")
                self.use_embeddings = False
            except Exception as e:
                self.logger.error(f"❌ Failed to load embedding model: {e}")
                self.use_embeddings = False
        return self._embedding_model

    def _get_embedding(self, text: str):
        """Get cached or compute embedding for text."""
        if text in self._embedding_cache:
            return self._embedding_cache[text]
        
        model = self._get_embedding_model()
        if model is None:
            return None
        
        try:
            embedding = model.encode(text, convert_to_tensor=False)
            self._embedding_cache[text] = embedding
            return embedding
        except Exception as e:
            self.logger.debug(f"Error computing embedding: {e}")
            return None

    def _cosine_similarity(self, embedding1, embedding2) -> float:
        """Calculate cosine similarity between two embeddings."""
        try:
            import numpy as np
            dot_product = np.dot(embedding1, embedding2)
            norm1 = np.linalg.norm(embedding1)
            norm2 = np.linalg.norm(embedding2)
            if norm1 == 0 or norm2 == 0:
                return 0.0
            return float(dot_product / (norm1 * norm2))
        except Exception as e:
            self.logger.debug(f"Error calculating similarity: {e}")
            return 0.0

    def _are_similar_markets(self, q1: str, q2: str) -> bool:
        """Check if two questions are about the same event using hybrid approach."""
        if not q1 or not q2:
            return False
        
        # Method 1: Regex-based normalization (fast pre-filter)
        norm1 = self._normalize_question(q1)
        norm2 = self._normalize_question(q2)
        
        # If normalized strings match exactly, they're similar
        if norm1 == norm2 and norm1:
            return True
        
        # Method 2: Embedding-based similarity (semantic understanding)
        if self.use_embeddings:
            emb1 = self._get_embedding(q1)
            emb2 = self._get_embedding(q2)
            
            if emb1 is not None and emb2 is not None:
                similarity = self._cosine_similarity(emb1, emb2)
                self.logger.debug(f"Similarity {similarity:.3f}: '{q1[:40]}' vs '{q2[:40]}'")
                return similarity >= self.similarity_threshold
        
        # Fallback: use regex match
        return norm1 == norm2 and norm1 != ""

    def _normalize_question(self, q: str) -> str:
        """הורדת ביטויי זמן כדי לקבץ שווקים של אותו אירוע בסיסי.
        ניסיונית: מסירה תבניות 'by end of <month>' / 'by <month>'/ תאריכים.
        """
        if not q:
            return ""
        s = q.lower()
        # Remove common date/time phrases
        s = re.sub(r"by\s+end\s+of\s+(" + "|".join(MONTH_WORDS) + r")", "", s)
        s = re.sub(r"by\s+(the\s+)?end\s+of\s+\d{4}", "", s)
        s = re.sub(r"by\s+(" + "|".join(MONTH_WORDS) + r")(\s+\d{4})?", "", s)
        s = re.sub(r"until\s+(the\s+)?end\s+of\s+\d{4}", "", s)
        s = re.sub(r"until\s+(" + "|".join(MONTH_WORDS) + r")(\s+\d{4})?", "", s)
        s = re.sub(r"before\s+(" + "|".join(MONTH_WORDS) + r")(\s+\d{4})?", "", s)
        s = re.sub(r"\b\d{1,2}\s+(" + "|".join(MONTH_WORDS) + r")\b", "", s)
        s = re.sub(r"\b(" + "|".join(MONTH_WORDS) + r")\s+\d{1,2}\b", "", s)
        s = re.sub(r"\b\d{4}\b", "", s)
        # Collapse whitespace
        s = re.sub(r"\s+", " ", s).strip()
        return s

    def _get_token_ids(self, market: Dict) -> List[str]:
        token_ids = market.get("clobTokenIds", [])
        if isinstance(token_ids, str):
            import json
            try:
                token_ids = json.loads(token_ids)
            except:
                return []
        return [str(t) for t in token_ids] if isinstance(token_ids, list) else []

    def _get_end_date(self, market: Dict) -> Optional[str]:
        return market.get("endDate")

    def _calculate_annualized_roi(self, profit: float, days_until_close: float) -> float:
        """חישוב תשואה שנתית (Annualized ROI)."""
        if days_until_close <= 0:
            return 0.0
        # ROI = (profit / investment) * (365 / days)
        # For calendar arb, investment ≈ total_cost
        # Simplified: annualized_profit = profit * (365 / days)
        return profit * (365.0 / days_until_close)

    def _parse_end_date(self, end_date_str: Optional[str]):
        """Parse endDate string to timezone-aware datetime. Returns None on failure."""
        if not end_date_str:
            return None
        try:
            from datetime import datetime
            return datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        except Exception:
            return None

    # Months → number, used by the question-title date parser below.
    _MONTH_LOOKUP = {m: i + 1 for i, m in enumerate(MONTH_WORDS)}

    # Capture group 1: "by"|"before"|"until"|"prior to"|"no later than"|"end of"
    # Group 2: optional "the", group 3: optional "end of "
    # Group 4 = month name OR full numeric date OR bare year.
    # We compile a non-greedy version that surfaces both the month/day/year
    # parts; the calling helper does the actual interpretation.
    _RESOLUTION_DATE_RE = re.compile(
        r"\b(?:by|until|before|prior\s+to|no\s+later\s+than|end\s+of)\s+"
        r"(?:the\s+)?(?:end\s+of\s+)?"
        r"("
        + r"|".join(MONTH_WORDS) +
        r")(?:\s+(\d{1,2}))?(?:[,]?\s+(\d{4}))?"           # month [day] [year]
        r"|\b(?:by|until|before|prior\s+to|no\s+later\s+than|end\s+of)\s+"
        r"(?:the\s+)?(?:end\s+of\s+)?"
        r"(\d{4})\b",                                        # bare year
        re.IGNORECASE,
    )

    def _resolution_date_from_question(self, question: Optional[str], reference: Optional[Any] = None):
        """Extract the deadline a market actually resolves on from its title.

        Polymarket's ``endDate`` field is when *trading* closes, which is
        not always the same as when the question resolves. Recycled
        markets can carry an ``endDate`` weeks after the resolution
        criterion (e.g. "Iran closes airspace by May 8" with
        ``endDate=May 31``). Sorting / containment by ``endDate``
        therefore inverts pairs and produces fake edges. The title is
        the source of truth: it always names the resolution date.

        Returns a timezone-aware datetime (UTC, end of day) or None when
        no parseable deadline is in the question.

        ``reference`` is an optional anchor datetime used to disambiguate
        bare month phrases like "by April 30?" — without a year, we
        assume the next occurrence on or after the reference (default
        now()). Calendar arb needs the FUTURE instance, never the past.
        """
        if not question or not isinstance(question, str):
            return None
        from datetime import datetime, timezone
        m = self._RESOLUTION_DATE_RE.search(question.lower())
        if not m:
            return None
        month_name, day_str, year_str, bare_year = m.groups()

        # Bare year ("by 2027" / "before 2026") → Dec 31 of that year
        if bare_year:
            try:
                return datetime(int(bare_year), 12, 31, 23, 59, tzinfo=timezone.utc)
            except (ValueError, TypeError):
                return None

        if not month_name:
            return None
        month = self._MONTH_LOOKUP.get(month_name.lower())
        if not month:
            return None
        ref = reference or datetime.now(timezone.utc)
        try:
            day = int(day_str) if day_str else None
        except (ValueError, TypeError):
            day = None
        # Pick year: explicit year wins; otherwise infer from the
        # reference. If the resulting date would already be past, bump
        # to next year (since "by April 30" in May means NEXT April).
        if year_str:
            try:
                year = int(year_str)
            except (ValueError, TypeError):
                year = ref.year
        else:
            year = ref.year
        # If no day provided, use last day of month — calendar arb
        # treats "by April" as "by end of April".
        if day is None:
            from calendar import monthrange
            day = monthrange(year, month)[1]
        try:
            cand = datetime(year, month, day, 23, 59, tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None
        if not year_str and cand <= ref:
            try:
                cand = datetime(year + 1, month, day, 23, 59, tzinfo=timezone.utc)
            except (ValueError, TypeError):
                return None
        return cand

    def _market_resolution_date(self, market: Optional[Dict[str, Any]]):
        """Best-effort resolution date for a market: parse the question
        title first; fall back to the ``endDate`` field if the title
        doesn't carry a parseable deadline. Returns a tz-aware datetime
        or None.

        Anchors year-disambiguation to ``now()``, never to the market's
        endDate. Polymarket recycles markets with stale endDate fields
        (e.g. a "by May 6" market shown today as endDate=May 31), and
        anchoring to that endDate would make the parser bump "by May 6"
        to NEXT year (because May 6 is before the May 31 anchor) —
        producing an inverted pair against the sibling "by May 31"
        market. Using ``now`` keeps the future-most-instance semantics
        without trusting a possibly-stale field."""
        if not market:
            return None
        from_title = self._resolution_date_from_question(market.get("question"))
        if from_title is not None:
            return from_title
        return self._parse_end_date(market.get("endDate"))

    def _days_until_close(self, end_date_str: Optional[str]) -> float:
        """חישוב ימים עד סגירת השוק."""
        end_date = self._parse_end_date(end_date_str)
        if end_date is None:
            return 365.0  # default fallback
        try:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            delta = (end_date - now).total_seconds() / 86400  # days
            return max(0.1, delta)  # minimum 0.1 day
        except Exception:
            return 365.0

    def _validate_temporal_containment(self, early: Dict, late: Dict) -> bool:
        """Strict validation: early.endDate must be strictly earlier than late.endDate
        AND early.endDate must be in the future.

        Two failures this guards against:

        1. Inverted pairs ("late" actually closes BEFORE "early"). Early⊂late
           containment breaks and the calendar-arb math goes the other way:
           you can hit a scenario where both legs lose simultaneously.

        2. Stale-metadata pairs where the early leg's endDate already passed.
           Polymarket recycles market IDs and sometimes leaves a market's
           ``endDate`` field set to a past date even though the title says a
           future deadline (e.g. "Will Russia capture Lyman by June 30, 2026?"
           with endDate=2025-12-31). On those, the bot reads the old endDate,
           validates against an even-later "late" market, and reports a fake
           positive edge — but the early leg is effectively unsellable
           (closed-but-not-closed market), so any "guaranteed +X%" is
           illusory. Reject early_end <= now to refuse those entirely.
        """
        # Use the resolution date parsed from each market's question title
        # rather than the ``endDate`` field. ``endDate`` is when trading
        # closes — sometimes weeks after the question's actual deadline
        # — and was producing inverted pairs (e.g. "by May 8" market
        # had endDate May 31, sorting it AFTER "by May 15"). The title
        # is the source of truth for resolution timing.
        early_end = self._market_resolution_date(early)
        late_end = self._market_resolution_date(late)
        if early_end is None or late_end is None:
            self.logger.debug(
                f"Rejected pair: missing resolution date (early='{(early.get('question') or '')[:40]}', "
                f"late='{(late.get('question') or '')[:40]}')"
            )
            return False
        if early_end > late_end:
            self.logger.warning(
                f"❌ Temporal violation: early {early_end.isoformat()} > late {late_end.isoformat()} "
                f"('{early.get('question', '')[:40]}' vs '{late.get('question', '')[:40]}')"
            )
            return False
        if early_end == late_end:
            return False
        # Early leg must still be tradeable. Past resolution → stale
        # market → fake edge.
        from datetime import datetime, timezone
        now_utc = datetime.now(timezone.utc)
        # Also reject pairs whose markets have an endDate already past.
        # When Polymarket's endDate is in the past, the market is either
        # being finalized for resolution (orderbook frozen) or the operator
        # forgot to update it. Either way the orderbook quotes are stale
        # and any reported edge is illusory. The Hormuz "by end of April"
        # market with endDate=2026-04-30 (already past) demonstrates this:
        # NO bid 99.9¢ but no ask, YES ask 0.1¢ but no bid → essentially
        # settled as NO, not actually tradeable.
        for label, mkt in (("early", early), ("late", late)):
            mkt_end = self._parse_end_date(mkt.get("endDate"))
            if mkt_end is not None and mkt_end <= now_utc:
                self.logger.debug(
                    f"Rejected pair: {label} market endDate {mkt_end.isoformat()} "
                    f"already past ('{mkt.get('question', '')[:50]}')"
                )
                return False
        if early_end <= now_utc:
            self.logger.debug(
                f"Rejected pair: early endDate {early_end.isoformat()} already past "
                f"('{early.get('question', '')[:50]}')"
            )
            return False
        return True
    
    def _cluster_markets_by_embeddings(self, markets: List[Dict]) -> List[List[Dict]]:
        """Build groups using semantic similarity (embeddings)."""
        groups: List[List[Dict]] = []
        processed = set()
        
        for i, m1 in enumerate(markets):
            if i in processed:
                continue
            q1 = m1.get("question", "")
            if not q1:
                continue
                
            # Start new group with m1
            group = [m1]
            processed.add(i)
            
            # Find all similar markets
            for j, m2 in enumerate(markets[i+1:], start=i+1):
                if j in processed:
                    continue
                q2 = m2.get("question", "")
                if self._are_similar_markets(q1, q2):
                    group.append(m2)
                    processed.add(j)
            
            if len(group) >= 2:
                groups.append(group)
        
        return groups

    @staticmethod
    def _orderbook_side(book, side: str):
        """py-clob-client returns OrderBookSummary (dataclass with .asks/.bids,
        each entry an OrderSummary with .price/.size). Older code assumed a
        dict. Accept either shape defensively."""
        if not book:
            return []
        if hasattr(book, side):
            return getattr(book, side) or []
        if hasattr(book, "get"):
            return book.get(side, []) or []
        return []

    @staticmethod
    def _orderbook_entry(e):
        """Return (price, size) floats from either an OrderSummary dataclass
        or a {price, size} dict."""
        if e is None:
            return None, None
        p = getattr(e, "price", None) if not isinstance(e, dict) else e.get("price")
        s = getattr(e, "size", None) if not isinstance(e, dict) else e.get("size")
        try:
            return float(p), float(s) if s is not None else 0.0
        except (TypeError, ValueError):
            return None, None

    def _best_ask(self, token_id: str) -> Optional[Dict[str, float]]:
        """Lowest ask in the book — the price we'd pay to BUY right now.

        Polymarket's CLOB returns ``asks`` sorted by price descending
        (worst → best). Taking ``asks[0]`` would give the WORST ask
        ($0.99 on illiquid markets) and inflate every pair's entry cost
        to ~$1.98, making the bot believe nothing is ever profitable.
        We explicitly scan for the minimum price instead so we're
        robust to API sort-order changes.
        """
        try:
            book = self.executor.client.get_order_book(token_id)
            asks = self._orderbook_side(book, "asks")
            best_p, best_s = None, None
            for entry in asks:
                p, s = self._orderbook_entry(entry)
                if p is None:
                    continue
                if best_p is None or p < best_p:
                    best_p, best_s = p, s
            if best_p is not None:
                return {"price": best_p, "size": best_s or 0.0}
        except Exception as e:
            self.logger.debug(f"_best_ask failed for {token_id[:12]}: {e}")
            return None
        return None

    def _best_bid(self, token_id: str) -> Optional[Dict[str, float]]:
        """Highest bid in the book — the price we'd get if we SELL now.

        Bids come back sorted ascending (worst → best). ``bids[0]`` is
        the LOWEST bid, useless to a seller. Take the maximum bid price
        explicitly. Same robustness rationale as ``_best_ask``.
        """
        try:
            book = self.executor.client.get_order_book(token_id)
            bids = self._orderbook_side(book, "bids")
            best_p, best_s = None, None
            for entry in bids:
                p, s = self._orderbook_entry(entry)
                if p is None:
                    continue
                if best_p is None or p > best_p:
                    best_p, best_s = p, s
            if best_p is not None:
                return {"price": best_p, "size": best_s or 0.0}
        except Exception as e:
            self.logger.debug(f"_best_bid failed for {token_id[:12]}: {e}")
            return None
        return None

    def _simulate_fill(self, token_id: str, side: str, size: float) -> Optional[Dict[str, float]]:
        """
        סימולציית מילוי - חישוב מחיר ממוצע משוקלל לפי עומק ה-Order Book.
        Fill simulation for slippage calculation.
        """
        try:
            book = self.executor.client.get_order_book(token_id)
            if not book:
                return None

            # For BUY: consume asks (sellers), for SELL: consume bids (buyers)
            # Handles both OrderBookSummary dataclass and dict-shaped responses.
            orders = self._orderbook_side(book, "asks" if side == "BUY" else "bids")
            if not orders:
                return None

            # Polymarket returns asks sorted price DESCENDING and bids sorted
            # price ASCENDING — i.e. worst-first in both cases. For fill
            # simulation we want to consume from BEST first, so re-sort
            # explicitly by what's "best" for the side we're trading:
            #   BUY  consumes asks → ascending (lowest price first)
            #   SELL consumes bids → descending (highest price first)
            normalized = []
            for o in orders:
                p, s = self._orderbook_entry(o)
                if p is None or s is None or s <= 0:
                    continue
                normalized.append((p, s))
            if not normalized:
                return None
            normalized.sort(key=lambda x: x[0], reverse=(side == "SELL"))

            remaining_size = size
            total_cost = 0.0
            filled_size = 0.0
            first_price = None

            for order_price, order_size in normalized:
                if order_price is None or order_size is None or order_size <= 0:
                    continue
                if first_price is None:
                    first_price = order_price

                fill_amount = min(remaining_size, order_size)
                total_cost += fill_amount * order_price
                filled_size += fill_amount
                remaining_size -= fill_amount

                if remaining_size <= 0:
                    break

            if filled_size == 0:
                return None

            avg_price = total_cost / filled_size
            return {
                "avg_price": avg_price,
                "filled_size": filled_size,
                "requested_size": size,
                "fully_filled": remaining_size <= 0.01,  # tolerance
                "slippage": (avg_price - first_price) if first_price is not None else 0.0,
            }
        except Exception as e:
            self.logger.debug(f"Error simulating fill: {e}")
            return None

    def _has_invalid_risk(self, market: Dict) -> bool:
        """בודק אם יש סיכון Invalid (שוק יכול להיות מבוטל)."""
        if not self.check_invalid_risk:
            return False
        # Check if market has 'enableOrderBook' = false or other invalid indicators
        # Polymarket: check if outcomes include 'Invalid' or market is not binary
        outcomes = market.get("outcomes", [])
        if isinstance(outcomes, list) and len(outcomes) > 2:
            # More than YES/NO suggests potential invalid outcome
            return True
        # Check market tags/description for 'invalid' keyword
        description = str(market.get("description", "")).lower()
        question = str(market.get("question", "")).lower()
        if "invalid" in description or "invalid" in question:
            return True
        return False


    # Pattern matching MONOTONIC deadline phrases only ("by June 30", "until 2026",
    # "before December", "end of November"). Crucially excludes snapshot-style
    # phrases like "on June 30" or "at the April meeting" — those are NOT
    # calendar-arb-safe because the event being measured on a specific date
    # doesn't propagate forward (e.g. "largest company on June 30" vs "on Dec 31"
    # is two separate snapshots, not a monotonic deadline).
    _MONOTONIC_DEADLINE_RE = re.compile(
        r"\b(by|until|before|prior\s+to|no\s+later\s+than|end\s+of)\s+"
        r"(the\s+)?(end\s+of\s+)?"
        r"(\d{4}|\d{1,2}([/\-]\d{1,2}([/\-]\d{2,4})?)?|"
        + r"|".join(MONTH_WORDS) + r")",
        re.IGNORECASE,
    )

    def _has_monotonic_deadline(self, question: str) -> bool:
        """True only if the question phrases its deadline monotonically.

        Examples:
          'Will X happen by November 15?'       → True
          'Will X hit $100 by end of 2026?'     → True
          'Will X be largest on June 30?'       → False (snapshot)
          'Will X happen in November?'          → False (period-specific)
          'Will Fed cut at April 29 meeting?'   → False (snapshot)
        """
        if not question:
            return False
        return bool(self._MONOTONIC_DEADLINE_RE.search(question))

    @staticmethod
    def _event_series_key(event: Dict[str, Any]) -> Optional[str]:
        """Return a stable series identifier for a Gamma event, or None.

        Polymarket events expose their series in a few shapes depending on
        the endpoint version. Try them in order:
          1. ``series`` array (newest shape — list of ``{id, slug, title}``)
          2. ``series`` dict (older shape — single ``{id, slug}``)
          3. ``seriesSlug`` flat string
          4. ``series_id`` flat int
        Returns None when the event isn't part of any series (so it's
        skipped by the discovery loop instead of bucketed alone).
        """
        series_field = event.get("series")
        if isinstance(series_field, list) and series_field:
            first = series_field[0]
            if isinstance(first, dict):
                slug = first.get("slug") or first.get("ticker")
                if isinstance(slug, str) and slug:
                    return slug
                sid = first.get("id")
                if sid is not None:
                    return f"id:{sid}"
        if isinstance(series_field, dict):
            slug = series_field.get("slug") or series_field.get("ticker")
            if isinstance(slug, str) and slug:
                return slug
            sid = series_field.get("id")
            if sid is not None:
                return f"id:{sid}"
        slug = event.get("seriesSlug")
        if isinstance(slug, str) and slug:
            return slug
        sid = event.get("series_id") or event.get("seriesId")
        if sid is not None:
            return f"id:{sid}"
        return None

    @staticmethod
    def _binary_market_from_event(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Extract the single binary (YES/NO) market from a single-market
        deadline-variant event, or None if the event doesn't fit that shape.

        Series-based calendar arbitrage only makes sense when each event is
        a single yes/no question with a hard deadline (e.g. "Russia x
        Ukraine ceasefire by April 30"). Multi-outcome events (price
        ranges, brackets, election tiers) need different handling and are
        deferred to the LLM path.
        """
        markets = event.get("markets")
        if not isinstance(markets, list) or len(markets) != 1:
            return None
        m = markets[0]
        if not isinstance(m, dict):
            return None
        if not m.get("id"):
            return None
        # Reject multi-outcome markets — those need bracket logic, not calendar
        outcomes = m.get("outcomes")
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except Exception:
                outcomes = None
        if isinstance(outcomes, list) and len(outcomes) != 2:
            return None
        return m

    def _series_discover_pairs(
        self,
        market_map: Optional[Dict[str, Dict]] = None,
        events: Optional[List[Dict]] = None,
    ) -> int:
        """Ground-truth calendar-arb discovery via Polymarket's own series
        metadata.

        Polymarket groups multi-deadline questions about the same event
        into a single ``series`` (e.g. "Russia x Ukraine ceasefire" has
        deadline variants by Apr 30, May 31, Jun 30, Dec 31, Jun 30 2027,
        Dec 31 2027). When the operator wires those events into a series,
        the resolution criteria are *guaranteed* identical apart from the
        end date — exactly the calendar-arb monotonicity property.

        This pre-pass:
          1. Fetches active events (each carries ``series`` + ``markets``).
          2. Groups events by series key.
          3. For each series with ≥2 single-binary-market events, sorts
             events by endDate and creates consecutive-pair entries.
          4. Auto-adds them to ``confirmed_pairs`` (no LLM, no Telegram
             gate) — Polymarket's own metadata is the strongest signal we
             can possibly have.

        Returns the count of newly-discovered pairs. Cheap to call every
        scan: an event-batch refresh + dict bucketing.

        Side effect: when ``market_map`` is provided, every market we
        extract from a series event is merged into it. The default Gamma
        ``/markets`` list endpoint paginates by recency / volume; tail
        markets — multi-year deadline variants in slow series like
        ``russia-x-ukraine-ceasefire-by-2027`` — frequently fall outside
        the top 5000 returned. Without merging them here the monitoring
        loop's ``market_map.get(pair['early_id'])`` returns ``None`` and
        the pair is silently skipped, leaving the dashboard stuck on
        "Waiting for orderbook pricing…".
        """
        import time as _time

        if events is None:
            try:
                events = self.scanner.get_all_active_events(max_events=3000)
            except AttributeError:
                self.logger.warning(
                    "Scanner missing get_all_active_events; series discovery skipped."
                )
                return 0
            except Exception as e:
                self.logger.warning(f"Series discovery: event fetch failed: {e}")
                return 0
        if not events:
            return 0

        # Group events by series key. Only events that (a) belong to a
        # series and (b) carry exactly one binary market participate.
        by_series: Dict[str, List[Dict[str, Any]]] = {}
        for ev in events:
            if not isinstance(ev, dict):
                continue
            skey = self._event_series_key(ev)
            if not skey:
                continue
            mkt = self._binary_market_from_event(ev)
            if not mkt:
                continue
            # Prefer the resolution date parsed from the market's title.
            # ``endDate`` (event or market) is when trading closes, which
            # can be weeks after the actual resolution criterion on
            # recycled Polymarket markets. The title is authoritative.
            end_dt = self._market_resolution_date(mkt)
            if end_dt is None:
                # Fallback: try the event's own endDate as a last resort.
                end_dt = self._parse_end_date(ev.get("endDate"))
            if end_dt is None:
                continue
            by_series.setdefault(skey, []).append({
                "event": ev,
                "market": mkt,
                "end": end_dt,
            })

        if not by_series:
            return 0

        existing_pair_keys = {
            tuple(sorted((p.get("early_id", ""), p.get("late_id", ""))))
            for p in self.discovered_pairs
        }

        new_count = 0
        backfilled = False
        now = _time.time()
        for skey, entries in by_series.items():
            if len(entries) < 2:
                continue
            entries.sort(key=lambda x: x["end"])
            # Drop duplicates that share an end date (shouldn't normally
            # happen inside a series but guards against operator typos).
            uniq: List[Dict[str, Any]] = []
            seen_ends = set()
            for e in entries:
                key = e["end"].isoformat()
                if key in seen_ends:
                    continue
                seen_ends.add(key)
                uniq.append(e)
            if len(uniq) < 2:
                continue

            # Consecutive pairs only. The non-consecutive pairs (i, i+2),
            # (i, i+3), … are implied by transitivity of the early⊂late
            # containment, so monitoring just the (i, i+1) chain catches
            # every spread without quadratic blowup. Larger-gap arbs that
            # the consecutive chain can't price-check are rare and not
            # worth the noise.
            for i in range(len(uniq) - 1):
                early = uniq[i]
                late = uniq[i + 1]
                early_m = early["market"]
                late_m = late["market"]
                # Reject series where the events are NOT monotonic-deadline
                # questions. Polymarket groups several non-calendar shapes
                # under "series": rolling 5-min crypto windows ("XRP Up or
                # Down 11:30-11:35"), sports matchups on the same date
                # ("KBO: Eagles vs Landers"), tournament brackets, etc.
                # Those satisfy "shared series" but NOT "early⊂late
                # containment" — the YES outcome of one window says
                # nothing about the next. Requiring monotonic phrasing
                # ("by X", "before X", "until X", "end of X") on both
                # endpoints keeps only true calendar chains.
                if not (
                    self._has_monotonic_deadline(early_m.get("question", ""))
                    and self._has_monotonic_deadline(late_m.get("question", ""))
                ):
                    continue
                if not self._validate_temporal_containment(early_m, late_m):
                    continue
                # Inject markets into the caller's map BEFORE the
                # already-known check so previously-saved series pairs also
                # get their legs price-checkable on every scan.
                if market_map is not None:
                    market_map.setdefault(early_m["id"], early_m)
                    market_map.setdefault(late_m["id"], late_m)
                pair_tuple = tuple(sorted((early_m["id"], late_m["id"])))
                if pair_tuple in existing_pair_keys:
                    # Self-heal: backfill late_question / series_slug onto
                    # existing pairs that were saved before those fields
                    # were tracked. The dashboard uses pair.late_question
                    # as a fallback when the live snapshot is empty.
                    existing_q = (early_m.get("question")
                                  or early["event"].get("title") or "")
                    late_q_now = (late_m.get("question")
                                  or late["event"].get("title") or "")
                    for sp in self.discovered_pairs:
                        sp_tuple = tuple(sorted((str(sp.get("early_id", "")),
                                                 str(sp.get("late_id", "")))))
                        if sp_tuple != pair_tuple:
                            continue
                        if not sp.get("late_question") and late_q_now:
                            sp["late_question"] = late_q_now
                            backfilled = True
                        if not sp.get("series_slug"):
                            sp["series_slug"] = skey
                            backfilled = True
                        if not sp.get("early_question") and existing_q:
                            sp["early_question"] = existing_q
                            backfilled = True
                    continue
                existing_pair_keys.add(pair_tuple)

                early_q = (early_m.get("question")
                           or early["event"].get("title")
                           or "")
                late_q = (late_m.get("question")
                          or late["event"].get("title")
                          or "")
                series_title = (
                    (early["event"].get("series") or [{}])[0].get("title")
                    if isinstance(early["event"].get("series"), list)
                    else (early["event"].get("series") or {}).get("title")
                    if isinstance(early["event"].get("series"), dict)
                    else None
                ) or skey

                self.discovered_pairs.append({
                    "early_id": early_m["id"],
                    "late_id": late_m["id"],
                    "description": (
                        f"Polymarket series '{series_title}': "
                        f"{early['end'].date()} → {late['end'].date()}"
                    ),
                    "early_question": early_q,
                    "late_question": late_q,
                    "resolution_match_confidence": 1.0,
                    "discovery_method": "series",
                    "series_slug": skey,
                })

                pair_key = self._pair_key(early_m["id"], late_m["id"])
                self.confirmed_pairs[pair_key] = {
                    "confirmed_at": now,
                    "confirmed_by": "series_auto",
                    "early_id": early_m["id"],
                    "late_id": late_m["id"],
                    "early_question": early_q,
                    "late_question": late_q,
                    "series_slug": skey,
                    "discovery_method": "series",
                }
                new_count += 1
                self.logger.info(
                    f"🔗 Series pair: '{series_title[:50]}' "
                    f"({early['end'].date()} ⊂ {late['end'].date()})"
                )

        if new_count > 0:
            self._save_discovered_pairs()
            self._save_json_state(self.CONFIRMED_FILE, self.confirmed_pairs)
            self.logger.info(
                f"🔗 Series discovery: {new_count} new auto-confirmed "
                f"pair(s) across {len(by_series)} series"
            )
        elif backfilled:
            # Persist backfilled late_question / series_slug onto existing
            # entries so later restarts don't show empty leg labels again.
            self._save_discovered_pairs()
            self.logger.info("🔗 Series discovery: backfilled metadata on existing pairs")
        return new_count

    def _intra_event_discover_pairs(
        self,
        market_map: Optional[Dict[str, Dict]] = None,
        events: Optional[List[Dict]] = None,
    ) -> int:
        """Calendar-arb discovery for the OTHER Polymarket multi-deadline shape.

        Series discovery (``_series_discover_pairs``) handles the case where
        each deadline variant is its own *event* sharing a series slug —
        e.g. the Russia-x-Ukraine ceasefire has 6 separate events under
        ``seriesSlug=russia-x-ukraine-ceasefire``.

        But many calendar chains use a different shape: ONE event with N
        markets *inside* it, each with a different endDate. Example:
        ``trump-announces-us-blockade-of-hormuz-lifted-by`` — a single
        event whose ``markets`` array contains 12 binary markets dated
        Apr 23 / Apr 30 / May 8 / May 15 / May 22 / May 31 / … . The
        event has no ``series`` field at all, so the existing series
        method skips it; ``_binary_market_from_event`` rejects events
        with ``len(markets) != 1``, leaving the chain entirely
        unmonitored.

        This method picks those up. It iterates every active event,
        keeps the binary YES/NO markets that are still tradeable
        (``closed=False``, 2 outcomes, parsable endDate), sorts them by
        deadline, and emits consecutive ``(early, late)`` pairs the
        same way the series method does. Side effect: every market it
        accepts is merged into the caller's ``market_map`` so the
        monitoring loop can price the new pairs immediately.

        Auto-confirmed (``discovery_method="intra_event"``) for the same
        reason series pairs are: the operator put these markets in one
        event, so the resolution criteria are guaranteed identical apart
        from the deadline.
        """
        import time as _time

        if events is None:
            try:
                events = self.scanner.get_all_active_events(max_events=3000)
            except AttributeError:
                self.logger.warning(
                    "Scanner missing get_all_active_events; intra-event discovery skipped."
                )
                return 0
            except Exception as e:
                self.logger.warning(f"Intra-event discovery: event fetch failed: {e}")
                return 0
        if not events:
            return 0

        existing_pair_keys = {
            tuple(sorted((str(p.get("early_id", "")), str(p.get("late_id", "")))))
            for p in self.discovered_pairs
        }

        new_count = 0
        backfilled = False
        events_with_chains = 0
        now = _time.time()

        for ev in events:
            if not isinstance(ev, dict):
                continue
            markets = ev.get("markets")
            if not isinstance(markets, list) or len(markets) < 2:
                continue

            # Keep only the legs we can actually price-check: tradeable
            # binary YES/NO markets with a parseable endDate.
            candidates: List[Dict[str, Any]] = []
            for m in markets:
                if not isinstance(m, dict) or not m.get("id"):
                    continue
                if m.get("closed"):
                    continue
                outcomes = m.get("outcomes")
                if isinstance(outcomes, str):
                    try:
                        outcomes = json.loads(outcomes)
                    except Exception:
                        outcomes = None
                if not isinstance(outcomes, list) or len(outcomes) != 2:
                    continue
                # Prefer the resolution date parsed from the question
                # title; fall back to endDate when the title has no
                # deadline phrase. This sorts pairs by when each market
                # actually RESOLVES, not when trading closes — which
                # are different on recycled Polymarket markets.
                end_dt = self._market_resolution_date(m)
                if end_dt is None:
                    continue
                # Inject the parent event's slug into the market dict so
                # downstream _event_slug() and the dashboard's "Open on
                # Polymarket" link both find it. Markets fetched via
                # /events nested-children don't carry an ``events`` array,
                # so without this the per-leg link would be missing.
                ev_slug = ev.get("slug") or ""
                ev_id = ev.get("id")
                if ev_slug and not m.get("events"):
                    m["events"] = [{"slug": ev_slug, "id": ev_id}]
                candidates.append({"market": m, "end": end_dt})

            if len(candidates) < 2:
                continue

            candidates.sort(key=lambda x: x["end"])
            # Drop markets that share an endDate (resolution race conditions
            # produce duplicates on Polymarket sometimes).
            uniq: List[Dict[str, Any]] = []
            seen_ends = set()
            for c in candidates:
                key = c["end"].isoformat()
                if key in seen_ends:
                    continue
                seen_ends.add(key)
                uniq.append(c)
            if len(uniq) < 2:
                continue

            event_title = (ev.get("title") or ev.get("slug") or "event")[:80]
            event_slug = ev.get("slug") or ""
            events_with_chains += 1

            for i in range(len(uniq) - 1):
                early_m = uniq[i]["market"]
                late_m = uniq[i + 1]["market"]
                # Same monotonic-deadline guard as series discovery: filters
                # out non-calendar shapes (rolling 5-min windows, sports
                # matchups) that happen to share an event slug.
                if not (
                    self._has_monotonic_deadline(early_m.get("question", ""))
                    and self._has_monotonic_deadline(late_m.get("question", ""))
                ):
                    continue
                if not self._validate_temporal_containment(early_m, late_m):
                    continue
                # Inject markets into the caller's map BEFORE the
                # already-known check so previously-saved intra-event
                # pairs also become price-checkable on every scan.
                if market_map is not None:
                    market_map.setdefault(early_m["id"], early_m)
                    market_map.setdefault(late_m["id"], late_m)
                pair_tuple = tuple(sorted((str(early_m["id"]), str(late_m["id"]))))
                if pair_tuple in existing_pair_keys:
                    # Self-heal old entries that pre-date the late_question
                    # / event_slug fields (same pattern as series).
                    early_q_now = early_m.get("question", "") or ""
                    late_q_now = late_m.get("question", "") or ""
                    for sp in self.discovered_pairs:
                        sp_tuple = tuple(sorted((str(sp.get("early_id", "")),
                                                 str(sp.get("late_id", "")))))
                        if sp_tuple != pair_tuple:
                            continue
                        if not sp.get("late_question") and late_q_now:
                            sp["late_question"] = late_q_now
                            backfilled = True
                        if not sp.get("event_slug") and event_slug:
                            sp["event_slug"] = event_slug
                            backfilled = True
                        if not sp.get("early_question") and early_q_now:
                            sp["early_question"] = early_q_now
                            backfilled = True
                    continue
                existing_pair_keys.add(pair_tuple)

                early_q = early_m.get("question", "") or ""
                late_q = late_m.get("question", "") or ""

                self.discovered_pairs.append({
                    "early_id": early_m["id"],
                    "late_id": late_m["id"],
                    "description": (
                        f"Polymarket event '{event_title}': "
                        f"{uniq[i]['end'].date()} → {uniq[i + 1]['end'].date()}"
                    ),
                    "early_question": early_q,
                    "late_question": late_q,
                    "resolution_match_confidence": 1.0,
                    "discovery_method": "intra_event",
                    "event_slug": event_slug,
                })

                pair_key = self._pair_key(early_m["id"], late_m["id"])
                self.confirmed_pairs[pair_key] = {
                    "confirmed_at": now,
                    "confirmed_by": "intra_event_auto",
                    "early_id": early_m["id"],
                    "late_id": late_m["id"],
                    "early_question": early_q,
                    "late_question": late_q,
                    "event_slug": event_slug,
                    "discovery_method": "intra_event",
                }
                new_count += 1
                self.logger.info(
                    f"🧩 Intra-event pair: '{event_title[:50]}' "
                    f"({uniq[i]['end'].date()} ⊂ {uniq[i + 1]['end'].date()})"
                )

        if new_count > 0:
            self._save_discovered_pairs()
            self._save_json_state(self.CONFIRMED_FILE, self.confirmed_pairs)
            self.logger.info(
                f"🧩 Intra-event discovery: {new_count} new auto-confirmed "
                f"pair(s) across {events_with_chains} multi-market events"
            )
        elif backfilled:
            self._save_discovered_pairs()
            self.logger.info("🧩 Intra-event discovery: backfilled metadata on existing pairs")
        return new_count

    def _regex_discover_obvious_pairs(self, all_markets: List[Dict]) -> int:
        """Fast AI-free pre-pass: find pairs whose normalized title is IDENTICAL
        (after stripping temporal words). These are literally the same bet with
        different deadlines — a mathematical certainty that doesn't need LLM
        verification. Auto-added to confirmed_pairs so they trade at full size.

        Returns count of newly-discovered pairs.

        Safety guardrails:
        - Normalized question must be ≥ 20 chars (avoids trivial false matches)
        - Must have strictly different endDates (temporal containment)
        - Title alone doesn't guarantee identical resolution rules, but for
          Polymarket's operator templates this is overwhelmingly reliable.
        """
        import time as _time

        existing_pair_keys = {
            tuple(sorted((p.get('early_id', ''), p.get('late_id', ''))))
            for p in self.discovered_pairs
        }

        # Bucket markets by normalized question. Only markets whose original
        # question uses a MONOTONIC deadline phrase ("by X", "until X",
        # "before X", "end of X") qualify. Snapshot phrasings ("on June 30")
        # look similar after normalization but don't satisfy the containment
        # property needed for calendar arbitrage.
        by_norm: Dict[str, List[Dict]] = {}
        for m in all_markets:
            q = m.get('question', '') or ''
            if not self._has_monotonic_deadline(q):
                continue
            norm = self._normalize_question(q)
            if len(norm) < 20:
                continue  # Too short → risk of spurious matches
            by_norm.setdefault(norm, []).append(m)

        new_count = 0
        now = _time.time()
        for norm_q, markets in by_norm.items():
            if len(markets) < 2:
                continue
            # Sort by RESOLUTION DATE (parsed from question title), not the
            # raw endDate field. Polymarket recycles markets and sometimes
            # leaves endDate set to a date that doesn't match the title's
            # actual deadline. Sorting by endDate would put a "by May 31"
            # market BEFORE a "by May 6" one if its endDate happened to be
            # earlier — producing inverted pairs. The resolution date
            # parsed from the title is the source of truth.
            dated = []
            for m in markets:
                d = self._market_resolution_date(m)
                if d is not None:
                    dated.append((m, d))
            dated.sort(key=lambda x: x[1])
            if len(dated) < 2:
                continue

            for i in range(len(dated)):
                early_m, early_end = dated[i]
                for j in range(i + 1, len(dated)):
                    late_m, late_end = dated[j]
                    if early_end >= late_end:
                        continue  # Same date or inverted → not a calendar pair
                    # Defense in depth: also let the unified validator have
                    # a say (rejects past-resolution early legs etc.).
                    if not self._validate_temporal_containment(early_m, late_m):
                        continue
                    key_tuple = tuple(sorted((early_m['id'], late_m['id'])))
                    if key_tuple in existing_pair_keys:
                        continue
                    existing_pair_keys.add(key_tuple)

                    self.discovered_pairs.append({
                        "early_id": early_m['id'],
                        "late_id": late_m['id'],
                        "description": f"Regex auto-match (identical normalized title): \"{norm_q[:80]}\"",
                        "early_question": early_m.get('question', ''),
                        "late_question": late_m.get('question', ''),
                        "resolution_match_confidence": 1.0,
                        "discovery_method": "regex_exact",
                    })
                    # Auto-confirmed: trade at confirmed_usd without Telegram gate
                    pair_key = self._pair_key(early_m['id'], late_m['id'])
                    self.confirmed_pairs[pair_key] = {
                        "confirmed_at": now,
                        "confirmed_by": "regex_auto",
                        "early_id": early_m['id'],
                        "late_id": late_m['id'],
                        "early_question": early_m.get('question', ''),
                        "late_question": late_m.get('question', ''),
                        "normalized_title": norm_q[:120],
                    }
                    new_count += 1
                    self.logger.info(
                        f"🎯 Auto-confirmed regex pair: '{norm_q[:60]}' "
                        f"(early resolves {early_end.date()}, late resolves {late_end.date()})"
                    )

        if new_count > 0:
            self._save_discovered_pairs()
            self._save_json_state(self.CONFIRMED_FILE, self.confirmed_pairs)
            self.logger.info(f"📐 Regex discovery: {new_count} new auto-confirmed pair(s)")
        return new_count

    def _purge_pairs_with_invalid_titles(self) -> int:
        """Drop saved pairs whose question titles parse as inverted or past.

        Older code added pairs based on Polymarket's ``endDate`` field even
        when the resolution criterion parsed from the title was different
        (e.g. "by May 8" market with endDate=May 31 → mis-sorted ahead of
        "by May 15"). After upgrading the parser, we want those bad
        entries gone NOW, not in 5 missing-scan cycles.

        Title-only check — no API calls, no orderbook fetches. Drops a
        pair when:
          - both titles parse to a resolution date AND early >= late
          - early's parsed date is already past
        Pairs without a parseable deadline (rare — "before his term
        ends") are LEFT alone; they fall back to the existing missing-
        scans purge path.
        """
        from datetime import datetime, timezone
        now_utc = datetime.now(timezone.utc)
        # Pull raw endDates from the price snapshot — they're stored
        # there per pair when the monitoring loop builds an entry. Using
        # this to cross-check stale-but-still-active markets like the
        # Hormuz "by end of April" leg that has endDate=2026-04-30 even
        # though the title parses (after year-bump) to 2027-04-30.
        snap = {}
        try:
            import os
            if os.path.exists(self.PRICE_SNAPSHOT_FILE):
                with open(self.PRICE_SNAPSHOT_FILE, "r", encoding="utf-8") as f:
                    snap = json.load(f) or {}
        except Exception:
            snap = {}
        kept: List[Dict[str, Any]] = []
        dropped: List[Dict[str, Any]] = []
        for p in self.discovered_pairs:
            eq = p.get("early_question") or ""
            lq = p.get("late_question") or ""
            method = p.get("discovery_method")
            # Drop legacy artifacts: pairs with no late_question saved AND
            # no discovery_method are leftover entries from the original LLM
            # clustering pass (e.g. "Billionaire wealth tax on California
            # ballot" matched against "passes in California election 2026"
            # — different RESOLUTION criteria, not just different deadlines,
            # so calendar-arb math doesn't apply). Once these legacy "?"
            # pairs are removed they won't be re-discovered by the strict
            # series/intra_event/regex paths.
            if not lq and not method:
                dropped.append(p); continue
            # Snapshot-anchored stale check: if either leg's raw endDate
            # is already past, the market is being finalized (or worse,
            # forgotten by Polymarket) and its orderbook quotes are
            # untrustworthy. Drop the pair.
            pair_key = self._pair_key(p.get("early_id", ""), p.get("late_id", ""))
            s = snap.get(pair_key) or {}
            stale = False
            for fld in ("early_end", "late_end"):
                v = s.get(fld)
                if not v: continue
                d = self._parse_end_date(v)
                if d is not None and d <= now_utc:
                    stale = True
                    break
            if stale:
                dropped.append(p); continue
            e_res = self._resolution_date_from_question(eq)
            l_res = self._resolution_date_from_question(lq)
            # Keep pairs we can't fully parse — let the slow path handle them
            if e_res is None or l_res is None:
                kept.append(p); continue
            if e_res >= l_res or e_res <= now_utc:
                dropped.append(p)
            else:
                kept.append(p)
        if not dropped:
            return 0
        self.discovered_pairs = kept
        # Also remove from confirmed/pending/rejected so the dashboard
        # stops showing ghost cards for them.
        dropped_keys = {self._pair_key(p["early_id"], p["late_id"]) for p in dropped}
        self.confirmed_pairs = {k: v for k, v in self.confirmed_pairs.items() if k not in dropped_keys}
        self.pending_pairs   = {k: v for k, v in self.pending_pairs.items()   if k not in dropped_keys}
        self.rejected_pairs  = {k: v for k, v in self.rejected_pairs.items()  if k not in dropped_keys}
        self._save_discovered_pairs()
        self._save_json_state(self.CONFIRMED_FILE, self.confirmed_pairs)
        self._save_json_state(self.PENDING_FILE,   self.pending_pairs)
        self._save_json_state(self.REJECTED_FILE,  self.rejected_pairs)
        sample = ", ".join((p.get("early_question") or "")[:40] for p in dropped[:3])
        self.logger.info(
            f"🧹 Purged {len(dropped)} pair(s) with inverted/past titles. Sample: {sample}"
        )
        return len(dropped)

    def _purge_pairs_with_stale_markets(self, market_map: Dict[str, Dict]) -> int:
        """Purge saved pairs whose live market endDate is already past.

        Complements the title-based purge: that runs before market fetch
        and catches pairs by their saved questions, but it can't see
        Polymarket's actual ``endDate`` when the snapshot for the pair
        hasn't been built yet (e.g. a brand-new pair the monitoring
        loop just rejected on validation). This pass runs AFTER market
        fetch — using the live ``market_map`` — so any stale-market
        pair is dropped on the same scan instead of waiting 5 missing-
        scan cycles via _cleanup_expired_pairs.
        """
        from datetime import datetime, timezone
        now_utc = datetime.now(timezone.utc)
        kept: List[Dict[str, Any]] = []
        dropped: List[Dict[str, Any]] = []
        for p in self.discovered_pairs:
            early = market_map.get(p.get("early_id"))
            late  = market_map.get(p.get("late_id"))
            stale = False
            for m in (early, late):
                if not m: continue
                d = self._parse_end_date(m.get("endDate"))
                if d is not None and d <= now_utc:
                    stale = True
                    break
            if stale:
                dropped.append(p)
            else:
                kept.append(p)
        if not dropped:
            return 0
        self.discovered_pairs = kept
        dropped_keys = {self._pair_key(p["early_id"], p["late_id"]) for p in dropped}
        self.confirmed_pairs = {k: v for k, v in self.confirmed_pairs.items() if k not in dropped_keys}
        self.pending_pairs   = {k: v for k, v in self.pending_pairs.items()   if k not in dropped_keys}
        self.rejected_pairs  = {k: v for k, v in self.rejected_pairs.items()  if k not in dropped_keys}
        self._save_discovered_pairs()
        self._save_json_state(self.CONFIRMED_FILE, self.confirmed_pairs)
        self._save_json_state(self.PENDING_FILE,   self.pending_pairs)
        self._save_json_state(self.REJECTED_FILE,  self.rejected_pairs)
        sample = ", ".join((p.get("early_question") or "")[:40] for p in dropped[:3])
        self.logger.info(
            f"🧹 Purged {len(dropped)} pair(s) with stale market endDates. Sample: {sample}"
        )
        return len(dropped)

    async def scan(self) -> List[Dict[str, Any]]:
        # Fast pre-flight: drop any saved pair whose question titles already
        # parse as inverted (early resolution > late) or past. These were
        # added by older code that trusted Polymarket's endDate field even
        # when the resolution criterion (parsed from the title) was
        # different. Without this immediate purge they'd persist for 5
        # missing-scan cycles before _cleanup_expired_pairs caught them
        # — meanwhile producing fake +X% locked-profit cards. Title-only
        # check, no orderbook fetches, runs in microseconds.
        self._purge_pairs_with_invalid_titles()

        all_markets = self.scanner.get_all_active_markets(max_markets=5000)
        if not all_markets:
            return []

        # Cache the market_map for use by _check_escalations without re-fetching.
        active_ids = {m['id'] for m in all_markets}
        market_map = {m['id']: m for m in all_markets}
        # Cache for _check_escalations (avoids a second scanner fetch).
        self._last_market_map = market_map

        # Second purge pass — uses live market_map endDate to drop pairs
        # with markets whose Polymarket endDate is already past, even if
        # the title parser bumped the year (e.g. Hormuz "by end of April"
        # whose title parses to 2027 but whose Polymarket endDate is
        # 2026-04-30 already past). The title-only purge above couldn't
        # see this; here we have the live truth.
        self._purge_pairs_with_stale_markets(market_map)

        # --- Discovery from Polymarket events metadata (ground truth) ---
        # Two complementary shapes — both are calendar arbitrage that the
        # operator has already declared share resolution criteria. Fetch
        # the events once, pass to both methods so we don't pay for the
        # HTTP call twice.
        # max_events=10000: Polymarket reports ~10k active+open events
        # globally, and tail-end multi-deadline chains (e.g.
        # trump-announces-us-blockade-of-hormuz-lifted-by, 4 active
        # markets) sit beyond offset 3000. The earlier 3000 cap was
        # silently dropping them. The added pagination cost is ~8s per
        # scan but the data flows through both discovery passes for free.
        try:
            _events = self.scanner.get_all_active_events(max_events=10000)
        except Exception as e:
            self.logger.warning(f"Event fetch failed (series + intra-event skipped): {e}")
            _events = []

        # Series shape: each deadline = its own event sharing a series slug
        # (e.g. russia-x-ukraine-ceasefire across 6 events).
        self._series_discover_pairs(market_map=market_map, events=_events)

        # Intra-event shape: ONE event with N binary markets inside, each at
        # a different deadline (e.g. trump-announces-us-blockade-of-hormuz-
        # lifted-by — 12 markets, no series field). Without this path the
        # bot misses dozens of valid calendar chains.
        self._intra_event_discover_pairs(market_map=market_map, events=_events)

        # --- Regex-based Discovery (AI-free pre-pass) ---
        # Catches pairs whose titles are identical after date-stripping. Runs
        # every scan, regardless of LLM availability, so the bot keeps finding
        # obvious opportunities even if GEMINI_API_KEY is unset/invalid.
        self._regex_discover_obvious_pairs(all_markets)

        # Idempotent backfill: any LLM-discovered pair that predates the
        # pre-trade-verification gate (so it's sitting at tier "probe" but
        # never got a Telegram alert) is migrated into pending_pairs now.
        # No-op for regex pairs (already confirmed) and for already-
        # decided pairs (in confirmed / rejected / pending).
        self._migrate_probe_llm_pairs_to_pending()

        # --- Discovery Phase (LLM) ---
        if self.use_llm and self._llm_agent:
            start = self.market_offset
            end = min(start + self.llm_batch_size, len(all_markets))
            batch = all_markets[start:end]
            
            self.logger.info(f"📦 Discovery: Markets {start}-{end} / {len(all_markets)}")
            try:
                new_pairs, raw_text = await self._llm_agent.cluster_markets_debug(
                    batch,
                    min_resolution_confidence=self.min_resolution_match_confidence,
                )

                existing_keys = {
                    tuple(sorted((p.get('early_id', ''), p.get('late_id', ''))))
                    for p in self.discovered_pairs
                }
                import time as _time
                now_ts = _time.time()
                pending_changed = False
                for b_early, b_late, reason, confidence in new_pairs:
                    early_id, late_id = batch[b_early]['id'], batch[b_late]['id']
                    pair_key = tuple(sorted((early_id, late_id)))
                    if pair_key in existing_keys:
                        continue
                    existing_keys.add(pair_key)
                    self.discovered_pairs.append({
                        "early_id": early_id,
                        "late_id": late_id,
                        "description": reason,
                        "early_question": batch[b_early]['question'],
                        "resolution_match_confidence": confidence,
                    })
                    self.logger.info(
                        f"✨ New pair (conf={confidence:.2f}): {batch[b_early]['question']}"
                    )
                    # Route LLM-discovered pairs through a pre-trade Telegram
                    # verification gate. Regex matches go straight to
                    # confirmed_pairs (see _regex_discover_obvious_pairs) —
                    # only uncertain LLM matches need human sign-off. The
                    # pair sits at tier "pending" (size=0 via _size_for_tier)
                    # until the user replies ✅ or ❌.
                    str_key = self._pair_key(early_id, late_id)
                    if (str_key not in self.confirmed_pairs
                        and str_key not in self.rejected_pairs
                        and str_key not in self.pending_pairs):
                        self.pending_pairs[str_key] = {
                            "opened_at": now_ts,
                            "early_id": early_id,
                            "late_id": late_id,
                            "early_question": batch[b_early].get("question", ""),
                            "late_question": batch[b_late].get("question", ""),
                            "llm_reason": reason,
                            "llm_confidence": confidence,
                            "discovery_method": "llm",
                            "source": "pre_trade_verification",
                            "alerted": False,
                        }
                        pending_changed = True
                self._save_discovered_pairs()
                if pending_changed:
                    self._save_json_state(self.PENDING_FILE, self.pending_pairs)
            except Exception as e:
                self.logger.error(f"❌ LLM failed: {e}")

            self.market_offset = end
            if self.market_offset >= len(all_markets):
                self.market_offset = 0
                # Cleanup now runs every scan at the top of scan(), no
                # need for an extra pass on LLM cycle wraparound.

        # --- Monitoring Phase ---
        self.logger.info(f"📈 Checking prices for {len(self.discovered_pairs)} saved pairs...")
        opportunities = []
        import time as _time
        price_snapshot: Dict[str, Any] = {}
        # Pair keys we successfully priced this scan. Anything NOT in this
        # set (market missing, temporal containment failed, bad token IDs)
        # will have its missing_scans counter bumped by _cleanup_expired_pairs
        # at the end of this scan.
        healthy_pair_keys: set = set()
        snapshot_now = _time.time()

        for pair in self.discovered_pairs:
            early, late = market_map.get(pair['early_id']), market_map.get(pair['late_id'])
            if not early or not late: continue

            if not self._validate_temporal_containment(early, late):
                continue

            tid_early, tid_late = self._get_token_ids(early), self._get_token_ids(late)
            if len(tid_early) < 2 or len(tid_late) < 2: continue

            yes_early, no_early = tid_early[0], tid_early[1]
            yes_late,  no_late  = tid_late[0], tid_late[1]
            # The arb leg we trade: NO on early + YES on late.
            ask_no, ask_yes = self._best_ask(no_early), self._best_ask(yes_late)
            bid_no, bid_yes = self._best_bid(no_early), self._best_bid(yes_late)
            # The OTHER side of each market — fetched so the dashboard can
            # show full YES-bid/YES-ask/NO-bid/NO-ask for both legs (the
            # same view Polymarket itself shows). Cheap because the
            # orderbook is per-token and we already have the IDs.
            ask_yes_e = self._best_ask(yes_early); bid_yes_e = self._best_bid(yes_early)
            ask_no_l  = self._best_ask(no_late);   bid_no_l  = self._best_bid(no_late)

            pair_key = self._pair_key(pair['early_id'], pair['late_id'])
            days = self._days_until_close(late.get("endDate"))
            tier = self._get_tier_status(pair['early_id'], pair['late_id'])

            # Build snapshot entry for this pair (even if no arb opportunity right
            # now — the dashboard still wants to render it).
            early_q = early.get('question', '')
            late_q = late.get('question', '')
            # Cap descriptions — some Gamma entries run many KB and we don't
            # want the snapshot (or the translator request) to blow up.
            early_desc = (early.get('description') or '')[:400]
            late_desc = (late.get('description') or '')[:400]

            snap_entry: Dict[str, Any] = {
                "pair_key": pair_key,
                "early_id": pair['early_id'],
                "late_id": pair['late_id'],
                "early_question": early_q,
                "late_question": late_q,
                "early_desc": early_desc,
                "late_desc": late_desc,
                "early_end": early.get('endDate'),
                "late_end": late.get('endDate'),
                "days_until_close": days,
                "tier": tier,
                "discovery_method": pair.get("discovery_method", "llm"),
                "resolution_match_confidence": pair.get("resolution_match_confidence"),
                # Slugs power per-leg Polymarket links on the dashboard. We
                # refresh them every scan from the live market map so legacy
                # pairs pick them up without a migration.
                "early_slug": early.get("slug"),
                "late_slug": late.get("slug"),
                "early_event_slug": self._event_slug(early),
                "late_event_slug": self._event_slug(late),
                "updated_at": snapshot_now,
            }
            # Attach cached Hebrew translations (no-op if translator is
            # disabled). Missing strings get queued for the next flush so
            # they'll appear on a later scan.
            if self.translator is not None:
                for f_src, f_he in (
                    ("early_question", "early_question_he"),
                    ("late_question", "late_question_he"),
                    ("early_desc", "early_desc_he"),
                    ("late_desc", "late_desc_he"),
                ):
                    src = snap_entry.get(f_src) or ""
                    if src:
                        self.translator.queue(src)
                        he = self.translator.lookup(src)
                        if he:
                            snap_entry[f_he] = he
            # Per-leg pricing for the dashboard. Show the full YES/NO
            # ask+bid for both markets — the same view Polymarket itself
            # exposes — so the user can see exactly what each leg looks
            # like, not just the trade-relevant pair (NO_early + YES_late).
            #
            # Naming convention: ``<side>_<token>_<leg>`` where:
            #   side  ∈ {ask, bid}     (price you'd pay / receive)
            #   token ∈ {yes, no}      (which outcome)
            #   leg   ∈ {early, late}  (which market)
            #
            # Legacy keys ``ask_no_early`` and ``ask_yes_late`` are kept
            # so existing dashboard code that reads them still works.
            def _price(q):
                return q["price"] if q else None
            snap_entry["ask_yes_early"] = _price(ask_yes_e)
            snap_entry["bid_yes_early"] = _price(bid_yes_e)
            snap_entry["ask_no_early"]  = _price(ask_no)
            snap_entry["bid_no_early"]  = _price(bid_no)
            snap_entry["ask_yes_late"]  = _price(ask_yes)
            snap_entry["bid_yes_late"]  = _price(bid_yes)
            snap_entry["ask_no_late"]   = _price(ask_no_l)
            snap_entry["bid_no_late"]   = _price(bid_no_l)
            if ask_no and ask_yes:
                total_cost = ask_no["price"] + ask_yes["price"]
                snap_entry.update({
                    "total_cost": total_cost,
                    "entry_profit_usd": round(1.0 - total_cost, 4),
                    "entry_profit_pct": round((1.0 - total_cost) * 100, 2),
                    "annualized_roi": round(
                        self._calculate_annualized_roi(1.0 - total_cost, days) * 100, 2
                    ),
                })
            if bid_no and bid_yes:
                exit_value = bid_no["price"] + bid_yes["price"]
                snap_entry["exit_value"] = round(exit_value, 4)
            price_snapshot[pair_key] = snap_entry
            healthy_pair_keys.add(pair_key)

            # Arb decision path (unchanged from previous version)
            if not ask_no or not ask_yes: continue
            total_cost = ask_no["price"] + ask_yes["price"]
            if total_cost < (1.0 - (self.min_profit_threshold + 2*self.estimated_fee)):
                if self._has_invalid_risk(early) or self._has_invalid_risk(late): continue

                roi = self._calculate_annualized_roi(1.0 - total_cost, days)
                if roi >= self.min_annualized_roi:
                    if tier == "rejected":
                        continue  # user-blacklisted
                    if tier == "pending":
                        continue  # awaiting user reply → don't add more positions
                    # Tier is "probe" or "confirmed" — size accordingly
                    desired_size = self._size_for_tier(tier, total_cost)
                    # Cap by orderbook depth
                    max_book_size = min(ask_no.get("size", 0), ask_yes.get("size", 0))
                    size = min(desired_size, max_book_size)
                    if size <= 0:
                        continue
                    opportunities.append({
                        "token_id": f"{no_early}:{yes_late}",
                        "no_early_token": no_early, "yes_late_token": yes_late,
                        "ask_no_early": ask_no["price"], "ask_yes_late": ask_yes["price"],
                        "total_cost": total_cost,
                        "days_until_close": days,
                        "size": size,
                        "tier": tier,
                        "pair_key": pair_key,
                        "early_id": pair['early_id'], "late_id": pair['late_id'],
                        "early_desc": early.get("description", ""),
                        "late_desc": late.get("description", ""),
                        "early_end": early.get("endDate"), "late_end": late.get("endDate"),
                        "annualized_roi": roi, "llm_reason": pair.get("description", ""),
                        "early_question": early['question'], "late_question": late['question']
                    })

        # Persist snapshot for the dashboard
        self._save_json_state(self.PRICE_SNAPSHOT_FILE, price_snapshot)

        # Bump miss-counters and purge anything that's been unhealthy for
        # PURGE_AFTER_MISSING_SCANS consecutive scans. Runs after the
        # monitoring loop so we have an accurate healthy_pair_keys set.
        self._cleanup_expired_pairs(healthy_pair_keys)

        # Human-in-the-loop Telegram flow. Both of these also live in the
        # legacy run() loop, but BaseStrategy.scan_loop() never calls run()
        # — so without wiring them here, the bot would silently never send
        # pair-alerts or process ✅/❌ replies.
        await self._check_escalations(market_map)
        await self._process_telegram_replies()

        # Flush any Hebrew translations queued while building snap_entries.
        # Throttled internally to one Gemini batch per scan to respect the
        # shared free-tier quota.
        if self.translator is not None:
            try:
                await self.translator.flush()
            except Exception as e:
                self.logger.debug(f"Translator flush failed: {e}")

        # Dashboard heartbeat — writes data/status_snapshot.json.
        await self._write_heartbeat_snapshot()
        return opportunities

    def _migrate_probe_llm_pairs_to_pending(self):
        """Ensure every LLM-discovered pair that isn't already decided sits
        in pending_pairs awaiting ✅/❌. Idempotent — called each scan so
        pairs from before the pre-trade-verification gate was introduced
        still end up getting their alert."""
        import time as _time
        now = _time.time()
        changed = False
        for p in self.discovered_pairs:
            method = p.get("discovery_method", "llm")
            if "regex" in method:
                continue  # regex pairs are auto-trusted — see _regex_discover_obvious_pairs
            early_id = p.get("early_id")
            late_id = p.get("late_id")
            if not early_id or not late_id:
                continue
            key = self._pair_key(early_id, late_id)
            if (key in self.confirmed_pairs
                or key in self.rejected_pairs
                or key in self.pending_pairs):
                continue
            self.pending_pairs[key] = {
                "opened_at": now,
                "early_id": early_id,
                "late_id": late_id,
                "early_question": p.get("early_question", ""),
                "llm_reason": p.get("description", ""),
                "llm_confidence": p.get("resolution_match_confidence"),
                "discovery_method": method,
                "source": "pre_trade_verification",
                "alerted": False,
            }
            changed = True
        if changed:
            self._save_json_state(self.PENDING_FILE, self.pending_pairs)

    async def _write_heartbeat_snapshot(self):
        """Writes data/status_snapshot.json with balance + stats + strategy
        config so the dashboard can render a fresh overview every 10s
        without re-reading the bot log.

        Also registers this strategy in the shared ``strategies`` section
        so duplicate_arb (or any future strategy) can co-exist. Failures
        are swallowed at WARNING level — a broken heartbeat must never
        take the bot down."""
        try:
            import time as _time
            bal = await self.executor.get_balance()
            cal_stats = {
                "label": "Calendar",
                "discovered": len(self.discovered_pairs),
                "confirmed": len(self.confirmed_pairs),
                "pending": len(self.pending_pairs),
                "rejected": len(self.rejected_pairs),
                "trades_entered": int(self.stats.get("trades_entered", 0)),
                "trades_exited": int(self.stats.get("trades_exited", 0)),
                "open_positions": len(getattr(self, "open_positions", {}) or {}),
                "loop": int(self.stats.get("scans", 0)),
            }
            # Register in shared strategies section (singleton heartbeat)
            try:
                from core.heartbeat import MultiStrategyHeartbeat
                MultiStrategyHeartbeat.instance().write(
                    strategy_key="calendar_arb",
                    balance_usd=float(bal) if bal is not None else None,
                    stats=cal_stats,
                )
            except Exception:
                pass

            # Preserve the original flat format so the existing dashboard views
            # (which read heartbeat.stats, heartbeat.pair_counts, heartbeat.strategy)
            # keep working.
            snapshot = self._load_json_state(os.path.join("data", "status_snapshot.json")) or {}
            if not isinstance(snapshot, dict):
                snapshot = {}
            snapshot.update({
                "balance_usd": float(bal) if bal is not None else snapshot.get("balance_usd"),
                "updated_at": _time.time(),
                "loop": int(self.stats.get("scans", 0)),
                "scan_interval_s": int(self.scan_interval),
                "stats": {
                    "trades_entered": int(self.stats.get("trades_entered", 0)),
                    "trades_exited": int(self.stats.get("trades_exited", 0)),
                },
                "open_positions": len(getattr(self, "open_positions", {}) or {}),
                "pair_counts": {
                    "discovered": len(self.discovered_pairs),
                    "confirmed": len(self.confirmed_pairs),
                    "pending": len(self.pending_pairs),
                    "rejected": len(self.rejected_pairs),
                },
                "dry_run": bool(getattr(self, "dry_run", False)),
                "strategy": {
                    "name": "CalendarArbitrage",
                    "dry_run": bool(getattr(self, "dry_run", False)),
                    "min_profit_threshold": float(self.min_profit_threshold),
                    "early_exit_threshold": float(self.early_exit_threshold),
                    "min_annualized_roi": float(self.min_annualized_roi),
                    "estimated_fee": float(self.estimated_fee),
                    "probe_usd": float(self.probe_usd),
                    "confirmed_usd": float(self.confirmed_usd),
                    "llm_model": str(self.llm_model) if self.use_llm else None,
                },
            })
            # Preserve the "strategies" section the shared heartbeat just wrote
            self._save_json_state(
                os.path.join("data", "status_snapshot.json"), snapshot
            )
        except Exception as e:
            self.logger.warning(f"Heartbeat snapshot failed: {e}")

    async def should_enter(self, opportunity: Dict[str, Any]) -> bool:
        # Basic sanity + we already used orderbook asks, so thresholds are conservative
        return True

    async def _emergency_sell(self, token_id: str, size: float) -> bool:
        """Best-effort rollback: sell `size` of `token_id` using aggressive IOC orders.

        Walks the bid book via _simulate_fill to pick a limit that should fully
        fill, then submits IOC so unmatched size cancels instead of lingering as
        a stale limit order. Retries twice at progressively lower prices if the
        first attempt doesn't fully fill."""
        attempts = []
        for price_floor_ratio in (0.95, 0.70, 0.30):
            sim = self._simulate_fill(token_id, "SELL", size)
            if sim and sim.get("avg_price"):
                # Use the worst price needed to fill the size, with safety buffer below
                limit_price = max(0.01, float(sim["avg_price"]) * price_floor_ratio)
            else:
                bid = self._best_bid(token_id)
                limit_price = max(0.01, float(bid["price"]) * price_floor_ratio) if bid else 0.01

            attempts.append(limit_price)
            try:
                result = await self.executor.execute_trade(
                    token_id=token_id, side="SELL", size=size, price=limit_price, order_type="IOC"
                )
            except Exception as e:
                self.logger.error(f"🚨 Rollback attempt exception (price={limit_price:.4f}): {e}")
                continue

            if result and result.get("success"):
                filled = float(result.get("sizeFilled", 0))
                if filled >= size * 0.99:
                    self.logger.info(f"✅ Rollback filled: {filled:.2f}/{size:.2f} @ ${limit_price:.4f}")
                    return True
                self.logger.warning(
                    f"⚠️ Partial rollback: {filled:.2f}/{size:.2f} @ ${limit_price:.4f} — retrying lower"
                )
                size -= filled  # reduce remaining size for next attempt
                if size <= 0:
                    return True

        self.logger.critical(
            f"🚨 ROLLBACK EXHAUSTED for {token_id[:12]} — tried prices {attempts}. "
            f"MANUAL INTERVENTION REQUIRED: hold {size:.2f} units open."
        )
        return False

    async def enter_position(self, opportunity: Dict[str, Any]) -> bool:
        """ביצוע שתי העסקאות במקביל: קניית NO במוקדם ו-YES במאוחר עם טיפול בסיכון רגליים."""
        no_early_token = opportunity["no_early_token"]
        yes_late_token = opportunity["yes_late_token"]
        size = float(opportunity.get("size", 1.0))
        price_no_early = float(opportunity["ask_no_early"])  # pay ask
        price_yes_late = float(opportunity["ask_yes_late"])  # pay ask

        # Simulate fills for slippage protection
        fill_no = self._simulate_fill(no_early_token, "BUY", size)
        fill_yes = self._simulate_fill(yes_late_token, "BUY", size)

        # Check if we can actually fill both legs
        if not fill_no or not fill_yes:
            self.logger.warning("⚠️ Cannot simulate fills - insufficient orderbook data")
            return False

        if not fill_no.get("fully_filled") or not fill_yes.get("fully_filled"):
            self.logger.warning(
                f"⚠️ Insufficient liquidity: NO={fill_no.get('filled_size'):.1f}/{size:.1f}, "
                f"YES={fill_yes.get('filled_size'):.1f}/{size:.1f}"
            )
            return False

        # Calculate total cost with slippage. Include a buffer for exit-side slippage
        # in case we ever early-exit instead of holding to resolution.
        total_cost_with_slippage = fill_no["avg_price"] + fill_yes["avg_price"]
        min_profit_total = self.min_profit_threshold + (4 * self.estimated_fee)  # 2 entry + 2 exit legs

        if total_cost_with_slippage >= (1.0 - min_profit_total):
            self.logger.warning(
                f"⚠️ Slippage kills profit: ${total_cost_with_slippage:.4f} >= ${1.0 - min_profit_total:.4f} "
                f"(threshold now includes exit-leg buffer)"
            )
            return False

        # CRITICAL: pre-trade balance check. Prevents submitting the first leg and
        # then discovering we can't afford the second.
        required_usdc = (fill_no["avg_price"] + fill_yes["avg_price"]) * size
        try:
            balance = await self.executor.get_balance()
        except Exception as e:
            self.logger.error(f"⚠️ Balance check failed, refusing to trade: {e}")
            return False
        # Keep a small buffer for rounding/fees on the exchange side.
        if balance < required_usdc * 1.02:
            self.logger.warning(
                f"⚠️ Insufficient USDC: balance=${balance:.2f} < required=${required_usdc * 1.02:.2f} "
                f"(size={size}, combined_ask=${(fill_no['avg_price'] + fill_yes['avg_price']):.4f})"
            )
            return False

        tier = opportunity.get("tier", "probe")
        self.logger.info(f"🧮 Calendar Arbitrage Opportunity [tier={tier.upper()}]:")
        self.logger.info(f"   Early(NO) ask: ${price_no_early:.4f} (avg: ${fill_no['avg_price']:.4f})")
        self.logger.info(f"   Late(YES) ask: ${price_yes_late:.4f} (avg: ${fill_yes['avg_price']:.4f})")
        self.logger.info(f"   Total cost: ${opportunity['total_cost']:.4f} | With slippage: ${total_cost_with_slippage:.4f}")
        self.logger.info(f"   Annualized ROI: {opportunity.get('annualized_roi', 0):.1%} ({opportunity.get('days_until_close', 0):.1f} days)")
        self.logger.info(f"   Balance: ${balance:.2f} | Required: ${required_usdc:.2f} | Size: {size}")
        self.logger.info(f"   Early: {opportunity['early_question'][:60]}")
        self.logger.info(f"   Late:  {opportunity['late_question'][:60]}")

        # Execute both legs concurrently. FOK (Fill-Or-Kill) guarantees all-or-nothing
        # per leg at the limit price — no partial fills, no lingering limit orders.
        tasks = [
            self.executor.execute_trade(
                token_id=no_early_token, side="BUY", size=size, price=fill_no["avg_price"], order_type="FOK"
            ),
            self.executor.execute_trade(
                token_id=yes_late_token, side="BUY", size=size, price=fill_yes["avg_price"], order_type="FOK"
            ),
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Check both success flag AND filled size to guard against brokers that
        # return success on partial fills.
        def leg_ok(res, expected_size):
            if not isinstance(res, dict) or not res.get("success"):
                return False
            filled = float(res.get("sizeFilled", 0))
            return filled >= expected_size * 0.99

        no_early_success = leg_ok(results[0], size)
        yes_late_success = leg_ok(results[1], size)

        # Handle leg risk - rollback via _emergency_sell which uses IOC + price ladder
        if no_early_success and not yes_late_success:
            self.logger.error("❌ YES leg failed or partial — rolling back NO position")
            await self._emergency_sell(no_early_token, size)
            return False

        elif yes_late_success and not no_early_success:
            self.logger.error("❌ NO leg failed or partial — rolling back YES position")
            await self._emergency_sell(yes_late_token, size)
            return False

        elif not no_early_success and not yes_late_success:
            self.logger.error("❌ Both legs failed (or partially filled)")
            # Defensive: even if success=False, some brokers may have filled partially.
            for res, tok in ((results[0], no_early_token), (results[1], yes_late_token)):
                if isinstance(res, dict) and float(res.get("sizeFilled", 0)) > 0:
                    self.logger.warning(f"   Partial fill detected on {tok[:12]} — emergency-selling")
                    await self._emergency_sell(tok, float(res["sizeFilled"]))
            return False
        
        # Both succeeded
        import time as _time
        entry_wall_time = _time.time()
        group_id = f"CAL-{no_early_token[:6]}-{yes_late_token[:6]}"
        pos_data = {
            **opportunity,
            "entry_time": asyncio.get_event_loop().time(),
            "entry_wall_time": entry_wall_time,
            "size": size,
            "strategy_name": self.strategy_name,
            "group_id": group_id,
            "actual_entry_cost": total_cost_with_slippage,  # Track actual cost
        }

        # Tier bookkeeping — if this was a probe entry, track it for escalation.
        pair_key = opportunity.get("pair_key")
        if pair_key and tier == "probe":
            self.pending_pairs.setdefault(pair_key, {
                "opened_at": entry_wall_time,
                "alerted": False,
                "early_id": opportunity.get("early_id"),
                "late_id": opportunity.get("late_id"),
                "early_question": opportunity.get("early_question"),
                "late_question": opportunity.get("late_question"),
                "probe_size": size,
                "probe_cost": total_cost_with_slippage,
            })
            self._save_json_state(self.PENDING_FILE, self.pending_pairs)
            self.logger.info(f"🧪 Probe opened for pair_key={pair_key} (escalation in {self.escalation_seconds/60:.0f} min)")
        
        # Save to database if enabled
        if self.use_database and self.db:
            try:
                # Create position records
                no_pos_id = await self.db.create_position(
                    strategy=self.strategy_name,
                    token_id=no_early_token,
                    side="BUY",
                    size=size,
                    entry_price=fill_no["avg_price"],
                    metadata={"leg": "NO_early", "group_id": group_id, **opportunity},
                )
                yes_pos_id = await self.db.create_position(
                    strategy=self.strategy_name,
                    token_id=yes_late_token,
                    side="BUY",
                    size=size,
                    entry_price=fill_yes["avg_price"],
                    metadata={"leg": "YES_late", "group_id": group_id, **opportunity},
                )
                
                # Record trades
                await self.db.record_trade(
                    position_id=no_pos_id,
                    strategy=self.strategy_name,
                    token_id=no_early_token,
                    side="BUY",
                    size=size,
                    price=fill_no["avg_price"],
                    fee=self.estimated_fee * size,
                )
                await self.db.record_trade(
                    position_id=yes_pos_id,
                    strategy=self.strategy_name,
                    token_id=yes_late_token,
                    side="BUY",
                    size=size,
                    price=fill_yes["avg_price"],
                    fee=self.estimated_fee * size,
                )
                
                self.logger.debug(f"💾 Saved positions to database: {no_pos_id}, {yes_pos_id}")
            except Exception as e:
                self.logger.warning(f"Failed to save to database: {e}")
        
        # Track both tokens under same group (in-memory fallback)
        self.open_positions[no_early_token] = pos_data
        self.open_positions[yes_late_token] = pos_data
        self.position_manager.add_position(token_id=no_early_token, entry_price=fill_no["avg_price"], size=size, metadata=pos_data)
        self.position_manager.add_position(token_id=yes_late_token, entry_price=fill_yes["avg_price"], size=size, metadata=pos_data)
        self.stats["trades_entered"] += 1
        self.logger.info("✅ Calendar arbitrage legs filled")
        return True

    async def should_exit(self, position: Dict[str, Any]) -> bool:
        """
        בודק האם ניתן לצאת ברווח לפני סגירת השוק (Settlement).
        Early exit based on BID prices (actual exit value).
        """
        no_early_token = position.get("no_early_token")
        yes_late_token = position.get("yes_late_token")
        entry_cost = position.get("total_cost")

        if not no_early_token or not yes_late_token or entry_cost is None:
            return False

        try:
            # Get current BID prices (what we can sell for)
            bid_no = self._best_bid(no_early_token)
            bid_yes = self._best_bid(yes_late_token)

            if not bid_no or not bid_yes:
                return False

            # Current exit value = sum of bids
            current_exit_value = bid_no["price"] + bid_yes["price"]
            
            # Exit threshold: entry cost + selling fees + small profit margin
            exit_threshold = entry_cost + (2 * self.estimated_fee) + self.early_exit_threshold

            # Exit if we can sell for profit
            if current_exit_value > exit_threshold:
                profit = current_exit_value - entry_cost - (2 * self.estimated_fee)
                self.logger.info(
                    f"💰 Early exit triggered! Exit value: ${current_exit_value:.4f} > "
                    f"Threshold: ${exit_threshold:.4f} (Profit: ${profit:.4f})"
                )
                return True

            # Also check if spread narrowed too much (loss prevention)
            if current_exit_value < entry_cost:
                loss = entry_cost - current_exit_value
                # Exit if loss exceeds threshold (e.g., 2%)
                if loss > 0.02:  # Max 2% loss tolerance
                    self.logger.warning(
                        f"📉 Early exit: spread reversed! Loss: ${loss:.4f} "
                        f"(Exit: ${current_exit_value:.4f} < Entry: ${entry_cost:.4f})"
                    )
                    return True

        except Exception as e:
            self.logger.debug(f"Error checking early exit: {e}")

        return False

    async def exit_position(self, token_id: str, exit_price: Optional[float] = None) -> bool:
        """
        ביצוע יציאה מוקדמת - מכירת שני צדי הארביטראז'.
        Executes early exit by selling both legs concurrently.
        """
        position = self.open_positions.get(token_id)
        if not position:
            return False

        no_early_token = position.get("no_early_token")
        yes_late_token = position.get("yes_late_token")
        size = position.get("size", 1.0)
        entry_cost = position.get("total_cost", 0)

        if not no_early_token or not yes_late_token:
            self.logger.error(f"Missing token IDs in position: {token_id}")
            return False

        self.logger.info(f"🚪 Executing early exit for {position.get('key', 'unknown')[:40]}")

        try:
            # Get current bid prices for logging
            bid_no = self._best_bid(no_early_token)
            bid_yes = self._best_bid(yes_late_token)
            
            if bid_no and bid_yes:
                exit_value = bid_no["price"] + bid_yes["price"]
                expected_pnl = exit_value - entry_cost - (2 * self.estimated_fee)
                self.logger.info(f"   Expected exit: ${exit_value:.4f} | P&L: ${expected_pnl:.4f}")

            # Execute SELL orders for both legs concurrently using IOC so unmatched
            # size cancels instead of lingering. Limit price uses simulated fill
            # (worst bid required to clear our size) — if book thins during the RTT
            # we fall back to _emergency_sell for each unfilled leg.
            sim_no = self._simulate_fill(no_early_token, "SELL", size)
            sim_yes = self._simulate_fill(yes_late_token, "SELL", size)
            no_price = (sim_no["avg_price"] if sim_no else (bid_no["price"] if bid_no else 0.01))
            yes_price = (sim_yes["avg_price"] if sim_yes else (bid_yes["price"] if bid_yes else 0.01))
            tasks = [
                self.executor.execute_trade(
                    token_id=no_early_token, side="SELL", size=size, price=no_price, order_type="IOC"
                ),
                self.executor.execute_trade(
                    token_id=yes_late_token, side="SELL", size=size, price=yes_price, order_type="IOC"
                ),
            ]

            results = await asyncio.gather(*tasks, return_exceptions=True)

            def _leg_fully_filled(res, expected):
                return (
                    isinstance(res, dict)
                    and res.get("success")
                    and float(res.get("sizeFilled", 0)) >= expected * 0.99
                )

            no_ok = _leg_fully_filled(results[0], size)
            yes_ok = _leg_fully_filled(results[1], size)

            # If either leg failed to fully close, escalate via _emergency_sell.
            if not no_ok:
                filled = float(results[0].get("sizeFilled", 0)) if isinstance(results[0], dict) else 0
                self.logger.warning(f"⚠️ NO exit partial ({filled:.2f}/{size:.2f}) — escalating")
                await self._emergency_sell(no_early_token, size - filled)
            if not yes_ok:
                filled = float(results[1].get("sizeFilled", 0)) if isinstance(results[1], dict) else 0
                self.logger.warning(f"⚠️ YES exit partial ({filled:.2f}/{size:.2f}) — escalating")
                await self._emergency_sell(yes_late_token, size - filled)

            if no_ok and yes_ok:
                # Calculate P&L
                exit_value = (bid_no["price"] if bid_no else 0) + (bid_yes["price"] if bid_yes else 0)
                pnl = exit_value - entry_cost - (2 * self.estimated_fee)
                
                # Save to database if enabled
                if self.use_database and self.db:
                    try:
                        # Get position IDs from database
                        no_pos = await self.db.get_position_by_token(no_early_token, self.strategy_name)
                        yes_pos = await self.db.get_position_by_token(yes_late_token, self.strategy_name)
                        
                        if no_pos:
                            await self.db.close_position(
                                position_id=no_pos["id"],
                                exit_price=bid_no["price"] if bid_no else 0,
                                pnl=pnl / 2,  # Split P&L between legs
                            )
                            await self.db.record_trade(
                                position_id=no_pos["id"],
                                strategy=self.strategy_name,
                                token_id=no_early_token,
                                side="SELL",
                                size=size,
                                price=bid_no["price"] if bid_no else 0,
                                fee=self.estimated_fee * size,
                            )
                        
                        if yes_pos:
                            await self.db.close_position(
                                position_id=yes_pos["id"],
                                exit_price=bid_yes["price"] if bid_yes else 0,
                                pnl=pnl / 2,
                            )
                            await self.db.record_trade(
                                position_id=yes_pos["id"],
                                strategy=self.strategy_name,
                                token_id=yes_late_token,
                                side="SELL",
                                size=size,
                                price=bid_yes["price"] if bid_yes else 0,
                                fee=self.estimated_fee * size,
                            )
                        
                        self.logger.debug(f"💾 Saved exit to database. P&L: {pnl:.4f}")
                    except Exception as e:
                        self.logger.warning(f"Failed to save exit to database: {e}")
                
                # Clean up positions from memory and manager
                self.open_positions.pop(no_early_token, None)
                self.open_positions.pop(yes_late_token, None)
                self.position_manager.remove_position(no_early_token)
                self.position_manager.remove_position(yes_late_token)
                
                self.stats["trades_exited"] += 1
                self.logger.info(f"✅ Successfully exited both legs (P&L: {pnl:.4f})")
                # Notify Telegram so the user sees capital rotations in real time
                if self.telegram and self.telegram.enabled:
                    try:
                        early_q = (position.get("early_question") or "")[:60]
                        pnl_pct = (pnl / entry_cost * 100) if entry_cost else 0.0
                        await self.telegram.send_notice(
                            f"💰 Early exit @ ${pnl:+.4f} ({pnl_pct:+.1f}%)\n"
                            f"   Early: {early_q}\n"
                            f"   Exit value ${exit_value:.4f} vs entry ${entry_cost:.4f} "
                            f"(size={size})"
                        )
                    except Exception as e:
                        self.logger.debug(f"Telegram exit notice failed: {e}")
                return True
            else:
                # Log failures
                for i, (leg_name, result) in enumerate([("NO_early", results[0]), ("YES_late", results[1])]):
                    if isinstance(result, Exception) or not result.get("success"):
                        self.logger.error(f"Failed to exit {leg_name}: {result}")
                
                self.logger.error("❌ Partial exit - manual intervention may be required")
                return False

        except Exception as e:
            self.logger.error(f"Error during exit execution: {e}")
            return False
    
    async def _check_escalations(self, market_map: Dict[str, Dict]):
        """For each pending (probe) pair, if older than escalation_seconds and
        the spread is still exploitable, send a Telegram alert for human review."""
        if not self.telegram or not self.telegram.enabled:
            return
        import time as _time
        now = _time.time()
        changed = False
        for pair_key, state in list(self.pending_pairs.items()):
            if state.get("alerted"):
                continue
            opened_at = state.get("opened_at", now)
            # Pre-trade verification pairs bypass the 30-min grace window —
            # they have no position yet, so we want the user to see them
            # immediately on the next scan. Probe-then-escalate pairs still
            # wait escalation_seconds so we don't spam at the 1-min mark.
            is_pre_trade = state.get("source") == "pre_trade_verification"
            if not is_pre_trade and now - opened_at < self.escalation_seconds:
                continue

            early_id = state.get("early_id")
            late_id = state.get("late_id")
            early = market_map.get(early_id)
            late = market_map.get(late_id)
            if not early or not late:
                self.logger.warning(f"Pending pair {pair_key} missing markets — dropping")
                self.pending_pairs.pop(pair_key, None)
                changed = True
                continue

            tid_early = self._get_token_ids(early)
            tid_late = self._get_token_ids(late)
            ask_no = ask_yes = None
            total_cost = None
            roi = None
            if len(tid_early) >= 2 and len(tid_late) >= 2:
                ask_no = self._best_ask(tid_early[1])
                ask_yes = self._best_ask(tid_late[0])
                if ask_no and ask_yes:
                    total_cost = ask_no["price"] + ask_yes["price"]
                    days = self._days_until_close(late.get("endDate"))
                    roi = self._calculate_annualized_roi(1.0 - total_cost, days)
            # Probe-escalation needs live pricing (the whole point is "is
            # the spread still exploitable?"). Pre-trade verification only
            # needs identity confirmation, so send even without a book.
            if (ask_no is None or ask_yes is None) and not is_pre_trade:
                continue

            alert_info = {
                "early_question": early.get("question", ""),
                "late_question": late.get("question", ""),
                "early_desc": early.get("description", ""),
                "late_desc": late.get("description", ""),
                "early_end": early.get("endDate"),
                "late_end": late.get("endDate"),
                "ask_no_early": ask_no["price"] if ask_no else 0,
                "ask_yes_late": ask_yes["price"] if ask_yes else 0,
                "total_cost": total_cost if total_cost is not None else 0,
                "annualized_roi": roi if roi is not None else 0,
            }
            ok = await self.telegram.send_pair_alert(pair_key, alert_info, strategy_label="Calendar")
            if ok:
                state["alerted"] = True
                state["alerted_at"] = now
                changed = True
                self.logger.info(f"📨 Telegram alert sent for pair_key={pair_key}")
        if changed:
            self._save_json_state(self.PENDING_FILE, self.pending_pairs)

    async def _process_telegram_replies(self):
        """Pull user decisions from Telegram and update confirmed/rejected state."""
        if not self.telegram or not self.telegram.enabled:
            return
        replies = await self.telegram.poll_replies()
        if not replies:
            return
        import time as _time
        now = _time.time()
        for reply in replies:
            state = self.pending_pairs.pop(reply.pair_key, None)
            if not state:
                self.logger.warning(f"Got Telegram reply for unknown pair_key={reply.pair_key}")
                continue
            if reply.decision == "approve":
                self.confirmed_pairs[reply.pair_key] = {
                    **state,
                    "confirmed_at": now,
                    "confirmed_by_user": reply.user_id,
                }
                self.logger.info(f"✅ Pair CONFIRMED by user: {reply.pair_key}")
            else:
                self.rejected_pairs[reply.pair_key] = {
                    **state,
                    "rejected_at": now,
                    "rejected_by_user": reply.user_id,
                }
                self.logger.info(f"❌ Pair REJECTED by user: {reply.pair_key}")
        self._save_json_state(self.PENDING_FILE, self.pending_pairs)
        self._save_json_state(self.CONFIRMED_FILE, self.confirmed_pairs)
        self._save_json_state(self.REJECTED_FILE, self.rejected_pairs)

    async def _on_websocket_price_update(self, token_id: str, prices: Dict[str, float]):
        """WebSocket callback: triggered on sub-second price updates.
        
        Checks if any open position should exit based on spread closure or loss prevention.
        """
        if not self.open_positions:
            return
        
        # Update price cache
        self.price_updates[token_id] = prices
        
        # Check each open position for early exit
        for no_early_token, position in list(self.open_positions.items()):
            yes_late_token = position.get("yes_late_token")
            if not yes_late_token:
                continue
            
            # Check if we should exit based on current WebSocket prices
            should_exit = await self.should_exit(position)
            if should_exit:
                self.logger.info(f"🔴 WebSocket early exit triggered for {no_early_token} <-> {yes_late_token}")
                # Schedule exit (fire and forget to avoid blocking price stream)
                asyncio.create_task(self.exit_position(no_early_token))
    
    async def run(self):
        """Main strategy loop: scan for opportunities and monitor for early exits with real-time WebSocket."""
        try:
            # Connect to database if enabled
            if self.use_database:
                self.logger.info("🗄️ Connecting to PostgreSQL database...")
                self.db = await get_database()
                if self.db:
                    self.logger.info("✅ Database connected - positions will be persisted")
                else:
                    self.logger.warning("⚠️ Database connection failed - using in-memory fallback")
                    self.use_database = False
            
            # Start WebSocket connection for real-time price monitoring
            self.logger.info("🔌 Starting WebSocket connection for real-time price monitoring...")
            self.ws_manager.set_price_update_callback(self._on_websocket_price_update)
            
            # Run WebSocket in background
            ws_task = asyncio.create_task(self.ws_manager.run())
            self.ws_running = True
            
            # Wait for WebSocket to connect before starting strategy loop
            await self.ws_manager.wait_connected()
            self.logger.info("✅ WebSocket connected - ready for real-time monitoring")
            
            # Main strategy loop
            loop_count = 0
            while self.running:
                try:
                    loop_count += 1
                    self.logger.info(f"\n{'='*60}")
                    self.logger.info(f"📊 Scan #{loop_count}")
                    self.logger.info(f"{'='*60}")
                    
                    # Full market scan
                    opportunities = await self.scan()

                    # Human-in-the-loop: poll for user Telegram replies, then
                    # check if any probes need escalation. Both are cheap; run
                    # every loop.
                    await self._process_telegram_replies()
                    await self._check_escalations(getattr(self, "_last_market_map", {}))

                    if opportunities:
                        self.logger.info(f"\n✨ Found {len(opportunities)} opportunity/opportunities:")
                        for idx, opp in enumerate(opportunities[:self.max_pairs], 1):
                            self.logger.info(f"\n  {idx}. [{opp.get('tier','probe').upper()}] NO_early: {opp['early_question']} (ask: {opp['ask_no_early']:.3f})")
                            self.logger.info(f"     YES_late: {opp['late_question']} (ask: {opp['ask_yes_late']:.3f})")
                            self.logger.info(f"     Total cost: ${opp.get('total_cost', 0):.4f}")
                            self.logger.info(f"     ROI (annualized): {opp.get('annualized_roi', 0):.1%}")

                            # Try to enter position
                            entered = await self.enter_position(opp)
                            if entered:
                                break  # Enter one position per scan
                    else:
                        self.logger.info("No opportunities found")
                    
                    # Log current positions
                    if self.open_positions:
                        self.logger.info(f"\n📍 Open positions: {len(self.open_positions)}")
                        for no_token, pos in self.open_positions.items():
                            yes_token = pos.get("yes_late_token", "?")
                            entry_cost = pos.get("entry_cost", 0)
                            self.logger.info(f"   {no_token} <-> {yes_token} (cost: ${entry_cost:.4f})")
                    
                    # Log stats
                    self.logger.info(f"\n📈 Stats: {self.stats['trades_entered']} entered, {self.stats['trades_exited']} exited")

                    # Heartbeat snapshot for the dashboard (balance + stats).
                    # Dashboard reads data/status_snapshot.json for a live
                    # balance readout — the bot itself otherwise only logs
                    # balance just before a trade.
                    try:
                        import time as _time
                        bal = await self.executor.get_balance()
                        snapshot = {
                            "balance_usd": float(bal) if bal is not None else None,
                            "updated_at": _time.time(),
                            "loop": loop_count,
                            "scan_interval_s": int(self.scan_interval),
                            "stats": {
                                "trades_entered": int(self.stats.get("trades_entered", 0)),
                                "trades_exited": int(self.stats.get("trades_exited", 0)),
                            },
                            "open_positions": len(self.open_positions),
                            "pair_counts": {
                                "discovered": len(self.discovered_pairs),
                                "confirmed": len(self.confirmed_pairs),
                                "pending": len(self.pending_pairs),
                                "rejected": len(self.rejected_pairs),
                            },
                            "strategy": {
                                "name": "CalendarArbitrage",
                                "min_profit_threshold": float(self.min_profit_threshold),
                                "early_exit_threshold": float(self.early_exit_threshold),
                                "min_annualized_roi": float(self.min_annualized_roi),
                                "estimated_fee": float(self.estimated_fee),
                                "probe_usd": float(self.probe_usd),
                                "confirmed_usd": float(self.confirmed_usd),
                                "llm_model": str(self.llm_model) if self.use_llm else None,
                            },
                        }
                        self._save_json_state(
                            os.path.join("data", "status_snapshot.json"), snapshot
                        )
                    except Exception as e:
                        self.logger.debug(f"Heartbeat snapshot failed: {e}")

                    # Wait for next scan
                    await asyncio.sleep(self.scan_interval)
                    
                except asyncio.CancelledError:
                    self.logger.info("Strategy run cancelled")
                    break
                except Exception as e:
                    self.logger.error(f"Error in strategy loop: {e}", exc_info=True)
                    await asyncio.sleep(5)  # Brief pause before retry
        
        finally:
            # Cleanup
            self.ws_running = False
            await self.ws_manager.close()
            self.logger.info("🛑 Strategy stopped")
