# Calendar Arbitrage Bot - Configuration Guide

## Quick Start

### Dry-Run (Simulation)

```bash
python run_calendar_bot.py
```

### Live Trading

```bash
python run_calendar_bot.py --live --env config/.env
```

## Windows Users

Double-click:

```
run_calendar_bot.bat
```

Or with options:

```
run_calendar_bot.bat --live --profit 0.03
```

## Command Line Options

| Flag              | Default       | Description                            |
| ----------------- | ------------- | -------------------------------------- |
| `--live`          | False         | Enable live trading (default: dry-run) |
| `--env PATH`      | `config/.env` | Path to .env credentials file          |
| `--profit FLOAT`  | 0.02          | Min profit threshold (2% default)      |
| `--scan INT`      | 10            | Scan interval in seconds               |
| `--max-pairs INT` | 1000          | Max market pair groups to evaluate     |
| `--log-level`     | INFO          | DEBUG, INFO, WARNING, ERROR            |
| `--log-rotation`  | time          | size (10MB) or time (daily)            |

## Examples

### Default Dry-Run

```bash
python run_calendar_bot.py
```

✅ Simulates trading with 2% profit threshold, scans every 10s

### Conservative Live Trading (3% profit threshold)

```bash
python run_calendar_bot.py --live --profit 0.03
```

🔴 Only takes trades with ≥3% profit margin

### Aggressive Scanning (5s interval)

```bash
python run_calendar_bot.py --scan 5 --profit 0.015
```

⚡ Scans every 5 seconds, lower profit threshold (1.5%)

### Debug Mode with Custom Env

```bash
python run_calendar_bot.py --log-level DEBUG --env config/account1.env
```

🐛 Verbose logging for troubleshooting

## Setup & Prerequisites

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure Credentials

Copy and edit `.env` file:

```bash
cp config/.env.example config/.env
```

Fill in your Polymarket credentials:

```
POLYMARKET_API_KEY=...
POLYMARKET_API_SECRET=...
POLYMARKET_API_PASSPHRASE=...
POLYMARKET_PRIVATE_KEY=...
POLYMARKET_FUNDER_ADDRESS=...
DEFAULT_SLIPPAGE=0.01
```

### 3. Verify Installation

```bash
python run_calendar_bot.py --help
```

## Strategy Behavior

### What It Does

1. **Scans** ~5000 active prediction markets every N seconds
2. **Groups** markets by normalized question (removes time phrases)
3. **Identifies** adjacent time-spaced pairs (early, late)
4. **Calculates** portfolio cost: `ASK(NO_early) + ASK(YES_late)`
5. **Evaluates** if `total_cost < 1.0 - (profit_threshold + 2×fee)`
6. **Executes** parallel orders on both legs when conditions met

### Expected Opportunities

- **Dry-run**: 0-10 opportunities per scan (market-dependent)
- **Live**: Similar rate, but actual fills depend on liquidity
- **Frequency**: Scans every 10-60 seconds (configurable)

## Monitoring

### Log Files

Logs saved to: `logs/bot_YYYYMMDD.log`

### Key Log Indicators

```
🧮 Calendar Arbitrage Opportunity:     # Found a trade
   Early(NO) ask: $X.XXXX | Late(YES) ask: $Y.YYYY
   Total cost: $Z.ZZZZ | Profit >= $P.PPPP

✅ Calendar arbitrage legs filled      # Successfully entered both sides

📊 [stats_loop]                        # Periodic stats report
   Scans: N | Opportunities: N | Entered: N | Exited: N
```

## Performance Tuning

### High-Frequency Scanning

```bash
python run_calendar_bot.py --scan 5 --profit 0.015 --max-pairs 2000
```

- Scans every 5s (vs 10s default)
- Lower threshold (1.5% vs 2%)
- Evaluates more pairs (2000 vs 1000)
- ⚠️ Higher API load

### Conservative Strategy

```bash
python run_calendar_bot.py --scan 30 --profit 0.05
```

- Scans every 30s
- High threshold (5% profit required)
- Fewer, higher-quality trades
- ✅ Lower API costs

## Troubleshooting

### "No opportunities found"

- Market conditions may not support arbitrage currently
- Try lowering `--profit` threshold
- Increase `--max-pairs` to evaluate more groups

### "Connection refused"

- Verify `.env` file has correct credentials
- Check Polymarket API status: https://polymarket.com

### High memory usage

- Reduce `--max-pairs` (default 1000 is safe)
- Use smaller `--scan` interval (faster cleanup)

### Dry-run shows trades but live doesn't fill

- Liquidity may be insufficient at order time
- Try increasing `--scan` interval (slower = more stable conditions)
- Use higher `--profit` threshold for safer trades

## Advanced: Custom Configuration

Edit `run_calendar_bot.py` to add persistent config:

```python
# Line ~30, add:
DEFAULT_ARGS = {
    'profit': 0.025,        # 2.5% default
    'scan_interval': 15,    # 15 second scans
    'max_pairs': 1500,      # More pair evaluation
}
```

## Support & Logs

For issues, attach:

1. Output of `python run_calendar_bot.py --log-level DEBUG` (first 100 lines)
2. Contents of `logs/bot_YYYYMMDD.log` (last 50 lines)
3. Command used to launch the bot

---

**Happy trading! 🚀**
