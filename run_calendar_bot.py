#!/usr/bin/env python
"""
Calendar Arbitrage Bot Launcher

שימוש קל:
    python run_calendar_bot.py                    # ריצה ב-dry-run
    python run_calendar_bot.py --live             # ריצה חיה (צריך .env)
    python run_calendar_bot.py --profit 0.03      # סף רווח 3%
    python run_calendar_bot.py --scan-interval 5  # סריקה כל 5 שניות
"""
import argparse
import asyncio
import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from main import run_strategy, setup_logging
from dotenv import dotenv_values


def main():
    parser = argparse.ArgumentParser(
        description="🤖 Calendar Arbitrage Bot - Logical Spread Trader",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_calendar_bot.py                              # Dry-run
  python run_calendar_bot.py --live                       # Live trading (requires .env)
  python run_calendar_bot.py --profit 0.02 --scan 10      # Custom thresholds
  python run_calendar_bot.py --live --env config/.env     # Specify .env file
        """
    )
    
    # Mode
    parser.add_argument(
        '--live',
        action='store_true',
        help='Enable live trading (default: dry-run simulation)'
    )
    
    # Environment
    parser.add_argument(
        '--env',
        default='config/.env',
        help='Path to .env file (default: config/.env)'
    )
    
    # Strategy parameters
    parser.add_argument(
        '--profit',
        type=float,
        default=0.02,
        help='Min profit threshold (default: 0.02 = 2%%)'
    )
    
    parser.add_argument(
        '--scan',
        '--scan-interval',
        type=int,
        default=10,
        dest='scan_interval',
        help='Scan interval in seconds (default: 10)'
    )
    
    parser.add_argument(
        '--max-pairs',
        type=int,
        default=1000,
        help='Max pair groups to evaluate (default: 1000)'
    )
    
    # Logging
    parser.add_argument(
        '--log-level',
        default='INFO',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        help='Logging level (default: INFO)'
    )
    
    parser.add_argument(
        '--log-rotation',
        default='time',
        choices=['size', 'time'],
        help='Log rotation mode: size (10MB) or time (daily) (default: time)'
    )
    
    args = parser.parse_args()
    
    # Print banner
    print("\n" + "="*60)
    print("🚀 CALENDAR ARBITRAGE BOT")
    print("="*60)
    print(f"📊 Mode: {'LIVE TRADING 🔴' if args.live else 'DRY-RUN 🟢'}")
    print(f"📈 Min profit threshold: {args.profit*100:.1f}%")
    print(f"⏱️  Scan interval: {args.scan_interval}s")
    print(f"📦 Max pairs to evaluate: {args.max_pairs}")
    print(f"📝 Log level: {args.log_level}")
    print("="*60 + "\n")
    
    # Setup logging
    setup_logging(log_level=args.log_level, rotation_mode=args.log_rotation)
    
    # Prepare strategy arguments
    strategy_kwargs = {
        'min_profit_threshold': args.profit,
        'scan_interval': args.scan_interval,
        'max_pairs': args.max_pairs,
    }
    
    # Run strategy
    try:
        strategies = run_strategy(
            strategy_name='calendar_arbitrage',
            env_paths=[args.env] if args.env else [None],
            strategy_kwargs=strategy_kwargs,
            log_level=args.log_level,
            dry_run=not args.live,
        )
        
        if not strategies:
            print("❌ Failed to initialize strategy")
            sys.exit(1)
        
        # Run async
        asyncio.run(run_async(strategies))
        
    except KeyboardInterrupt:
        print("\n👋 Stopped by user")
        sys.exit(0)
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


async def run_async(strategies):
    """Run all strategies concurrently."""
    import logging
    logger = logging.getLogger(__name__)
    
    tasks = [asyncio.create_task(s.start()) for s in strategies]
    
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for res in results:
            if isinstance(res, Exception):
                logger.error(f"🚨 Strategy crashed: {res}")
    except KeyboardInterrupt:
        logger.info("⏹️  Stopping all strategies...")
        for s in strategies:
            s.stop()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == '__main__':
    main()
