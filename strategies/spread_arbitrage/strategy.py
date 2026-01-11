"""
Spread Arbitrage Strategy

מחפש שווקים עם מרווח גדול (Spread > $0.40) ומחיר נמוך (< $0.30).
קונה ב-BestBid+0.01, מוכר בדינמיקה: רווח 20 סנט או מתחת ל-BestAsk-0.01.
עם timeout של 60 דקות שמוריד מחיר ב-5 סנט לדקה.

WebSocket Integration: Real-time price monitoring for immediate penny defense detection.
"""
import asyncio
import logging
from typing import Dict, List, Any, Optional

from core import PolymarketConnection
from core.ws_manager import WebSocketManager
from strategies.base_strategy import BaseStrategy

logger = logging.getLogger(__name__)


class SpreadArbitrageStrategy(BaseStrategy):
    """
    אסטרטגיית ארביטראז' מרווח (Spread Arbitrage).
    
    מאפיינים:
    - סורקת שווקים עם Spread גדול (> $0.40) ומחיר נמוך (< $0.30)
    - כניסה: BestBid + $0.01 (להיות ראשון בתור)
    - יציאה דינמית:
      * אם Spread > $0.20: מוכר ב-min(entry+0.20, BestAsk-0.01)
      * אם Spread <= $0.20: מוכר ב-BestAsk-0.01
    - Timeout: אחרי 60 דקות, הוריד מחיר ב-5 סנט לדקה
    """
    
    def __init__(
        self,
        strategy_name: str = "SpreadArbitrageStrategy",
        scan_interval: int = 30,
        log_level: str = "INFO",
        connection: Optional[PolymarketConnection] = None,
        dry_run: bool = False,
        max_price: float = 0.30,
        min_spread: float = 0.40,
        target_profit: float = 0.20,
        entry_offset: float = 0.01,
        timeout_minutes: int = 60,
        timeout_price_step: float = 0.05,
        min_volume: float = 100.0,
    ):
        """
        אתחול אסטרטגיה.
        
        Args:
            strategy_name: שם האסטרטגיה
            scan_interval: מרווח סריקה בשניות
            log_level: רמת לוג
            connection: חיבור ל-Polymarket (injected)
            dry_run: מצב סימולציה
            max_price: מחיר מקסימלי לקנייה ($0.30)
            min_spread: spread מינימלי להיכנס ($0.40)
            target_profit: רווח מטרה ($0.20)
            entry_offset: offset מ-BestBid ($0.01)
            timeout_minutes: דקות לפני הורדת מחיר (60)
            timeout_price_step: כמה להוריד בכל דקה ($0.05)
            min_volume: נפח מסחר מינימלי בדולרים ($100)
        """
        super().__init__(
            strategy_name=strategy_name,
            scan_interval=scan_interval,
            log_level=log_level,
            connection=connection,
            dry_run=dry_run,
        )
        
        # Spread arbitrage config
        self.max_price = max_price
        self.min_spread = min_spread
        self.target_profit = target_profit
        self.entry_offset = entry_offset
        self.timeout_minutes = timeout_minutes
        self.timeout_price_step = timeout_price_step
        self.min_volume = min_volume
        self.entry_times = {}  # Track entry time per token
        
        # WebSocket for real-time price monitoring
        self.ws_manager = WebSocketManager(auto_reconnect=True)
        self.ws_enabled = not dry_run  # Enable WebSocket in live mode
        self.price_updates: Dict[str, Dict[str, Any]] = {}  # Cache latest prices from WS
        
        self.logger.info(f"⚙️ Configuration:")
        self.logger.info(f"   Max price: ${max_price:.2f}")
        self.logger.info(f"   Min spread: ${min_spread:.2f}")
        self.logger.info(f"   Target profit: ${target_profit:.2f}")
        self.logger.info(f"   Min volume: ${min_volume:.0f}")
        self.logger.info(f"   Timeout: {timeout_minutes}min ({timeout_price_step:.2f}/min)")
        if self.ws_enabled:
            self.logger.info(f"   WebSocket: ENABLED (real-time price monitoring)")
    
    def _handle_price_update(self, token_id: str, data: Dict[str, Any]):
        """
        WebSocket price update callback.
        
        Detects immediate penny defense signals and triggers quick exit if needed.
        """
        if token_id not in self.open_positions:
            return
        
        position = self.open_positions[token_id]
        entry_price = position.get('entry_price')
        
        # Extract bids from update
        bids = data.get('bids', [])
        if bids:
            try:
                best_bid = float(bids[0].get('price', 0)) if isinstance(bids, list) else float(bids)
                
                # PENNY DEFENSE: Real-time check
                if best_bid > entry_price:
                    self.logger.warning(
                        f"🚨 PENNY DEFENSE (WS): {token_id[:12]}... "
                        f"was out-bid: ${entry_price:.4f} → ${best_bid:.4f}. "
                        f"IMMEDIATE EXIT!"
                    )
                    # Set flag for quick exit in monitor_loop
                    position['force_exit'] = True
            except:
                pass
        
        # Cache the update for later use
        self.price_updates[token_id] = data
    
    async def _setup_websocket(self):
        """Initialize WebSocket connection and subscribe to tracked markets."""
        if not self.ws_enabled:
            return
        
        try:
            if not await self.ws_manager.connect():
                self.logger.warning("Failed to connect WebSocket")
                return
            
            # Subscribe to any open positions
            if self.open_positions:
                token_ids = list(self.open_positions.keys())
                if await self.ws_manager.subscribe(token_ids):
                    self.logger.info(f"📡 Subscribed to {len(token_ids)} markets via WebSocket")
                    
                    # Start receiving updates
                    await self.ws_manager.receive_data(
                        callback=lambda token_id, data: self._handle_price_update(token_id, data)
                    )
        except Exception as e:
            self.logger.error(f"WebSocket setup error: {e}")
    
    async def scan(self) -> List[Dict[str, Any]]:
        """סורקת שווקים עם Spread > min_spread, מחיר < max_price, וnvolume > min_volume."""
        try:
            # Get all active markets
            markets = self.scanner.get_all_active_markets(max_markets=5000)
            
            # Filter by volume (עדכון: סנן לפי נפח מסחר)
            markets = self.scanner.filter_by_volume(markets, min_volume=self.min_volume)
            
            opportunities = []
            
            for market in markets:
                try:
                    # Extract token_ids from market
                    token_ids = market.get('clobTokenIds', [])
                    if isinstance(token_ids, str):
                        import json
                        try:
                            token_ids = json.loads(token_ids)
                        except:
                            continue
                    
                    if not token_ids or len(token_ids) < 2:
                        continue
                    
                    # Check each outcome
                    for token_id in token_ids:
                        # Get orderbook
                        book = self.executor.client.get_order_book(token_id)
                        if not book:
                            continue
                        
                        bids = book.get('bids', [])
                        asks = book.get('asks', [])
                        
                        if not bids or not asks:
                            continue
                        
                        best_bid = float(bids[0].get('price', 0))
                        best_ask = float(asks[0].get('price', 0))
                        spread = best_ask - best_bid
                        
                        # Filter: Spread > min_spread AND best_bid < max_price
                        if spread >= self.min_spread and best_bid < self.max_price:
                            opportunities.append({
                                'token_id': token_id,
                                'question': market.get('question', ''),
                                'best_bid': best_bid,
                                'best_ask': best_ask,
                                'spread': spread,
                                'price': best_bid,
                                'size': 100,
                            })
                except Exception as e:
                    self.logger.debug(f"Error scanning market: {e}")
            
            # WebSocket: Subscribe to found opportunities for real-time monitoring
            if self.ws_enabled and opportunities:
                token_ids_to_watch = [opp['token_id'] for opp in opportunities]
                try:
                    if not self.ws_manager.is_connected:
                        await self.ws_manager.connect()
                    await self.ws_manager.subscribe(token_ids_to_watch)
                    self.logger.debug(f"📡 Subscribed to {len(token_ids_to_watch)} markets via WebSocket")
                except Exception as ws_err:
                    self.logger.debug(f"WebSocket subscription skipped: {ws_err}")
            
            return opportunities
        except Exception as e:
            self.logger.error(f"Scan error: {e}")
            return []
    
    async def should_enter(self, opportunity: Dict[str, Any]) -> bool:
        """כל הזדמנות שעברה הסינון הוא זדון לכניסה."""
        return True
    
    async def should_exit(self, position: Dict[str, Any]) -> bool:
        """בדיקה אם עדיף לצאת בהתאם לתנאים דינמיים."""
        token_id = position.get('token_id')
        entry_price = position.get('entry_price')
        entry_time = position.get('entry_time', 0)
        
        if not token_id:
            return False
        
        # Check if WebSocket forced an exit (penny defense)
        if position.get('force_exit'):
            self.logger.warning(f"⚡ Force exit from WebSocket penny defense")
            return True
        
        try:
            # Get current book
            book = self.executor.client.get_order_book(token_id)
            if not book:
                return False
            
            asks = book.get('asks', [])
            bids = book.get('bids', [])
            
            if not asks:
                return False
            
            best_ask = float(asks[0].get('price', 0))
            current_spread = best_ask - entry_price
            
            # PENNY DEFENSE: If best_bid > entry_price, we got beaten
            if bids:
                best_bid = float(bids[0].get('price', 0))
                if best_bid > entry_price:
                    self.logger.warning(f"💪 Penny defense triggered. Exiting.")
                    return True
            
            # Calculate timeout (elapsed minutes)
            elapsed_minutes = (asyncio.get_event_loop().time() - entry_time) / 60
            
            # Timeout: after N minutes, start dropping price
            if elapsed_minutes > self.timeout_minutes:
                minutes_over = elapsed_minutes - self.timeout_minutes
                price_drop = minutes_over * self.timeout_price_step
                self.logger.info(
                    f"⏱️ Timeout: {elapsed_minutes:.1f}min, "
                    f"price drop: ${price_drop:.2f}"
                )
                return True
            
            # Normal exit: if spread allows target profit
            if current_spread >= self.target_profit:
                self.logger.info(
                    f"🎯 Spread {current_spread:.2f} >= target {self.target_profit:.2f}"
                )
                return True
            
            return False
        except Exception as e:
            self.logger.error(f"Exit check error: {e}")
            return False
    
    async def enter_position(self, opportunity: Dict[str, Any]) -> bool:
        """כניסה עם BestBid + offset."""
        token_id = opportunity.get('token_id')
        best_bid = opportunity.get('best_bid', 0)
        
        # Entry price: BestBid + 0.01
        entry_price = round(best_bid + self.entry_offset, 4)
        size = 100
        
        self.logger.info(
            f"🎯 Spread arbitrage: {opportunity.get('question', '')[:50]}"
        )
        self.logger.info(
            f"   BestBid: ${best_bid:.4f}, Entry: ${entry_price:.4f}, "
            f"Spread: ${opportunity.get('spread', 0):.2f}"
        )
        
        result = await self.executor.execute_trade(
            token_id=token_id,
            side='BUY',
            size=size,
            price=entry_price
        )
        
        if result and result.get('success'):
            self.entry_times[token_id] = asyncio.get_event_loop().time()
            
            executor_position = self.executor.get_position(token_id)
            actual_size = executor_position.get('size', size) if executor_position else size
            
            position_data = {
                **opportunity,
                'entry_time': asyncio.get_event_loop().time(),
                'entry_price': entry_price,
                'size': actual_size,
                'strategy_name': self.strategy_name
            }
            
            self.open_positions[token_id] = position_data
            self.position_manager.add_position(
                token_id=token_id,
                entry_price=entry_price,
                size=actual_size,
                metadata=position_data
            )
            
            self.stats['trades_entered'] += 1
            self.logger.info(f"✅ Entry OK (size: {actual_size})")
            return True
        
        return False
    
    async def exit_position(self, token_id: str, exit_price: Optional[float] = None) -> bool:
        """יציאה עם dynamic pricing."""
        position = self.open_positions.get(token_id)
        if not position:
            self.logger.warning(f"Position not found: {token_id[:12]}...")
            return False
        
        entry_price = position.get('entry_price')
        elapsed_minutes = (asyncio.get_event_loop().time() - position.get('entry_time', 0)) / 60
        
        # Load fee/rebate from environment or default to 1% slippage
        import os
        estimated_fee = float(os.getenv('DEFAULT_SLIPPAGE', '0.01'))
        
        # Calculate exit price dynamically
        if exit_price is None:
            try:
                book = self.executor.client.get_order_book(token_id)
                asks = book.get('asks', [])
                best_ask = float(asks[0].get('price', 0)) if asks else None
                
                if best_ask:
                    current_spread = best_ask - entry_price
                    
                    # Account for fees: reduce target profit by estimated fee
                    adjusted_target = self.target_profit + estimated_fee
                    
                    # If spread > adjusted target, take target profit
                    if current_spread >= adjusted_target:
                        exit_price = entry_price + self.target_profit
                        self.logger.debug(f"Fee-aware exit: spread ${current_spread:.3f} >= ${adjusted_target:.3f}")
                    else:
                        # Otherwise, use BestAsk - 0.01
                        exit_price = best_ask - 0.01
            except:
                exit_price = entry_price + 0.10
        
        self.logger.info(
            f"🚪 Spread exit: {position.get('question', '')[:50]}"
        )
        self.logger.info(
            f"   Entry: ${entry_price:.4f}, Exit: ${exit_price:.4f}, "
            f"Held: {elapsed_minutes:.1f}min"
        )
        
        result = await self.executor.close_position(
            token_id, exit_price, position_data=position
        )
        
        if result and result.get('success'):
            pnl = result.get('pnl', 0)
            pnl_pct = result.get('pnl_pct', 0)
            
            self.stats['trades_exited'] += 1
            self.stats['total_pnl'] += pnl
            
            self.logger.info(f"✅ Spread closed: ${pnl:.2f} ({pnl_pct:+.1f}%)")
            
            del self.open_positions[token_id]
            self.position_manager.remove_position(token_id)
            return True
        
        self.logger.warning(f"Failed to close spread")
        return False
