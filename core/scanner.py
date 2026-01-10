"""
Market Scanner Module

מודול לסריקת שווקים ב-Polymarket.
מספק פונקציות חיפוש מתקדמות למציאת הזדמנויות.
"""
import requests
import logging
from typing import List, Dict, Optional, Callable
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# Gamma API endpoint
GAMMA_API_URL = "https://gamma-api.polymarket.com"


class MarketScanner:
    """
    סורק שווקים ב-Polymarket.
    
    דוגמת שימוש:
        scanner = MarketScanner()
        markets = scanner.get_active_markets(limit=100)
        crypto_markets = scanner.filter_markets(markets, category="crypto")
    """
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'PolymarketBot/1.0'
        })
    
    def get_active_markets(
        self,
        limit: int = 500,
        offset: int = 0,
        timeout: int = 30
    ) -> List[Dict]:
        """
        מושך שווקים פעילים מ-Gamma API.
        
        Args:
            limit: מספר שווקים מקסימלי לכל בקשה
            offset: היסט להתחלה
            timeout: זמן המתנה מקסימלי
            
        Returns:
            רשימת שווקים
        """
        try:
            url = f"{GAMMA_API_URL}/markets"
            params = {
                'active': 'true',
                'closed': 'false',
                'limit': limit,
                'offset': offset
            }
            
            response = self.session.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            markets = response.json()
            
            logger.debug(f"Fetched {len(markets)} markets (offset={offset})")
            return markets
            
        except Exception as e:
            logger.error(f"Error fetching markets: {e}")
            return []
    
    def get_all_active_markets(
        self,
        max_markets: int = 5000,
        batch_size: int = 500
    ) -> List[Dict]:
        """
        מושך את כל השווקים הפעילים עם pagination.
        
        Args:
            max_markets: מספר שווקים מקסימלי
            batch_size: גודל batch לכל בקשה
            
        Returns:
            רשימת כל השווקים
        """
        all_markets = []
        offset = 0
        
        logger.info(f"🔍 Scanning markets (max: {max_markets})...")
        
        while len(all_markets) < max_markets:
            batch = self.get_active_markets(limit=batch_size, offset=offset)
            
            if not batch:
                break
            
            all_markets.extend(batch)
            
            if len(batch) < batch_size:
                # No more markets
                break
            
            offset += batch_size
        
        logger.info(f"✅ Found {len(all_markets)} active markets")
        return all_markets
    
    def get_events(
        self,
        limit: int = 500,
        offset: int = 0,
        active: bool = True,
        closed: bool = False
    ) -> List[Dict]:
        """
        מושך events (כל event יכול להכיל מספר markets).
        
        Args:
            limit: מספר events מקסימלי
            offset: היסט
            active: רק events פעילים
            closed: כולל events סגורים
            
        Returns:
            רשימת events
        """
        try:
            url = f"{GAMMA_API_URL}/events"
            params = {
                'limit': limit,
                'offset': offset,
                'active': str(active).lower(),
                'closed': str(closed).lower()
            }
            
            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()
            events = response.json()
            
            logger.debug(f"Fetched {len(events)} events")
            return events
            
        except Exception as e:
            logger.error(f"Error fetching events: {e}")
            return []
    
    def filter_markets(
        self,
        markets: List[Dict],
        filter_func: Optional[Callable[[Dict], bool]] = None,
        **kwargs
    ) -> List[Dict]:
        """
        מסנן שווקים לפי קריטריונים.
        
        Args:
            markets: רשימת שווקים לסינון
            filter_func: פונקציית סינון מותאמת אישית
            **kwargs: פרמטרים לסינון מהיר:
                - category: str
                - min_hours_until_close: int
                - max_hours_until_close: int
                - keyword: str (חיפוש בשאלה)
                
        Returns:
            רשימת שווקים מסוננת
        """
        filtered = markets.copy()
        
        # Custom filter function
        if filter_func:
            filtered = [m for m in filtered if filter_func(m)]
        
        # Quick filters
        if 'category' in kwargs:
            category = kwargs['category'].lower()
            filtered = [
                m for m in filtered 
                if category in str(m.get('question', '')).lower() or
                   category in str(m.get('category', '')).lower()
            ]
        
        if 'keyword' in kwargs:
            keyword = kwargs['keyword'].lower()
            filtered = [
                m for m in filtered
                if keyword in str(m.get('question', '')).lower()
            ]
        
        if 'min_hours_until_close' in kwargs or 'max_hours_until_close' in kwargs:
            now = datetime.now(timezone.utc)
            min_hours = kwargs.get('min_hours_until_close', 0)
            max_hours = kwargs.get('max_hours_until_close', float('inf'))
            
            def time_filter(market):
                end_date_str = market.get('endDate')
                if not end_date_str:
                    return False
                try:
                    end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
                    hours_until_close = (end_date - now).total_seconds() / 3600
                    return min_hours <= hours_until_close <= max_hours
                except:
                    return False
            
            filtered = [m for m in filtered if time_filter(m)]
        
        logger.debug(f"Filtered {len(markets)} → {len(filtered)} markets")
        return filtered
    
    def find_extreme_prices(
        self,
        markets: List[Dict],
        low_threshold: float = 0.01,
        high_threshold: float = 0.99
    ) -> List[Dict]:
        """
        מוצא שווקים עם מחירים קיצוניים.
        
        Args:
            markets: רשימת שווקים
            low_threshold: סף מחיר נמוך ($0.01 = 1 cent)
            high_threshold: סף מחיר גבוה ($0.99 = 99 cents)
            
        Returns:
            רשימת שווקים עם מחירים קיצוניים
        """
        extreme_markets = []
        
        for market in markets:
            outcome_prices = market.get('outcomePrices', [])
            
            # Parse if string
            if isinstance(outcome_prices, str):
                try:
                    import json
                    outcome_prices = json.loads(outcome_prices)
                except:
                    continue
            
            if not isinstance(outcome_prices, list) or len(outcome_prices) < 2:
                continue
            
            try:
                yes_price = float(outcome_prices[0])
                no_price = float(outcome_prices[1])
                
                # Check for extreme prices
                if yes_price <= low_threshold or yes_price >= high_threshold:
                    extreme_markets.append({
                        **market,
                        'extreme_price': yes_price,
                        'extreme_side': 'YES'
                    })
                elif no_price <= low_threshold or no_price >= high_threshold:
                    extreme_markets.append({
                        **market,
                        'extreme_price': no_price,
                        'extreme_side': 'NO'
                    })
            except (ValueError, TypeError):
                continue
        
        logger.info(f"💎 Found {len(extreme_markets)} markets with extreme prices")
        return extreme_markets
    
    def search_by_keywords(
        self,
        keywords: List[str],
        max_results: int = 1000
    ) -> List[Dict]:
        """
        חיפוש שווקים לפי מילות מפתח.
        
        Args:
            keywords: רשימת מילות מפתח
            max_results: מספר תוצאות מקסימלי
            
        Returns:
            שווקים תואמים
        """
        markets = self.get_all_active_markets(max_markets=max_results)
        
        matching = []
        for market in markets:
            question = market.get('question', '').lower()
            description = market.get('description', '').lower()
            
            # Check if all keywords match
            if all(
                any(kw.lower() in text for text in [question, description])
                for kw in keywords
            ):
                matching.append(market)
        
        logger.info(f"🔎 Found {len(matching)} markets matching keywords: {keywords}")
        return matching
