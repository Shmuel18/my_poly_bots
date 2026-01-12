"""
PostgreSQL Database Layer for Position and Trade Persistence

Replaces in-memory PositionManager with persistent storage.
Enables:
- Position history tracking
- P&L calculation across sessions
- Trade audit logs
- Performance analytics
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

try:
    import asyncpg
    ASYNCPG_AVAILABLE = True
except ImportError:
    ASYNCPG_AVAILABLE = False
    logger.warning("asyncpg not installed. Database features disabled. Install: pip install asyncpg")


class DatabaseManager:
    """Async PostgreSQL manager for trading data."""

    def __init__(
        self,
        host: str = None,
        port: int = None,
        database: str = None,
        user: str = None,
        password: str = None,
    ):
        """
        Initialize database connection.

        Args:
            host: PostgreSQL host (defaults to POSTGRES_HOST env)
            port: PostgreSQL port (defaults to POSTGRES_PORT env or 5432)
            database: Database name (defaults to POSTGRES_DB env)
            user: Username (defaults to POSTGRES_USER env)
            password: Password (defaults to POSTGRES_PASSWORD env)
        """
        if not ASYNCPG_AVAILABLE:
            raise ImportError("asyncpg not installed. Run: pip install asyncpg")

        self.host = host or os.getenv("POSTGRES_HOST", "localhost")
        self.port = port or int(os.getenv("POSTGRES_PORT", "5432"))
        self.database = database or os.getenv("POSTGRES_DB", "polymarket_bot")
        self.user = user or os.getenv("POSTGRES_USER", "postgres")
        self.password = password or os.getenv("POSTGRES_PASSWORD", "")

        self.pool: Optional[asyncpg.Pool] = None
        self.logger = logger

    async def connect(self):
        """Create connection pool."""
        if self.pool:
            return

        try:
            self.logger.info(f"🔌 Connecting to PostgreSQL: {self.user}@{self.host}:{self.port}/{self.database}")
            self.pool = await asyncpg.create_pool(
                host=self.host,
                port=self.port,
                database=self.database,
                user=self.user,
                password=self.password,
                min_size=2,
                max_size=10,
                command_timeout=60,
            )
            self.logger.info("✅ PostgreSQL connected")

            # Initialize schema
            await self.init_schema()

        except Exception as e:
            self.logger.error(f"❌ PostgreSQL connection failed: {e}")
            raise

    async def disconnect(self):
        """Close connection pool."""
        if self.pool:
            await self.pool.close()
            self.pool = None
            self.logger.info("🔌 PostgreSQL disconnected")

    async def init_schema(self):
        """Create tables if they don't exist."""
        schema = """
        -- Markets table
        CREATE TABLE IF NOT EXISTS markets (
            market_id VARCHAR(255) PRIMARY KEY,
            question TEXT NOT NULL,
            description TEXT,
            end_date TIMESTAMP WITH TIME ZONE,
            outcomes JSONB,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        );

        -- Positions table
        CREATE TABLE IF NOT EXISTS positions (
            id SERIAL PRIMARY KEY,
            strategy VARCHAR(100) NOT NULL,
            token_id VARCHAR(255) NOT NULL,
            side VARCHAR(10) NOT NULL,  -- BUY, SELL
            size DECIMAL(20, 6) NOT NULL,
            entry_price DECIMAL(10, 6) NOT NULL,
            entry_cost DECIMAL(20, 6) NOT NULL,
            entry_time TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            exit_price DECIMAL(10, 6),
            exit_time TIMESTAMP WITH TIME ZONE,
            pnl DECIMAL(20, 6),
            status VARCHAR(20) DEFAULT 'OPEN',  -- OPEN, CLOSED, FAILED
            metadata JSONB,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        );

        -- Trades table (execution history)
        CREATE TABLE IF NOT EXISTS trades (
            id SERIAL PRIMARY KEY,
            position_id INTEGER REFERENCES positions(id),
            strategy VARCHAR(100) NOT NULL,
            token_id VARCHAR(255) NOT NULL,
            side VARCHAR(10) NOT NULL,
            size DECIMAL(20, 6) NOT NULL,
            price DECIMAL(10, 6) NOT NULL,
            total_cost DECIMAL(20, 6) NOT NULL,
            fee DECIMAL(20, 6),
            executed_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            order_id VARCHAR(255),
            metadata JSONB
        );

        -- Performance snapshots
        CREATE TABLE IF NOT EXISTS performance_snapshots (
            id SERIAL PRIMARY KEY,
            strategy VARCHAR(100) NOT NULL,
            timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            total_pnl DECIMAL(20, 6),
            open_positions INTEGER,
            closed_positions INTEGER,
            win_rate DECIMAL(5, 2),
            metadata JSONB
        );

        -- Indexes for faster queries
        CREATE INDEX IF NOT EXISTS idx_positions_strategy ON positions(strategy);
        CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
        CREATE INDEX IF NOT EXISTS idx_positions_token ON positions(token_id);
        CREATE INDEX IF NOT EXISTS idx_trades_position ON trades(position_id);
        CREATE INDEX IF NOT EXISTS idx_trades_token ON trades(token_id);
        CREATE INDEX IF NOT EXISTS idx_performance_strategy ON performance_snapshots(strategy);
        """

        async with self.pool.acquire() as conn:
            await conn.execute(schema)
            self.logger.info("✅ Database schema initialized")

    # ==================== POSITION OPERATIONS ====================

    async def create_position(
        self,
        strategy: str,
        token_id: str,
        side: str,
        size: float,
        entry_price: float,
        metadata: Optional[Dict] = None,
    ) -> int:
        """
        Create new position.

        Returns:
            position_id
        """
        entry_cost = size * entry_price
        metadata_json = metadata or {}

        async with self.pool.acquire() as conn:
            result = await conn.fetchrow(
                """
                INSERT INTO positions (strategy, token_id, side, size, entry_price, entry_cost, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id
                """,
                strategy, token_id, side, size, entry_price, entry_cost, metadata_json,
            )
            position_id = result["id"]
            self.logger.debug(f"Created position #{position_id}: {side} {size} {token_id} @ {entry_price}")
            return position_id

    async def get_position(self, position_id: int) -> Optional[Dict]:
        """Get position by ID."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM positions WHERE id = $1",
                position_id,
            )
            return dict(row) if row else None

    async def get_open_positions(self, strategy: Optional[str] = None) -> List[Dict]:
        """Get all open positions, optionally filtered by strategy."""
        async with self.pool.acquire() as conn:
            if strategy:
                rows = await conn.fetch(
                    "SELECT * FROM positions WHERE status = 'OPEN' AND strategy = $1 ORDER BY entry_time DESC",
                    strategy,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM positions WHERE status = 'OPEN' ORDER BY entry_time DESC"
                )
            return [dict(row) for row in rows]

    async def close_position(
        self,
        position_id: int,
        exit_price: float,
        pnl: float,
    ) -> bool:
        """Close position and calculate P&L."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE positions 
                SET status = 'CLOSED', exit_price = $2, exit_time = NOW(), pnl = $3, updated_at = NOW()
                WHERE id = $1
                """,
                position_id, exit_price, pnl,
            )
            success = result.split()[-1] == "1"
            if success:
                self.logger.debug(f"Closed position #{position_id}: P&L = {pnl:.4f}")
            return success

    async def get_position_by_token(self, token_id: str, strategy: str) -> Optional[Dict]:
        """Get open position by token ID."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM positions 
                WHERE token_id = $1 AND strategy = $2 AND status = 'OPEN'
                ORDER BY entry_time DESC LIMIT 1
                """,
                token_id, strategy,
            )
            return dict(row) if row else None

    # ==================== TRADE OPERATIONS ====================

    async def record_trade(
        self,
        position_id: Optional[int],
        strategy: str,
        token_id: str,
        side: str,
        size: float,
        price: float,
        fee: float = 0.0,
        order_id: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> int:
        """Record trade execution."""
        total_cost = size * price
        metadata_json = metadata or {}

        async with self.pool.acquire() as conn:
            result = await conn.fetchrow(
                """
                INSERT INTO trades (position_id, strategy, token_id, side, size, price, total_cost, fee, order_id, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                RETURNING id
                """,
                position_id, strategy, token_id, side, size, price, total_cost, fee, order_id, metadata_json,
            )
            trade_id = result["id"]
            self.logger.debug(f"Recorded trade #{trade_id}: {side} {size} {token_id} @ {price}")
            return trade_id

    async def get_trades_by_position(self, position_id: int) -> List[Dict]:
        """Get all trades for a position."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM trades WHERE position_id = $1 ORDER BY executed_at",
                position_id,
            )
            return [dict(row) for row in rows]

    # ==================== ANALYTICS ====================

    async def get_pnl_summary(self, strategy: Optional[str] = None) -> Dict:
        """Get P&L summary."""
        async with self.pool.acquire() as conn:
            if strategy:
                query = """
                    SELECT 
                        COUNT(*) FILTER (WHERE status = 'CLOSED') as closed_positions,
                        COUNT(*) FILTER (WHERE status = 'OPEN') as open_positions,
                        COALESCE(SUM(pnl) FILTER (WHERE status = 'CLOSED'), 0) as total_pnl,
                        COALESCE(AVG(pnl) FILTER (WHERE status = 'CLOSED'), 0) as avg_pnl,
                        COUNT(*) FILTER (WHERE pnl > 0) as wins,
                        COUNT(*) FILTER (WHERE pnl < 0) as losses
                    FROM positions
                    WHERE strategy = $1
                """
                row = await conn.fetchrow(query, strategy)
            else:
                query = """
                    SELECT 
                        COUNT(*) FILTER (WHERE status = 'CLOSED') as closed_positions,
                        COUNT(*) FILTER (WHERE status = 'OPEN') as open_positions,
                        COALESCE(SUM(pnl) FILTER (WHERE status = 'CLOSED'), 0) as total_pnl,
                        COALESCE(AVG(pnl) FILTER (WHERE status = 'CLOSED'), 0) as avg_pnl,
                        COUNT(*) FILTER (WHERE pnl > 0) as wins,
                        COUNT(*) FILTER (WHERE pnl < 0) as losses
                    FROM positions
                """
                row = await conn.fetchrow(query)

            wins = row["wins"] or 0
            losses = row["losses"] or 0
            total_closed = wins + losses
            win_rate = (wins / total_closed * 100) if total_closed > 0 else 0

            return {
                "closed_positions": row["closed_positions"],
                "open_positions": row["open_positions"],
                "total_pnl": float(row["total_pnl"]),
                "avg_pnl": float(row["avg_pnl"]),
                "wins": wins,
                "losses": losses,
                "win_rate": win_rate,
            }

    async def save_performance_snapshot(
        self,
        strategy: str,
        metadata: Optional[Dict] = None,
    ):
        """Save performance snapshot."""
        summary = await self.get_pnl_summary(strategy)
        metadata_json = metadata or {}

        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO performance_snapshots (strategy, total_pnl, open_positions, closed_positions, win_rate, metadata)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                strategy,
                summary["total_pnl"],
                summary["open_positions"],
                summary["closed_positions"],
                summary["win_rate"],
                metadata_json,
            )


# Singleton instance
_db_instance: Optional[DatabaseManager] = None


async def get_database(
    host: str = None,
    port: int = None,
    database: str = None,
    user: str = None,
    password: str = None,
) -> Optional[DatabaseManager]:
    """
    Get or create singleton database instance.

    Returns None if asyncpg is not available.
    """
    global _db_instance

    if not ASYNCPG_AVAILABLE:
        return None

    if _db_instance is None:
        try:
            _db_instance = DatabaseManager(
                host=host,
                port=port,
                database=database,
                user=user,
                password=password,
            )
            await _db_instance.connect()
        except Exception as e:
            logger.warning(f"Database disabled: {e}")
            return None

    return _db_instance


@asynccontextmanager
async def database_session():
    """Context manager for database operations."""
    db = await get_database()
    if not db:
        yield None
        return

    try:
        yield db
    finally:
        pass  # Pool handles connection lifecycle
