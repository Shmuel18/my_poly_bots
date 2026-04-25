"""Cross-Market Duplicate Arbitrage.

Finds pairs of *different* Polymarket markets that resolve on effectively
IDENTICAL criteria (same event, same deadline, same resolution source) and
exploits their independent CLOBs when ``ask_YES(A) + ask_NO(B) < $1``
(or the symmetric ``ask_NO(A) + ask_YES(B)``).

Discovery is a 4-stage pipeline:

1. **Embeddings** — sentence-transformers all-MiniLM-L6-v2 encodes each
   question, then cosine similarity ≥ 0.90 marks candidate pairs.
2. **Structural match** — endDate within 24h, same resolutionSource, same
   outcome count, distinct eventId, both have non-trivial volume.
3. **LLM verification** — Gemini is given the full description of both
   markets and must return ``identical=true`` with ``confidence ≥ 0.95``.
4. **Human approval via Telegram** — first live trade on a pair only
   executes a $5 probe; after 30 minutes an alert is sent with both
   markets' questions and descriptions. The user explicitly ✅ the pair
   before it's scaled to the confirmed size. ❌ blacklists forever.

A pair is *cheap* whenever

    ask_YES(A) + ask_NO(B)  <  1.0 − (min_profit + 4 × fee)

The bot buys the two legs via FOK at the quoted asks. Guaranteed payoff:
regardless of how the event resolves, exactly one of the pair is $1 at
settlement, so gross receipts = $1 per unit, profit = $1 − total_cost.

Only safe because duplicate detection is ≥ 0.95-confident AND human-
approved; a false positive (two genuinely different events) would lose
the full position.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from strategies.base_strategy import BaseStrategy
from strategies.duplicate_arbitrage.llm_agent import (
    DuplicateArbitrageLLMAgent,
    get_duplicate_llm_agent,
)

logger = logging.getLogger(__name__)

STRATEGY_LABEL = "Duplicate"


class DuplicateArbitrageStrategy(BaseStrategy):
    """Cross-market duplicate arbitrage. See module docstring for design."""

    def __init__(
        self,
        strategy_name: str = "DuplicateArbitrageStrategy",
        scan_interval: int = 45,
        log_level: str = "INFO",
        min_profit_threshold: float = 0.01,
        estimated_fee: Optional[float] = None,
        early_exit_threshold: float = 0.20,
        similarity_threshold: float = 0.90,
        min_confidence: float = 0.95,
        probe_usd: float = 5.0,
        confirmed_usd: float = 20.0,
        escalation_minutes: float = 30.0,
        max_open_positions: int = 2,
        use_telegram: bool = True,
        llm_model: str = "gemini-2.5-flash-lite",
        dry_run: bool = False,
        **kwargs,
    ):
        super().__init__(
            strategy_name=strategy_name,
            scan_interval=scan_interval,
            log_level=log_level,
            connection=kwargs.get("connection"),
            dry_run=dry_run,
        )

        # Strategy config
        self.min_profit_threshold = float(min_profit_threshold)
        self.estimated_fee = float(
            estimated_fee if estimated_fee is not None
            else os.getenv("DEFAULT_SLIPPAGE", "0.01")
        )
        self.early_exit_threshold = float(early_exit_threshold)
        self.similarity_threshold = float(similarity_threshold)
        self.min_confidence = float(min_confidence)
        self.probe_usd = float(probe_usd)
        self.confirmed_usd = float(confirmed_usd)
        self.escalation_seconds = float(escalation_minutes) * 60.0
        self.max_open_positions = int(max_open_positions)
        self.llm_model = llm_model

        # State files (separate from calendar_arb)
        self.DISCOVERED_FILE = os.path.join("data", "duplicate_discovered.json")
        self.CONFIRMED_FILE = os.path.join("data", "duplicate_confirmed.json")
        self.PENDING_FILE = os.path.join("data", "duplicate_pending.json")
        self.REJECTED_FILE = os.path.join("data", "duplicate_rejected.json")

        # Pair state
        self.discovered_pairs: List[Dict[str, Any]] = self._load_list(self.DISCOVERED_FILE)
        self.confirmed_pairs: Dict[str, Any] = self._load_dict(self.CONFIRMED_FILE)
        self.pending_pairs: Dict[str, Any] = self._load_dict(self.PENDING_FILE)
        self.rejected_pairs: Dict[str, Any] = self._load_dict(self.REJECTED_FILE)

        # LLM verifier (re-created lazily if disabled on init)
        self._llm: Optional[DuplicateArbitrageLLMAgent] = None
        try:
            self._llm = get_duplicate_llm_agent(model=llm_model)
            if self._llm:
                self.logger.info(f"🔁 Duplicate LLM verifier: {llm_model}")
        except Exception as e:
            self.logger.warning(f"Duplicate LLM init failed: {e}")

        # Embeddings (lazy) — share semantics with calendar_arb
        self._embedding_model = None
        self._embedding_cache: Dict[str, Any] = {}

        # Telegram (optional)
        self.telegram = None
        if use_telegram:
            try:
                from utils.telegram_notifier import TelegramNotifier
                self.telegram = TelegramNotifier()
            except Exception as e:
                self.logger.warning(f"Telegram init failed: {e}")

        # Discovery offset — scan a slice each loop to keep LLM cost bounded
        self.discovery_batch_size = 120
        self._discovery_offset = 0

        self.logger.info(
            f"✅ Strategy Initialized | {len(self.discovered_pairs)} pairs tracked "
            f"(confirmed={len(self.confirmed_pairs)}, pending={len(self.pending_pairs)})"
        )

    # ------------------------------------------------------------------
    # JSON state helpers
    # ------------------------------------------------------------------
    def _load_list(self, path: str) -> List[Dict[str, Any]]:
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception as e:
            self.logger.warning(f"Failed to load {path}: {e}")
            return []

    def _load_dict(self, path: str) -> Dict[str, Any]:
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception as e:
            self.logger.warning(f"Failed to load {path}: {e}")
            return {}

    def _save_json(self, path: str, data: Any):
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.logger.error(f"Failed to save {path}: {e}")

    @staticmethod
    def _pair_key(id_a: str, id_b: str) -> str:
        a, b = sorted((str(id_a), str(id_b)))
        return f"dup__{a[:12]}__{b[:12]}"

    def _tier_for(self, pair_key: str) -> str:
        if pair_key in self.rejected_pairs:
            return "rejected"
        if pair_key in self.confirmed_pairs:
            return "confirmed"
        if pair_key in self.pending_pairs:
            return "pending"
        return "probe"

    def _size_for_tier(self, tier: str, combined_ask: float) -> float:
        if tier == "confirmed":
            usd = self.confirmed_usd
        elif tier == "probe":
            usd = self.probe_usd
        else:
            return 0.0
        if combined_ask <= 0:
            return 0.0
        return max(1.0, round(usd / combined_ask, 2))

    # ------------------------------------------------------------------
    # Embedding + similarity (port of calendar_arb impl)
    # ------------------------------------------------------------------
    def _get_embedding_model(self):
        if self._embedding_model is None:
            try:
                from sentence_transformers import SentenceTransformer
                self.logger.info("📦 Loading sentence-transformers (all-MiniLM-L6-v2)…")
                self._embedding_model = SentenceTransformer("all-MiniLM-L6-v2")
            except ImportError:
                self.logger.warning("sentence-transformers not installed")
            except Exception as e:
                self.logger.error(f"Failed loading embedding model: {e}")
        return self._embedding_model

    def _get_embedding(self, text: str):
        if text in self._embedding_cache:
            return self._embedding_cache[text]
        model = self._get_embedding_model()
        if model is None:
            return None
        try:
            emb = model.encode(text, convert_to_tensor=False)
            self._embedding_cache[text] = emb
            return emb
        except Exception as e:
            self.logger.debug(f"Embedding failed: {e}")
            return None

    def _cosine(self, a, b) -> float:
        try:
            import numpy as np
            na, nb = np.linalg.norm(a), np.linalg.norm(b)
            if na == 0 or nb == 0:
                return 0.0
            return float(np.dot(a, b) / (na * nb))
        except Exception:
            return 0.0

    # ------------------------------------------------------------------
    # Orderbook helpers (mirror of calendar_arb, works with both dataclass
    # OrderBookSummary and dict-shaped responses)
    # ------------------------------------------------------------------
    @staticmethod
    def _orderbook_side(book, side: str):
        if not book:
            return []
        if hasattr(book, side):
            return getattr(book, side) or []
        if hasattr(book, "get"):
            return book.get(side, []) or []
        return []

    @staticmethod
    def _orderbook_entry(e):
        if e is None:
            return None, None
        p = getattr(e, "price", None) if not isinstance(e, dict) else e.get("price")
        s = getattr(e, "size", None) if not isinstance(e, dict) else e.get("size")
        try:
            return float(p), (float(s) if s is not None else 0.0)
        except (TypeError, ValueError):
            return None, None

    def _best_ask(self, token_id: str) -> Optional[Dict[str, float]]:
        try:
            book = self.executor.client.get_order_book(token_id)
            asks = self._orderbook_side(book, "asks")
            if asks:
                p, s = self._orderbook_entry(asks[0])
                if p is not None:
                    return {"price": p, "size": s or 0.0}
        except Exception as e:
            self.logger.debug(f"_best_ask {token_id[:12]}: {e}")
        return None

    def _best_bid(self, token_id: str) -> Optional[Dict[str, float]]:
        try:
            book = self.executor.client.get_order_book(token_id)
            bids = self._orderbook_side(book, "bids")
            if bids:
                p, s = self._orderbook_entry(bids[0])
                if p is not None:
                    return {"price": p, "size": s or 0.0}
        except Exception as e:
            self.logger.debug(f"_best_bid {token_id[:12]}: {e}")
        return None

    def _simulate_fill(self, token_id: str, side: str, size: float) -> Optional[Dict[str, float]]:
        try:
            book = self.executor.client.get_order_book(token_id)
            orders = self._orderbook_side(book, "asks" if side == "BUY" else "bids")
            if not orders:
                return None
            remaining, cost, filled, first_price = size, 0.0, 0.0, None
            for o in orders:
                p, s = self._orderbook_entry(o)
                if p is None or s is None or s <= 0:
                    continue
                if first_price is None:
                    first_price = p
                amt = min(remaining, s)
                cost += amt * p
                filled += amt
                remaining -= amt
                if remaining <= 0:
                    break
            if filled == 0:
                return None
            return {
                "avg_price": cost / filled,
                "filled_size": filled,
                "requested_size": size,
                "fully_filled": remaining <= 0.01,
                "slippage": (cost / filled - first_price) if first_price is not None else 0.0,
            }
        except Exception as e:
            self.logger.debug(f"sim fill failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Market utilities
    # ------------------------------------------------------------------
    @staticmethod
    def _get_token_ids(market: Dict) -> List[str]:
        tids = market.get("clobTokenIds", [])
        if isinstance(tids, str):
            try:
                tids = json.loads(tids)
            except Exception:
                return []
        return [str(t) for t in tids] if isinstance(tids, list) else []

    @staticmethod
    def _parse_end_date(s: Optional[str]):
        if not s:
            return None
        try:
            from datetime import datetime
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None

    def _is_structural_match(self, a: Dict, b: Dict) -> bool:
        """Stage-2 filter: endDate within 24h, same resolution source,
        same outcome count, distinct eventId, both non-zero volume."""
        if a.get("id") == b.get("id"):
            return False
        if (a.get("eventId") and b.get("eventId") and
                a["eventId"] == b["eventId"]):
            # Same event → they're probably the same-event-different-
            # deadline pattern (that's calendar_arb's job).
            return False

        ea, eb = self._parse_end_date(a.get("endDate")), self._parse_end_date(b.get("endDate"))
        if ea is None or eb is None:
            return False
        if abs((ea - eb).total_seconds()) > 24 * 3600:
            return False  # > 24h apart — not a duplicate, likely calendar arb

        outs_a = a.get("outcomes", [])
        outs_b = b.get("outcomes", [])
        if isinstance(outs_a, str):
            try: outs_a = json.loads(outs_a)
            except Exception: outs_a = []
        if isinstance(outs_b, str):
            try: outs_b = json.loads(outs_b)
            except Exception: outs_b = []
        if not outs_a or not outs_b or len(outs_a) != len(outs_b):
            return False

        # Resolution source must match (trivially different = ok)
        rs_a = (a.get("resolutionSource") or "").strip().lower()
        rs_b = (b.get("resolutionSource") or "").strip().lower()
        if rs_a and rs_b and rs_a != rs_b:
            return False

        # Both must have some activity
        vol_a = float(a.get("volumeNum") or a.get("volume") or 0)
        vol_b = float(b.get("volumeNum") or b.get("volume") or 0)
        if vol_a < 100 or vol_b < 100:
            return False

        return True

    # ------------------------------------------------------------------
    # Discovery (the 4 stages)
    # ------------------------------------------------------------------
    async def _discover_candidates(self, markets: List[Dict]) -> int:
        """Run stages 1-3 over a batch of markets; add survivors to
        discovered_pairs with their LLM confidence. Returns count added."""
        if not markets or self._llm is None:
            return 0

        # Stage 1: embeddings for each market in the batch
        emb_pairs: List[Tuple[int, int, float]] = []
        embs = []
        for i, m in enumerate(markets):
            q = m.get("question") or ""
            if len(q) < 15:
                embs.append(None)
                continue
            embs.append(self._get_embedding(q))

        existing_keys = {p.get("pair_key") for p in self.discovered_pairs}
        new_count = 0

        for i in range(len(markets)):
            if embs[i] is None:
                continue
            for j in range(i + 1, len(markets)):
                if embs[j] is None:
                    continue
                sim = self._cosine(embs[i], embs[j])
                if sim < self.similarity_threshold:
                    continue
                a, b = markets[i], markets[j]
                # Stage 2: structural
                if not self._is_structural_match(a, b):
                    continue
                pair_key = self._pair_key(a["id"], b["id"])
                if pair_key in existing_keys:
                    continue
                if pair_key in self.rejected_pairs:
                    continue
                # Stage 3: LLM
                try:
                    identical, conf, reason = await self._llm.verify(a, b)
                except Exception as e:
                    self.logger.debug(f"LLM verify failed: {e}")
                    continue
                if not identical or conf < self.min_confidence:
                    self.logger.debug(
                        f"🔁 Duplicate rejected pair {pair_key[:30]}: "
                        f"identical={identical} conf={conf:.2f} — {reason[:80]}"
                    )
                    continue

                # Passed all 3 stages
                self.discovered_pairs.append({
                    "pair_key": pair_key,
                    "a_id": a["id"],
                    "b_id": b["id"],
                    "a_question": a.get("question", ""),
                    "b_question": b.get("question", ""),
                    "a_end": a.get("endDate"),
                    "b_end": b.get("endDate"),
                    "a_resolution": (a.get("resolutionSource") or "")[:120],
                    "b_resolution": (b.get("resolutionSource") or "")[:120],
                    "similarity": round(sim, 3),
                    "llm_confidence": round(conf, 3),
                    "llm_reasoning": reason,
                    "discovered_at": time.time(),
                    "discovery_method": "llm",
                })
                existing_keys.add(pair_key)
                new_count += 1
                self.logger.info(
                    f"🔁 New duplicate candidate (sim={sim:.2f}, conf={conf:.2f}): "
                    f"'{a.get('question','')[:60]}' ≈ '{b.get('question','')[:60]}'"
                )

        if new_count > 0:
            self._save_json(self.DISCOVERED_FILE, self.discovered_pairs)
        return new_count

    # ------------------------------------------------------------------
    # Scan: discover + price-check known pairs
    # ------------------------------------------------------------------
    async def scan(self) -> List[Dict[str, Any]]:
        all_markets = self.scanner.get_all_active_markets(max_markets=5000)
        if not all_markets:
            return []

        market_map = {m["id"]: m for m in all_markets}
        self._last_market_map = market_map

        # Discovery — process one batch at a time (keeps LLM cost bounded)
        start = self._discovery_offset
        end = min(start + self.discovery_batch_size, len(all_markets))
        batch = all_markets[start:end]
        self.logger.info(f"🔁 Duplicate discovery: markets {start}-{end}/{len(all_markets)}")
        try:
            added = await self._discover_candidates(batch)
            if added:
                self.logger.info(f"🔁 Added {added} new duplicate candidate(s)")
        except Exception as e:
            self.logger.error(f"🔁 discovery failed: {e}")

        self._discovery_offset = end
        if self._discovery_offset >= len(all_markets):
            self._discovery_offset = 0
            # Drop pairs where one of the markets is no longer active
            before = len(self.discovered_pairs)
            active_ids = set(market_map.keys())
            self.discovered_pairs = [
                p for p in self.discovered_pairs
                if p.get("a_id") in active_ids and p.get("b_id") in active_ids
            ]
            if len(self.discovered_pairs) < before:
                self._save_json(self.DISCOVERED_FILE, self.discovered_pairs)
                self.logger.info(
                    f"🧹 Cleanup: dropped {before - len(self.discovered_pairs)} expired duplicate pair(s)"
                )

        # Monitoring phase: check prices for every known pair
        opportunities: List[Dict[str, Any]] = []
        price_snapshot: Dict[str, Any] = {}
        snap_now = time.time()

        for pair in self.discovered_pairs:
            mk_a = market_map.get(pair.get("a_id"))
            mk_b = market_map.get(pair.get("b_id"))
            if not mk_a or not mk_b:
                continue

            tids_a = self._get_token_ids(mk_a)
            tids_b = self._get_token_ids(mk_b)
            if len(tids_a) < 2 or len(tids_b) < 2:
                continue

            # Polymarket convention: tokenIds[0] = YES, tokenIds[1] = NO
            yes_a, no_a = tids_a[0], tids_a[1]
            yes_b, no_b = tids_b[0], tids_b[1]

            ask_yes_a = self._best_ask(yes_a)
            ask_no_a = self._best_ask(no_a)
            ask_yes_b = self._best_ask(yes_b)
            ask_no_b = self._best_ask(no_b)
            bid_yes_a = self._best_bid(yes_a)
            bid_no_a = self._best_bid(no_a)
            bid_yes_b = self._best_bid(yes_b)
            bid_no_b = self._best_bid(no_b)

            pair_key = pair["pair_key"]
            tier = self._tier_for(pair_key)

            snap_entry: Dict[str, Any] = {
                "strategy": "duplicate",
                "pair_key": pair_key,
                "a_id": pair.get("a_id"),
                "b_id": pair.get("b_id"),
                "a_question": pair.get("a_question", ""),
                "b_question": pair.get("b_question", ""),
                "a_end": pair.get("a_end"),
                "b_end": pair.get("b_end"),
                "llm_confidence": pair.get("llm_confidence"),
                "similarity": pair.get("similarity"),
                "tier": tier,
                "updated_at": snap_now,
            }
            # Best direction: min of YES(A)+NO(B) and NO(A)+YES(B)
            ya_nb_cost = None
            na_yb_cost = None
            if ask_yes_a and ask_no_b:
                ya_nb_cost = ask_yes_a["price"] + ask_no_b["price"]
            if ask_no_a and ask_yes_b:
                na_yb_cost = ask_no_a["price"] + ask_yes_b["price"]

            best_cost, best_dir = None, None
            if ya_nb_cost is not None and (na_yb_cost is None or ya_nb_cost <= na_yb_cost):
                best_cost, best_dir = ya_nb_cost, "YES_A+NO_B"
            elif na_yb_cost is not None:
                best_cost, best_dir = na_yb_cost, "NO_A+YES_B"

            if best_cost is not None:
                snap_entry.update({
                    "total_cost": best_cost,
                    "entry_direction": best_dir,
                    "entry_profit_usd": round(1.0 - best_cost, 4),
                    "entry_profit_pct": round((1.0 - best_cost) * 100, 2),
                })
            price_snapshot[pair_key] = snap_entry

            if best_cost is None:
                continue

            if tier == "rejected" or tier == "pending":
                continue

            min_edge = self.min_profit_threshold + (4 * self.estimated_fee)
            if best_cost >= 1.0 - min_edge:
                continue  # No profit after fees

            # Respect max_open_positions
            if len(self.open_positions) >= self.max_open_positions:
                self.logger.debug(
                    f"🔁 Opportunity skipped (max positions reached): {pair_key}"
                )
                continue

            # Compose the concrete legs
            if best_dir == "YES_A+NO_B":
                leg1_token, leg1_ask = yes_a, ask_yes_a
                leg2_token, leg2_ask = no_b, ask_no_b
            else:
                leg1_token, leg1_ask = no_a, ask_no_a
                leg2_token, leg2_ask = yes_b, ask_yes_b

            # Depth-cap the size
            desired = self._size_for_tier(tier, best_cost)
            max_depth = min(leg1_ask.get("size", 0), leg2_ask.get("size", 0))
            size = min(desired, max_depth)
            if size <= 0:
                continue

            opportunities.append({
                "pair_key": pair_key,
                "direction": best_dir,
                "leg1_token": leg1_token, "leg1_ask": leg1_ask["price"],
                "leg2_token": leg2_token, "leg2_ask": leg2_ask["price"],
                "total_cost": best_cost,
                "size": size,
                "tier": tier,
                "a_id": pair.get("a_id"), "b_id": pair.get("b_id"),
                "a_question": pair.get("a_question"),
                "b_question": pair.get("b_question"),
                "a_end": pair.get("a_end"), "b_end": pair.get("b_end"),
                "llm_confidence": pair.get("llm_confidence"),
                "strategy": "duplicate",
            })

        # Persist snapshot
        self._save_json(os.path.join("data", "duplicate_price_snapshot.json"), price_snapshot)
        return opportunities

    # ------------------------------------------------------------------
    # Entry / exit
    # ------------------------------------------------------------------
    async def should_enter(self, opp: Dict[str, Any]) -> bool:
        return True  # scan() already applied filters

    async def _emergency_sell(self, token_id: str, size: float) -> bool:
        """Port of calendar_arb emergency_sell: IOC ladder against best bid."""
        for ratio in (0.95, 0.70, 0.30):
            sim = self._simulate_fill(token_id, "SELL", size)
            if sim and sim.get("avg_price"):
                limit = max(0.01, float(sim["avg_price"]) * ratio)
            else:
                bid = self._best_bid(token_id)
                limit = max(0.01, float(bid["price"]) * ratio) if bid else 0.01
            try:
                r = await self.executor.execute_trade(
                    token_id=token_id, side="SELL", size=size, price=limit, order_type="IOC"
                )
            except Exception as e:
                self.logger.error(f"🚨 Rollback attempt exception: {e}")
                continue
            if r and r.get("success"):
                filled = float(r.get("sizeFilled", 0))
                if filled >= size * 0.99:
                    self.logger.info(f"✅ Rollback filled {filled:.2f}/{size:.2f} @ ${limit:.4f}")
                    return True
                size -= filled
                if size <= 0:
                    return True
        self.logger.critical(
            f"🚨 ROLLBACK EXHAUSTED for {token_id[:12]} — manual intervention required"
        )
        return False

    async def enter_position(self, opp: Dict[str, Any]) -> bool:
        leg1_token, leg2_token = opp["leg1_token"], opp["leg2_token"]
        size = float(opp["size"])

        # Slippage-aware simulated fills
        f1 = self._simulate_fill(leg1_token, "BUY", size)
        f2 = self._simulate_fill(leg2_token, "BUY", size)
        if not f1 or not f2 or not f1.get("fully_filled") or not f2.get("fully_filled"):
            self.logger.warning("⚠️ Insufficient liquidity for duplicate pair")
            return False

        total = f1["avg_price"] + f2["avg_price"]
        min_edge = self.min_profit_threshold + (4 * self.estimated_fee)
        if total >= 1.0 - min_edge:
            self.logger.warning(
                f"⚠️ Slippage kills edge: ${total:.4f} ≥ ${1.0 - min_edge:.4f}"
            )
            return False

        required = total * size
        try:
            balance = await self.executor.get_balance()
        except Exception as e:
            self.logger.error(f"⚠️ Balance fetch failed: {e}")
            return False
        if balance < required * 1.02:
            self.logger.warning(
                f"⚠️ Insufficient USDC: ${balance:.2f} < required ${required * 1.02:.2f}"
            )
            return False

        tier = opp.get("tier", "probe")
        self.logger.info(f"🔁 [tier={tier.upper()}] Duplicate-Arb Opportunity:")
        self.logger.info(f"   Direction: {opp['direction']} (size {size})")
        self.logger.info(f"   Leg1 ask ${opp['leg1_ask']:.4f} (avg ${f1['avg_price']:.4f})")
        self.logger.info(f"   Leg2 ask ${opp['leg2_ask']:.4f} (avg ${f2['avg_price']:.4f})")
        self.logger.info(f"   Total ${total:.4f} (profit ${1 - total:.4f} / {(1-total)*100:.1f}%)")
        self.logger.info(f"   A: {(opp.get('a_question') or '')[:60]}")
        self.logger.info(f"   B: {(opp.get('b_question') or '')[:60]}")

        tasks = [
            self.executor.execute_trade(
                token_id=leg1_token, side="BUY", size=size,
                price=f1["avg_price"], order_type="FOK",
            ),
            self.executor.execute_trade(
                token_id=leg2_token, side="BUY", size=size,
                price=f2["avg_price"], order_type="FOK",
            ),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        def leg_ok(res, exp):
            return (
                isinstance(res, dict) and res.get("success")
                and float(res.get("sizeFilled", 0)) >= exp * 0.99
            )

        ok1 = leg_ok(results[0], size)
        ok2 = leg_ok(results[1], size)

        if ok1 and not ok2:
            self.logger.error("❌ Leg2 failed — rolling back Leg1")
            await self._emergency_sell(leg1_token, size)
            return False
        if ok2 and not ok1:
            self.logger.error("❌ Leg1 failed — rolling back Leg2")
            await self._emergency_sell(leg2_token, size)
            return False
        if not ok1 and not ok2:
            self.logger.error("❌ Both legs failed")
            for res, tok in ((results[0], leg1_token), (results[1], leg2_token)):
                if isinstance(res, dict) and float(res.get("sizeFilled", 0)) > 0:
                    await self._emergency_sell(tok, float(res["sizeFilled"]))
            return False

        # Success — record
        pair_key = opp["pair_key"]
        pos = {
            **opp,
            "strategy": "duplicate",
            "entry_wall_time": time.time(),
            "entry_time": asyncio.get_event_loop().time(),
            "size": size,
            "actual_entry_cost": total,
            "strategy_name": self.strategy_name,
        }
        # Track both legs so exit can find them
        self.open_positions[leg1_token] = {**pos, "leg_role": "leg1"}
        self.open_positions[leg2_token] = {**pos, "leg_role": "leg2",
                                            "leg1_token": leg1_token,
                                            "leg2_token": leg2_token}
        self.open_positions[leg1_token]["leg1_token"] = leg1_token
        self.open_positions[leg1_token]["leg2_token"] = leg2_token

        if tier == "probe":
            self.pending_pairs.setdefault(pair_key, {
                "opened_at": pos["entry_wall_time"],
                "alerted": False,
                "a_id": opp.get("a_id"), "b_id": opp.get("b_id"),
                "a_question": opp.get("a_question"),
                "b_question": opp.get("b_question"),
                "probe_size": size, "probe_cost": total,
                "direction": opp["direction"],
            })
            self._save_json(self.PENDING_FILE, self.pending_pairs)

        self.stats["trades_entered"] += 1
        self.logger.info(f"✅ Duplicate-Arb entered (tier={tier}, pair={pair_key[:30]})")
        return True

    async def should_exit(self, position: Dict[str, Any]) -> bool:
        """Early exit when current exit value > entry cost + fees + threshold."""
        leg1 = position.get("leg1_token")
        leg2 = position.get("leg2_token")
        entry = position.get("actual_entry_cost") or position.get("total_cost")
        if not leg1 or not leg2 or entry is None:
            return False
        try:
            b1 = self._best_bid(leg1)
            b2 = self._best_bid(leg2)
            if not b1 or not b2:
                return False
            exit_value = b1["price"] + b2["price"]
            threshold = entry + (2 * self.estimated_fee) + self.early_exit_threshold
            if exit_value > threshold:
                self.logger.info(
                    f"💰 Duplicate early exit triggered: ${exit_value:.4f} > ${threshold:.4f}"
                )
                return True
        except Exception as e:
            self.logger.debug(f"should_exit error: {e}")
        return False

    async def exit_position(self, token_id: str) -> bool:
        pos = self.open_positions.get(token_id)
        if not pos:
            return False
        leg1 = pos.get("leg1_token")
        leg2 = pos.get("leg2_token")
        size = float(pos.get("size", 1.0))
        entry = pos.get("actual_entry_cost") or pos.get("total_cost") or 0

        sim1 = self._simulate_fill(leg1, "SELL", size)
        sim2 = self._simulate_fill(leg2, "SELL", size)
        price1 = (sim1 or {}).get("avg_price") or (self._best_bid(leg1) or {}).get("price", 0.01)
        price2 = (sim2 or {}).get("avg_price") or (self._best_bid(leg2) or {}).get("price", 0.01)

        tasks = [
            self.executor.execute_trade(
                token_id=leg1, side="SELL", size=size, price=price1, order_type="IOC"
            ),
            self.executor.execute_trade(
                token_id=leg2, side="SELL", size=size, price=price2, order_type="IOC"
            ),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        def ok(r, exp):
            return (
                isinstance(r, dict) and r.get("success")
                and float(r.get("sizeFilled", 0)) >= exp * 0.99
            )

        ok1, ok2 = ok(results[0], size), ok(results[1], size)
        if not ok1:
            filled = float(results[0].get("sizeFilled", 0)) if isinstance(results[0], dict) else 0
            await self._emergency_sell(leg1, size - filled)
        if not ok2:
            filled = float(results[1].get("sizeFilled", 0)) if isinstance(results[1], dict) else 0
            await self._emergency_sell(leg2, size - filled)

        exit_value = price1 + price2
        pnl = exit_value - entry - (2 * self.estimated_fee)
        self.open_positions.pop(leg1, None)
        self.open_positions.pop(leg2, None)
        self.stats["trades_exited"] += 1
        self.logger.info(f"✅ Duplicate-Arb exited (P&L ${pnl:+.4f} on size {size})")

        if self.telegram and self.telegram.enabled:
            try:
                pnl_pct = (pnl / entry * 100) if entry else 0.0
                q = (pos.get("a_question") or "")[:60]
                await self.telegram.send_notice(
                    f"🔁 [Duplicate] Early exit @ ${pnl:+.4f} ({pnl_pct:+.1f}%)\n"
                    f"   A: {q}\n"
                    f"   Exit value ${exit_value:.4f} vs entry ${entry:.4f} (size {size})"
                )
            except Exception:
                pass
        return True

    # ------------------------------------------------------------------
    # Telegram: escalate probes, poll for user decisions
    # ------------------------------------------------------------------
    async def _check_escalations(self, market_map: Dict[str, Dict]):
        if not self.telegram or not self.telegram.enabled:
            return
        now = time.time()
        changed = False
        for pair_key, state in list(self.pending_pairs.items()):
            if state.get("alerted"):
                continue
            if now - state.get("opened_at", now) < self.escalation_seconds:
                continue

            mk_a = market_map.get(state.get("a_id"))
            mk_b = market_map.get(state.get("b_id"))
            if not mk_a or not mk_b:
                self.pending_pairs.pop(pair_key, None)
                changed = True
                continue

            alert = {
                "strategy_label": STRATEGY_LABEL,
                "early_question": f"A: {mk_a.get('question', '')[:90]}",
                "late_question": f"B: {mk_b.get('question', '')[:90]}",
                "early_desc": (mk_a.get("description") or "")[:400],
                "late_desc": (mk_b.get("description") or "")[:400],
                "early_end": mk_a.get("endDate"),
                "late_end": mk_b.get("endDate"),
                "ask_no_early": state.get("probe_cost", 0) / 2,
                "ask_yes_late": state.get("probe_cost", 0) / 2,
                "total_cost": state.get("probe_cost", 0),
                "annualized_roi": 0,
            }
            try:
                ok = await self.telegram.send_pair_alert(
                    pair_key, alert, strategy_label=STRATEGY_LABEL
                )
            except TypeError:
                ok = await self.telegram.send_pair_alert(pair_key, alert)
            if ok:
                state["alerted"] = True
                state["alerted_at"] = now
                changed = True
        if changed:
            self._save_json(self.PENDING_FILE, self.pending_pairs)

    async def _process_telegram_replies(self):
        if not self.telegram or not self.telegram.enabled:
            return
        try:
            replies = await self.telegram.poll_replies()
        except Exception as e:
            self.logger.debug(f"telegram poll failed: {e}")
            return
        if not replies:
            return
        now = time.time()
        for reply in replies:
            key = reply.pair_key
            # Only handle keys that belong to this strategy (prefix dup__)
            if not key.startswith("dup__"):
                continue
            state = self.pending_pairs.pop(key, None)
            if not state:
                continue
            if reply.decision == "approve":
                self.confirmed_pairs[key] = {
                    **state,
                    "confirmed_at": now,
                    "confirmed_by": reply.user_id,
                }
                self.logger.info(f"✅ Duplicate pair CONFIRMED: {key}")
            else:
                self.rejected_pairs[key] = {
                    **state,
                    "rejected_at": now,
                    "rejected_by": reply.user_id,
                }
                self.logger.info(f"❌ Duplicate pair REJECTED: {key}")
        self._save_json(self.PENDING_FILE, self.pending_pairs)
        self._save_json(self.CONFIRMED_FILE, self.confirmed_pairs)
        self._save_json(self.REJECTED_FILE, self.rejected_pairs)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    async def run(self):
        self.logger.info("🔁 Duplicate Arb starting main loop")
        loop_count = 0
        while self.running:
            try:
                loop_count += 1
                self.logger.info(f"\n{'='*60}\n🔁 Duplicate Scan #{loop_count}\n{'='*60}")
                opportunities = await self.scan()
                await self._process_telegram_replies()
                await self._check_escalations(getattr(self, "_last_market_map", {}))

                if opportunities:
                    self.logger.info(f"🔁 {len(opportunities)} duplicate opportunity/opportunities:")
                    for idx, opp in enumerate(opportunities[:3], 1):
                        self.logger.info(
                            f"  {idx}. [{opp['tier'].upper()}] {opp['direction']} "
                            f"cost=${opp['total_cost']:.4f} profit="
                            f"${1-opp['total_cost']:.4f} | "
                            f"A='{opp.get('a_question','')[:40]}'"
                        )
                        entered = await self.enter_position(opp)
                        if entered:
                            break
                else:
                    self.logger.info("🔁 No duplicate opportunities this cycle")

                self.logger.info(
                    f"🔁 Stats: entered={self.stats['trades_entered']} "
                    f"exited={self.stats['trades_exited']} "
                    f"discovered={len(self.discovered_pairs)}"
                )

                # Heartbeat (shared file with calendar_arb). Balance is updated
                # by whichever strategy ticks first; we leave None so we don't
                # trample calendar_arb's fresher read if our scan is slower.
                try:
                    from core.heartbeat import MultiStrategyHeartbeat
                    MultiStrategyHeartbeat.instance().write(
                        strategy_key="duplicate_arb",
                        balance_usd=None,
                        stats={
                            "label": STRATEGY_LABEL,
                            "discovered": len(self.discovered_pairs),
                            "confirmed": len(self.confirmed_pairs),
                            "pending": len(self.pending_pairs),
                            "rejected": len(self.rejected_pairs),
                            "trades_entered": int(self.stats.get("trades_entered", 0)),
                            "trades_exited": int(self.stats.get("trades_exited", 0)),
                            "open_positions": len(self.open_positions),
                            "loop": loop_count,
                        },
                    )
                except Exception as e:
                    self.logger.debug(f"Heartbeat write failed: {e}")

                await asyncio.sleep(self.scan_interval)
            except asyncio.CancelledError:
                self.logger.info("🔁 Duplicate Arb cancelled")
                break
            except Exception as e:
                self.logger.error(f"🔁 Duplicate loop error: {e}", exc_info=True)
                await asyncio.sleep(5)

    def start(self):
        self.running = True
        return self.run()

    def stop(self):
        self.running = False
