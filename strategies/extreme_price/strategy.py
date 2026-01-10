"""
Extreme Price Strategy

אסטרטגיה לקנייה במחירים קיצוניים והמתנה להכפלה.
מבוסס על הקוד של FlickTrade.
"""
import logging
from typing import List, Dict, Any

from strategies.base_strategy import BaseStrategy
from utils import calculate_position_size, parse_outcome_prices

logger = logging.getLogger(__name__)


class ExtremePriceStrategy(BaseStrategy):
    """
    אסטרטגיית מחירים קיצוניים:
    1. מחפש שווקים במחירים קיצוניים (0.004$ או 0.996$)
    2. קונה במחיר נמוך
    3. ממתין להכפלת המחיר
    4. מוכר ברווח
    """
    
    def __init__(
        self,
        buy_threshold: float = 0.004,  # $0.004 = 0.4 cents
        sell_multiplier: float = 2.0,   # Sell at 2x
        min_hours_until_close: int = 1,
        portfolio_percent: float = 0.005,  # 0.5% per trade
        min_position_usd: float = 1.0,
        **kwargs
    ):
        """
        אתחול אסטרטגיית מחירים קיצוניים.
        
        Args:
            buy_threshold: מחיר מקסימלי לקנייה
            sell_multiplier: מכפיל למכירה (2.0 = הכפלה)
            min_hours_until_close: מינימום שעות עד סגירה
            portfolio_percent: אחוז מהתיק לכל עסקה
            min_position_usd: מינימום דולרים לעסקה
        """
        super().__init__(strategy_name="ExtremePriceStrategy", **kwargs)
        
        self.buy_threshold = buy_threshold
        self.sell_multiplier = sell_multiplier
        self.min_hours_until_close = min_hours_until_close
        self.portfolio_percent = portfolio_percent
        self.min_position_usd = min_position_usd
        
        logger.info(f"⚙️ Configuration:")
        logger.info(f"   Buy threshold: ${buy_threshold} ({buy_threshold*100:.1f} cents)")
        logger.info(f"   Sell multiplier: {sell_multiplier}x")
        logger.info(f"   Min hours until close: {min_hours_until_close}h")
        logger.info(f"   Portfolio %: {portfolio_percent*100:.1f}%")
    
    async def scan(self) -> List[Dict[str, Any]]:
        """
        סורק שווקים עם מחירים קיצוניים.
        
        Returns:
            רשימת הזדמנויות
        """
        # Get all active markets
        markets = self.scanner.get_all_active_markets(max_markets=5000)
        
        # Filter by time
        markets = self.scanner.filter_markets(
            markets,
            min_hours_until_close=self.min_hours_until_close
        )
        
        # Find extreme prices
        extreme_markets = self.scanner.find_extreme_prices(
            markets,
            low_threshold=self.buy_threshold,
            high_threshold=0.99
        )
        
        opportunities = []
        
        for market in extreme_markets:
            extreme_price = market.get('extreme_price', 0)
            
            # Only buy low prices (not high prices)
            if extreme_price > self.buy_threshold:
                continue
            
            token_ids = market.get('clobTokenIds', [])
            if isinstance(token_ids, str):
                import json
                try:
                    token_ids = json.loads(token_ids)
                except:
                    continue
            
            if not token_ids or len(token_ids) < 2:
                continue
            
            # YES token (usually first or second)
            side = market.get('extreme_side', 'YES')
            token_id = token_ids[0] if side == 'YES' else token_ids[1]
            
            # Calculate position size
            balance = await self.executor.get_balance()
            size = calculate_position_size(
                balance=balance,
                percent_of_balance=self.portfolio_percent,
                price=extreme_price,
                min_size=5.0
            )
            
            opportunities.append({
                'token_id': token_id,
                'question': market.get('question', ''),
                'price': extreme_price,
                'side': side,
                'size': size,
                'target_price': extreme_price * self.sell_multiplier,
                'market': market
            })
        
        return opportunities
    
    async def should_enter(self, opportunity: Dict[str, Any]) -> bool:
        """
        מחליט האם להיכנס לעסקה.
        
        Args:
            opportunity: הזדמנות
            
        Returns:
            True אם צריך להיכנס
        """
        # Check if we have enough balance
        balance = await self.executor.get_balance()
        required = opportunity.get('price', 0) * opportunity.get('size', 0)
        
        if balance < required:
            logger.debug(f"Insufficient balance: ${balance:.2f} < ${required:.2f}")
            return False
        
        # Check liquidity
        liquidity = self.executor.check_liquidity(
            token_id=opportunity.get('token_id'),
            side='BUY',
            size=opportunity.get('size', 0)
        )
        
        if not liquidity.get('available', False):
            logger.debug(f"Insufficient liquidity: {liquidity.get('reason', 'Unknown')}")
            return False
        
        return True
    
    async def should_exit(self, position: Dict[str, Any]) -> bool:
        """
        מחליט האם לצאת מפוזיציה.
        
        Args:
            position: פוזיציה פתוחה
            
        Returns:
            True אם צריך לצאת
        """
        token_id = position.get('token_id')
        target_price = position.get('target_price', 0)
        
        # Get current price from orderbook
        try:
            book = self.executor.client.get_order_book(token_id)
            bids = book.get('bids', [])
            
            if not bids:
                return False
            
            current_price = float(bids[0].get('price', 0))
            
            # Check if target reached
            if current_price >= target_price:
                logger.info(f"🎯 Target reached! {position.get('entry_price', 0):.4f} → {current_price:.4f}")
                return True
            
        except Exception as e:
            logger.debug(f"Could not check price: {e}")
        
        return False


async def main():
    """הרצת האסטרטגיה"""
    strategy = ExtremePriceStrategy(
        buy_threshold=0.004,      # 0.4 cents
        sell_multiplier=2.0,       # 2x = 0.8 cents
        min_hours_until_close=1,
        portfolio_percent=0.005,   # 0.5% of portfolio
        scan_interval=300,         # Scan every 5 minutes
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
