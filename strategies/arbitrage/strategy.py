"""
Arbitrage Strategy

אסטרטגיית ארביטראז' בין שווקים היררכיים.
"""
import logging
from typing import List, Dict, Any
from datetime import datetime, timezone, timedelta

from strategies.base_strategy import BaseStrategy

logger = logging.getLogger(__name__)


class ArbitrageStrategy(BaseStrategy):
    """
    אסטרטגיית ארביטראז':
    מחפש הבדלי מחירים בין שווקים קשורים.
    """
    
    def __init__(
        self,
        min_profit_pct: float = 2.0,  # 2% minimum profit
        max_hours_until_close: int = 24,
        **kwargs
    ):
        """
        אתחול אסטרטגיית ארביטראז'.
        
        Args:
            min_profit_pct: אחוז רווח מינימלי
            max_hours_until_close: מקסימום שעות עד סגירה
        """
        super().__init__(strategy_name="ArbitrageStrategy", **kwargs)
        
        self.min_profit_pct = min_profit_pct
        self.max_hours_until_close = max_hours_until_close
        
        logger.info(f"⚙️ Configuration:")
        logger.info(f"   Min profit: {min_profit_pct}%")
        logger.info(f"   Max hours until close: {max_hours_until_close}h")
    
    async def scan(self) -> List[Dict[str, Any]]:
        """
        סורק הזדמנויות ארביטראז'.
        
        Returns:
            רשימת הזדמנויות
        """
        opportunities = []
        
        # Get events (hierarchical markets)
        events = self.scanner.get_events(limit=1000)
        
        # Filter by time
        now = datetime.now(timezone.utc)
        max_end = now + timedelta(hours=self.max_hours_until_close)
        
        for event in events:
            markets = event.get('markets', [])
            
            # Need at least 2 markets for arbitrage
            if len(markets) < 2:
                continue
            
            # Check end date
            end_date_str = event.get('endDate')
            if end_date_str:
                try:
                    end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
                    if end_date > max_end:
                        continue
                except:
                    continue
            
            # Look for price discrepancies using REAL orderbook prices
            for i in range(len(markets) - 1):
                market1 = markets[i]
                market2 = markets[i + 1]
                
                token_ids1 = self._get_token_ids(market1)
                token_ids2 = self._get_token_ids(market2)
                
                if not token_ids1 or not token_ids2:
                    continue
                
                # Get REAL prices from orderbook (not mid-prices)
                try:
                    # For buying: we pay the ASK price
                    book1 = self.executor.client.get_order_book(token_ids1[0])
                    asks1 = book1.get('asks', [])
                    buy_price = float(asks1[0].get('price', 0)) if asks1 else 0
                    
                    # For selling: we receive the BID price
                    book2 = self.executor.client.get_order_book(token_ids2[0])
                    bids2 = book2.get('bids', [])
                    sell_price = float(bids2[0].get('price', 0)) if bids2 else 0
                    
                    # Calculate REAL profit after spread
                    if buy_price > 0 and sell_price > buy_price:
                        profit_pct = ((sell_price / buy_price) - 1) * 100
                        
                        if profit_pct >= self.min_profit_pct:
                            opportunities.append({
                                'event_title': event.get('title', ''),
                                'market1_question': market1.get('question', ''),
                                'market2_question': market2.get('question', ''),
                                'buy_token': token_ids1[0],
                                'sell_token': token_ids2[0],
                                'buy_price': buy_price,  # Real ASK
                                'sell_price': sell_price,  # Real BID
                                'profit_pct': profit_pct,
                                'token_id': token_ids1[0]  # For tracking
                            })
                except Exception as e:
                    logger.debug(f"Failed to get orderbook prices: {e}")
                    continue
        
        return opportunities
    
    def _get_prices(self, market: Dict) -> Dict[str, float]:
        """מחלץ מחירים משוק"""
        prices_raw = market.get('outcomePrices', [])
        
        if isinstance(prices_raw, str):
            import json
            try:
                prices_raw = json.loads(prices_raw)
            except:
                return {}
        
        if isinstance(prices_raw, list) and len(prices_raw) >= 2:
            try:
                return {
                    'YES': float(prices_raw[0]),
                    'NO': float(prices_raw[1])
                }
            except:
                pass
        
        return {}
    
    def _get_token_ids(self, market: Dict) -> List[str]:
        """מחלץ token IDs משוק"""
        token_ids = market.get('clobTokenIds', [])
        
        if isinstance(token_ids, str):
            import json
            try:
                token_ids = json.loads(token_ids)
            except:
                return []
        
        if isinstance(token_ids, list):
            return [str(tid) for tid in token_ids if tid]
        
        return []
    
    async def should_enter(self, opportunity: Dict[str, Any]) -> bool:
        """
        מחליט האם להיכנס לארביטראז'.
        
        Args:
            opportunity: הזדמנות
            
        Returns:
            True אם צריך להיכנס
        """
        # Check balance
        balance = await self.executor.get_balance()
        
        # Need enough for both legs
        required = opportunity.get('buy_price', 0) * 10  # Example: 10 units
        
        if balance < required * 2:  # Need 2x for both legs
            return False
        
        # Check liquidity on both sides
        buy_liq = self.executor.check_liquidity(
            token_id=opportunity.get('buy_token'),
            side='BUY',
            size=10
        )
        
        sell_liq = self.executor.check_liquidity(
            token_id=opportunity.get('sell_token'),
            side='SELL',
            size=10
        )
        
        if not buy_liq.get('available') or not sell_liq.get('available'):
            return False
        
        return True
    
    async def should_exit(self, position: Dict[str, Any]) -> bool:
        """
        ארביטראז' בדרך כלל מבוצע מיידית (שתי רגליים בו-זמנית).
        פונקציה זו מטפלת במקרה שצריך לצאת ידנית.
        
        Args:
            position: פוזיציה פתוחה
            
        Returns:
            True אם צריך לצאת
        """
        # In arbitrage, we usually exit immediately
        # This is for edge cases where we need to manually exit
        return False


async def main():
    """הרצת האסטרטגיה"""
    strategy = ArbitrageStrategy(
        min_profit_pct=2.0,         # 2% minimum
        max_hours_until_close=24,
        scan_interval=300,          # Scan every 5 minutes
        log_level="INFO"
    )
    
    try:
        await strategy.start()
    except KeyboardInterrupt:
        logger.info("\n👋 Shutting down...")
        strategy.stop()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
