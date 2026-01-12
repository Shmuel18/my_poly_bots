"""
Calendar (Logical) Arbitrage Strategy

קונה NO בשוק מוקדם (תת-קבוצה) ו-YES בשוק המאוחר (על-קבוצה),
כאשר סכום העלויות (ASK_NO מוקדם + ASK_YES מאוחר) קטן מ-1 פחות סף רווח ועמלות.
"""
import asyncio
import logging
import os
import re
from typing import Dict, List, Any, Optional

from strategies.base_strategy import BaseStrategy

logger = logging.getLogger(__name__)


MONTH_WORDS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december"
]


class CalendarArbitrageStrategy(BaseStrategy):
    """ארביטראז' לוגי בין שווקים עם טווחי זמן שונים לאותו אירוע."""

    def __init__(
        self,
        strategy_name: str = "CalendarArbitrageStrategy",
        scan_interval: int = 10,
        log_level: str = "INFO",
        min_profit_threshold: float = 0.02,  # 2%
        max_pairs: int = 1000,
        dry_run: bool = False,
        early_exit_threshold: float = 0.005,  # Exit if spread narrows to 0.5%
        min_annualized_roi: float = 0.15,  # 15% annualized minimum
        check_invalid_risk: bool = True,  # Check for invalid market risk
        use_embeddings: bool = True,  # Use sentence embeddings for similarity
        similarity_threshold: float = 0.85,  # Cosine similarity threshold (0-1)
        **kwargs,
    ):
        super().__init__(
            strategy_name=strategy_name,
            scan_interval=scan_interval,
            log_level=log_level,
            connection=kwargs.get("connection"),
            dry_run=dry_run,
        )

        self.min_profit_threshold = float(min_profit_threshold)
        self.max_pairs = max_pairs
        self.estimated_fee = float(os.getenv("DEFAULT_SLIPPAGE", "0.01"))  # per leg
        self.early_exit_threshold = float(early_exit_threshold)
        self.min_annualized_roi = float(min_annualized_roi)
        self.check_invalid_risk = check_invalid_risk
        self.use_embeddings = use_embeddings
        self.similarity_threshold = float(similarity_threshold)
        
        # Initialize sentence transformer model (lazy loading)
        self._embedding_model = None
        self._embedding_cache = {}  # Cache embeddings to avoid recomputation

        self.logger.info("⚙️ Configuration:")
        self.logger.info(f"   Min profit threshold: {self.min_profit_threshold:.3f}")
        self.logger.info(f"   Early exit threshold: {self.early_exit_threshold:.3f}")
        self.logger.info(f"   Min annualized ROI: {self.min_annualized_roi:.1%}")
        self.logger.info(f"   Estimated fee/slippage per leg: {self.estimated_fee:.3f}")
        self.logger.info(f"   Check invalid risk: {self.check_invalid_risk}")
        self.logger.info(f"   Use embeddings: {self.use_embeddings}")
        if self.use_embeddings:
            self.logger.info(f"   Similarity threshold: {self.similarity_threshold:.2f}")
        self.logger.info(f"   Scan interval: {scan_interval}s")

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

    def _days_until_close(self, end_date_str: Optional[str]) -> float:
        """חישוב ימים עד סגירת השוק."""
        if not end_date_str:
            return 365.0  # default fallback
        try:
            from datetime import datetime, timezone
            end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            delta = (end_date - now).total_seconds() / 86400  # days
            return max(0.1, delta)  # minimum 0.1 day
        except:
            return 365.0

    def _best_ask(self, token_id: str) -> Optional[Dict[str, float]]:
        try:
            book = self.executor.client.get_order_book(token_id)
            asks = book.get("asks", []) if book else []
            if asks:
                p = float(asks[0].get("price", 0))
                s = float(asks[0].get("size", 0)) if asks[0].get("size") is not None else 0.0
                return {"price": p, "size": s}
        except Exception:
            return None
        return None

    def _best_bid(self, token_id: str) -> Optional[Dict[str, float]]:
        """Get best bid price (price we can sell at)."""
        try:
            book = self.executor.client.get_order_book(token_id)
            bids = book.get("bids", []) if book else []
            if bids:
                p = float(bids[0].get("price", 0))
                s = float(bids[0].get("size", 0)) if bids[0].get("size") is not None else 0.0
                return {"price": p, "size": s}
        except Exception:
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
            orders = book.get("asks" if side == "BUY" else "bids", [])
            if not orders:
                return None
            
            remaining_size = size
            total_cost = 0.0
            filled_size = 0.0
            
            for order in orders:
                order_price = float(order.get("price", 0))
                order_size = float(order.get("size", 0)) if order.get("size") is not None else 0.0
                
                if order_size <= 0:
                    continue
                
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
                "slippage": (avg_price - float(orders[0].get("price", 0))) if orders else 0.0,
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

    async def scan(self) -> List[Dict[str, Any]]:
        """חיפוש זוגות (מוקדם, מאוחר) לאותו אירוע בסיסי, שבו ASK_NO_early + ASK_YES_late < 1 - (threshold+fees)."""
        markets = self.scanner.get_all_active_markets(max_markets=5000)
        
        # Hybrid approach: use both regex normalization AND embedding similarity
        if self.use_embeddings:
            # Build groups using semantic similarity
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
        else:
            # Fallback: regex-only grouping (faster but less accurate)
            groups_dict: Dict[str, List[Dict]] = {}
            for m in markets:
                key = self._normalize_question(m.get("question", ""))
                if not key:
                    continue
                groups_dict.setdefault(key, []).append(m)
            groups = [g for g in groups_dict.values() if len(g) >= 2]

        opportunities: List[Dict[str, Any]] = []

        for group in groups:
            if len(group) < 2:
                continue

            # Sort by endDate ascending (earlier first)
            def _end(m):
                ed = self._get_end_date(m)
                return ed or "9999-12-31T00:00:00Z"

            group_sorted = sorted(group, key=_end)

            # Evaluate adjacent pairs (early, late)
            for i in range(len(group_sorted) - 1):
                early = group_sorted[i]
                late = group_sorted[i + 1]

                tid_early = self._get_token_ids(early)
                tid_late = self._get_token_ids(late)
                if len(tid_early) < 2 or len(tid_late) < 2:
                    continue

                yes_early, no_early = tid_early[0], tid_early[1]
                yes_late, no_late = tid_late[0], tid_late[1]

                ask_no_early = self._best_ask(no_early)
                ask_yes_late = self._best_ask(yes_late)
                if not ask_no_early or not ask_yes_late:
                    continue

                total_cost = (ask_no_early["price"] + ask_yes_late["price"])  # cost per unit
                # Account for fees per leg
                min_profit_total = self.min_profit_threshold + (2 * self.estimated_fee)
                threshold = 1.0 - min_profit_total

                if total_cost < threshold:
                    # Check for invalid market risk
                    if self._has_invalid_risk(early) or self._has_invalid_risk(late):
                        early_q = early.get("question", "")[:40]
                        self.logger.debug(f"Skipping pair with invalid risk: {early_q}")
                        continue

                    size_cap = min(ask_no_early.get("size", 0), ask_yes_late.get("size", 0))
                    if size_cap <= 0:
                        continue

                    expected_profit = 1.0 - total_cost
                    days_until_late = self._days_until_close(late.get("endDate"))
                    annualized_roi = self._calculate_annualized_roi(expected_profit, days_until_late)

                    # Filter by annualized ROI
                    if annualized_roi < self.min_annualized_roi:
                        self.logger.debug(
                            f"Skipping low annualized ROI: {annualized_roi:.1%} < {self.min_annualized_roi:.1%}"
                        )
                        continue

                    opportunities.append({
                        "key": self._normalize_question(early.get("question", "")),
                        "early_question": early.get("question", ""),
                        "late_question": late.get("question", ""),
                        "early_end": early.get("endDate"),
                        "late_end": late.get("endDate"),
                        "no_early_token": no_early,
                        "yes_late_token": yes_late,
                        "ask_no_early": ask_no_early["price"],
                        "ask_yes_late": ask_yes_late["price"],
                        "size": max(1.0, min(10.0, size_cap)),  # conservative default
                        "total_cost": total_cost,
                        "guaranteed_payoff": 1.0,
                        "expected_profit": expected_profit,
                        "annualized_roi": annualized_roi,
                        "days_until_close": days_until_late,
                        "token_id": f"{no_early}:{yes_late}",  # for tracking
                    })

                if len(opportunities) >= self.max_pairs:
                    break

        return opportunities

    async def should_enter(self, opportunity: Dict[str, Any]) -> bool:
        # Basic sanity + we already used orderbook asks, so thresholds are conservative
        return True

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
        
        # Calculate total cost with slippage
        total_cost_with_slippage = fill_no["avg_price"] + fill_yes["avg_price"]
        min_profit_total = self.min_profit_threshold + (2 * self.estimated_fee)
        
        if total_cost_with_slippage >= (1.0 - min_profit_total):
            self.logger.warning(
                f"⚠️ Slippage kills profit: ${total_cost_with_slippage:.4f} >= ${1.0 - min_profit_total:.4f}"
            )
            return False

        self.logger.info("🧮 Calendar Arbitrage Opportunity:")
        self.logger.info(f"   Early(NO) ask: ${price_no_early:.4f} (avg: ${fill_no['avg_price']:.4f})")
        self.logger.info(f"   Late(YES) ask: ${price_yes_late:.4f} (avg: ${fill_yes['avg_price']:.4f})")
        self.logger.info(f"   Total cost: ${opportunity['total_cost']:.4f} | With slippage: ${total_cost_with_slippage:.4f}")
        self.logger.info(f"   Annualized ROI: {opportunity.get('annualized_roi', 0):.1%} ({opportunity.get('days_until_close', 0):.1f} days)")
        self.logger.info(f"   Early: {opportunity['early_question'][:60]}")
        self.logger.info(f"   Late:  {opportunity['late_question'][:60]}")

        # Execute both legs concurrently
        tasks = [
            self.executor.execute_trade(token_id=no_early_token, side="BUY", size=size, price=fill_no["avg_price"]),
            self.executor.execute_trade(token_id=yes_late_token, side="BUY", size=size, price=fill_yes["avg_price"]),
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Check results
        no_early_success = isinstance(results[0], dict) and results[0].get("success")
        yes_late_success = isinstance(results[1], dict) and results[1].get("success")
        
        # Handle leg risk - rollback if partial fill
        if no_early_success and not yes_late_success:
            self.logger.error("❌ YES leg failed, rolling back NO position")
            try:
                rollback = await self.executor.execute_trade(
                    token_id=no_early_token, side="SELL", size=size, price=0.01
                )
                if rollback and rollback.get("success"):
                    self.logger.info("✅ Rollback successful")
                else:
                    self.logger.error("🚨 ROLLBACK FAILED - manual intervention required!")
            except Exception as e:
                self.logger.error(f"🚨 Rollback exception: {e}")
            return False
        
        elif yes_late_success and not no_early_success:
            self.logger.error("❌ NO leg failed, rolling back YES position")
            try:
                rollback = await self.executor.execute_trade(
                    token_id=yes_late_token, side="SELL", size=size, price=0.01
                )
                if rollback and rollback.get("success"):
                    self.logger.info("✅ Rollback successful")
                else:
                    self.logger.error("🚨 ROLLBACK FAILED - manual intervention required!")
            except Exception as e:
                self.logger.error(f"🚨 Rollback exception: {e}")
            return False
        
        elif not no_early_success and not yes_late_success:
            self.logger.error("❌ Both legs failed")
            return False
        
        # Both succeeded
        group_id = f"CAL-{no_early_token[:6]}-{yes_late_token[:6]}"
        pos_data = {
            **opportunity,
            "entry_time": asyncio.get_event_loop().time(),
            "size": size,
            "strategy_name": self.strategy_name,
            "group_id": group_id,
            "actual_entry_cost": total_cost_with_slippage,  # Track actual cost
        }
        # Track both tokens under same group
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

            # Execute SELL orders for both legs concurrently
            # Using market orders (price=0.01 for SELL means accept any bid)
            tasks = [
                self.executor.execute_trade(
                    token_id=no_early_token, 
                    side="SELL", 
                    size=size, 
                    price=bid_no["price"] if bid_no else 0.01
                ),
                self.executor.execute_trade(
                    token_id=yes_late_token, 
                    side="SELL", 
                    size=size, 
                    price=bid_yes["price"] if bid_yes else 0.01
                ),
            ]

            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Check if both orders succeeded
            if all(not isinstance(r, Exception) and r.get("success") for r in results):
                # Clean up positions from memory and manager
                self.open_positions.pop(no_early_token, None)
                self.open_positions.pop(yes_late_token, None)
                self.position_manager.remove_position(no_early_token)
                self.position_manager.remove_position(yes_late_token)
                
                self.stats["trades_exited"] += 1
                self.logger.info("✅ Successfully exited both legs")
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
