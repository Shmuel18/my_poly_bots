#!/usr/bin/env python
"""
Cross-Platform Arbitrage Bot Launcher

Simultaneously monitors Polymarket and Kalshi for price discrepancies.

Usage:
    python run_cross_platform_bot.py                    # Dry-run
    python run_cross_platform_bot.py --live             # Live trading
    python run_cross_platform_bot.py --use-llm          # Enable LLM matching
"""
import argparse
import asyncio
import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from main import run_strategy, setup_logging


def main():
    parser = argparse.ArgumentParser(
        description="🌍 Cross-Platform Arbitrage Bot - Polymarket vs Kalshi",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_cross_platform_bot.py                                # Dry-run
  python run_cross_platform_bot.py --live                         # Live trading
  python run_cross_platform_bot.py --live --use-llm               # With LLM matching
  python run_cross_platform_bot.py --profit 0.03 --scan 60        # Custom params
        """
    )
    
    # Mode
    parser.add_argument(
        '--live',
        action='store_true',
        help='Enable live trading (default: dry-run)'
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
        default=30,
        dest='scan_interval',
        help='Scan interval in seconds (default: 30)'
    )
    
    parser.add_argument(
        '--max-positions',
        type=int,
        default=10,
        help='Max simultaneous positions (default: 10)'
    )
    
    # LLM
    parser.add_argument(
        '--use-llm',
        action='store_true',
        help='Use LLM for intelligent market matching (recommended)'
    )
    
    parser.add_argument(
        '--llm-model',
        default='gpt-4o-mini',
        help='LLM model (default: gpt-4o-mini)'
    )
    
    # Database
    parser.add_argument(
        '--use-database',
        action='store_true',
        help='Use PostgreSQL for persistence'
    )
    
    # Logging
    parser.add_argument(
        '--log-level',
        default='INFO',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        help='Logging level (default: INFO)'
    )
    
    args = parser.parse_args()
    
    # Print banner
    print("\n" + "="*60)
    print("🌍 CROSS-PLATFORM ARBITRAGE BOT")
    print("="*60)
    print(f"📊 Mode: {'LIVE TRADING 🔴' if args.live else 'DRY-RUN 🟢'}")
    print(f"💰 Min profit: {args.profit*100:.1f}%")
    print(f"⏱️  Scan interval: {args.scan_interval}s")
    print(f"📦 Max positions: {args.max_positions}")
    print(f"🤖 LLM matching: {args.use_llm}")
    print(f"🗄️  Database: {args.use_database}")
    print("="*60 + "\n")
    
    # Setup logging
    setup_logging(log_level=args.log_level)
    
    # Prepare strategy arguments
    strategy_kwargs = {
        'min_profit_threshold': args.profit,
        'scan_interval': args.scan_interval,
        'max_positions': args.max_positions,
        'use_llm': args.use_llm,
        'llm_model': args.llm_model,
        'use_database': args.use_database,
    }
    
    # Run strategy
    try:
        strategies = run_strategy(
            strategy_name='cross_platform',
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
