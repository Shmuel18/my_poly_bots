# PostgreSQL Database Setup Guide

## Overview

The bot supports PostgreSQL for **persistent storage** of positions, trades, and performance metrics. This replaces the in-memory PositionManager and enables:

- **Position history** across bot restarts
- **P&L calculation** over time
- **Trade audit logs** for compliance
- **Performance analytics** and backtesting
- **Multi-instance support** (share data between bots)

## Quick Start

### Option 1: Docker (Recommended)

```bash
# Start PostgreSQL in Docker
docker run -d \
  --name polymarket-db \
  -e POSTGRES_PASSWORD=your_password \
  -e POSTGRES_DB=polymarket_bot \
  -p 5432:5432 \
  postgres:16-alpine

# Verify connection
docker exec -it polymarket-db psql -U postgres -d polymarket_bot -c "SELECT version();"
```

### Option 2: Local Installation

#### Windows

1. Download installer: https://www.postgresql.org/download/windows/
2. Run installer, set password
3. Add to PATH: `C:\Program Files\PostgreSQL\16\bin`
4. Create database:
   ```cmd
   psql -U postgres
   CREATE DATABASE polymarket_bot;
   \q
   ```

#### Mac

```bash
brew install postgresql@16
brew services start postgresql@16
createdb polymarket_bot
```

#### Linux

```bash
sudo apt update
sudo apt install postgresql-16
sudo systemctl start postgresql
sudo -u postgres createdb polymarket_bot
```

## Configuration

### 1. Install Python Package

```bash
pip install asyncpg psycopg2-binary
```

### 2. Set Environment Variables

Edit `config/.env`:

```env
# PostgreSQL Database
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=polymarket_bot
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your_password_here
```

### 3. Enable in Bot

```bash
# With database persistence
python run_calendar_bot.py --use-database

# Without database (in-memory fallback)
python run_calendar_bot.py
```

## Database Schema

### Tables

#### `markets`
Stores market metadata for reference.

| Column | Type | Description |
|--------|------|-------------|
| market_id | VARCHAR(255) | Primary key |
| question | TEXT | Market question |
| description | TEXT | Full description |
| end_date | TIMESTAMP | Market close time |
| outcomes | JSONB | Available outcomes |
| created_at | TIMESTAMP | First seen |
| updated_at | TIMESTAMP | Last updated |

#### `positions`
Tracks open and closed positions.

| Column | Type | Description |
|--------|------|-------------|
| id | SERIAL | Primary key |
| strategy | VARCHAR(100) | Strategy name |
| token_id | VARCHAR(255) | Market token |
| side | VARCHAR(10) | BUY or SELL |
| size | DECIMAL(20,6) | Position size |
| entry_price | DECIMAL(10,6) | Entry price |
| entry_cost | DECIMAL(20,6) | Total cost |
| entry_time | TIMESTAMP | Entry timestamp |
| exit_price | DECIMAL(10,6) | Exit price (NULL if open) |
| exit_time | TIMESTAMP | Exit timestamp |
| pnl | DECIMAL(20,6) | Profit/Loss |
| status | VARCHAR(20) | OPEN, CLOSED, FAILED |
| metadata | JSONB | Custom data |

#### `trades`
Execution history (audit log).

| Column | Type | Description |
|--------|------|-------------|
| id | SERIAL | Primary key |
| position_id | INTEGER | Foreign key to positions |
| strategy | VARCHAR(100) | Strategy name |
| token_id | VARCHAR(255) | Market token |
| side | VARCHAR(10) | BUY or SELL |
| size | DECIMAL(20,6) | Trade size |
| price | DECIMAL(10,6) | Execution price |
| total_cost | DECIMAL(20,6) | Total cost/proceeds |
| fee | DECIMAL(20,6) | Trading fee |
| executed_at | TIMESTAMP | Execution time |
| order_id | VARCHAR(255) | Exchange order ID |
| metadata | JSONB | Custom data |

#### `performance_snapshots`
Periodic performance metrics.

| Column | Type | Description |
|--------|------|-------------|
| id | SERIAL | Primary key |
| strategy | VARCHAR(100) | Strategy name |
| timestamp | TIMESTAMP | Snapshot time |
| total_pnl | DECIMAL(20,6) | Cumulative P&L |
| open_positions | INTEGER | # open positions |
| closed_positions | INTEGER | # closed positions |
| win_rate | DECIMAL(5,2) | Win percentage |
| metadata | JSONB | Custom data |

## Usage Examples

### Query Open Positions

```sql
-- Get all open positions
SELECT * FROM positions WHERE status = 'OPEN';

-- Get open positions for calendar arbitrage
SELECT * FROM positions 
WHERE strategy = 'CalendarArbitrageStrategy' AND status = 'OPEN';

-- Count open positions by strategy
SELECT strategy, COUNT(*) 
FROM positions 
WHERE status = 'OPEN' 
GROUP BY strategy;
```

### Calculate P&L

```sql
-- Total P&L by strategy
SELECT 
    strategy, 
    SUM(pnl) as total_pnl,
    COUNT(*) as closed_trades,
    AVG(pnl) as avg_pnl
FROM positions 
WHERE status = 'CLOSED'
GROUP BY strategy;

-- Win rate
SELECT 
    strategy,
    COUNT(*) FILTER (WHERE pnl > 0) as wins,
    COUNT(*) FILTER (WHERE pnl < 0) as losses,
    ROUND(COUNT(*) FILTER (WHERE pnl > 0) * 100.0 / COUNT(*), 2) as win_rate
FROM positions 
WHERE status = 'CLOSED'
GROUP BY strategy;
```

