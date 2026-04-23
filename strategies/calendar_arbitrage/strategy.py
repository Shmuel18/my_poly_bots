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
        """Strict validation: early.endDate must be strictly earlier than late.endDate.

        This guards against false calendar-arb pairs where the LLM groups two markets
        as same-event but their actual deadlines don't satisfy the "early ⊂ late"
        containment property. Without this, we could buy a "guaranteed" arbitrage
        that isn't guaranteed at all."""
        early_end = self._parse_end_date(early.get("endDate"))
        late_end = self._parse_end_date(late.get("endDate"))
        if early_end is None or late_end is None:
            self.logger.debug(
                f"Rejected pair: missing endDate (early={early.get('endDate')}, late={late.get('endDate')})"
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
        try:
            book = self.executor.client.get_order_book(token_id)
            asks = self._orderbook_side(book, "asks")
            if asks:
                p, s = self._orderbook_entry(asks[0])
                if p is not None:
                    return {"price": p, "size": s or 0.0}
        except Exception as e:
            self.logger.debug(f"_best_ask failed for {token_id[:12]}: {e}")
            return None
        return None

    def _best_bid(self, token_id: str) -> Optional[Dict[str, float]]:
        """Get best bid price (price we can sell at)."""
        try:
            book = self.executor.client.get_order_book(token_id)
            bids = self._orderbook_side(book, "bids")
            if bids:
                p, s = self._orderbook_entry(bids[0])
                if p is not None:
                    return {"price": p, "size": s or 0.0}
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

            remaining_size = size
            total_cost = 0.0
            filled_size = 0.0
            first_price = None

            for order in orders:
                order_price, order_size = self._orderbook_entry(order)
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
            # Attach parsed end dates, drop markets without them
            dated = []
            for m in markets:
                d = self._parse_end_date(m.get('endDate'))
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
                        continue  # Same endDate or inverted → not a calendar pair
                    key_tuple = tuple(sorted((early_m['id'], late_m['id'])))
                    if key_tuple in existing_pair_keys:
                        continue
                    existing_pair_keys.add(key_tuple)

                    self.discovered_pairs.append({
                        "early_id": early_m['id'],
                        "late_id": late_m['id'],
                        "description": f"Regex auto-match (identical normalized title): \"{norm_q[:80]}\"",
                        "early_question": early_m.get('question', ''),
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
                        f"(early ends {early_end.date()}, late ends {late_end.date()})"
                    )

        if new_count > 0:
            self._save_discovered_pairs()
            self._save_json_state(self.CONFIRMED_FILE, self.confirmed_pairs)
            self.logger.info(f"📐 Regex discovery: {new_count} new auto-confirmed pair(s)")
        return new_count

    async def scan(self) -> List[Dict[str, Any]]:
        all_markets = self.scanner.get_all_active_markets(max_markets=5000)
        if not all_markets:
            return []

        # Cache the market_map for use by _check_escalations without re-fetching.
        active_ids = {m['id'] for m in all_markets}
        market_map = {m['id']: m for m in all_markets}
        # Cache for _check_escalations (avoids a second scanner fetch).
        self._last_market_map = market_map

        # --- Regex-based Discovery (AI-free pre-pass) ---
        # Catches pairs whose titles are identical after date-stripping. Runs
        # every scan, regardless of LLM availability, so the bot keeps finding
        # obvious opportunities even if GEMINI_API_KEY is unset/invalid.
        self._regex_discover_obvious_pairs(all_markets)

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
                self._save_discovered_pairs()
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

            no_early, yes_late = tid_early[1], tid_late[0]
            ask_no, ask_yes = self._best_ask(no_early), self._best_ask(yes_late)
            bid_no, bid_yes = self._best_bid(no_early), self._best_bid(yes_late)

            pair_key = self._pair_key(pair['early_id'], pair['late_id'])
            days = self._days_until_close(late.get("endDate"))
            tier = self._get_tier_status(pair['early_id'], pair['late_id'])

            # Build snapshot entry for this pair (even if no arb opportunity right
            # now — the dashboard still wants to render it).
            snap_entry: Dict[str, Any] = {
                "pair_key": pair_key,
                "early_id": pair['early_id'],
                "late_id": pair['late_id'],
                "early_question": early.get('question', ''),
                "late_question": late.get('question', ''),
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
            if ask_no and ask_yes:
                total_cost = ask_no["price"] + ask_yes["price"]
                snap_entry.update({
                    "ask_no_early": ask_no["price"],
                    "ask_yes_late": ask_yes["price"],
                    "total_cost": total_cost,
                    "entry_profit_usd": round(1.0 - total_cost, 4),
                    "entry_profit_pct": round((1.0 - total_cost) * 100, 2),
                    "annualized_roi": round(
                        self._calculate_annualized_roi(1.0 - total_cost, days) * 100, 2
                    ),
                })
            if bid_no and bid_yes:
                exit_value = bid_no["price"] + bid_yes["price"]
                snap_entry["bid_no_early"] = bid_no["price"]
                snap_entry["bid_yes_late"] = bid_yes["price"]
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
        return opportunities

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
            if now - opened_at < self.escalation_seconds:
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
            if len(tid_early) < 2 or len(tid_late) < 2:
                continue
            ask_no = self._best_ask(tid_early[1])
            ask_yes = self._best_ask(tid_late[0])
            if not ask_no or not ask_yes:
                continue
            total_cost = ask_no["price"] + ask_yes["price"]
            days = self._days_until_close(late.get("endDate"))
            roi = self._calculate_annualized_roi(1.0 - total_cost, days)

            alert_info = {
                "early_question": early.get("question", ""),
                "late_question": late.get("question", ""),
                "early_desc": early.get("description", ""),
                "late_desc": late.get("description", ""),
                "early_end": early.get("endDate"),
                "late_end": late.get("endDate"),
                "ask_no_early": ask_no["price"],
                "ask_yes_late": ask_yes["price"],
                "total_cost": total_cost,
                "annualized_roi": roi,
            }
            ok = await self.telegram.send_pair_alert(pair_key, alert_info)
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
