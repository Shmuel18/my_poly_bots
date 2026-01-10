"""
WebSocket Manager Module

מנהל חיבור WebSocket לקבלת עדכוני מחירים בזמן אמת.
"""
import asyncio
import logging
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
        ping_timeout: int = 20
    ):
        """
        אתחול WebSocket Manager.
        
        Args:
            ping_interval: מרווח ping בשניות
            ping_timeout: timeout ל-ping
        """
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.subscribed_tokens: Set[str] = set()
        self.is_connected = False
        
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
