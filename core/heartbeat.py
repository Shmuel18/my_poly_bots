"""Unified heartbeat writer for multi-strategy bots.

Every scan cycle, each strategy calls ``MultiStrategyHeartbeat.write()`` with
its own snapshot dict. The file written at ``data/status_snapshot.json`` is
the UNION of all strategies, with per-strategy stats and aggregate totals.

This is what the dashboard reads to render the live balance + per-strategy
counts.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class MultiStrategyHeartbeat:
    """Thread-safe writer for ``data/status_snapshot.json``. Each strategy
    has its own section; last-write-wins per section. Aggregates are
    recomputed on every write."""

    _instance: Optional["MultiStrategyHeartbeat"] = None
    _lock = threading.Lock()

    def __init__(self, path: str = "data/status_snapshot.json"):
        self.path = path
        self._state: Dict[str, Any] = {"strategies": {}}
        # Load whatever is already on disk so writes merge with it (preserves
        # top-level keys like balance_usd / stats / pair_counts / strategy
        # that other strategies or the pre-existing SPA depend on).
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                if isinstance(existing, dict):
                    self._state = existing
                    self._state.setdefault("strategies", {})
        except Exception as e:
            logger.debug(f"Heartbeat initial load skipped: {e}")

    @classmethod
    def instance(cls) -> "MultiStrategyHeartbeat":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def write(
        self,
        strategy_key: str,
        balance_usd: Optional[float],
        stats: Dict[str, Any],
    ) -> None:
        """Update the snapshot for a given strategy and flush to disk.

        - ``strategy_key``: e.g. ``"calendar_arb"``, ``"duplicate_arb"``
        - ``balance_usd``: the bot's live Polymarket balance (any strategy
          may update it; they share the wallet). ``None`` leaves prior value.
        - ``stats``: dict of ``{discovered, confirmed, pending, rejected,
          trades_entered, trades_exited, open_positions, loop}`` etc.
        """
        with self._lock:
            if balance_usd is not None:
                self._state["balance_usd"] = float(balance_usd)
            self._state["updated_at"] = time.time()
            self._state.setdefault("strategies", {})
            section = dict(stats or {})
            section["last_scan_ts"] = time.time()
            self._state["strategies"][strategy_key] = section

            # Aggregates
            total_open = sum(
                s.get("open_positions", 0) for s in self._state["strategies"].values()
            )
            total_entered = sum(
                s.get("trades_entered", 0) for s in self._state["strategies"].values()
            )
            total_exited = sum(
                s.get("trades_exited", 0) for s in self._state["strategies"].values()
            )
            self._state["open_positions"] = total_open
            self._state["trades_entered"] = total_entered
            self._state["trades_exited"] = total_exited

            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                with open(self.path, "w", encoding="utf-8") as f:
                    json.dump(self._state, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.warning(f"Heartbeat write failed: {e}")
