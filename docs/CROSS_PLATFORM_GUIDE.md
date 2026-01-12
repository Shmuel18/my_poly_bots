# Cross-Platform Arbitrage: Polymarket vs Kalshi

## Overview

Cross-platform arbitrage exploits **price discrepancies** between Polymarket and Kalshi for equivalent markets. When the same event is priced differently on both platforms, you can lock in guaranteed profit.

## How It Works

### Basic Principle

If Polymarket prices YES at 52¢ and Kalshi prices NO at 46¢:

- **Buy YES on Polymarket:** Pay $0.52
- **Buy NO on Kalshi:** Pay $0.46
- **Total cost:** $0.98
- **Guaranteed payout:** $1.00 (one will pay out)
- **Profit:** $0.02 (2%)

### Both Strategies

**Strategy 1: YES(Poly) + NO(Kalshi)**

- Works when: `P(YES_poly) + P(NO_kalshi) < 1.0 - fees`
- Example: 0.52 + 0.46 = 0.98 < 1.00 ✅

**Strategy 2: NO(Poly) + YES(Kalshi)**

- Works when: `P(NO_poly) + P(YES_kalshi) < 1.0 - fees`
- Example: 0.48 + 0.54 = 1.02 > 1.00 ❌

The bot automatically chooses the profitable strategy.

## Setup

### 1. Get Kalshi API Key

1. Sign up at https://kalshi.com
2. Navigate to Settings → API
3. Generate API key
4. Copy to `.env`:

```env
KALSHI_API_KEY=your_api_key_here
```

### 2. Install Dependencies

```bash
pip install aiohttp
```

### 3. Run Bot

```bash
# Dry-run (test mode)
python run_cross_platform_bot.py

# Live trading (requires both Polymarket and Kalshi credentials)
python run_cross_platform_bot.py --live

# With LLM for better market matching
python run_cross_platform_bot.py --live --use-llm
```

## Market Matching

The challenge is identifying **equivalent markets** across platforms. Markets phrase the same event differently:

| Polymarket                       | Kalshi              | Equivalent?             |
| -------------------------------- | ------------------- | ----------------------- |
| "Bitcoin above $100k by Dec 31?" | "BTC-31DEC-B100K"   | ✅ Yes                  |
| "Trump wins 2024 election"       | "PRES-2024-TRUMP"   | ✅ Yes                  |
| "Fed raises rates in March"      | "FEDRATE-MAR-RAISE" | ✅ Yes                  |
| "S&P 500 above 5000"             | "INX-31DEC-B5000"   | ⚠️ Maybe (check expiry) |

### Matching Methods

#### 1. Simple Keyword Matching (Default)

- Extracts keywords from questions
- Requires 3+ common words
- Fast but prone to false positives

#### 2. LLM Matching (Recommended)

- Uses GPT-4 to understand semantic equivalence
- Detects causal relationships
- Handles different phrasings
- More accurate but costs ~$0.01 per scan

Example:

```bash
python run_cross_platform_bot.py --use-llm
```

## Risk Management

### 1. Execution Risk

**Problem:** One leg fills, other doesn't

**Solution:** Concurrent execution with timeout

```python
poly_task = execute_poly_trade()
kalshi_task = execute_kalshi_trade()
results = await asyncio.gather(poly_task, kalshi_task, timeout=30)
```

**Mitigation:** If one fails, immediately cancel/exit the other

### 2. Timing Risk

**Problem:** Prices change between scan and execution

**Mitigation:**

- Fast execution (< 1 second)
- Limit orders (not market orders)
- Slippage tolerance (reject if price moved > 1%)

### 3. Settlement Risk

**Problem:** Platforms settle differently

**Example:**

- Polymarket: "Biden wins" → Resolves based on inauguration
- Kalshi: "PRES-2024-BIDEN" → Resolves based on electoral college

**Mitigation:**

- LLM verification of resolution criteria
- Manual review of flagged markets
- Avoid ambiguous markets

### 4. Platform Risk

**Problem:** Exchange goes offline or delays withdrawals

**Mitigation:**

