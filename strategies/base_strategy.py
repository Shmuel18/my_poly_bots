"""
Base Strategy Class

מחלקת בסיס שכל אסטרטגיה צריכה לרשת ממנה.
"""
import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any

from core import PolymarketConnection, MarketScanner, TradeExecutor, WebSocketManager
from utils import setup_logging, calculate_pnl
from utils.position_manager import PositionManager

logger = logging.getLogger(__name__)


class BaseStrategy(ABC):
    """
    מחלקת בסיס לכל האסטרטגיות.
    
    כל אסטרטגיה צריכה לממש:
    - scan(): חיפוש הזדמנויות
    - should_enter(): האם להיכנס לעסקה
    - should_exit(): האם לצאת מעסקה
    """
    
    def __init__(
        self,
        strategy_name: str = "BaseStrategy",
        scan_interval: int = 300,
        log_level: str = "INFO",
        connection: Optional[PolymarketConnection] = None,
        dry_run: bool = False,
    ):
        """
        אתחול אסטרטגיה.
        
        Args:
            strategy_name: שם האסטרטגיה
            scan_interval: מרווח סריקה בשניות
            log_level: רמת לוג
        """
        self.strategy_name = strategy_name
        self.scan_interval = scan_interval
        self.dry_run = dry_run
        
        # Setup logging
        setup_logging(log_level=log_level)
        
        # Initialize core components (accept injected connection for multi-account support)
        self.connection = connection if connection is not None else PolymarketConnection()
        wallet_short = (self.connection.get_address() or '')[:6]
        self.logger = logging.getLogger(f"{strategy_name}_{wallet_short}")
        mode = "DRY-RUN" if self.dry_run else "LIVE"
        self.logger.info(f"🤖 Initializing {strategy_name} ({wallet_short}) - {mode}")
        self.scanner = MarketScanner()
        self.executor = TradeExecutor(self.connection, dry_run=self.dry_run)
        self.ws_manager = WebSocketManager()
        
        # Position manager for persistence
        self.position_manager = PositionManager(f"data/positions_{wallet_short}.json")
        
        # Sync positions from PositionManager
        self.open_positions = self.position_manager.get_positions_by_strategy(strategy_name)
        if self.open_positions:
            self.logger.info(f"📂 Restored {len(self.open_positions)} positions from disk")
        
        # State
        self.running = False
        self.open_positions: Dict[str, Dict[str, Any]] = {}
        self.seen_opportunities: set = set()
        
        # Statistics
        self.stats = {
            'scans': 0,
            'opportunities_found': 0,
            'trades_entered': 0,
            'trades_exited': 0,
            'total_pnl': 0.0
        }
    
    @abstractmethod
    async def scan(self) -> List[Dict[str, Any]]:
        """
        סורק ומחפש הזדמנויות.
        
        Returns:
            רשימת הזדמנויות
        """
        pass
    
    @abstractmethod
    async def should_enter(self, opportunity: Dict[str, Any]) -> bool:
        """
        מחליט האם להיכנס לעסקה.
        
        Args:
            opportunity: הזדמנות שנמצאה
            
        Returns:
            True אם צריך להיכנס
        """
        pass
    
    @abstractmethod
    async def should_exit(self, position: Dict[str, Any]) -> bool:
        """
        מחליט האם לצאת מעסקה.
        
        Args:
            position: פוזיציה פתוחה
            
        Returns:
            True אם צריך לצאת
        """
        pass
    
    async def enter_position(
        self,
        opportunity: Dict[str, Any]
    ) -> bool:
        """
        נכנס לפוזיציה.
        
        Args:
            opportunity: הזדמנות
            
        Returns:
            True אם נכנס בהצלחה
        """
        token_id = opportunity.get('token_id')
        price = opportunity.get('price')
        size = opportunity.get('size', 10)
        
        if not token_id or not price:
            self.logger.warning("Missing token_id or price")
            return False
        
        self.logger.info(f"🎯 Entering position: {opportunity.get('question', '')[:50]}")
        self.logger.info(f"   {size} units @ ${price:.4f}")
        
        result = await self.executor.execute_trade(
            token_id=token_id,
            side='BUY',
            size=size,
            price=price
        )
        
        if result and result.get('success'):
            # Get actual filled size from executor
            executor_position = self.executor.get_position(token_id)
            actual_size = executor_position.get('size', size) if executor_position else size
            
            position_data = {
                **opportunity,
                'entry_time': asyncio.get_event_loop().time(),
                'entry_price': price,
                'size': actual_size,
                'strategy_name': self.strategy_name
            }
            
            # Save to both memory and disk
            self.open_positions[token_id] = position_data
            self.position_manager.add_position(
                token_id=token_id,
                entry_price=price,
                size=actual_size,
                metadata=position_data
            )
            
            self.stats['trades_entered'] += 1
            self.logger.info(f"✅ Position entered successfully (size: {actual_size})")
            return True
        
        return False
    
    async def exit_position(
        self,
        token_id: str,
        exit_price: Optional[float] = None
    ) -> bool:
        """
        יוצא מפוזיציה.
        
        Args:
            token_id: מזהה טוקן
            exit_price: מחיר יציאה (אם None, משתמש במחיר שוק)
            
        Returns:
            True אם יצא בהצלחה
        """
        position = self.open_positions.get(token_id)
        if not position:
            self.logger.warning(f"⚠️ Position not found in strategy memory: {token_id[:12]}...")
            # Check if it exists in PositionManager
            position = self.position_manager.get_position(token_id)
            if not position:
                self.logger.error(f"❌ Position not found in PositionManager either")
                # Log executor positions for debugging
                executor_positions = self.executor.get_all_positions()
                self.logger.debug(f"Executor has {len(executor_positions)} positions: {list(executor_positions.keys())[:3]}")
                return False
            else:
                self.logger.info(f"Found position in PositionManager, restoring to memory")
                self.open_positions[token_id] = position
        
        self.logger.info(f"🚪 Exiting position: {position.get('question', '')[:50]}")
        
        # Pass position data to executor as fallback
        result = await self.executor.close_position(token_id, exit_price, position_data=position)
        
        if result and result.get('success'):
            pnl = result.get('pnl', 0)
            pnl_pct = result.get('pnl_pct', 0)
            
            self.stats['trades_exited'] += 1
            self.stats['total_pnl'] += pnl
            
            self.logger.info(f"✅ Position exited: ${pnl:.2f} ({pnl_pct:+.1f}%)")
            
            # Remove from both memory and disk
            del self.open_positions[token_id]
            self.position_manager.remove_position(token_id)
            return True
        else:
            self.logger.warning(f"Failed to close position in executor")
            return False
    
    async def scan_loop(self):
        """לולאת סריקה"""
        while self.running:
            try:
                self.stats['scans'] += 1
                self.logger.info(f"🔍 Scan #{self.stats['scans']}")
                
                # Scan for opportunities
                opportunities = await self.scan()
                
                if opportunities:
                    self.logger.info(f"💡 Found {len(opportunities)} opportunities")
                    self.stats['opportunities_found'] += len(opportunities)
                    
                    # Check each opportunity
                    for opp in opportunities:
                        token_id = opp.get('token_id')
                        
                        # Skip if already seen or in position
                        if token_id in self.seen_opportunities or token_id in self.open_positions:
                            continue
                        
                        self.seen_opportunities.add(token_id)
                        
                        # Check if should enter
                        if await self.should_enter(opp):
                            await self.enter_position(opp)
                
                # Wait before next scan
                await asyncio.sleep(self.scan_interval)
                
            except Exception as e:
                self.logger.error(f"Error in scan loop: {e}")
                await asyncio.sleep(60)
    
    async def monitor_loop(self):
        """לולאת מעקב אחר פוזיציות"""
        while self.running:
            try:
                # Check all open positions
                for token_id, position in list(self.open_positions.items()):
                    if await self.should_exit(position):
                        await self.exit_position(token_id)
                
                await asyncio.sleep(30)  # Check every 30 seconds
                
            except Exception as e:
                self.logger.error(f"Error in monitor loop: {e}")
                await asyncio.sleep(60)
    
    async def stats_loop(self):
        """לולאת דיווח סטטיסטיקות"""
        while self.running:
            await asyncio.sleep(600)  # Every 10 minutes
            
            self.logger.info("="*60)
            self.logger.info(f"📊 {self.strategy_name} Statistics")
            self.logger.info(f"   Scans: {self.stats['scans']}")
            self.logger.info(f"   Opportunities: {self.stats['opportunities_found']}")
            self.logger.info(f"   Trades Entered: {self.stats['trades_entered']}")
            self.logger.info(f"   Trades Exited: {self.stats['trades_exited']}")
            self.logger.info(f"   Total P&L: ${self.stats['total_pnl']:.2f}")
            self.logger.info(f"   Open Positions: {len(self.open_positions)}")
            self.logger.info("="*60)
    
    async def start(self):
        """מתחיל את האסטרטגיה"""
        self.running = True
        
        self.logger.info("="*60)
        self.logger.info(f"🚀 Starting {self.strategy_name}")
        self.logger.info("="*60)
        
        # Check balance
        balance = await self.executor.get_balance()
        self.logger.info(f"💰 Balance: ${balance:.2f} USDC")
        
        # Start loops
        await asyncio.gather(
            self.scan_loop(),
            self.monitor_loop(),
            self.stats_loop()
        )
    
    def stop(self):
        """עוצר את האסטרטגיה"""
        self.logger.info(f"🛑 Stopping {self.strategy_name}")
        self.running = False
