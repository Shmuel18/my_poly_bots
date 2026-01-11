"""
Polymarket Bots - CLI Runner

Run strategies with selectable accounts via .env files.

Usage:
    python main.py --strategy extreme_price --env config/.env
    python main.py --strategy arbitrage --env config/account1.env --env config/account2.env
"""
import argparse
import asyncio
import json
import logging
from typing import List, Optional, Type

from dotenv import dotenv_values

from utils import setup_logging
from utils.dynamic_loader import load_class


def load_connection_from_env(env_path: Optional[str], dry_run: bool = False):
    """Load credentials from an .env file and create a connection."""
    if env_path:
        creds = dotenv_values(env_path)
    else:
        creds = {}
    # Lazy import to avoid heavy core import when running --help
    from core import PolymarketConnection
    return PolymarketConnection(
        api_key=creds.get('POLYMARKET_API_KEY'),
        api_secret=creds.get('POLYMARKET_API_SECRET'),
        api_passphrase=creds.get('POLYMARKET_API_PASSPHRASE'),
        private_key=creds.get('POLYMARKET_PRIVATE_KEY'),
        funder_address=creds.get('POLYMARKET_FUNDER_ADDRESS'),
        clob_url=creds.get('CLOB_URL'),
        chain_id=int(creds.get('CHAIN_ID')) if creds.get('CHAIN_ID') else None,
        dry_run=dry_run,
    )


def run_strategy(
    strategy_name: Optional[str],
    env_paths: List[str],
    strategy_path: Optional[str] = None,
    strategy_kwargs: Optional[dict] = None,
    log_level: str = 'INFO',
    dry_run: bool = False,
) -> List:
    """Instantiate the selected strategy for each env file and return instances."""
    strategies = []
    
    StrategyClass: Optional[Type] = None
    if strategy_path:
        StrategyClass = load_class(strategy_path, default_class_name="Strategy")
    else:
        # Built-in strategies lazy import to avoid import cost on --help
        if strategy_name == 'extreme_price':
            from strategies.extreme_price import ExtremePriceStrategy as _BuiltIn
            StrategyClass = _BuiltIn
        elif strategy_name == 'arbitrage':
            from strategies.arbitrage import ArbitrageStrategy as _BuiltIn
            StrategyClass = _BuiltIn
        elif strategy_name == 'spread_arbitrage':
            from strategies.spread_arbitrage import SpreadArbitrageStrategy as _BuiltIn
            StrategyClass = _BuiltIn
    
    strategy_kwargs = strategy_kwargs or {}
    if dry_run:
        strategy_kwargs.setdefault('dry_run', True)
    
    for idx, env_path in enumerate(env_paths or [None]):
        connection = load_connection_from_env(env_path, dry_run=dry_run)
        
        if StrategyClass is not None:
            # For dynamically loaded strategies, assume constructor accepts at least `connection` and optional `log_level`.
            try:
                strategy = StrategyClass(connection=connection, log_level=log_level, **strategy_kwargs)
            except TypeError as e:
                import inspect
                sig = inspect.signature(StrategyClass)
                allowed = ", ".join(sig.parameters.keys())
                raise SystemExit(
                    f"Failed to initialize strategy from {strategy_path}: {e}. Allowed parameters: {allowed}"
                )
        else:
            if strategy_name == 'extreme_price':
                params = {
                    'buy_threshold': 0.004,
                    'sell_multiplier': 2.0,
                    'min_hours_until_close': 1,
                    'portfolio_percent': 0.005,
                    'scan_interval': 300,
                    'log_level': log_level,
                    'connection': connection,
                }
                params.update(strategy_kwargs)
                strategy = ExtremePriceStrategy(**params)
            elif strategy_name == 'arbitrage':
                params = {
                    'min_profit_pct': 2.0,
                    'max_hours_until_close': 24,
                    'scan_interval': 300,
                    'log_level': log_level,
                    'connection': connection,
                }
                params.update(strategy_kwargs)
                strategy = ArbitrageStrategy(**params)
            else:
                raise ValueError(f"Unknown strategy: {strategy_name}")
        strategies.append(strategy)
    
    return strategies


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Polymarket Bots Runner')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--strategy', choices=['extreme_price', 'arbitrage', 'spread_arbitrage'],
                       help='Built-in strategy to run')
    group.add_argument('--strategy-path',
                       help='Dotted module or file path to strategy class. Examples: strategies.arbitrage.strategy:ArbitrageStrategy or strategies/custom.py:CustomStrategy')
    parser.add_argument('--env', action='append', default=[],
                        help='Path to .env file for account credentials (repeatable for multiple accounts)')
    parser.add_argument('--log-level', default='INFO', help='Logging level')
    parser.add_argument('--strategy-args', default=None,
                        help='JSON string of extra constructor kwargs for the selected strategy (dynamic or built-in). Example: "{\"min_profit_pct\": 3.0, \"scan_interval\": 120}"')
    parser.add_argument('--dry-run', action='store_true',
                        help='Simulate trades without posting orders (passes dry_run=True into strategy/executor)')
    parser.add_argument('--log-rotation', type=str, default='size', choices=['size', 'time'],
                        help='סוג rotation ללוגים: size (לפי גודל) או time (יומי)')
    return parser.parse_args()


async def main_async():
    args = parse_args()
    setup_logging(log_level=args.log_level, rotation_mode=args.log_rotation)
    
    # If no env provided, fallback to process environment
    env_paths = args.env if args.env else [None]
    strategy_kwargs = None
    if args.strategy_args:
        try:
            strategy_kwargs = json.loads(args.strategy_args)
            if not isinstance(strategy_kwargs, dict):
                raise ValueError("--strategy-args must be a JSON object")
        except Exception as e:
            raise SystemExit(f"Failed to parse --strategy-args: {e}")
    strategies = run_strategy(
        args.strategy,
        env_paths,
        strategy_path=getattr(args, 'strategy_path', None),
        strategy_kwargs=strategy_kwargs,
        log_level=args.log_level,
        dry_run=args.dry_run,
    )

    tasks = [asyncio.create_task(s.start()) for s in strategies]

    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for res in results:
            if isinstance(res, Exception):
                logging.error(f"🚨 One of the strategies crashed: {res}")
    except KeyboardInterrupt:
        print('\n👋 Stopping all strategies...')
        for s in strategies:
            s.stop()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == '__main__':
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print('\n👋 Stopped by user')
