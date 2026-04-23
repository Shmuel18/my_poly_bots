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

    parser.add_argument(
        '--min-annualized-roi',
        type=float,
        default=0.15,
        help='Minimum annualized ROI to enter a trade (default: 0.15 = 15%%)'
    )

    parser.add_argument(
        '--min-resolution-confidence',
        type=float,
        default=0.9,
        help='Minimum LLM confidence that paired markets share resolution criteria (default: 0.9)'
    )

    parser.add_argument(
        '--early-exit-usd',
        type=float,
        default=0.20,
        dest='early_exit_threshold',
        help='Minimum USD profit per share-pair (after fees) to trigger early exit — '
             'e.g. 0.20 means exit once the spread has closed enough that selling both legs '
             'now nets ≥ 20¢ above entry cost. Lower = more aggressive exits, higher = more patient. '
             '(default: 0.20)'
    )

    # Human-in-the-loop tiered sizing
    parser.add_argument(
        '--probe-usd',
        type=float,
        default=5.0,
        help='USD size for initial probe trades on newly discovered pairs (default: 5.0)'
    )
    parser.add_argument(
        '--confirmed-usd',
        type=float,
        default=20.0,
        help='USD size for user-confirmed pairs (default: 20.0)'
    )
    parser.add_argument(
        '--escalation-minutes',
        type=float,
        default=30.0,
        help='Minutes a probe position must persist before sending a Telegram alert (default: 30)'
    )
    parser.add_argument(
        '--no-telegram',
        action='store_true',
        help='Disable Telegram notifier entirely (runs fully automated with probe sizing only)'
    )

    parser.add_argument(
        '--include-duplicates',
        action='store_true',
        default=True,
        help='Also run cross-market duplicate-arbitrage in parallel (default: enabled)'
    )
    parser.add_argument('--no-duplicates', dest='include_duplicates', action='store_false',
                        help='Disable duplicate-arbitrage strategy')
    parser.add_argument('--duplicate-scan-interval', type=int, default=45,
                        help='Duplicate-arb scan interval in seconds (default: 45)')
    parser.add_argument('--duplicate-similarity-threshold', type=float, default=0.90,
                        help='Cosine similarity threshold for duplicate candidates (default: 0.90)')
    parser.add_argument('--duplicate-min-confidence', type=float, default=0.95,
                        help='LLM confidence threshold to accept a duplicate pair (default: 0.95)')

    # NLP/LLM options
    parser.add_argument(
        '--use-embeddings',
        action='store_true',
        default=True,
        help='Use sentence embeddings for similarity (default: enabled)'
    )
    
    parser.add_argument(
        '--use-llm',
        action='store_true',
        help='Use LLM for semantic market clustering (requires GEMINI_API_KEY)'
    )

    parser.add_argument(
        '--llm-model',
        default='gemini-2.5-flash-lite',
        help='Gemini model to use (default: gemini-2.5-flash-lite)'
    )
    
    # Database options
    parser.add_argument(
        '--use-database',
        action='store_true',
        help='Use PostgreSQL for position persistence (requires database setup)'
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
    print(f"🧠 Use embeddings: {args.use_embeddings}")
    print(f"🤖 Use LLM: {args.use_llm}" + (f" ({args.llm_model})" if args.use_llm else ""))
    print(f"�️ Use database: {args.use_database}")
    print(f"�📝 Log level: {args.log_level}")
    print("="*60 + "\n")
    
    # Setup logging
    setup_logging(log_level=args.log_level, rotation_mode=args.log_rotation)
    
    # Prepare strategy arguments
    strategy_kwargs = {
        'min_profit_threshold': args.profit,
        'scan_interval': args.scan_interval,
        'max_pairs': args.max_pairs,
        'min_annualized_roi': args.min_annualized_roi,
        'min_resolution_match_confidence': args.min_resolution_confidence,
        'early_exit_threshold': args.early_exit_threshold,
        'probe_usd': args.probe_usd,
        'confirmed_usd': args.confirmed_usd,
        'escalation_minutes': args.escalation_minutes,
        'use_telegram': not args.no_telegram,
        'use_embeddings': args.use_embeddings,
        'use_llm': args.use_llm,
        'llm_model': args.llm_model,
        'use_database': args.use_database,
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

        # Optionally append duplicate-arb strategies sharing each account's connection.
        if args.include_duplicates:
            from strategies.duplicate_arbitrage import DuplicateArbitrageStrategy
            dup_strategies = []
            for base_strat in list(strategies):
                try:
                    dup = DuplicateArbitrageStrategy(
                        connection=base_strat.connection,
                        scan_interval=args.duplicate_scan_interval,
                        log_level=args.log_level,
                        min_profit_threshold=args.profit,
                        early_exit_threshold=args.early_exit_threshold,
                        similarity_threshold=args.duplicate_similarity_threshold,
                        min_confidence=args.duplicate_min_confidence,
                        probe_usd=args.probe_usd,
                        confirmed_usd=args.confirmed_usd,
                        escalation_minutes=args.escalation_minutes,
                        use_telegram=not args.no_telegram,
                        llm_model=args.llm_model,
                        dry_run=not args.live,
                    )
                    dup_strategies.append(dup)
                    print(f"🔁 Duplicate-Arb added (scan every {args.duplicate_scan_interval}s)")
                except Exception as e:
                    print(f"⚠️ Duplicate-Arb init failed: {e}")
            strategies.extend(dup_strategies)

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
