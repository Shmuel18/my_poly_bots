"""
Rate Limiter

מונע עומס יתר על ה-API ע"י הגבלת מספר הבקשות בזמן נתון.
"""
import asyncio
import time
from collections import deque
from typing import Optional
import logging

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    Rate Limiter שמבטיח שלא נעבור מגבול מסוים של בקשות בחלון זמן.
    
    דוגמת שימוש:
        limiter = RateLimiter(max_calls=10, time_window=60)
        
        async with limiter:
            # Execute API call here
            result = await api.post_order(...)
    """
    
    def __init__(
        self,
        max_calls: int = 100,
        time_window: float = 60.0,
        name: str = "RateLimiter"
    ):
        """
        אתחול Rate Limiter.
        
        Args:
            max_calls: מספר קריאות מקסימלי
            time_window: חלון זמן בשניות
            name: שם המזהה (ללוגים)
        """
        self.max_calls = max_calls
        self.time_window = time_window
        self.name = name
        self.calls: deque = deque()
        self._lock = asyncio.Lock()
        self.total_waits = 0
        self.total_calls = 0
    
    async def __aenter__(self):
        """Context manager entry - מחכה עד שיש מקום לבקשה חדשה."""
        await self.acquire()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        pass
    
    async def acquire(self) -> None:
        """
        ממתין עד שיש מקום לבקשה חדשה תוך שמירה על המגבלה.
        """
        async with self._lock:
            now = time.time()
            
            # Remove old calls outside time window
            while self.calls and self.calls[0] <= now - self.time_window:
                self.calls.popleft()
            
            # Check if we need to wait
            if len(self.calls) >= self.max_calls:
                # Calculate wait time until oldest call expires
                oldest_call = self.calls[0]
                wait_time = self.time_window - (now - oldest_call) + 0.1  # Add small buffer
                
                if wait_time > 0:
                    self.total_waits += 1
                    logger.warning(
                        f"⏳ {self.name}: Rate limit reached ({len(self.calls)}/{self.max_calls}), "
                        f"waiting {wait_time:.1f}s..."
                    )
                    await asyncio.sleep(wait_time)
                    
                    # Re-clean after wait
                    now = time.time()
                    while self.calls and self.calls[0] <= now - self.time_window:
                        self.calls.popleft()
            
            # Record this call
            self.calls.append(now)
            self.total_calls += 1
    
    def get_stats(self) -> dict:
        """מחזיר סטטיסטיקות על השימוש ב-Rate Limiter."""
        now = time.time()
        # Count active calls in current window
        active_calls = sum(1 for call_time in self.calls if call_time > now - self.time_window)
        
        return {
            'name': self.name,
            'total_calls': self.total_calls,
            'total_waits': self.total_waits,
            'active_calls': active_calls,
            'max_calls': self.max_calls,
            'time_window': self.time_window,
            'capacity_pct': (active_calls / self.max_calls * 100) if self.max_calls > 0 else 0
        }
    
    def reset(self) -> None:
        """מאפס את ה-Rate Limiter."""
        self.calls.clear()
        self.total_calls = 0
        self.total_waits = 0


class MultiTierRateLimiter:
    """
    Rate Limiter עם מספר שכבות (לדוגמה: 10/sec, 100/min, 1000/hour).
    """
    
    def __init__(self, tiers: list[tuple[int, float]], name: str = "MultiTier"):
        """
        אתחול Multi-Tier Rate Limiter.
        
        Args:
            tiers: רשימה של (max_calls, time_window) לכל שכבה
            name: שם מזהה
        """
        self.name = name
        self.limiters = [
            RateLimiter(max_calls=calls, time_window=window, name=f"{name}_T{i+1}")
            for i, (calls, window) in enumerate(tiers)
        ]
    
    async def __aenter__(self):
        """Context manager - עובר על כל השכבות."""
        for limiter in self.limiters:
            await limiter.acquire()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass
    
    def get_stats(self) -> list[dict]:
        """מחזיר סטטיסטיקות מכל השכבות."""
        return [limiter.get_stats() for limiter in self.limiters]


# Polymarket default rate limits (adjust based on actual limits)
POLYMARKET_RATE_LIMITER = MultiTierRateLimiter(
    tiers=[
        (5, 1.0),      # 5 per second
        (50, 60.0),    # 50 per minute
        (500, 3600.0)  # 500 per hour
    ],
    name="Polymarket"
)
