"""
WebSocket Manager for Calendar Arbitrage Strategy

Real-time price monitoring with sub-second latency.
Triggers early exit when bids collapse or spreads narrow.
"""

import asyncio
import json
import logging
from typing import Callable, Dict, Optional, List, Any
from datetime import datetime, timezone

import websockets

logger = logging.getLogger(__name__)


class CalendarArbitrageWebSocketManager:
    """Manages WebSocket connections for real-time calendar arbitrage monitoring."""

    def __init__(
        self,
        ws_url: str = "wss://clob.polymarket.com/ws",
        reconnect_interval: float = 5.0,
        reconnect_max_attempts: int = 10,
    ):
        """
        Initialize WebSocket manager.

        Args:
            ws_url: WebSocket endpoint
            reconnect_interval: Seconds between reconnection attempts
            reconnect_max_attempts: Max retries before giving up
        """
        self.ws_url = ws_url
        self.reconnect_interval = reconnect_interval
        self.reconnect_max_attempts = reconnect_max_attempts

        self.ws_connection = None
        self.is_connected = False
        self.is_running = False
        self.price_update_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None
        self.tokens_to_monitor: set = set()

        # Price cache for logging
        self.last_prices = {}  # {token_id: {"bid": X, "ask": Y, "time": Z}}
        
        # Event to signal when connected
        self._connected_event = asyncio.Event()

        self.logger = logger
    
    def set_price_update_callback(self, callback: Callable[[str, Dict[str, Any]], None]):
        """Set the callback function for price updates."""
        self.price_update_callback = callback
    
    def add_token_to_monitor(self, token_id: str):
        """Add a token to the monitoring list."""
        self.tokens_to_monitor.add(token_id)
    
    def add_tokens_to_monitor(self, token_ids: List[str]):
        """Add multiple tokens to the monitoring list."""
        self.tokens_to_monitor.update(token_ids)

    async def connect(self) -> bool:
        """Establish WebSocket connection."""
        if self.is_connected:
            return True

        try:
            self.logger.info(f"🔌 Connecting to WebSocket: {self.ws_url}")
            self.ws_connection = await websockets.connect(self.ws_url)
            self.is_connected = True
            self._connected_event.set()
            self.logger.info("✅ WebSocket connected")
            return True
        except Exception as e:
            self.logger.error(f"❌ WebSocket connection failed: {e}")
            self.is_connected = False
            return False
    
    async def wait_connected(self, timeout: float = 30.0):
        """Wait until WebSocket is connected."""
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self.logger.warning(f"WebSocket connection timeout ({timeout}s)")
            raise

    async def disconnect(self):
        """Close WebSocket connection."""
        if self.ws_connection:
            try:
                await self.ws_connection.close()
            except Exception as e:
                self.logger.warning(f"Error closing WebSocket: {e}")
        self.is_connected = False
        self.is_running = False
        self.logger.info("🔌 WebSocket disconnected")
    
    async def close(self):
        """Alias for disconnect() - used by strategy cleanup."""
        await self.disconnect()

    async def subscribe(self):
        """Subscribe to price updates for monitored tokens."""
        if not self.ws_connection:
            return

        try:
            for token_id in self.tokens_to_monitor:
                # Polymarket WebSocket subscription format
                subscribe_msg = {
                    "type": "subscribe",
                    "product_ids": [token_id],
                }
                await self.ws_connection.send(json.dumps(subscribe_msg))
                self.logger.debug(f"Subscribed to {token_id}")

            self.logger.info(f"✅ Subscribed to {len(self.tokens_to_monitor)} tokens")
        except Exception as e:
            self.logger.error(f"Subscription failed: {e}")

    async def listen(self):
        """Listen for incoming WebSocket messages."""
        if not self.ws_connection:
            return

        try:
            async for message in self.ws_connection:
                try:
                    data = json.loads(message)
                    await self._handle_message(data)
                except json.JSONDecodeError:
                    self.logger.debug(f"Invalid JSON: {message}")
                except Exception as e:
                    self.logger.error(f"Error handling message: {e}")
        except asyncio.CancelledError:
            self.logger.info("Listen loop cancelled")
        except Exception as e:
            self.logger.error(f"Listen error: {e}")
            self.is_connected = False

    async def _handle_message(self, data: Dict[str, Any]):
        """Process incoming WebSocket message."""
        msg_type = data.get("type")

        if msg_type == "price":
            # Polymarket price update format
            token_id = data.get("product_id")
            bid = data.get("bid")
            ask = data.get("ask")
            mid = data.get("mid")

            if token_id and (bid is not None or ask is not None):
                # Cache price
                self.last_prices[token_id] = {
                    "bid": bid,
                    "ask": ask,
                    "mid": mid,
                    "time": datetime.now(timezone.utc).isoformat(),
                }

                # Invoke callback (non-blocking) - supports both sync and async
                if self.price_update_callback:
                    try:
                        # Check if callback is async
                        if asyncio.iscoroutinefunction(self.price_update_callback):
                            await self.price_update_callback(
                                token_id,
                                {"bid": bid, "ask": ask, "mid": mid},
                            )
                        else:
                            # Run sync callback in thread pool
                            await asyncio.to_thread(
                                self.price_update_callback,
                                token_id,
                                {"bid": bid, "ask": ask, "mid": mid},
                            )
                    except Exception as e:
                        self.logger.error(f"Callback error for {token_id}: {e}", exc_info=True)

        elif msg_type == "subscribed":
            self.logger.debug(f"Subscribed: {data}")

        elif msg_type == "heartbeat":
            # Keep-alive ping
            pass

        else:
            self.logger.debug(f"Unknown message type: {msg_type}")

    async def run(self):
        """Main run loop with reconnection logic."""
        self.is_running = True
        reconnect_count = 0

        while self.is_running:
            try:
                # Connect if needed
                if not self.is_connected:
                    connected = await self.connect()
                    if not connected:
                        reconnect_count += 1
                        if reconnect_count > self.reconnect_max_attempts:
                            self.logger.error(
                                f"❌ Max reconnection attempts ({self.reconnect_max_attempts}) exceeded"
                            )
                            self.is_running = False
                            break

                        wait_time = self.reconnect_interval * (2 ** min(reconnect_count - 1, 3))
                        self.logger.info(f"⏳ Reconnecting in {wait_time:.1f}s...")
                        await asyncio.sleep(wait_time)
                        continue

                    reconnect_count = 0

                # Subscribe to tokens
                await self.subscribe()

                # Listen for messages
                await self.listen()

            except asyncio.CancelledError:
                self.logger.info("Run loop cancelled")
                break
            except Exception as e:
                self.logger.error(f"Unexpected error: {e}")
                self.is_connected = False
                await asyncio.sleep(self.reconnect_interval)

        await self.disconnect()

    def get_last_price(self, token_id: str) -> Optional[Dict[str, Any]]:
        """Get cached last price for a token."""
        return self.last_prices.get(token_id)

    async def start(self):
        """Start WebSocket in background task."""
        if not self.is_running:
            asyncio.create_task(self.run())
            await asyncio.sleep(0.5)  # Give it time to connect
            self.logger.info("🚀 WebSocket manager started")

    async def stop(self):
        """Stop WebSocket manager."""
        self.is_running = False
        await self.disconnect()
        self.logger.info("⏹️ WebSocket manager stopped")
