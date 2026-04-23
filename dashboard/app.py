"""Read-only web dashboard for the Polymarket calendar_arbitrage bot.

Serves a single HTML page and JSON endpoints summarising:
- systemd service status + memory + uptime
- Recent wallet balance (parsed from the bot's own log)
- Discovered / confirmed / pending / rejected pairs (from data/*.json)
- Open positions (from data/positions_*.json)
- Last N log lines

Runs as a separate process/service next to polybot so a dashboard crash
can never bring the bot down, and vice-versa.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BOT_DIR = Path(os.getenv("POLYBOT_DIR", "/opt/polybot"))
DATA_DIR = BOT_DIR / "data"
LOG_DIR = BOT_DIR / "logs"
SERVICE_NAME = os.getenv("POLYBOT_SERVICE", "polybot.service")

app = FastAPI(title="Polybot Dashboard")

STATIC_DIR = Path(__file__).resolve().parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

INDEX_HTML = Path(__file__).resolve().parent / "index.html"
LOGS_HTML = Path(__file__).resolve().parent / "logs.html"
ENV_HTML = Path(__file__).resolve().parent / "env.html"


def _read_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def _systemctl_status(unit: str) -> Dict[str, Any]:
    try:
        active = subprocess.run(
            ["systemctl", "is-active", unit], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        show = subprocess.run(
            ["systemctl", "show", unit, "-p", "ActiveEnterTimestamp,MainPID,MemoryCurrent"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        props = {}
        for line in show.strip().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                props[k] = v
        uptime = None
        try:
            # "Wed 2026-04-22 12:32:34 UTC"
            ts = props.get("ActiveEnterTimestamp", "")
            if ts and ts != "0":
                import datetime as _dt
                parsed = None
                for fmt in ("%a %Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S %Z"):
                    try:
                        parsed = _dt.datetime.strptime(ts, fmt)
                        break
                    except ValueError:
                        continue
                if parsed:
                    delta = _dt.datetime.utcnow() - parsed.replace(tzinfo=None)
                    uptime = int(delta.total_seconds())
        except Exception:
            pass
        mem_bytes = props.get("MemoryCurrent", "")
        try:
            mem_mb = int(mem_bytes) / 1024 / 1024 if mem_bytes and mem_bytes != "[not set]" else None
        except Exception:
            mem_mb = None
        return {
            "unit": unit,
            "active": active,
            "main_pid": props.get("MainPID"),
            "uptime_sec": uptime,
            "memory_mb": round(mem_mb, 1) if mem_mb is not None else None,
        }
    except Exception as e:
        return {"unit": unit, "active": "unknown", "error": str(e)}


def _latest_bot_log() -> Path | None:
    if not LOG_DIR.exists():
        return None
    candidates = sorted(LOG_DIR.glob("bot_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


_BALANCE_RE = re.compile(r"Balance:\s*\$([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)


def _parse_latest_balance(log_path: Path | None) -> float | None:
    if not log_path or not log_path.exists():
        return None
    try:
        # Read last 200 KB — plenty for recent balance lines
        with log_path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            read_from = max(0, size - 200_000)
            f.seek(read_from)
            tail = f.read().decode("utf-8", errors="ignore")
        matches = _BALANCE_RE.findall(tail)
        if matches:
            return float(matches[-1])
    except Exception:
        pass
    return None


def _tail_log(log_path: Path | None, n_lines: int = 60) -> List[str]:
    if not log_path or not log_path.exists():
        return []
    try:
        with log_path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            read_from = max(0, size - 80_000)
            f.seek(read_from)
            tail_bytes = f.read()
        tail_text = tail_bytes.decode("utf-8", errors="ignore")
        # Strip ANSI colour escapes
        tail_text = re.sub(r"\x1b\[[0-9;]*m", "", tail_text)
        lines = tail_text.strip().splitlines()
        return lines[-n_lines:]
    except Exception:
        return []


def _env_flags() -> Dict[str, bool]:
    env_path = BOT_DIR / "config" / ".env"
    flags = {
        "POLYMARKET_API_KEY": False,
        "POLYMARKET_PRIVATE_KEY": False,
        "POLYMARKET_FUNDER_ADDRESS": False,
        "GEMINI_API_KEY": False,
        "TELEGRAM_BOT_TOKEN": False,
        "TELEGRAM_CHAT_ID": False,
    }
    if not env_path.exists():
        return flags
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if "=" not in line or line.strip().startswith("#"):
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key in flags:
                flags[key] = bool(val)
    except Exception:
        pass
    return flags


@app.get("/", response_class=HTMLResponse)
def index():
    if INDEX_HTML.exists():
        return HTMLResponse(INDEX_HTML.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Polybot Dashboard</h1><p>index.html missing.</p>")


@app.get("/logs", response_class=HTMLResponse)
def logs_page():
    if LOGS_HTML.exists():
        return HTMLResponse(LOGS_HTML.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Logs</h1><p>logs.html missing.</p>")


@app.get("/env", response_class=HTMLResponse)
def env_page():
    if ENV_HTML.exists():
        return HTMLResponse(ENV_HTML.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Env</h1><p>env.html missing.</p>")


@app.get("/api/status")
def api_status():
    log = _latest_bot_log()
    # Prefer the bot's own heartbeat snapshot (written every scan) over
    # scraping a balance line out of the log file.
    heartbeat = _read_json(DATA_DIR / "status_snapshot.json", {})
    if not isinstance(heartbeat, dict):
        heartbeat = {}
    balance = heartbeat.get("balance_usd")
    if balance is None:
        balance = _parse_latest_balance(log)
    return JSONResponse({
        "server_time": int(time.time()),
        "service": _systemctl_status(SERVICE_NAME),
        "env": _env_flags(),
        "latest_balance_usd": balance,
        "heartbeat": heartbeat or None,
        "log_file": str(log) if log else None,
    })


def _pair_key(early_id: str, late_id: str) -> str:
    """Mirror of CalendarArbitrageStrategy._pair_key so the dashboard can
    cross-reference price snapshots without importing the strategy module."""
    a, b = sorted((str(early_id), str(late_id)))
    return f"{a[:12]}__{b[:12]}"


@app.get("/api/pairs")
def api_pairs():
    # ---------- Calendar arbitrage ----------
    cal_discovered = _read_json(DATA_DIR / "discovered_pairs.json", [])
    cal_confirmed = _read_json(DATA_DIR / "confirmed_pairs.json", {})
    cal_pending = _read_json(DATA_DIR / "pending_confirmation.json", {})
    cal_rejected = _read_json(DATA_DIR / "rejected_pairs.json", {})
    cal_snap = _read_json(DATA_DIR / "price_snapshot.json", {})

    if not isinstance(cal_discovered, list): cal_discovered = []
    if not isinstance(cal_confirmed, dict): cal_confirmed = {}
    if not isinstance(cal_pending, dict): cal_pending = {}
    if not isinstance(cal_rejected, dict): cal_rejected = {}
    if not isinstance(cal_snap, dict): cal_snap = {}

    for p in cal_discovered:
        key = _pair_key(p.get("early_id", ""), p.get("late_id", ""))
        p["pair_key"] = key
        p["strategy"] = "calendar"
        p["strategy_label"] = "Calendar"
        if key in cal_snap:
            p["live"] = cal_snap[key]

    # ---------- Duplicate arbitrage ----------
    dup_discovered = _read_json(DATA_DIR / "duplicate_discovered.json", [])
    dup_confirmed = _read_json(DATA_DIR / "duplicate_confirmed.json", {})
    dup_pending = _read_json(DATA_DIR / "duplicate_pending.json", {})
    dup_rejected = _read_json(DATA_DIR / "duplicate_rejected.json", {})
    dup_snap = _read_json(DATA_DIR / "duplicate_price_snapshot.json", {})

    if not isinstance(dup_discovered, list): dup_discovered = []
    if not isinstance(dup_confirmed, dict): dup_confirmed = {}
    if not isinstance(dup_pending, dict): dup_pending = {}
    if not isinstance(dup_rejected, dict): dup_rejected = {}
    if not isinstance(dup_snap, dict): dup_snap = {}

    for p in dup_discovered:
        key = p.get("pair_key") or ""
        p["strategy"] = "duplicate"
        p["strategy_label"] = "Duplicate"
        if key in dup_snap:
            p["live"] = dup_snap[key]

    # ---------- Merge + tag ----------
    def _tag(d: Dict[str, Any], strategy: str) -> Dict[str, Any]:
        out = {}
        for k, v in d.items():
            if isinstance(v, dict):
                v = {**v, "strategy": strategy}
            out[k] = v
        return out

    return JSONResponse({
        "discovered": cal_discovered + dup_discovered,
        "confirmed": {**_tag(cal_confirmed, "calendar"), **_tag(dup_confirmed, "duplicate")},
        "pending":   {**_tag(cal_pending, "calendar"),   **_tag(dup_pending, "duplicate")},
        "rejected":  {**_tag(cal_rejected, "calendar"),  **_tag(dup_rejected, "duplicate")},
        "counts": {
            "discovered": len(cal_discovered) + len(dup_discovered),
            "confirmed": len(cal_confirmed) + len(dup_confirmed),
            "pending": len(cal_pending) + len(dup_pending),
            "rejected": len(cal_rejected) + len(dup_rejected),
        },
        "by_strategy": {
            "calendar": {
                "discovered": len(cal_discovered),
                "confirmed": len(cal_confirmed),
                "pending": len(cal_pending),
                "rejected": len(cal_rejected),
            },
            "duplicate": {
                "discovered": len(dup_discovered),
                "confirmed": len(dup_confirmed),
                "pending": len(dup_pending),
                "rejected": len(dup_rejected),
            },
        },
    })


@app.get("/api/positions")
def api_positions():
    positions: List[Dict[str, Any]] = []
    if DATA_DIR.exists():
        for path in DATA_DIR.glob("positions_*.json"):
            data = _read_json(path, {})
            if isinstance(data, dict):
                for token_id, pos in data.items():
                    if isinstance(pos, dict):
                        positions.append({"token_id": token_id, **pos})
    return JSONResponse({"positions": positions, "count": len(positions)})


@app.get("/api/logs")
def api_logs(n: int = 60):
    n = max(1, min(400, int(n)))
    return JSONResponse({"lines": _tail_log(_latest_bot_log(), n_lines=n)})
