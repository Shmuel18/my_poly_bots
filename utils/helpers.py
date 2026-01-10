"""
Helper Functions Module

פונקציות עזר שימושיות לכל הבוטים.
"""
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional
import json


def calculate_pnl(
    entry_price: float,
    exit_price: float,
    size: float
) -> Dict[str, float]:
    """
    מחשב רווח/הפסד.
    
    Args:
        entry_price: מחיר כניסה
        exit_price: מחיר יציאה
        size: כמות
        
    Returns:
        מילון עם pnl ו-pnl_pct
    """
    pnl = (exit_price - entry_price) * size
    pnl_pct = ((exit_price / entry_price) - 1) * 100 if entry_price > 0 else 0
    
    return {
        'pnl': round(pnl, 2),
        'pnl_pct': round(pnl_pct, 2)
    }


def calculate_position_size(
    balance: float,
    percent_of_balance: float,
    price: float,
    min_size: float = 5.0
) -> float:
    """
    מחשב גודל פוזיציה לפי אחוז מהיתרה.
    
    Args:
        balance: יתרה נוכחית
        percent_of_balance: אחוז מהיתרה (0.01 = 1%)
        price: מחיר ליחידה
        min_size: מינימום יחידות
        
    Returns:
        מספר יחידות
    """
    if price <= 0:
        return min_size
    
    usd_to_invest = balance * percent_of_balance
    size = usd_to_invest / price
    
    return max(size, min_size)


def hours_until_close(end_date_str: str) -> Optional[float]:
    """
    מחשב כמה שעות עד סגירת השוק.
    
    Args:
        end_date_str: תאריך סגירה (ISO format)
        
    Returns:
        מספר שעות או None אם שגיאה
    """
    try:
        end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)
        delta = end_date - now
        return delta.total_seconds() / 3600
    except:
        return None


def format_market_info(market: Dict[str, Any]) -> str:
    """
    מעצב מידע על שוק לתצוגה.
    
    Args:
        market: מידע על שוק
        
    Returns:
        מחרוזת מעוצבת
    """
    question = market.get('question', 'Unknown')[:60]
    
    prices = market.get('outcomePrices', [])
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except:
            prices = []
    
    price_str = ""
    if prices and len(prices) >= 2:
        yes_price = float(prices[0])
        no_price = float(prices[1])
        price_str = f"YES: ${yes_price:.4f} | NO: ${no_price:.4f}"
    
    hours = hours_until_close(market.get('endDate', ''))
    time_str = f"{hours:.1f}h" if hours else "?"
    
    return f"{question} | {price_str} | Closes in: {time_str}"


def rate_limit(calls_per_second: float = 1.0):
    """
    דקורטור למניעת חריגת rate limit.
    
    Args:
        calls_per_second: מספר קריאות מקסימלי לשנייה
    """
    min_interval = 1.0 / calls_per_second
    last_called = [0.0]
    
    def decorator(func):
        def wrapper(*args, **kwargs):
            elapsed = time.time() - last_called[0]
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)
            
            result = func(*args, **kwargs)
            last_called[0] = time.time()
            return result
        
        return wrapper
    return decorator


def retry_on_failure(
    max_retries: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0
):
    """
    דקורטור לניסיון חוזר במקרה של כשל.
    
    Args:
        max_retries: מספר ניסיונות מקסימלי
        delay: המתנה בין ניסיונות (שניות)
        backoff: מכפיל להמתנה (exponential backoff)
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            current_delay = delay
            
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_retries - 1:
                        raise
                    
                    time.sleep(current_delay)
                    current_delay *= backoff
            
        return wrapper
    return decorator


def extract_token_ids(market: Dict[str, Any]) -> List[str]:
    """
    מחלץ token IDs משוק.
    
    Args:
        market: מידע על שוק
        
    Returns:
        רשימת token IDs
    """
    token_ids = market.get('clobTokenIds', [])
    
    # Handle string format
    if isinstance(token_ids, str):
        try:
            token_ids = json.loads(token_ids)
        except:
            return []
    
    # Ensure list of strings
    if isinstance(token_ids, list):
        return [str(tid) for tid in token_ids if tid]
    
    return []


def parse_outcome_prices(market: Dict[str, Any]) -> Dict[str, float]:
    """
    מפרק מחירי outcomes.
    
    Args:
        market: מידע על שוק
        
    Returns:
        מילון עם YES ו-NO prices
    """
    prices = market.get('outcomePrices', [])
    
    # Parse if string
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except:
            prices = []
    
    result = {}
    if isinstance(prices, list) and len(prices) >= 2:
        try:
            result['YES'] = float(prices[0])
            result['NO'] = float(prices[1])
        except (ValueError, TypeError):
            pass
    
    return result