- Diversify capital (don't put all funds on one platform)
- Monitor platform health
- Quick exits if issues detected

## Fee Structure

### Polymarket Fees

- **Maker fee:** 0% (provide liquidity)
- **Taker fee:** 2% (take liquidity)
- **Gas fees:** ~$0.50-2.00 per trade (Polygon)

### Kalshi Fees

- **Trading fee:** 7% on profits (not notional)
- **Withdrawal fee:** $0
- **No gas fees** (centralized exchange)

### Net Fee Impact

For $100 trade with 2% gross profit:

- **Gross profit:** $2.00
- **Polymarket fees:** $2.00 (taker)
- **Kalshi fees:** ~$0.14 (7% of $2 profit)
- **Gas:** $1.00
- **Net profit:** -$1.14 ❌

**Minimum profitable spread: ~5-7%** accounting for all fees

## Configuration

### Command-Line Options

```bash
python run_cross_platform_bot.py \
  --live \                        # Live trading
  --profit 0.05 \                 # 5% minimum profit
  --scan 60 \                     # Scan every 60 seconds
  --max-positions 5 \             # Max 5 simultaneous positions
  --use-llm \                     # LLM market matching
  --use-database                  # PostgreSQL persistence
```

### Environment Variables

```env
# Polymarket
POLYMARKET_API_KEY=...
POLYMARKET_PRIVATE_KEY=...

# Kalshi
KALSHI_API_KEY=...

# LLM (optional)
OPENAI_API_KEY=...

# Database (optional)
POSTGRES_HOST=localhost
POSTGRES_DB=polymarket_bot
```

## Performance Optimization

### 1. Reduce API Calls

```python
# Bad: Sequential
poly_markets = await get_poly_markets()  # 1s
kalshi_markets = await get_kalshi_markets()  # 1s
# Total: 2s

# Good: Parallel
poly_task = get_poly_markets()
kalshi_task = get_kalshi_markets()
poly_markets, kalshi_markets = await asyncio.gather(poly_task, kalshi_task)
# Total: 1s
```

### 2. Cache Market Metadata

- Store market questions/tickers in memory
- Only fetch orderbook on each scan
- Reduces API calls by 80%

### 3. Filter Before LLM

- Quick keyword filter first
- Only send promising pairs to LLM
- Saves API costs

## Monitoring

### Logs

```
INFO - 🌍 Cross-Platform Scan #12
INFO - 📊 Comparing 347 Polymarket vs 123 Kalshi markets
INFO - 🔗 Found 8 potentially equivalent market pairs
INFO - 💰 Found 2 cross-platform opportunities:
INFO -   1. YES_poly_NO_kalshi
INFO -      Poly: Bitcoin above $100k by Dec 31?
INFO -      Kalshi: BTC-31DEC-B100K
INFO -      Profit: 3.2%
INFO - ✅ Both legs executed successfully
```

### Metrics

Track:

- **Scan duration:** Should be < 5 seconds
- **Match rate:** % of markets with equivalents
- **Hit rate:** % of scans with opportunities
- **Fill rate:** % of opportunities executed
- **P&L:** Cumulative profit/loss

## Troubleshooting

### No Opportunities Found

**Causes:**

1. Markets not equivalent (poor matching)
2. Spreads too tight (< min profit)
3. Low liquidity (can't fill size)

**Solutions:**

- Enable LLM matching (`--use-llm`)
- Lower profit threshold (`--profit 0.01`)
- Increase scan frequency (`--scan 10`)

### One Leg Fails

**Cause:** Price moved or insufficient liquidity

**Solution:**

```python
if poly_success and not kalshi_success:
    # Immediately exit Polymarket position
    await self.executor.execute_trade(
        token_id=poly_token,
        side="SELL",
        size=size,
        price=current_bid,
    )
```

### Kalshi Connection Failed

```
ERROR - Failed to fetch Kalshi markets: 401 Unauthorized
```

**Solution:**

1. Check API key in `.env`
2. Verify account is approved for API trading
3. Check rate limits (max 10 req/s)

## Advanced Strategies

### 1. Multi-Leg Arbitrage

Instead of 1:1, use multiple markets:

**Example:**

- Polymarket: "Biden wins" YES @ 0.45
- Kalshi: "DEM wins" YES @ 0.42
- Kalshi: "TRUMP wins" NO @ 0.57
- Combined: 0.45 + 0.57 = 1.02 > 1.00 ❌

(Not profitable in this case)

### 2. Calendar Cross-Platform

Combine calendar arbitrage with cross-platform:

**Example:**

- Polymarket: "Bitcoin $100k by March" NO @ 0.60
- Kalshi: "BTC-31DEC-B100K" YES @ 0.35
- Total: 0.60 + 0.35 = 0.95 < 1.00 ✅

If Bitcoin hits $100k between March and December, both pay out!

### 3. Statistical Arbitrage

Use historical data to predict which platform will move first:

- Polymarket usually faster for political markets
- Kalshi faster for financial markets
- Front-run price convergence

## Compliance & Legal

### Regulations

- **US:** Kalshi is CFTC-regulated, Polymarket is not
- **KYC/AML:** Both platforms require identity verification
- **Tax reporting:** Required for profits (1099-MISC)
- **Wash sales:** May apply if trading both directions

### Best Practices

1. Keep detailed trade logs (database)
2. Report all profits to tax authority
3. Don't exceed position limits
4. Respect API rate limits
5. Don't manipulate markets

## Comparison to Other Strategies

| Feature              | Cross-Platform | Calendar | Simple Spread |
| -------------------- | -------------- | -------- | ------------- |
| **Complexity**       | High           | Medium   | Low           |
| **Frequency**        | Low            | Medium   | High          |
| **Profit/Trade**     | 2-10%          | 3-15%    | 1-5%          |
| **Risk**             | Low            | Medium   | Low           |
| **Capital Required** | 2x             | 2x       | 1x            |
| **Execution Speed**  | Fast           | Medium   | Fast          |

**Recommendation:** Start with simple spread, graduate to calendar, then cross-platform.

## Real Examples

### Example 1: Bitcoin $100k (Dec 2025)

**Polymarket:**

- Question: "Will Bitcoin hit $100k by Dec 31?"
- YES: 0.52
- NO: 0.48

**Kalshi:**

- Ticker: BTC-31DEC-B100K
- YES: 0.46
- NO: 0.54

**Arbitrage:**

- Buy YES on Polymarket: $0.52
- Buy NO on Kalshi: $0.54
- Total: $1.06 > $1.00 ❌ Not profitable

### Example 2: Fed Rate Decision (March 2026)

**Polymarket:**

- "Fed raises rates in March 2026"
- YES: 0.68
- NO: 0.32

**Kalshi:**

- Ticker: FEDRATE-MAR26-RAISE
- YES: 0.65
- NO: 0.35

**Arbitrage:**

- Buy NO on Polymarket: $0.32
- Buy YES on Kalshi: $0.65
- Total: $0.97 < $1.00 ✅
- **Gross profit: 3%**
- After fees: ~0.5% ⚠️ Marginal

## Future Enhancements

- [ ] Manifold Markets integration (3rd platform)
- [ ] Automatic rollback on partial fills
- [ ] Real-time P&L dashboard
- [ ] ML model for market matching
- [ ] High-frequency execution (<100ms)
- [ ] Multi-leg optimization (3+ positions)
