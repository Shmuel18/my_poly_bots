"""
Example: Custom Strategy Template

תבנית ליצירת אסטרטגיה מותאמת אישית.
"""
import asyncio
import logging
from typing import List, Dict, Any

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from strategies.base_strategy import BaseStrategy

logger = logging.getLogger(__name__)


class MyCustomStrategy(BaseStrategy):
    """
    האסטרטגיה המותאמת אישית שלך.
    
    הוסף את הלוגיקה שלך ב:
    - scan(): איך למצוא הזדמנויות
    - should_enter(): מתי להיכנס לעסקה
    - should_exit(): מתי לצאת מעסקה
    """
    
    def __init__(self, **kwargs):
        super().__init__(strategy_name="MyCustomStrategy", **kwargs)
        
        # הוסף פרמטרים מותאמים כאן
        self.my_param = kwargs.get('my_param', 10)
        
        logger.info(f"⚙️ My Parameter: {self.my_param}")
    
    async def scan(self) -> List[Dict[str, Any]]:
        """
        מצא הזדמנויות.
        
        כאן תוסיף את הלוגיקה לחיפוש הזדמנויות.
        """
        opportunities = []
        
        # דוגמה: חפש שווקים עם מילת מפתח מסוימת
        markets = self.scanner.search_by_keywords(
            keywords=['crypto', 'bitcoin'],
            max_results=100
        )
        
        for market in markets:
            # הוסף את הלוגיקה שלך לזיהוי הזדמנויות
            token_ids = market.get('clobTokenIds', [])
            
            if isinstance(token_ids, str):
                import json
                try:
                    token_ids = json.loads(token_ids)
                except:
                    continue
            
            if not token_ids:
                continue
            
            opportunities.append({
                'token_id': token_ids[0],
                'question': market.get('question', ''),
                'price': 0.05,  # החלף עם מחיר אמיתי
                'size': 10,
                'market': market
            })
        
        return opportunities
    
    async def should_enter(self, opportunity: Dict[str, Any]) -> bool:
        """
        מחליט האם להיכנס לעסקה.
        
        כאן תוסיף את הקריטריונים שלך להיכנס לעסקה.
        """
        # דוגמה: בדוק שיש מספיק יתרה
        balance = await self.executor.get_balance()
        required = opportunity.get('price', 0) * opportunity.get('size', 0)
        
        if balance < required:
            return False
        
        # הוסף קריטריונים נוספים כאן
        # לדוגמה: בדיקת נזילות, מחיר מינימום, וכו'
        
        return True
    
    async def should_exit(self, position: Dict[str, Any]) -> bool:
        """
        מחליט האם לצאת מפוזיציה.
        
        כאן תוסיף את הקריטריונים שלך לצאת מעסקה.
        """
        # דוגמה: צא אחרי זמן מסוים
        import time
        entry_time = position.get('entry_time', 0)
        current_time = time.time()
        
        # צא אחרי שעה
        if current_time - entry_time > 3600:
            return True
        
        # או: צא אם המחיר עלה ב-X%
        try:
            token_id = position.get('token_id')
            book = self.executor.client.get_order_book(token_id)
            bids = book.get('bids', [])
            
            if bids:
                current_price = float(bids[0].get('price', 0))
                entry_price = position.get('entry_price', 0)
                
                # צא אם רווח של 10%+
                if entry_price > 0 and (current_price / entry_price) >= 1.1:
                    return True
        except:
            pass
        
        return False


async def main():
    """הרצת האסטרטגיה"""
    strategy = MyCustomStrategy(
        my_param=20,
        scan_interval=300,
        log_level="INFO"
    )
    
    try:
        await strategy.start()
    except KeyboardInterrupt:
        logger.info("\n👋 Shutting down...")
        strategy.stop()


if __name__ == "__main__":
    asyncio.run(main())
