"""Cross-Market Duplicate Arbitrage strategy.

Finds two *different* Polymarket markets that resolve on identical criteria
(same event, same end date, same resolution source) and exploits their
independent order books when ask_YES(A) + ask_NO(B) < $1.

See strategies/calendar_arbitrage/ for the sister strategy (same-event,
different deadlines).
"""
from strategies.duplicate_arbitrage.strategy import DuplicateArbitrageStrategy

__all__ = ["DuplicateArbitrageStrategy"]
