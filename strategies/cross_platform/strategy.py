"""
Cross-Platform Arbitrage Strategy

Detects price discrepancies between Polymarket and Kalshi
for the same underlying event.

Example:
  Polymarket: "Bitcoin $100k by Dec" - YES @ 0.52
  Kalshi:     "BTC-31DEC-B100K" - NO @ 0.46
  Arbitrage:  Buy YES on Polymarket, Buy NO on Kalshi
  Guaranteed profit: 0.52 + 0.46 = 0.98 < 1.00 → 2% profit
"""

import asyncio
import logging
import os
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timezone

from strategies.base_strategy import BaseStrategy
from strategies.calendar_arbitrage.llm_agent import get_llm_agent
from core.kalshi_client import get_kalshi_client, KalshiClient
from core.database import get_database, DatabaseManager

logger = logging.getLogger(__name__)


class CrossPlatformArbitrageStrategy(BaseStrategy):
    """Arbitrage between Polymarket and Kalshi on equivalent markets."""

    def __init__(
        self,
        strategy_name: str = "CrossPlatformArbitrageStrategy",
        scan_interval: int = 30,  # Slower than single-platform
        log_level: str = "INFO",
        min_profit_threshold: float = 0.02,  # 2% minimum
        max_positions: int = 10,
        dry_run: bool = False,
        use_llm: bool = True,  # LLM recommended for cross-platform matching
        llm_model: str = "gpt-4o-mini",
        use_database: bool = False,
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
        self.max_positions = max_positions
        self.estimated_fee = float(os.getenv("DEFAULT_SLIPPAGE", "0.01"))  # per leg per platform
        self.use_llm = use_llm
        self.llm_model = llm_model
        self.use_database = use_database

        # Clients
        self.kalshi_client: Optional[KalshiClient] = None
        self.llm_agent = None
        self.db: Optional[DatabaseManager] = None

        # Tracking
        self.open_positions: Dict[str, Dict] = {}
        self.stats = {"opportunities_found": 0, "trades_entered": 0, "trades_exited": 0}

        self.logger.info("⚙️ Cross-Platform Arbitrage Configuration:")
        self.logger.info(f"   Min profit threshold: {self.min_profit_threshold:.3f}")
        self.logger.info(f"   Max positions: {self.max_positions}")
        self.logger.info(f"   Use LLM: {self.use_llm}")
        self.logger.info(f"   Scan interval: {scan_interval}s")

    async def scan(self) -> List[Dict[str, Any]]:
        """Find arbitrage opportunities between Polymarket and Kalshi."""
        opportunities = []

        # Get markets from both platforms
        poly_markets = self.scanner.get_all_active_markets(max_markets=500)
        
        if not self.kalshi_client:
            self.logger.warning("Kalshi client not available")
            return []

        try:
            kalshi_markets = await self.kalshi_client.get_markets(limit=200, status="open")
        except Exception as e:
            self.logger.error(f"Failed to fetch Kalshi markets: {e}")
            return []

        # Normalize Kalshi markets
        kalshi_normalized = [
            self.kalshi_client.normalize_market_data(m) for m in kalshi_markets
        ]

        self.logger.info(f"📊 Comparing {len(poly_markets)} Polymarket vs {len(kalshi_normalized)} Kalshi markets")

        # Use LLM to match equivalent markets
        if self.use_llm and self.llm_agent:
            matched_pairs = await self._match_markets_llm(poly_markets, kalshi_normalized)
        else:
            matched_pairs = self._match_markets_simple(poly_markets, kalshi_normalized)

        self.logger.info(f"🔗 Found {len(matched_pairs)} potentially equivalent market pairs")

        # Evaluate each pair for arbitrage
        for poly_market, kalshi_market in matched_pairs:
            arb = await self._evaluate_arbitrage(poly_market, kalshi_market)
            if arb:
                opportunities.append(arb)

        return opportunities

    async def _match_markets_llm(
        self,
        poly_markets: List[Dict],
        kalshi_markets: List[Dict],
    ) -> List[Tuple[Dict, Dict]]:
        """Use LLM to intelligently match equivalent markets across platforms."""
        if not self.llm_agent:
            return []

        # Build prompt for LLM
        prompt = self._build_matching_prompt(poly_markets, kalshi_markets)

        try:
            # Call LLM (simplified - real implementation would batch)
            pairs = []
            
            # For now, use simple heuristic + LLM verification
            for p_idx, p_market in enumerate(poly_markets[:50]):  # Limit for cost
                for k_idx, k_market in enumerate(kalshi_markets[:50]):
                    # Quick filter: similar keywords
                    if self._has_keyword_overlap(
                        p_market.get("question", ""),
                        k_market.get("question", ""),
                    ):
                        # Ask LLM to verify
                        explanation = await self.llm_agent.explain_relationship(
                            p_market.get("question", ""),
                            k_market.get("question", ""),
                        )
                        
                        if explanation and ("equivalent" in explanation.lower() or "same" in explanation.lower()):
                            pairs.append((p_market, k_market))
                            self.logger.debug(f"LLM match: {p_market.get('question', '')[:40]} ↔ {k_market.get('question', '')[:40]}")

            return pairs

        except Exception as e:
            self.logger.error(f"LLM matching failed: {e}")
            return self._match_markets_simple(poly_markets, kalshi_markets)

    def _match_markets_simple(
        self,
        poly_markets: List[Dict],
        kalshi_markets: List[Dict],
    ) -> List[Tuple[Dict, Dict]]:
        """Simple keyword-based matching (fallback)."""
        pairs = []

        for p_market in poly_markets:
            p_question = p_market.get("question", "").lower()
            
            for k_market in kalshi_markets:
                k_question = k_market.get("question", "").lower()
                
                if self._has_keyword_overlap(p_question, k_question, min_words=3):
                    pairs.append((p_market, k_market))

        return pairs

    def _has_keyword_overlap(self, text1: str, text2: str, min_words: int = 2) -> bool:
        """Check if two texts have significant keyword overlap."""
        # Extract meaningful words (ignore common words)
        stop_words = {"the", "a", "an", "in", "on", "at", "to", "for", "of", "by", "will", "be"}
        
        words1 = set(w for w in text1.lower().split() if len(w) > 3 and w not in stop_words)
        words2 = set(w for w in text2.lower().split() if len(w) > 3 and w not in stop_words)
        
        overlap = words1 & words2
        return len(overlap) >= min_words

    def _build_matching_prompt(self, poly_markets: List[Dict], kalshi_markets: List[Dict]) -> str:
        """Build LLM prompt for market matching."""
        # Simplified - full implementation would be more sophisticated
        return f"Match {len(poly_markets)} Polymarket markets with {len(kalshi_markets)} Kalshi markets"

    async def _evaluate_arbitrage(
        self,
        poly_market: Dict,
        kalshi_market: Dict,
    ) -> Optional[Dict]:
        """
        Evaluate if arbitrage exists between matched markets.

        Strategy:
          1. Get best prices from both platforms
          2. Check if YES(poly) + NO(kalshi) < 1 or NO(poly) + YES(kalshi) < 1
          3. Account for fees on both platforms
        """
        # Get Polymarket prices
        poly_tokens = self._get_token_ids(poly_market)
        if len(poly_tokens) < 2:
            return None

        poly_yes_token, poly_no_token = poly_tokens[0], poly_tokens[1]
        
        poly_yes_ask = self._best_ask(poly_yes_token)
        poly_no_ask = self._best_ask(poly_no_token)
        
        if not poly_yes_ask or not poly_no_ask:
            return None

        # Get Kalshi prices
        kalshi_ticker = kalshi_market.get("ticker")
        if not kalshi_ticker:
            return None

        try:
            kalshi_book = await self.kalshi_client.get_orderbook(kalshi_ticker)
            kalshi_yes_ask = kalshi_book.get("yes", [{}])[0] if kalshi_book.get("yes") else None
            kalshi_no_ask = kalshi_book.get("no", [{}])[0] if kalshi_book.get("no") else None
            
            if not kalshi_yes_ask or not kalshi_no_ask:
                return None

        except Exception as e:
            self.logger.debug(f"Failed to get Kalshi orderbook: {e}")
            return None

        # Strategy 1: Buy YES on Poly, Buy NO on Kalshi
        strategy1_cost = poly_yes_ask["price"] + kalshi_no_ask["price"]
        strategy1_fees = 2 * self.estimated_fee  # Fee on each platform
        strategy1_profit = 1.0 - strategy1_cost - strategy1_fees

        # Strategy 2: Buy NO on Poly, Buy YES on Kalshi
        strategy2_cost = poly_no_ask["price"] + kalshi_yes_ask["price"]
        strategy2_fees = 2 * self.estimated_fee
        strategy2_profit = 1.0 - strategy2_cost - strategy2_fees

        # Choose best strategy
        if strategy1_profit > self.min_profit_threshold and strategy1_profit > strategy2_profit:
            return {
                "poly_market": poly_market,
                "kalshi_market": kalshi_market,
                "strategy": "YES_poly_NO_kalshi",
                "poly_side": "YES",
                "poly_token": poly_yes_token,
                "poly_price": poly_yes_ask["price"],
                "kalshi_side": "NO",
                "kalshi_ticker": kalshi_ticker,
                "kalshi_price": kalshi_no_ask["price"],
                "total_cost": strategy1_cost,
                "expected_profit": strategy1_profit,
                "profit_pct": strategy1_profit * 100,
            }
        elif strategy2_profit > self.min_profit_threshold:
            return {
                "poly_market": poly_market,
                "kalshi_market": kalshi_market,
                "strategy": "NO_poly_YES_kalshi",
                "poly_side": "NO",
                "poly_token": poly_no_token,
                "poly_price": poly_no_ask["price"],
                "kalshi_side": "YES",
                "kalshi_ticker": kalshi_ticker,
                "kalshi_price": kalshi_yes_ask["price"],
                "total_cost": strategy2_cost,
                "expected_profit": strategy2_profit,
                "profit_pct": strategy2_profit * 100,
            }
        
        return None

    async def enter_position(self, opportunity: Dict) -> bool:
        """Execute arbitrage by placing orders on both platforms."""
        if len(self.open_positions) >= self.max_positions:
            self.logger.warning(f"Max positions ({self.max_positions}) reached")
            return False

        poly_token = opportunity["poly_token"]
        poly_side = opportunity["poly_side"]
        poly_price = opportunity["poly_price"]
        
        kalshi_ticker = opportunity["kalshi_ticker"]
        kalshi_side = opportunity["kalshi_side"]
        kalshi_price_cents = int(opportunity["kalshi_price"] * 100)  # Convert to cents
        
        size = 10  # Fixed size for now
        kalshi_quantity = 10  # Kalshi uses integer contracts

        self.logger.info(f"🌐 Cross-platform arbitrage:")
        self.logger.info(f"   Polymarket: {poly_side} @ {poly_price:.3f}")
        self.logger.info(f"   Kalshi: {kalshi_side} @ {opportunity['kalshi_price']:.3f}")
        self.logger.info(f"   Expected profit: {opportunity['profit_pct']:.2f}%")

        if self.dry_run:
            self.logger.info("🔍 DRY RUN - not executing")
            return False

        # Execute both legs concurrently
        try:
            poly_task = self.executor.execute_trade(
                token_id=poly_token,
                side="BUY",
                size=size,
                price=poly_price,
            )
            
            kalshi_task = self.kalshi_client.create_order(
                ticker=kalshi_ticker,
                side=kalshi_side.lower(),
                action="buy",
                quantity=kalshi_quantity,
                price=kalshi_price_cents,
            )
            
            poly_result, kalshi_result = await asyncio.gather(poly_task, kalshi_task, return_exceptions=True)
            
            poly_success = not isinstance(poly_result, Exception) and poly_result.get("success")
            kalshi_success = not isinstance(kalshi_result, Exception) and kalshi_result.get("order_id")
            
            if poly_success and kalshi_success:
                self.logger.info("✅ Both legs executed successfully")
                
                # Track position
                position_id = f"CROSS-{poly_token[:6]}-{kalshi_ticker[:6]}"
                self.open_positions[position_id] = {
                    **opportunity,
                    "size": size,
                    "entry_time": datetime.now(timezone.utc),
                }
                
                self.stats["trades_entered"] += 1
                return True
            else:
                self.logger.error(f"❌ Failed - Poly: {poly_success}, Kalshi: {kalshi_success}")
                # TODO: Implement rollback
                return False

        except Exception as e:
            self.logger.error(f"Execution error: {e}", exc_info=True)
            return False

    async def run(self):
        """Main strategy loop."""
        try:
            # Initialize Kalshi client
            if not self.kalshi_client:
                self.logger.info("🔌 Connecting to Kalshi...")
                self.kalshi_client = await get_kalshi_client()
                if not self.kalshi_client:
                    self.logger.error("❌ Kalshi connection failed - strategy cannot run")
                    return

            # Initialize LLM if enabled
            if self.use_llm:
                self.llm_agent = get_llm_agent(model=self.llm_model)

            # Initialize database if enabled
            if self.use_database:
                self.db = await get_database()

            # Main loop
            loop_count = 0
            while self.running:
                try:
                    loop_count += 1
                    self.logger.info(f"\n{'='*60}")
                    self.logger.info(f"🌍 Cross-Platform Scan #{loop_count}")
                    self.logger.info(f"{'='*60}")

                    opportunities = await self.scan()

                    if opportunities:
                        self.logger.info(f"\n💰 Found {len(opportunities)} cross-platform opportunities:")
                        for idx, opp in enumerate(opportunities[:5], 1):
                            self.logger.info(f"\n  {idx}. {opp['strategy']}")
                            self.logger.info(f"     Poly: {opp['poly_market'].get('question', '')[:50]}")
                            self.logger.info(f"     Kalshi: {opp['kalshi_market'].get('question', '')[:50]}")
                            self.logger.info(f"     Profit: {opp['profit_pct']:.2f}%")
                            
                            # Try to enter
                            if len(self.open_positions) < self.max_positions:
                                await self.enter_position(opp)
                    else:
                        self.logger.info("No opportunities found")

                    await asyncio.sleep(self.scan_interval)

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    self.logger.error(f"Error in strategy loop: {e}", exc_info=True)
                    await asyncio.sleep(5)

        finally:
            if self.kalshi_client:
                await self.kalshi_client.close()
            self.logger.info("🛑 Strategy stopped")

    # Helper methods from base strategy
    def _get_token_ids(self, market: Dict) -> List[str]:
        """Extract token IDs from market."""
        return market.get("tokens", [])

    def _best_ask(self, token_id: str) -> Optional[Dict[str, float]]:
        """Get best ask price from Polymarket."""
        try:
            book = self.executor.client.get_order_book(token_id)
            asks = book.get("asks", []) if book else []
            if asks:
                return {"price": float(asks[0].get("price", 0)), "size": float(asks[0].get("size", 0))}
        except Exception:
            pass
        return None
