"""
Trade Executor Module

מבצע עסקאות ב-Polymarket.
מטפל בהזמנות, בדיקת נזילות, ומעקב אחר פוזיציות.
"""
import logging
from typing import Optional, Dict, Any
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

logger = logging.getLogger(__name__)


class TradeExecutor:
    """
    מבצע עסקאות ב-Polymarket.
    
    דוגמת שימוש:
        executor = TradeExecutor(connection)
        result = executor.execute_trade(
            token_id='0x123...',
            side='BUY',
            size=100,
            price=0.05
        )
    """
    
    def __init__(self, connection):
        """
        אתחול Executor.
        
        Args:
            connection: אובייקט PolymarketConnection
        """
        self.connection = connection
        self.client = connection.get_client()
        self.open_positions: Dict[str, Dict[str, Any]] = {}
        
    async def get_balance(self) -> float:
        """מחזיר את יתרת USDC"""
        return await self.connection.get_balance()
    
    def execute_trade(
        self,
        token_id: str,
        side: str,
        size: float,
        price: float,
        order_type: str = 'GTC'
    ) -> Optional[Dict]:
        """
        מבצע עסקה.
        
        Args:
            token_id: מזהה הטוקן
            side: 'BUY' או 'SELL'
            size: כמות (מספר יחידות)
            price: מחיר ליחידה
            order_type: סוג הזמנה (GTC, FOK, IOC)
            
        Returns:
            תוצאת העסקה או None אם נכשל
        """
        try:
            # Create order
            order_args = OrderArgs(
                token_id=token_id,
                price=round(float(price), 3),
                size=round(float(size), 2),
                side=BUY if side.upper() == 'BUY' else SELL
            )
            
            logger.info(f"📝 Creating order: {side} {size} @ ${price:.4f}")
            
            # Sign order with Proxy signature
            signed_order = self.client.create_order(order_args)
            
            # Submit order
            logger.info(f"🚀 Posting order...")
            
            order_type_enum = OrderType.GTC
            if order_type.upper() == 'FOK':
                order_type_enum = OrderType.FOK
            elif order_type.upper() == 'IOC':
                order_type_enum = OrderType.IOC
            
            response = self.client.post_order(signed_order, order_type_enum)
            
            if response and response.get('success'):
                order_id = response.get('orderID', 'unknown')
                logger.info(f"✅ Order executed: {order_id}")
                
                # Track position
                if side.upper() == 'BUY':
                    self.open_positions[token_id] = {
                        'entry_price': price,
                        'size': size,
                        'order_id': order_id
                    }
                
                return response
            else:
                error_msg = response.get('errorMsg', 'Unknown error')
                logger.error(f"❌ Order failed: {error_msg}")
                return None
                
        except Exception as e:
            logger.error(f"❌ Trade execution failed: {e}")
            return None
    
    def check_liquidity(
        self,
        token_id: str,
        side: str,
        size: float
    ) -> Dict[str, Any]:
        """
        בודק נזילות לפני ביצוע עסקה.
        
        Args:
            token_id: מזהה טוקן
            side: BUY/SELL
            size: כמות מבוקשת
            
        Returns:
            מידע על נזילות
        """
        try:
            # Get orderbook
            book = self.client.get_order_book(token_id)
            
            if not book:
                return {'available': False, 'reason': 'No orderbook data'}
            
            # Check relevant side
            orders = book.get('bids' if side.upper() == 'SELL' else 'asks', [])
            
            if not orders:
                return {'available': False, 'reason': 'No orders on this side'}
            
            # Calculate available liquidity
            available_size = sum(float(order.get('size', 0)) for order in orders[:5])
            best_price = float(orders[0].get('price', 0)) if orders else 0
            
            return {
                'available': available_size >= size,
                'available_size': available_size,
                'best_price': best_price,
                'size_ratio': available_size / size if size > 0 else 0
            }
            
        except Exception as e:
            logger.warning(f"Failed to check liquidity: {e}")
            return {'available': False, 'reason': str(e)}
    
    def get_position(self, token_id: str) -> Optional[Dict[str, Any]]:
        """מחזיר פוזיציה פתוחה"""
        return self.open_positions.get(token_id)
    
    def close_position(
        self,
        token_id: str,
        price: Optional[float] = None
    ) -> Optional[Dict]:
        """
        סוגר פוזיציה פתוחה.
        
        Args:
            token_id: מזהה טוקן
            price: מחיר ליחידה (אם None, משתמש במחיר שוק)
            
        Returns:
            תוצאת העסקה
        """
        position = self.open_positions.get(token_id)
        
        if not position:
            logger.warning(f"No open position for {token_id}")
            return None
        
        # Get current price if not provided
        if price is None:
            try:
                book = self.client.get_order_book(token_id)
                bids = book.get('bids', [])
                price = float(bids[0].get('price', 0)) if bids else None
            except:
                logger.error("Could not get market price")
                return None
        
        if not price:
            return None
        
        # Execute sell order
        result = self.execute_trade(
            token_id=token_id,
            side='SELL',
            size=position['size'],
            price=price
        )
        
        if result and result.get('success'):
            # Calculate P&L
            entry_price = position['entry_price']
            size = position['size']
            pnl = (price - entry_price) * size
            pnl_pct = ((price / entry_price) - 1) * 100 if entry_price > 0 else 0
            
            logger.info(f"💰 Position closed: P&L ${pnl:.2f} ({pnl_pct:+.1f}%)")
            
            # Remove from open positions
            del self.open_positions[token_id]
            
            return {
                **result,
                'pnl': pnl,
                'pnl_pct': pnl_pct
            }
        
        return result
    
    def get_all_positions(self) -> Dict[str, Dict[str, Any]]:
        """מחזיר את כל הפוזיציות הפתוחות"""
        return self.open_positions.copy()
    
    async def check_and_settle_positions(self) -> None:
        """בודק ומסדר פוזיציות בשווקים סגורים"""
        for token_id in list(self.open_positions.keys()):
            try:
                # Try to get balance for this token
                balance = self.client.get_balance(token_id)
                
                if balance and float(balance) > 0:
                    logger.info(f"💰 Settling position for {token_id[:8]}...")
                    # Try to settle/redeem
                    # Note: Settlement is usually automatic in Polymarket
                    # This is a placeholder for manual settlement if needed
                    
            except Exception as e:
                logger.debug(f"Could not check position {token_id[:8]}: {e}")
