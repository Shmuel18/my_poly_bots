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


@app.get("/api/status")
def api_status():
    log = _latest_bot_log()
    return JSONResponse({
        "server_time": int(time.time()),
        "service": _systemctl_status(SERVICE_NAME),
        "env": _env_flags(),
        "latest_balance_usd": _parse_latest_balance(log),
        "log_file": str(log) if log else None,
    })


@app.get("/api/pairs")
def api_pairs():
    discovered = _read_json(DATA_DIR / "discovered_pairs.json", [])
    confirmed = _read_json(DATA_DIR / "confirmed_pairs.json", {})
    pending = _read_json(DATA_DIR / "pending_confirmation.json", {})
    rejected = _read_json(DATA_DIR / "rejected_pairs.json", {})
    # Ensure lists/dicts
    if not isinstance(discovered, list):
        discovered = []
    if not isinstance(confirmed, dict):
        confirmed = {}
    if not isinstance(pending, dict):
        pending = {}
    if not isinstance(rejected, dict):
        rejected = {}
    return JSONResponse({
        "discovered": discovered,
        "confirmed": confirmed,
        "pending": pending,
        "rejected": rejected,
        "counts": {
            "discovered": len(discovered),
            "confirmed": len(confirmed),
            "pending": len(pending),
            "rejected": len(rejected),
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
