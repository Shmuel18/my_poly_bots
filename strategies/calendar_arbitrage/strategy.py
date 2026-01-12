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

        self.logger.info("⚙️ Configuration:")
        self.logger.info(f"   Min profit threshold: {self.min_profit_threshold:.3f}")
        self.logger.info(f"   Estimated fee/slippage per leg: {self.estimated_fee:.3f}")
        self.logger.info(f"   Scan interval: {scan_interval}s")

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

    async def scan(self) -> List[Dict[str, Any]]:
        """חיפוש זוגות (מוקדם, מאוחר) לאותו אירוע בסיסי, שבו ASK_NO_early + ASK_YES_late < 1 - (threshold+fees)."""
        markets = self.scanner.get_all_active_markets(max_markets=5000)
        groups: Dict[str, List[Dict]] = {}

        for m in markets:
            key = self._normalize_question(m.get("question", ""))
            if not key:
                continue
            groups.setdefault(key, []).append(m)

        opportunities: List[Dict[str, Any]] = []

        for key, group in groups.items():
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
                    size_cap = min(ask_no_early.get("size", 0), ask_yes_late.get("size", 0))
                    if size_cap <= 0:
                        continue

                    opportunities.append({
                        "key": key,
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
                        "expected_profit": 1.0 - total_cost,
                        "token_id": f"{no_early}:{yes_late}",  # for tracking
                    })

                if len(opportunities) >= self.max_pairs:
                    break

        return opportunities

    async def should_enter(self, opportunity: Dict[str, Any]) -> bool:
        # Basic sanity + we already used orderbook asks, so thresholds are conservative
        return True

    async def enter_position(self, opportunity: Dict[str, Any]) -> bool:
        """ביצוע שתי העסקאות במקביל: קניית NO במוקדם ו-YES במאוחר."""
        no_early_token = opportunity["no_early_token"]
        yes_late_token = opportunity["yes_late_token"]
        size = float(opportunity.get("size", 1.0))
        price_no_early = float(opportunity["ask_no_early"])  # pay ask
        price_yes_late = float(opportunity["ask_yes_late"])  # pay ask

        self.logger.info("🧮 Calendar Arbitrage Opportunity:")
        self.logger.info(f"   Early(NO) ask: ${price_no_early:.4f} | Late(YES) ask: ${price_yes_late:.4f}")
        self.logger.info(f"   Total cost: ${opportunity['total_cost']:.4f} | Profit >= ${opportunity['expected_profit']:.4f}")
        self.logger.info(f"   Early: {opportunity['early_question'][:60]}")
        self.logger.info(f"   Late:  {opportunity['late_question'][:60]}")

        # Execute both legs concurrently
        tasks = [
            self.executor.execute_trade(token_id=no_early_token, side="BUY", size=size, price=price_no_early),
            self.executor.execute_trade(token_id=yes_late_token, side="BUY", size=size, price=price_yes_late),
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)
        success = all(isinstance(r, dict) and r.get("success") for r in results)
        if success:
            group_id = f"CAL-{no_early_token[:6]}-{yes_late_token[:6]}"
            pos_data = {
                **opportunity,
                "entry_time": asyncio.get_event_loop().time(),
                "size": size,
                "strategy_name": self.strategy_name,
                "group_id": group_id,
            }
            # Track both tokens under same group
            self.open_positions[no_early_token] = pos_data
            self.open_positions[yes_late_token] = pos_data
            self.position_manager.add_position(token_id=no_early_token, entry_price=price_no_early, size=size, metadata=pos_data)
            self.position_manager.add_position(token_id=yes_late_token, entry_price=price_yes_late, size=size, metadata=pos_data)
            self.stats["trades_entered"] += 1
            self.logger.info("✅ Calendar arbitrage legs filled")
            return True

        self.logger.warning("⚠️ Failed to execute both legs; consider manual review")
        return False

    async def should_exit(self, position: Dict[str, Any]) -> bool:
        # Positions are intended to be held to resolution; no active exit.
        return False