### Trade History

```sql
-- Recent trades
SELECT * FROM trades ORDER BY executed_at DESC LIMIT 20;

-- Trades for specific position
SELECT * FROM trades WHERE position_id = 123 ORDER BY executed_at;

-- Daily trade volume
SELECT 
    DATE(executed_at) as date,
    COUNT(*) as trade_count,
    SUM(total_cost) as volume
FROM trades
GROUP BY DATE(executed_at)
ORDER BY date DESC;
```

### Performance Over Time

```sql
-- Daily P&L
SELECT 
    DATE(exit_time) as date,
    SUM(pnl) as daily_pnl,
    COUNT(*) as trades
FROM positions
WHERE status = 'CLOSED' AND exit_time IS NOT NULL
GROUP BY DATE(exit_time)
ORDER BY date DESC;
```

## Programmatic Access

### Python Example

```python
from core.database import get_database

# Connect
db = await get_database()

# Get open positions
positions = await db.get_open_positions(strategy="CalendarArbitrageStrategy")
for pos in positions:
    print(f"Position #{pos['id']}: {pos['token_id']} - P&L: {pos['pnl']}")

# Get P&L summary
summary = await db.get_pnl_summary("CalendarArbitrageStrategy")
print(f"Total P&L: ${summary['total_pnl']:.2f}")
print(f"Win rate: {summary['win_rate']:.1f}%")

# Save performance snapshot
await db.save_performance_snapshot("CalendarArbitrageStrategy")
```

## Maintenance

### Backup

```bash
# Full backup
pg_dump -U postgres polymarket_bot > backup_$(date +%Y%m%d).sql

# Restore
psql -U postgres polymarket_bot < backup_20260112.sql
```

### Cleanup Old Data

```sql
-- Delete trades older than 90 days
DELETE FROM trades WHERE executed_at < NOW() - INTERVAL '90 days';

-- Delete closed positions older than 1 year
DELETE FROM positions WHERE status = 'CLOSED' AND exit_time < NOW() - INTERVAL '1 year';

-- Vacuum to reclaim space
VACUUM FULL;
```

### Monitor Size

```sql
-- Database size
SELECT pg_size_pretty(pg_database_size('polymarket_bot'));

-- Table sizes
SELECT 
    schemaname,
    tablename,
    pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) as size
FROM pg_tables
WHERE schemaname = 'public'
ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC;
```

## Troubleshooting

### Connection Failed

```
❌ PostgreSQL connection failed: could not connect to server
```

**Solutions:**
1. Check PostgreSQL is running: `systemctl status postgresql` (Linux) or `brew services list` (Mac)
2. Verify credentials in `.env`
3. Check firewall: `sudo ufw allow 5432/tcp`
4. Check `pg_hba.conf` allows local connections

### Permission Denied

```
ERROR: permission denied for table positions
```

**Solution:**
```sql
GRANT ALL PRIVILEGES ON DATABASE polymarket_bot TO postgres;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO postgres;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO postgres;
```

### Asyncpg Not Installed

```
WARNING: asyncpg not installed. Database features disabled.
```

**Solution:**
```bash
pip install asyncpg>=0.29.0
```

## Performance Optimization

### Indexes

The schema includes indexes on frequently queried columns:
- `positions(strategy)` - Filter by strategy
- `positions(status)` - Filter open/closed
- `positions(token_id)` - Lookup by token
- `trades(position_id)` - Join with positions
- `trades(token_id)` - Filter by token

### Connection Pooling

The bot uses `asyncpg.create_pool()` with:
- **min_size:** 2 connections
- **max_size:** 10 connections
- **command_timeout:** 60 seconds

For high-frequency trading, increase pool size:

```python
# In core/database.py
self.pool = await asyncpg.create_pool(
    ...,
    min_size=5,
    max_size=20,
)
```

## Security Best Practices

1. **Use strong passwords** (min 16 chars, alphanumeric + symbols)
2. **Restrict host access** in `pg_hba.conf`:
   ```
   host    all    all    127.0.0.1/32    md5
   ```
3. **Never commit `.env`** to git (already in `.gitignore`)
4. **Use SSL for remote connections**
5. **Regular backups** (daily via cron)
6. **Monitor for anomalies** (unusual P&L, trade volumes)

## Cloud Database Options

### AWS RDS PostgreSQL

```env
POSTGRES_HOST=my-polymarket-db.abc123.us-east-1.rds.amazonaws.com
POSTGRES_PORT=5432
POSTGRES_DB=polymarket_bot
POSTGRES_USER=admin
POSTGRES_PASSWORD=your_secure_password
```

### Heroku Postgres

```bash
heroku addons:create heroku-postgresql:standard-0
heroku config:get DATABASE_URL
```

Parse URL and set env vars.

### DigitalOcean Managed Database

```env
POSTGRES_HOST=db-postgresql-nyc1-12345.ondigitalocean.com
POSTGRES_PORT=25060
POSTGRES_DB=polymarket_bot
POSTGRES_USER=doadmin
POSTGRES_PASSWORD=your_password
```

## Migration from In-Memory

If you have running bots using in-memory storage:

1. **Enable database** with `--use-database`
2. Bot will **write to both** in-memory and database
3. Monitor logs for database errors
4. Once stable, database is authoritative source
5. Use SQL queries for historical analysis

## Future Enhancements

- [ ] SQLAlchemy ORM integration
- [ ] Automatic schema migrations (Alembic)
- [ ] Read replicas for analytics
- [ ] Time-series tables for tick data
- [ ] GraphQL API for dashboards
