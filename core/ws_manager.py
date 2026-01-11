"""
WebSocket Manager Module

מנהל חיבור WebSocket לקבלת עדכוני מחירים בזמן אמת.
"""
import asyncio
import logging
import time
import websockets
from typing import Optional, List, Dict, Callable, Set
import json

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class WebSocketManager:
    """
    מנהל WebSocket לעדכוני מחירים בזמן אמת.
    
    דוגמת שימוש:
        ws = WebSocketManager()
        await ws.connect()
        await ws.subscribe(['token_id_1', 'token_id_2'])
        await ws.receive_data(callback=my_price_handler)
    """
    
    def __init__(
        self,
        ping_interval: int = 20,
        ping_timeout: int = 20,
        auto_reconnect: bool = True,
        max_reconnect_delay: int = 60
    ):
        """
        אתחול WebSocket Manager.
        
        Args:
            ping_interval: מרווח ping בשניות
            ping_timeout: timeout ל-ping
            auto_reconnect: האם להתחבר מחדש אוטומטית
            max_reconnect_delay: המתנה מקסימלית בין ניסיונות
        """
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.subscribed_tokens: Set[str] = set()
        self.is_connected = False
        self.auto_reconnect = auto_reconnect
        self.max_reconnect_delay = max_reconnect_delay
        self.last_message_time = 0
        self._running = False
        self._reconnect_task: Optional[asyncio.Task] = None
        
    async def connect(self, max_retries: int = 3) -> bool:
        """
        מתחבר ל-WebSocket.
        
        Args:
            max_retries: מספר ניסיונות מקסימלי
            
        Returns:
            True אם החיבור הצליח
        """
        for attempt in range(max_retries):
            try:
                logger.info(f"🔌 Connecting to WebSocket... (attempt {attempt + 1}/{max_retries})")
                
                self.ws = await asyncio.wait_for(
                    websockets.connect(
                        WS_URL,
                        ping_interval=self.ping_interval,
                        ping_timeout=self.ping_timeout
                    ),
                    timeout=15
                )
                
                self.is_connected = True
                logger.info("✅ WebSocket connected")
                return True
                
            except Exception as e:
                logger.warning(f"Connection attempt {attempt + 1} failed: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)  # Exponential backoff
        
        logger.error("❌ Failed to connect to WebSocket")
        return False
    
    async def subscribe(self, token_ids: List[str]) -> bool:
        """
        מרשם ל-token IDs לקבלת עדכוני מחירים.
        
        Args:
            token_ids: רשימת token IDs
            
        Returns:
            True אם ההרשמה הצליחה
        """
        if not self.ws or not self.is_connected:
            logger.error("Not connected to WebSocket")
            return False
        
        try:
            # Polymarket WebSocket subscription format
            payload = {
                "type": "market",
                "assets_ids": token_ids
            }
            
            await self.ws.send(json.dumps(payload))
            self.subscribed_tokens.update(token_ids)
            
            logger.info(f"📡 Subscribed to {len(token_ids)} tokens")
            return True
            
        except Exception as e:
            logger.error(f"Subscription failed: {e}")
            return False
    
    async def subscribe_batch(
        self,
        token_ids: List[str],
        batch_size: int = 100
    ) -> int:
        """
        מרשם ל-tokens בבאצ'ים (למקרה של מספר גדול).
        
        Args:
            token_ids: רשימת token IDs
            batch_size: גודל batch
            
        Returns:
            מספר tokens שנרשמו בהצלחה
        """
        subscribed_count = 0
        
        for i in range(0, len(token_ids), batch_size):
            batch = token_ids[i:i + batch_size]
            if await self.subscribe(batch):
                subscribed_count += len(batch)
            else:
                logger.warning(f"Failed to subscribe batch {i // batch_size + 1}")
        
        return subscribed_count
    
    async def receive_data(
        self,
        callback: Callable[[str, float], None],
        timeout: Optional[int] = None
    ) -> None:
        """
        מאזין להודעות WebSocket וקורא ל-callback עבור כל עדכון מחיר.
        
        Args:
            callback: פונקציה שתקבל (token_id, price)
            timeout: timeout בשניות (None = אין הגבלה)
        """
        if not self.ws or not self.is_connected:
            logger.error("Not connected to WebSocket")
            return
        
        logger.info("👂 Listening for price updates...")
        message_count = 0
        
        try:
            while True:
                try:
                    message = await asyncio.wait_for(
                        self.ws.recv(),
                        timeout=timeout
                    )
                    
                    # Update last message timestamp for health monitoring
                    self.last_message_time = time.time()
                    
                    message_count += 1
                    
                    # Parse message
                    data = json.loads(message)
                    
                    # Log first few messages for debugging
                    if message_count <= 5:
                        logger.debug(f"Message {message_count}: {data}")
                    
                    # Extract price data
                    # Format varies - adapt based on actual Polymarket WS format
                    if isinstance(data, dict):
                        # Common format: {"asset_id": "...", "price": 0.123}
                        asset_id = data.get('asset_id') or data.get('token_id')
                        price = data.get('price') or data.get('bid')
                        
                        if asset_id and price is not None:
                            await callback(asset_id, float(price))
                    
                except asyncio.TimeoutError:
                    logger.warning("WebSocket receive timeout")
                    break
                except json.JSONDecodeError:
                    logger.warning(f"Failed to parse message: {message}")
                    continue
                    
        except websockets.exceptions.ConnectionClosed:
            logger.warning("WebSocket connection closed")
            self.is_connected = False
        except Exception as e:
            logger.error(f"Error receiving data: {e}")
            self.is_connected = False
    
    async def close(self) -> None:
        """סוגר את החיבור ל-WebSocket."""
        if self.ws:
            try:
                await self.ws.close()
                logger.info("WebSocket closed")
            except Exception as e:
                logger.warning(f"Error closing WebSocket: {e}")
        
        self.is_connected = False
        self.subscribed_tokens.clear()
    
    async def reconnect(self, max_retries: int = 3) -> bool:
        """
        מנסה להתחבר מחדש ל-WebSocket.
        
        Args:
            max_retries: מספר ניסיונות
            
        Returns:
            True אם החיבור מחדש הצליח
        """
        logger.info("🔄 Reconnecting to WebSocket...")
        
        await self.close()
        
        if await self.connect(max_retries):
            # Re-subscribe to previous tokens
            if self.subscribed_tokens:
                logger.info(f"Re-subscribing to {len(self.subscribed_tokens)} tokens...")
                return await self.subscribe(list(self.subscribed_tokens))
            return True
        
        return False
    
    def is_healthy(self, max_silence: int = 60) -> bool:
        """
        בודק אם החיבור "בריא" (קיבל הודעות לאחרונה).
        
        Args:
            max_silence: זמן מקסימלי ללא הודעות (שניות)
            
        Returns:
            True אם החיבור נראה פעיל
        """
        if not self.is_connected or not self.ws:
            return False
        
        if self.last_message_time == 0:
            return True  # Just connected, give it time
        
        silence_duration = time.time() - self.last_message_time
        return silence_duration < max_silence
    
    async def start_reconnect_loop(self) -> None:
        """
        לולאה שרצה ברקע ומתחברת מחדש במקרה של ניתוק.
        """
        if not self.auto_reconnect:
            return
        
        self._running = True
        reconnect_delay = 1
        
        while self._running:
            try:
                # Check connection health
                if not self.is_healthy(max_silence=90):
                    logger.warning("⚠️ WebSocket unhealthy, attempting reconnect...")
                    if await self.reconnect():
                        reconnect_delay = 1  # Reset delay on success
                    else:
                        reconnect_delay = min(reconnect_delay * 2, self.max_reconnect_delay)
                        logger.error(f"Reconnect failed, waiting {reconnect_delay}s...")
                        await asyncio.sleep(reconnect_delay)
                else:
                    # Connection healthy, check again in 30s
                    await asyncio.sleep(30)
            
            except asyncio.CancelledError:
                logger.info("Reconnect loop cancelled")
                break
            except Exception as e:
                logger.error(f"Error in reconnect loop: {e}")
                await asyncio.sleep(10)
        
        self._running = False
    
    async def stop(self) -> None:
        """
        עוצר את לולאת ה-reconnect וסוגר את החיבור.
        """
        self._running = False
        
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
        
        await self.close()
