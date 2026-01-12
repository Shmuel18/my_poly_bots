"""
Kalshi API Client for Cross-Platform Arbitrage

Enables simultaneous trading on Polymarket and Kalshi,
detecting price discrepancies between platforms.
"""

import asyncio
import logging
import os
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone

import aiohttp

logger = logging.getLogger(__name__)


class KalshiClient:
    """Async client for Kalshi prediction market API."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = None,
        demo: bool = False,
    ):
        """
        Initialize Kalshi client.

        Args:
            api_key: Kalshi API key (defaults to KALSHI_API_KEY env)
            base_url: API endpoint (production or demo)
            demo: Use demo environment
        """
        self.api_key = api_key or os.getenv("KALSHI_API_KEY")
        
        if demo:
            self.base_url = "https://demo-api.kalshi.co/trade-api/v2"
        else:
            self.base_url = base_url or os.getenv("KALSHI_API_URL", "https://api.kalshi.com/trade-api/v2")
        
        self.session: Optional[aiohttp.ClientSession] = None
        self.authenticated = False
        self.user_id: Optional[str] = None
        self.logger = logger

    async def __aenter__(self):
        """Context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        await self.close()

    async def connect(self):
        """Create HTTP session."""
        if self.session is None:
            self.session = aiohttp.ClientSession(
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                }
            )
            self.logger.info(f"🔌 Connected to Kalshi API: {self.base_url}")

    async def close(self):
        """Close HTTP session."""
        if self.session:
            await self.session.close()
            self.session = None
            self.logger.info("🔌 Kalshi connection closed")

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        data: Optional[Dict] = None,
    ) -> Dict:
        """Make authenticated API request."""
        if not self.session:
            await self.connect()

        url = f"{self.base_url}/{endpoint.lstrip('/')}"

        try:
            async with self.session.request(
                method,
                url,
                params=params,
                json=data,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                response.raise_for_status()
                return await response.json()

        except aiohttp.ClientError as e:
            self.logger.error(f"Kalshi API error: {e}")
            raise

    # ==================== MARKET DATA ====================

    async def get_markets(
        self,
        limit: int = 200,
        status: str = "open",
        series_ticker: Optional[str] = None,
    ) -> List[Dict]:
        """
        Get list of markets.

        Args:
            limit: Max markets to return
            status: Market status (open, closed, settled)
            series_ticker: Filter by series (e.g., "FEDRATE")

        Returns:
            List of market dictionaries
        """
        params = {
            "limit": limit,
            "status": status,
        }
        if series_ticker:
            params["series_ticker"] = series_ticker

        response = await self._request("GET", "/markets", params=params)
        return response.get("markets", [])

    async def get_market(self, ticker: str) -> Dict:
        """Get single market by ticker."""
        response = await self._request("GET", f"/markets/{ticker}")
        return response.get("market", {})

    async def get_orderbook(self, ticker: str) -> Dict:
        """
        Get order book for market.

        Returns:
            {
                "yes": [{"price": 0.52, "size": 100}, ...],
                "no": [{"price": 0.48, "size": 50}, ...]
            }
        """
        response = await self._request("GET", f"/markets/{ticker}/orderbook")
        
        # Convert Kalshi format to standardized format
        orderbook = response.get("orderbook", {})
        
        # Kalshi uses cents (0-100), convert to probability (0-1)
        yes_orders = [
            {
                "price": order["price"] / 100.0,
                "size": order["quantity"],
            }
            for order in orderbook.get("yes", [])
        ]
        
        no_orders = [
            {
                "price": order["price"] / 100.0,
                "size": order["quantity"],
            }
            for order in orderbook.get("no", [])
        ]
        
        return {
            "yes": yes_orders,
            "no": no_orders,
        }

    async def get_trades(
        self,
        ticker: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict]:
        """Get recent trades."""
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker

        response = await self._request("GET", "/portfolio/trades", params=params)
        return response.get("trades", [])

    # ==================== TRADING ====================

    async def create_order(
        self,
        ticker: str,
        side: str,  # "yes" or "no"
        action: str,  # "buy" or "sell"
        quantity: int,
        price: int,  # In cents (0-100)
        order_type: str = "limit",
    ) -> Dict:
        """
        Create new order.

        Args:
            ticker: Market ticker (e.g., "INXD-23DEC31-B4500")
            side: "yes" or "no"
            action: "buy" or "sell"
            quantity: Number of contracts
            price: Price in cents (e.g., 52 = $0.52)
            order_type: "limit" or "market"

        Returns:
            Order confirmation
        """
        data = {
            "ticker": ticker,
            "client_order_id": f"{ticker}-{int(datetime.now(timezone.utc).timestamp())}",
            "side": side.lower(),
            "action": action.lower(),
            "count": quantity,
            "type": order_type,
        }
        
        if order_type == "limit":
            data["yes_price"] = price if side == "yes" else None
            data["no_price"] = price if side == "no" else None

        response = await self._request("POST", "/portfolio/orders", data=data)
        
        order = response.get("order", {})
        self.logger.info(f"✅ Kalshi order created: {ticker} {action} {quantity} {side} @ {price}¢")
        
        return order

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel existing order."""
        try:
            await self._request("DELETE", f"/portfolio/orders/{order_id}")
            self.logger.info(f"❌ Kalshi order cancelled: {order_id}")
            return True
        except Exception as e:
            self.logger.error(f"Failed to cancel Kalshi order {order_id}: {e}")
            return False

    async def get_balance(self) -> Dict:
        """Get account balance."""
        response = await self._request("GET", "/portfolio/balance")
        balance = response.get("balance", {})
        
        # Convert cents to dollars
        return {
            "available": balance.get("balance", 0) / 100.0,
            "total": (balance.get("balance", 0) + balance.get("payout", 0)) / 100.0,
        }

    async def get_positions(self) -> List[Dict]:
        """Get open positions."""
        response = await self._request("GET", "/portfolio/positions")
        return response.get("positions", [])

    # ==================== UTILITY ====================

    def normalize_market_data(self, kalshi_market: Dict) -> Dict:
        """
        Convert Kalshi market format to standardized format.

        Returns format compatible with Polymarket scanner.
        """
        ticker = kalshi_market.get("ticker", "")
        title = kalshi_market.get("title", "")
        
        # Extract expiry time
        close_time = kalshi_market.get("close_time")
        if close_time:
            end_date = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        else:
            end_date = None
        
        # Kalshi has single ticker with yes/no, Polymarket has separate tokens
        # We'll create pseudo token IDs
        return {
            "market_id": ticker,
            "question": title,
            "description": kalshi_market.get("subtitle", ""),
            "end_date_iso": end_date.isoformat() if end_date else None,
            "endDate": end_date.isoformat() if end_date else None,
            "platform": "kalshi",
            "ticker": ticker,
            "tokens": {
                "yes": f"{ticker}:YES",
                "no": f"{ticker}:NO",
            },
            "status": kalshi_market.get("status", "open"),
            "raw": kalshi_market,
        }


# Singleton instance
_kalshi_client_instance: Optional[KalshiClient] = None


async def get_kalshi_client(
    api_key: Optional[str] = None,
    demo: bool = False,
) -> Optional[KalshiClient]:
    """
    Get or create singleton Kalshi client.

    Returns None if API key is not set.
    """
    global _kalshi_client_instance

    if not api_key and not os.getenv("KALSHI_API_KEY"):
        logger.warning("KALSHI_API_KEY not set - cross-platform arbitrage disabled")
        return None

    if _kalshi_client_instance is None:
        try:
            _kalshi_client_instance = KalshiClient(api_key=api_key, demo=demo)
            await _kalshi_client_instance.connect()
        except Exception as e:
            logger.warning(f"Kalshi client initialization failed: {e}")
            return None

    return _kalshi_client_instance
