"""Telegram notifier for human-in-the-loop pair confirmation.

Sends alerts to a single user chat with ✅/❌ inline buttons and polls
callback_query updates to surface the user's decisions back to the strategy.

If TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID are unset, the notifier becomes a
silent no-op so the strategy can still run without human-in-the-loop.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

APPROVE_PREFIX = "approve:"
REJECT_PREFIX = "reject:"


@dataclass
class TelegramReply:
    pair_key: str
    decision: str  # "approve" or "reject"
    user_id: int
    update_id: int


class TelegramNotifier:
    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        timeout_sec: float = 10.0,
    ):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self.timeout_sec = timeout_sec
        self._base_url = f"https://api.telegram.org/bot{self.bot_token}" if self.bot_token else None
        self._last_update_id = 0
        self.enabled = bool(self.bot_token and self.chat_id)
        if not self.enabled:
            logger.warning(
                "Telegram notifier DISABLED — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env to enable."
            )
        else:
            logger.info(f"📨 Telegram notifier enabled (chat_id={self.chat_id})")

    async def send_pair_alert(
        self,
        pair_key: str,
        pair_info: Dict[str, Any],
        strategy_label: Optional[str] = None,
    ) -> bool:
        """Send an arbitrage-candidate alert with Approve / Reject buttons.

        pair_info expects keys: early_question, late_question, early_desc,
        late_desc, early_end, late_end, ask_no_early, ask_yes_late, total_cost,
        annualized_roi, early_url (optional), late_url (optional).

        strategy_label — "Calendar" or "Duplicate". Defaults to "Calendar".
        """
        if not self.enabled:
            return False

        label = strategy_label or pair_info.get("strategy_label") or "Calendar"
        emoji = "🔁" if str(label).lower().startswith("dup") else "📅"

        roi = pair_info.get("annualized_roi", 0) * 100
        profit = (1.0 - pair_info.get("total_cost", 1.0)) * 100
        text = (
            f"🔔 *[{label}] pair candidate — please verify*\n\n"
            f"{emoji} *First market:* {pair_info.get('early_question', 'N/A')}\n"
            f"  ends: `{pair_info.get('early_end', '?')}` · ask\\_1: `${pair_info.get('ask_no_early', 0):.3f}`\n\n"
            f"{emoji} *Second market:* {pair_info.get('late_question', 'N/A')}\n"
            f"  ends: `{pair_info.get('late_end', '?')}` · ask\\_2: `${pair_info.get('ask_yes_late', 0):.3f}`\n\n"
            f"*Total cost:* `${pair_info.get('total_cost', 0):.4f}` · "
            f"*Locked profit:* `{profit:.2f}%`"
            + (f" · *ROI (annualized):* `{roi:.1f}%`\n\n" if roi else "\n\n")
            + "_Market 1 desc:_\n"
            f"{(pair_info.get('early_desc') or '')[:350]}\n\n"
            "_Market 2 desc:_\n"
            f"{(pair_info.get('late_desc') or '')[:350]}\n\n"
            "Are the resolution criteria IDENTICAL? ✅ Approve → bot trades at full size. ❌ Reject → blacklist."
        )

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "reply_markup": {
                "inline_keyboard": [[
                    {"text": "✅ Approve", "callback_data": f"{APPROVE_PREFIX}{pair_key}"},
                    {"text": "❌ Reject", "callback_data": f"{REJECT_PREFIX}{pair_key}"},
                ]]
            },
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
                r = await client.post(f"{self._base_url}/sendMessage", json=payload)
            if r.status_code == 200 and r.json().get("ok"):
                logger.info(f"📤 Telegram alert sent for pair {pair_key}")
                return True
            logger.error(f"Telegram sendMessage failed: {r.status_code} {r.text[:300]}")
            return False
        except Exception as e:
            logger.error(f"Telegram send exception: {e}")
            return False

    async def poll_replies(self) -> List[TelegramReply]:
        """Long-poll Telegram for new callback_query replies. Advances the
        internal update cursor so each reply is returned exactly once."""
        if not self.enabled:
            return []

        params = {"timeout": 0, "allowed_updates": ["callback_query"]}
        if self._last_update_id:
            params["offset"] = self._last_update_id + 1

        try:
            async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
                r = await client.get(f"{self._base_url}/getUpdates", params=params)
            if r.status_code != 200:
                logger.debug(f"Telegram getUpdates non-200: {r.status_code}")
                return []
            data = r.json()
            if not data.get("ok"):
                return []
        except Exception as e:
            logger.debug(f"Telegram poll exception: {e}")
            return []

        replies: List[TelegramReply] = []
        for update in data.get("result", []):
            update_id = update.get("update_id", 0)
            self._last_update_id = max(self._last_update_id, update_id)

            cq = update.get("callback_query")
            if not cq:
                continue
            data_field = cq.get("data", "")
            user_id = cq.get("from", {}).get("id", 0)

            decision = None
            pair_key = None
            if data_field.startswith(APPROVE_PREFIX):
                decision = "approve"
                pair_key = data_field[len(APPROVE_PREFIX):]
            elif data_field.startswith(REJECT_PREFIX):
                decision = "reject"
                pair_key = data_field[len(REJECT_PREFIX):]
            if not decision or not pair_key:
                continue

            # Acknowledge the callback so the user's button stops spinning.
            try:
                async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
                    await client.post(
                        f"{self._base_url}/answerCallbackQuery",
                        json={"callback_query_id": cq.get("id"), "text": f"Recorded: {decision}"},
                    )
            except Exception:
                pass

            replies.append(TelegramReply(
                pair_key=pair_key, decision=decision, user_id=user_id, update_id=update_id
            ))

        if replies:
            logger.info(f"📥 Telegram poll: {len(replies)} reply/replies")
        return replies

    async def send_notice(self, text: str) -> bool:
        """Free-form notice (e.g. probe opened, exhausted rollback). Plain text."""
        if not self.enabled:
            return False
        try:
            async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
                r = await client.post(
                    f"{self._base_url}/sendMessage",
                    json={"chat_id": self.chat_id, "text": text},
                )
            return r.status_code == 200
        except Exception:
            return False
