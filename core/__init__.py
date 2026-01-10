"""Core package for Polymarket trading framework"""
from .connection import PolymarketConnection
from .scanner import MarketScanner
from .executor import TradeExecutor
from .ws_manager import WebSocketManager

__all__ = [
    'PolymarketConnection',
    'MarketScanner',
    'TradeExecutor',
    'WebSocketManager'
]
