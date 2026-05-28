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

from fastapi import FastAPI, Request
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


# Lines matching these patterns are redacted before the (public) dashboard
# serves them. Defense in depth: the bot itself shouldn't log secrets, but
# if any code path ever does, this prevents the log-tail endpoint from
# leaking key material / API secrets / private keys to anyone who can hit
# /api/logs.
_SECRET_LINE_RE = re.compile(
    r"(private[_-]?key|api[_-]?secret|api[_-]?passphrase|passphrase|"
    r"mnemonic|seed[_-]?phrase|0x[a-fA-F0-9]{40,})",
    re.IGNORECASE,
)


def _scrub_secret(line: str) -> str:
    if _SECRET_LINE_RE.search(line):
        return "[redacted — line matched a secret pattern]"
    return line


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
        return [_scrub_secret(ln) for ln in lines[-n_lines:]]
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


@app.get("/healthz")
def healthz():
    """Liveness+freshness probe for external uptime monitors.

    Returns 200 only when the bot wrote its heartbeat recently. A stale
    heartbeat (bot crashed, hung on an orderbook fetch, or stuck in a
    scan-failure sleep loop) returns 503 so UptimeRobot / Healthchecks.io
    can alert. ``max_age_s`` defaults to 1800s (30 min) — generous given
    scans can take a while, but catches a truly dead bot.
    """
    max_age_s = 1800
    hb = _read_json(DATA_DIR / "status_snapshot.json", {})
    updated = hb.get("updated_at") if isinstance(hb, dict) else None
    now = time.time()
    age = (now - updated) if isinstance(updated, (int, float)) else None
    healthy = age is not None and age <= max_age_s
    body = {
        "status": "ok" if healthy else "stale",
        "heartbeat_age_s": round(age, 1) if age is not None else None,
        "max_age_s": max_age_s,
        "server_time": int(now),
    }
    return JSONResponse(body, status_code=200 if healthy else 503)


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
            # Skip backup / archive files. The bot writes one canonical
            # ``positions_<addr>.json`` per account; anything with a
            # suffix like ``positions_<addr>.backup-<ts>.json`` is a
            # snapshot, not a live state, and should not show up as
            # active positions on the dashboard.
            if ".backup-" in path.name or ".bak" in path.name:
                continue
            data = _read_json(path, {})
            if isinstance(data, dict):
                for token_id, pos in data.items():
                    if isinstance(pos, dict):
                        positions.append({"token_id": token_id, **pos})
    return JSONResponse({"positions": positions, "count": len(positions)})


# ----- Pending-pair approval/rejection from the dashboard ---------------
# The bot loads pending/confirmed/rejected dicts ONCE at startup and only
# writes them on its own scan loop. To let the dashboard mutate that
# state without racing the bot's writes, we append the user's intent to
# a small queue file (``dashboard_approvals.json``); the bot drains it
# at the top of every scan and applies the changes.
APPROVALS_FILE = DATA_DIR / "dashboard_approvals.json"


# Optional write-protection for the approval endpoints. When
# POLYBOT_APPROVAL_TOKEN is set in the environment, confirm/reject
# require a matching ``X-Approve-Token`` header. Off by default so the
# current (pre-go-live) dashboard keeps working; flip it on before
# making the site public for real-money trading.
APPROVAL_TOKEN = os.getenv("POLYBOT_APPROVAL_TOKEN", "").strip()


def _check_approval_token(request_token: str | None) -> bool:
    if not APPROVAL_TOKEN:
        return True  # protection disabled
    return bool(request_token) and request_token == APPROVAL_TOKEN


def _append_approval(pair_key: str, action: str) -> Dict[str, Any]:
    """Add an entry to the queue (atomic write). Returns resulting state."""
    if not isinstance(pair_key, str) or not pair_key:
        return {"ok": False, "error": "invalid pair_key"}
    if action not in ("confirm", "reject"):
        return {"ok": False, "error": f"invalid action: {action}"}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    queue: List[Dict[str, Any]] = []
    try:
        if APPROVALS_FILE.exists():
            queue = json.loads(APPROVALS_FILE.read_text(encoding="utf-8") or "[]")
            if not isinstance(queue, list):
                queue = []
    except Exception:
        queue = []
    # Replace any pending entry for the same pair_key — last write wins.
    queue = [q for q in queue if q.get("pair_key") != pair_key]
    queue.append({
        "pair_key": pair_key,
        "action": action,
        "queued_at": int(time.time()),
        "source": "dashboard",
    })
    # Atomic write: temp file + os.replace so a crash mid-write can't leave
    # a half-written queue (which the bot would fail to parse and skip).
    tmp = APPROVALS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, APPROVALS_FILE)
    return {"ok": True, "queued": len(queue), "applied_on_next_scan": True}


@app.post("/api/pairs/{pair_key}/confirm")
def api_pair_confirm(pair_key: str, request: Request):
    if not _check_approval_token(request.headers.get("X-Approve-Token")):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    return JSONResponse(_append_approval(pair_key, "confirm"))


@app.post("/api/pairs/{pair_key}/reject")
def api_pair_reject(pair_key: str, request: Request):
    if not _check_approval_token(request.headers.get("X-Approve-Token")):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    return JSONResponse(_append_approval(pair_key, "reject"))


@app.get("/api/approvals/queue")
def api_approvals_queue():
    """Inspect the queue. Useful for debugging — drain happens in the bot."""
    if not APPROVALS_FILE.exists():
        return JSONResponse({"queue": []})
    try:
        return JSONResponse({"queue": json.loads(APPROVALS_FILE.read_text(encoding="utf-8"))})
    except Exception:
        return JSONResponse({"queue": []})


@app.get("/api/logs")
def api_logs(n: int = 60):
    n = max(1, min(400, int(n)))
    return JSONResponse({"lines": _tail_log(_latest_bot_log(), n_lines=n)})
