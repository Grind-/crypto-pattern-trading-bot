import asyncio
import logging
import math
import os
import re as _re
import secrets
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import FastAPI, BackgroundTasks, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from .data_fetcher import INTERVAL_MINUTES, fetch_klines, fetch_latest_klines, get_available_symbols

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)
from .indicators import compute_indicators
from .claude_analyst import (analyze_with_claude, get_live_signal,
                              get_regime,
                              scan_market, test_connection,
                              synthesize_learnings,
                              synthesize_community_patterns,
                              distill_and_promote_rules,
                              promote_symbol_patterns_via_claude)
from .news_analyst import run_news_cycle, get_news_intelligence
from .risk_agent import calculate_risk_params
from .news_analyst import get_news_score_for_symbol
from .simulator import run_simulation, FEE_TIERS
from .calibration import calibrate_thresholds, calibration_meta
from .binance_trader import BinanceTrader
from .state_store import (save_live_state, load_live_state, clear_live_state,
                          deactivate_live_state, update_position)
from .sim_store import (save_simulation, load_simulations as _load_sims,
                        load_simulation_detail)
from .database import init_db
from .user_store import (init_users, list_users, get_user, authenticate,
                         create_user, delete_user, set_enabled, reset_password,
                         update_claude_config, set_platform_access,
                         get_claude_api_key, get_claude_oauth_token,
                         uses_platform, uses_subscription,
                         save_binance_keys, get_binance_keys, set_email)
from .knowledge_store import (append_trade_log, load_trade_log,
                               save_live_state_snapshot, load_live_state_snapshot,
                               load_user_settings, save_user_settings,
                               append_live_log, load_live_log, trim_live_log)

def _floor_to_step(qty: float, step: float) -> float:
    """Floor qty to the nearest multiple of step (avoids LOT_SIZE filter errors)."""
    if step <= 0:
        return qty
    precision = max(0, -int(math.floor(math.log10(step))))
    floored = math.floor(qty / step) * step
    return round(floored, precision)


# ── Session store ─────────────────────────────────────────────────────────────
_SESSIONS: dict[str, dict] = {}  # token → {"username": str, "expiry": float}
_SESSION_TTL = 86400 * 7

PUBLIC_PATHS = {"/", "/login", "/auth/login", "/auth/logout", "/guide", "/documentation"}

# ── Login rate limiter ─────────────────────────────────────────────────────────
_LOGIN_ATTEMPTS: dict[str, list] = defaultdict(list)
_LOGIN_MAX = 5
_LOGIN_WINDOW = 60  # seconds


def _check_rate_limit(ip: str) -> bool:
    """Return True if the IP is allowed to attempt a login, False if rate-limited."""
    now = time.time()
    attempts = [t for t in _LOGIN_ATTEMPTS[ip] if now - t < _LOGIN_WINDOW]
    _LOGIN_ATTEMPTS[ip] = attempts
    if len(attempts) >= _LOGIN_MAX:
        return False
    attempts.append(now)
    return True


def _valid_session(token: str) -> Optional[str]:
    entry = _SESSIONS.get(token)
    if entry and time.time() < entry["expiry"]:
        return entry["username"]
    _SESSIONS.pop(token, None)
    return None


def _get_current_user(request: Request) -> dict:
    token = request.cookies.get("session", "")
    username = _valid_session(token)
    if not username:
        raise HTTPException(401, "Not authenticated")
    user = get_user(username)
    if not user or not user.get("enabled"):
        raise HTTPException(401, "Account disabled")
    return {"username": username, **user}


def _require_admin(request: Request) -> dict:
    user = _get_current_user(request)
    if user.get("role") != "admin":
        raise HTTPException(403, "Admin-Rechte erforderlich")
    return user


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith("/static/"):
            return await call_next(request)
        token = request.cookies.get("session", "")
        if not _valid_session(token):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Not authenticated"}, status_code=401)
            return RedirectResponse("/", status_code=302)
        return await call_next(request)


app = FastAPI(title="Crypto Pattern AI", docs_url=None, redoc_url=None)
app.add_middleware(AuthMiddleware)
app.mount("/static", StaticFiles(directory="frontend"), name="static")


@app.on_event("startup")
async def _startup():
    init_db()
    init_users()
    await _auto_resume_all()
    asyncio.create_task(_news_loop())
    asyncio.create_task(_session_cleanup_loop())


async def _session_cleanup_loop():
    """Periodically evict expired sessions to prevent unbounded growth."""
    while True:
        await asyncio.sleep(900)
        now = time.time()
        expired = [k for k, v in list(_SESSIONS.items()) if v["expiry"] <= now]
        for k in expired:
            _SESSIONS.pop(k, None)
        if expired:
            logger.debug("Evicted %d expired sessions", len(expired))


async def _news_loop():
    """Background task: News Agent runs every hour."""
    await asyncio.sleep(30)  # brief delay so startup completes first
    while True:
        try:
            await run_news_cycle()  # uses platform proxy (no explicit creds needed)
        except Exception:
            pass
        await asyncio.sleep(3600)


async def _auto_resume_all():
    """Resume live trading for any user who was active before restart."""
    for username, user_data in list_users().items():
        if not user_data.get("enabled"):
            continue
        saved = load_live_state(username)
        if not saved or not saved.get("was_running"):
            continue

        # Prefer keys from persistent user store; fall back to live_states row
        bkey, bsec = get_binance_keys(username)
        if not bkey or not bsec:
            bkey = saved.get("api_key") or ""
            bsec = saved.get("api_secret") or ""
        if not bkey or not bsec:
            deactivate_live_state(username)
            continue

        saved_symbol = saved.get("symbol") or ""
        saved_mode = (saved.get("strategy_name") or "single").strip()
        if saved_mode not in ("single", "portfolio"):
            saved_mode = "single"
        req = LiveRequest(
            api_key=bkey,
            api_secret=bsec,
            symbol="" if saved_mode == "portfolio" else saved_symbol,
            interval=saved["interval"],
            trade_amount_usdt=saved["trade_amount"],
            compounding_mode=saved.get("compounding_mode", "compound"),
            analysis_weight=int(saved.get("analysis_weight") or 70),
            min_confidence=int(saved.get("min_confidence") or 55),
            min_confidence_sell=int(saved.get("min_confidence_sell") or 40),
            sl_atr_mult=float(saved.get("sl_atr_mult") or 1.5),
            tp_atr_mult=float(saved.get("tp_atr_mult") or 2.5),
            mode=saved_mode,
            max_per_position=float(saved.get("max_per_position") or 0.0),
        )
        trader = BinanceTrader(bkey, bsec)
        valid = await trader.validate_keys()
        if not valid:
            deactivate_live_state(username)
            continue

        api_key, oauth_token = _claude_creds(username)
        session_token = str(uuid.uuid4())
        state = _get_live_state(username)
        resumed_log = load_live_log(username)
        resumed_log.append("── Bot neu gestartet, Trading fortgesetzt ──")

        if saved_mode == "portfolio":
            state.update({
                "running": True,
                "status": "active",
                "position": "FLAT",
                "symbol": "",
                "interval": req.interval,
                "trade_amount": req.trade_amount_usdt,
                "current_capital": saved.get("current_capital") or req.trade_amount_usdt,
                "position_qty": 0,
                "compounding_mode": req.compounding_mode,
                "signals": [],
                "log": resumed_log,
                "api_key": req.api_key,
                "api_secret": req.api_secret,
                "next_check_ts": None,
                "next_check_str": None,
                "candle_count": 0,
                "analysis_weight": req.analysis_weight,
                "min_confidence": req.min_confidence,
                "min_confidence_sell": req.min_confidence_sell,
                "sl_atr_mult": req.sl_atr_mult,
                "tp_atr_mult": req.tp_atr_mult,
                "trade_history": saved.get("trade_history", []),
                "live_candles": [],
                "buy_price": None,
                "_session_token": session_token,
                "_username": username,
                "_is_resume": True,
                "last_regime": None,
                "last_risk": None,
                "last_news_score": None,
                "calibrated_thresholds": saved.get("calibrated_thresholds") or {},
                "mode": "portfolio",
                "portfolio_positions": {},  # loop will rebuild from balances
                "max_per_position": req.max_per_position,
            })
            asyncio.create_task(_portfolio_loop(req, username, api_key, oauth_token, session_token))
        else:
            # Single mode: reconcile state against actual Binance holdings
            reconciled_position = saved.get("position", "FLAT")
            reconciled_qty = saved.get("position_qty") or 0
            reconciled_buy_price = saved.get("buy_price")
            if saved_symbol:
                try:
                    balances = await trader.get_balances()
                    usdc = balances.get("USDC", 0.0)
                    base_asset = saved_symbol.replace("USDC", "").replace("USDT", "")
                    crypto_held = balances.get(base_asset, 0.0)
                    if reconciled_position == "IN_POSITION" and crypto_held <= 0:
                        logger.warning(f"[{username}] Resume desync: saved=IN_POSITION but {base_asset}=0 → FLAT")
                        reconciled_position = "FLAT"
                        reconciled_qty = 0
                        reconciled_buy_price = None
                    elif reconciled_position == "FLAT" and crypto_held > 0 and usdc < 10.0:
                        logger.info(f"[{username}] Resume: found {crypto_held} {base_asset}, no USDC → IN_POSITION")
                        reconciled_position = "IN_POSITION"
                        reconciled_qty = crypto_held
                except Exception as e:
                    logger.warning(f"[{username}] Could not reconcile on resume: {e}")

            snapshot = load_live_state_snapshot(username, saved_symbol) if saved_symbol else None
            state.update({
                "running": True,
                "status": "active",
                "position": reconciled_position,
                "symbol": saved_symbol,
                "interval": req.interval,
                "trade_amount": req.trade_amount_usdt,
                "current_capital": saved.get("current_capital") or req.trade_amount_usdt,
                "position_qty": reconciled_qty,
                "compounding_mode": req.compounding_mode,
                "signals": [],
                "log": resumed_log,
                "api_key": req.api_key,
                "api_secret": req.api_secret,
                "next_check_ts": None,
                "next_check_str": None,
                "candle_count": 0,
                "analysis_weight": req.analysis_weight,
                "trade_history": saved.get("trade_history", []),
                "live_candles": [],
                "buy_price": reconciled_buy_price or (snapshot or {}).get("buy_price"),
                "sl_pct": (snapshot or saved).get("sl_pct"),
                "tp_pct": (snapshot or saved).get("tp_pct"),
                "_session_token": session_token,
                "_username": username,
                "_is_resume": True,
                "last_regime": None,
                "last_risk": None,
                "last_news_score": None,
                "calibrated_thresholds": saved.get("calibrated_thresholds") or {},
                "mode": "single",
            })
            update_position(username, reconciled_position)
            asyncio.create_task(_live_loop(req, username, api_key, oauth_token, session_token))


# ── Per-user state ────────────────────────────────────────────────────────────

sim_states: dict[str, dict] = {}
live_states: dict[str, dict] = {}


def _default_sim_state() -> dict:
    return {
        "running": False, "iteration": 0,
        "status": "idle", "results": [], "best_result": None, "log": [],
        "symbol": None, "interval": None,
        "candle_prices": [], "candle_timestamps": [],
    }


def _default_live_state() -> dict:
    return {
        "running": False, "status": "idle", "position": "FLAT",
        "symbol": None, "interval": None, "trade_amount": 0, "current_capital": 0,
        "position_qty": 0, "compounding_mode": "compound",
        "signals": [], "log": [], "api_key": None, "api_secret": None,
        "next_check_ts": None, "next_check_str": None, "candle_count": 0,
        "analysis_weight": 70,
        "trade_history": [], "live_candles": [], "buy_price": None,
        "sl_pct": None, "tp_pct": None,
        "last_regime": None, "last_risk": None, "last_news_score": None,
        "calibrated_thresholds": {},
        "_cycle_running": False,
        "mode": "single",
        "portfolio_positions": {},
        "max_per_position": 0.0,
        "cooldowns": {},
        "trading_halted_until_ts": None,
        "loss_streak": 0,
        "sl_price": None,
        "entry_candle_count": None,
    }


def _add_synthetic_buy_if_needed(live_state: dict, username: str, symbol: str,
                                  price: float, qty: float) -> None:
    """Add a synthetic BUY record when startup detects existing crypto holdings.
    Only adds if no unmatched BUY for this symbol already exists in trade_history."""
    hist = live_state.get("trade_history", [])
    # Check symbol-specific: scan history for the most recent BUY/SELL pair for this symbol
    pending_buy = None
    for t in reversed(hist):
        if t.get("symbol") != symbol:
            continue
        if t.get("type") == "BUY":
            pending_buy = t
            break
        if t.get("type") == "SELL":
            break  # found a sell before any buy for this symbol — no pending BUY
    if pending_buy is not None:
        return  # already have an open BUY for this symbol
    hist.append({
        "type": "BUY", "symbol": symbol,
        "price": price, "timestamp": int(time.time() * 1000),
        "order_id": "startup_detected", "pnl_pct": None, "qty": qty,
    })
    live_state["trade_history"] = hist


def _build_capital_series(
    sorted_trades: list,
    trade_amount: float,
    compounding_mode: str,
    position: str,
    buy_price,
    committed: float,
    symbol_candles: list,
    now_ms: int,
    current_capital: float,
    start_ts: int,
) -> tuple[list[tuple[int, float]], dict[int, float]]:
    sell_checkpoints: dict[int, float] = {}
    running_cap = trade_amount
    for t in sorted_trades:
        if t["type"] == "SELL" and t.get("pnl_pct") is not None:
            real_bal = t.get("real_usdc_balance")
            if real_bal is not None:
                # Real Binance balance snapshot — use directly as ground truth
                running_cap = real_bal
            else:
                pnl = t["pnl_pct"]
                if compounding_mode == "fixed":
                    running_cap = trade_amount
                elif compounding_mode == "compound_wins":
                    running_cap = max(round(running_cap * (1 + pnl / 100), 2), trade_amount)
                else:
                    running_cap = round(running_cap * (1 + pnl / 100), 2)
            sell_checkpoints[t["timestamp"]] = running_cap

    has_real_snapshots = any(
        t.get("real_usdc_balance") is not None
        for t in sorted_trades if t.get("type") == "SELL"
    )
    cap_series_raw: list[tuple[int, float]] = (
        [] if (has_real_snapshots or not trade_amount) else [(start_ts, trade_amount)]
    )
    last_realised = trade_amount
    last_sell_ts = start_ts
    for ts_sell, cap_after in sorted(sell_checkpoints.items()):
        cap_series_raw.append((ts_sell, cap_after))
        last_realised = cap_after
        last_sell_ts = ts_sell

    if position == "IN_POSITION" and buy_price and buy_price > 0 and symbol_candles:
        for c in symbol_candles:
            c_ts = c.get("timestamp", 0)
            if c_ts <= last_sell_ts:
                continue
            mtm_cap = round(last_realised - committed + committed * (c["close"] / buy_price), 2)
            cap_series_raw.append((c_ts, mtm_cap))

    cap_series_raw.append((now_ms, current_capital))
    cap_series_raw.sort(key=lambda x: x[0])

    # Deduplicate: keep last value per timestamp (later = more accurate)
    deduped: dict[int, float] = {}
    for ts, cap in cap_series_raw:
        deduped[ts] = cap
    cap_series_raw = sorted(deduped.items())

    return cap_series_raw, sell_checkpoints


def _claude_creds(username: str) -> tuple[Optional[str], str]:
    """Returns (api_key, oauth_token) for the user based on their claude_mode."""
    return get_claude_api_key(username), get_claude_oauth_token(username)


def _claude_configured(username: str) -> bool:
    """True if the user has usable Claude credentials."""
    if uses_platform(username):
        return True
    if uses_subscription(username):
        return bool(get_claude_oauth_token(username))
    return bool(get_claude_api_key(username))


def _get_sim_state(username: str) -> dict:
    if username not in sim_states:
        sim_states[username] = _default_sim_state()
    return sim_states[username]


def _get_live_state(username: str) -> dict:
    if username not in live_states:
        live_states[username] = _default_live_state()
    return live_states[username]


# ── Pydantic models ────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str


class SimRequest(BaseModel):
    symbol: str = "BTCUSDC"
    interval: str = "4h"
    days: int = 30
    initial_capital: float = 1000.0
    fee_tier: str = "standard"
    compounding_mode: str = "compound"   # "fixed" | "compound" | "compound_wins"
    analysis_weight: int = 70            # 0=pure KB, 100=pure market analysis
    min_confidence: int = 55             # minimum confidence % to execute BUY signals
    min_confidence_sell: int = 40        # minimum confidence % to execute SELL signals


class LiveRequest(BaseModel):
    api_key: str = ""
    api_secret: str = ""
    symbol: str = ""          # empty = agent decides on startup
    interval: str = "4h"
    trade_amount_usdt: float = 50.0
    compounding_mode: str = "compound"   # "fixed" | "compound" | "compound_wins"
    analysis_weight: int = 70            # 0=pure KB, 100=pure market analysis
    min_confidence: int = 55             # minimum Claude confidence % to act on BUY
    min_confidence_sell: int = 40        # minimum Claude confidence % to act on SELL
    sl_atr_mult: float = 1.5             # stop-loss = sl_atr_mult × ATR
    tp_atr_mult: float = 2.5             # take-profit = tp_atr_mult × ATR
    mode: str = "single"                 # "single" | "portfolio"
    max_per_position: float = 0.0        # 0 = no per-position cap
    trailing_stop: bool = False
    trailing_activate_pct: float = 1.0
    cooldown_candles: int = 0
    max_consecutive_losses: int = 0
    halt_candles: int = 4
    min_hold_candles: int = 0


class TopupRequest(BaseModel):
    amount: float


# ── Auth routes ────────────────────────────────────────────────────────────────

@app.get("/login")
async def login_page():
    return FileResponse("frontend/login.html")


@app.post("/auth/login")
async def do_login(req: LoginRequest, request: Request, response: Response):
    ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(ip):
        raise HTTPException(status_code=429, detail="Zu viele Loginversuche. Bitte 60 Sekunden warten.")
    user = authenticate(req.username, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="Ungültige Anmeldedaten")
    token = secrets.token_hex(32)
    _SESSIONS[token] = {"username": req.username, "expiry": time.time() + _SESSION_TTL}
    response.set_cookie(
        "session", token,
        httponly=True, samesite="lax", secure=True, max_age=_SESSION_TTL,
    )
    return {"ok": True}


@app.post("/auth/logout")
async def do_logout(request: Request, response: Response):
    token = request.cookies.get("session", "")
    _SESSIONS.pop(token, None)
    response.delete_cookie("session")
    return RedirectResponse("/login", status_code=302)


@app.post("/api/admin/switch-user")
async def admin_switch_user(body: dict, request: Request, response: Response):
    """Admin-only: create a session for another user without needing their password."""
    admin = _require_admin(request)
    target = (body.get("username") or "").strip().lower()
    if not target:
        raise HTTPException(400, "username erforderlich")
    target_user = get_user(target)
    if not target_user or not target_user.get("enabled"):
        raise HTTPException(404, "User nicht gefunden oder deaktiviert")
    # Invalidate old session
    old_token = request.cookies.get("session", "")
    _SESSIONS.pop(old_token, None)
    # Create new session for target user
    new_token = secrets.token_hex(32)
    _SESSIONS[new_token] = {"username": target, "expiry": time.time() + _SESSION_TTL}
    response.set_cookie("session", new_token, httponly=True, samesite="lax", secure=True, max_age=_SESSION_TTL)
    return {"ok": True, "username": target, "switched_from": admin["username"]}


# ── Page routes ────────────────────────────────────────────────────────────────

_NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}

@app.get("/")
async def index(request: Request):
    token = request.cookies.get("session", "")
    if _valid_session(token):
        return FileResponse("frontend/index.html", headers=_NO_CACHE)
    return FileResponse("frontend/landing.html", headers=_NO_CACHE)


@app.get("/admin")
async def admin_page(request: Request):
    _require_admin(request)
    return FileResponse("frontend/admin.html")


@app.get("/settings")
async def settings_page():
    return FileResponse("frontend/settings.html")


@app.get("/documentation")
async def docs_page(request: Request):
    _require_admin(request)
    return FileResponse("frontend/docs.html")


@app.get("/guide")
async def guide_page(request: Request):
    _get_current_user(request)
    return FileResponse("frontend/guide.html")


# ── User profile + settings API ────────────────────────────────────────────────

@app.get("/api/user/profile")
async def get_profile(request: Request):
    user = _get_current_user(request)
    owner = user.get("owner")
    return {
        "username": user["username"],
        "role": user["role"],
        "claude_mode": user.get("claude_mode", "api_key"),
        "has_api_key": bool(user.get("claude_api_key")),
        "has_oauth_token": bool(user.get("claude_oauth_token")),
        "email": user.get("email"),
        "owner": owner,
        "is_subaccount": owner is not None,
    }


@app.get("/api/user/settings")
async def get_user_settings(request: Request):
    user = _get_current_user(request)
    return load_user_settings(user["username"])


@app.post("/api/user/settings")
async def post_user_settings(body: dict, request: Request):
    user = _get_current_user(request)
    save_user_settings(user["username"], body)
    return {"ok": True}


@app.post("/api/user/claude-config")
async def set_claude_config(body: dict, request: Request):
    user = _get_current_user(request)
    mode = body.get("mode", "api_key")
    api_key = body.get("api_key", "").strip() or None
    oauth_token = body.get("oauth_token", "").strip() or None
    if mode == "platform" and user["role"] != "admin":
        raise HTTPException(403, "Platform-Zugang nur durch Admin aktivierbar")
    if mode not in ("platform", "api_key", "subscription"):
        raise HTTPException(400, "Ungültiger Modus")
    update_claude_config(user["username"], mode, api_key=api_key, oauth_token=oauth_token)
    return {"ok": True}


@app.post("/api/user/test-claude")
async def test_claude(request: Request):
    user = _get_current_user(request)
    api_key, oauth_token = _claude_creds(user["username"])
    ok = await test_connection(api_key=api_key, oauth_token=oauth_token)
    return {"ok": ok}


@app.post("/api/user/change-password")
async def change_password(body: dict, request: Request):
    user = _get_current_user(request)
    current = body.get("current_password", "")
    new_pw = body.get("new_password", "")
    if not new_pw or len(new_pw) < 6:
        raise HTTPException(400, "Neues Passwort zu kurz (min. 6 Zeichen)")
    # Re-authenticate to verify current password
    if not authenticate(user["username"], current):
        raise HTTPException(400, "Aktuelles Passwort falsch")
    reset_password(user["username"], new_pw)
    return {"ok": True}


# ── Admin API ─────────────────────────────────────────────────────────────────

@app.get("/api/admin/users")
async def admin_list_users(request: Request):
    _require_admin(request)
    users = list_users()
    safe = []
    for uname, udata in users.items():
        safe.append({
            "username": uname,
            "role": udata.get("role", "user"),
            "enabled": udata.get("enabled", True),
            "created_at": udata.get("created_at", ""),
            "claude_mode": udata.get("claude_mode", "api_key"),
            "has_api_key": bool(udata.get("claude_api_key")),
            "has_oauth_token": bool(udata.get("claude_oauth_token")),
            "owner": udata.get("owner"),
            "email": udata.get("email"),
        })
    return {"users": safe}


@app.post("/api/admin/users")
async def admin_create_user(body: dict, request: Request):
    admin = _require_admin(request)
    username = (body.get("username") or "").strip().lower()
    password = body.get("password", "")
    role = body.get("role", "user")
    claude_mode = body.get("claude_mode", "api_key")
    if not username or not password:
        raise HTTPException(400, "Username und Passwort erforderlich")
    if len(password) < 6:
        raise HTTPException(400, "Passwort zu kurz (min. 6 Zeichen)")
    if role not in ("user", "admin"):
        raise HTTPException(400, "Ungültige Rolle")
    # Sub-account inherits creator's email so they appear in the same switcher group
    creator_data = get_user(admin["username"])
    creator_email = creator_data.get("email") if creator_data else None
    if not create_user(username, password, role=role, claude_mode=claude_mode,
                       owner=admin["username"], email=creator_email):
        raise HTTPException(409, f"User '{username}' existiert bereits")
    return {"ok": True}


@app.delete("/api/admin/users/{username}")
async def admin_delete_user(username: str, request: Request):
    _require_admin(request)
    if username == "admin":
        raise HTTPException(400, "Admin-Account kann nicht gelöscht werden")
    if not delete_user(username):
        raise HTTPException(404, "User nicht gefunden")
    return {"ok": True}


@app.patch("/api/admin/users/{username}")
async def admin_update_user(username: str, body: dict, request: Request):
    _require_admin(request)
    if "enabled" in body:
        if not set_enabled(username, bool(body["enabled"])):
            raise HTTPException(404, "User nicht gefunden")
    if "new_password" in body:
        if len(body["new_password"]) < 6:
            raise HTTPException(400, "Passwort zu kurz")
        if not reset_password(username, body["new_password"]):
            raise HTTPException(404, "User nicht gefunden")
    if "claude_mode" in body:
        mode = body["claude_mode"]
        if mode not in ("platform", "api_key", "subscription"):
            raise HTTPException(400, "Ungültiger Modus")
        api_key = body.get("claude_api_key")
        if not update_claude_config(username, mode, api_key=api_key):
            raise HTTPException(404, "User nicht gefunden")
    if "email" in body:
        ok, conflict = set_email(username, (body["email"] or "").strip().lower())
        if not ok:
            raise HTTPException(409, f"E-Mail wird bereits von '{conflict}' verwendet")
    return {"ok": True}


@app.post("/api/user/email")
async def update_own_email(body: dict, request: Request):
    user = _get_current_user(request)
    if user.get("email"):
        raise HTTPException(409, "E-Mail-Adresse ist bereits gesetzt und kann nicht geändert werden.")
    email = (body.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(400, "E-Mail-Adresse darf nicht leer sein.")
    ok, conflict = set_email(user["username"], email)
    if not ok:
        raise HTTPException(409, "Diese E-Mail-Adresse ist bereits einem anderen Account zugeordnet.")
    return {"ok": True}


# ── Sub-account self-service ───────────────────────────────────────────────────

@app.get("/api/user/subaccounts")
async def list_own_subaccounts(request: Request):
    user = _get_current_user(request)
    all_users = list_users()
    subs = [
        {"username": uname, "enabled": udata.get("enabled", True),
         "created_at": udata.get("created_at", "")}
        for uname, udata in all_users.items()
        if udata.get("owner") == user["username"]
    ]
    return {"subaccounts": subs, "email": user.get("email")}


@app.post("/api/user/subaccounts")
async def create_own_subaccount(body: dict, request: Request):
    user = _get_current_user(request)
    email = user.get("email")
    if not email:
        raise HTTPException(400, "Bitte zuerst eine E-Mail-Adresse in den Einstellungen hinterlegen.")
    username = (body.get("username") or "").strip().lower()
    password = body.get("password", "")
    if not username or not password:
        raise HTTPException(400, "Username und Passwort erforderlich")
    if len(username) < 2:
        raise HTTPException(400, "Username zu kurz (min. 2 Zeichen)")
    if len(password) < 6:
        raise HTTPException(400, "Passwort zu kurz (min. 6 Zeichen)")
    if not create_user(username, password, role="user",
                       owner=user["username"], email=email):
        raise HTTPException(409, f"Username '{username}' ist bereits vergeben")
    return {"ok": True}


@app.delete("/api/user/subaccounts/{username}")
async def delete_own_subaccount(username: str, request: Request):
    user = _get_current_user(request)
    target = get_user(username)
    if not target or target.get("owner") != user["username"]:
        raise HTTPException(403, "Kein Zugriff auf diesen Account")
    if not delete_user(username):
        raise HTTPException(404, "User nicht gefunden")
    return {"ok": True}


# ── Admin knowledge endpoints ─────────────────────────────────────────────────

@app.get("/api/admin/knowledge/status")
async def knowledge_status(request: Request):
    _require_admin(request)
    from .knowledge_store import get_knowledge_status
    return get_knowledge_status()


@app.post("/api/admin/knowledge/promote")
async def knowledge_promote(body: dict, request: Request):
    """
    Promote user patterns to the core knowledge base.
    body: {"type": "rules"}
       OR {"type": "symbol", "username": "...", "symbol": "BTCUSDC", "interval": "4h"}
    """
    admin = _require_admin(request)
    api_key, oauth_token = _claude_creds(admin["username"])
    promote_type = body.get("type", "rules")

    if promote_type == "rules":
        new_rules = await distill_and_promote_rules(api_key=api_key, oauth_token=oauth_token)
        if not new_rules:
            raise HTTPException(400, "Zu wenig Simulationsdaten oder Claude-Fehler")
        return {"ok": True, "rules_count": len(new_rules)}

    elif promote_type == "symbol":
        username = body.get("username", "").strip()
        symbol   = body.get("symbol", "").strip().upper()
        interval = body.get("interval", "").strip()
        if not (username and symbol and interval):
            raise HTTPException(400, "username, symbol und interval erforderlich")
        ok = await promote_symbol_patterns_via_claude(
            username, symbol, interval, api_key=api_key, oauth_token=oauth_token
        )
        if not ok:
            raise HTTPException(400, f"Keine Patterns für {username}/{symbol}/{interval} oder Claude-Fehler")
        return {"ok": True, "promoted": f"{username}/{symbol}/{interval}"}

    else:
        raise HTTPException(400, "type muss 'rules' oder 'symbol' sein")


# ── News Agent endpoints ──────────────────────────────────────────────────────

@app.get("/api/news/intelligence")
async def news_intelligence(request: Request):
    _get_current_user(request)  # auth check
    return get_news_intelligence()


@app.post("/api/news/refresh")
async def news_refresh(request: Request):
    """Manually trigger a News Agent cycle (admin only)."""
    _require_admin(request)
    result = await run_news_cycle()
    if not result:
        raise HTTPException(502, "News Agent cycle fehlgeschlagen")
    return result


# ── Market scanner ─────────────────────────────────────────────────────────────

# Fallback scan list used only when News Agent intelligence is unavailable
_SCAN_FALLBACK_TOP = [
    "BTCUSDC", "ETHUSDC", "BNBUSDC", "SOLUSDC", "XRPUSDC",
    "ADAUSDC", "AVAXUSDC", "DOGEUSDC",
]
_SCAN_FALLBACK_UNDERDOGS = ["NEARUSDC", "INJUSDC"]
# Keep SCAN_SYMBOLS as alias so existing references (e.g. log messages) still work
SCAN_SYMBOLS = _SCAN_FALLBACK_TOP

# Extended pool for second-round scan when primary candidates all fail buy criteria
_SCAN_EXTENDED_POOL = [
    "LINKUSDC", "UNIUSDC", "DOTUSDC", "LTCUSDC", "MATICUSDC",
    "ARBUSDC", "OPUSDC", "ATOMUSDC", "SUIUSDC", "APTUSDC",
    "FTMUSDC", "AAVEUSDC", "LDOUSDC", "TIAUSDC", "SEIUSDC",
    "JUPUSDC", "ONDOUSDC", "EIGENUSDC", "ENAUSDC", "WIFUSDC",
]


def _get_extended_scan_pairs(already_scanned: set, held: set) -> list[str]:
    """Return up to 10 additional pairs for a second-round scan.
    Prefers pairs from News Agent's extended top list, falls back to static pool."""
    intel = get_news_intelligence()
    news_top_all = [
        s.upper() for s in intel.get("recommended_scan_pairs", {}).get("top", [])
        if str(s).upper().endswith("USDC")
    ]
    # Pairs from news agent beyond the first 8 (already in primary scan)
    news_extended = [s for s in news_top_all[8:] if s not in already_scanned and s not in held]
    static_extended = [s for s in _SCAN_EXTENDED_POOL if s not in already_scanned and s not in held]
    combined = news_extended + [s for s in static_extended if s not in news_extended]
    return combined[:10]


def _get_scan_pairs_from_news(held: set) -> tuple[list[str], list[str]]:
    """Return (top_8, underdogs_2) from News Agent recommended_scan_pairs.
    Falls back to static defaults when intelligence is unavailable or stale."""
    intel = get_news_intelligence()
    scan = intel.get("recommended_scan_pairs", {})

    top = [s.upper() for s in scan.get("top", []) if str(s).upper().endswith("USDC")][:8]
    underdogs = [
        s.upper() for s in scan.get("underdogs", [])
        if str(s).upper().endswith("USDC") and s.upper() not in top
    ][:2]

    if not top:
        top = _SCAN_FALLBACK_TOP
    if not underdogs:
        # Fallback: first top_opportunities not in top
        for opp in intel.get("top_opportunities", []):
            sym = str(opp.get("symbol", "")).upper()
            if sym.endswith("USDC") and sym not in top:
                underdogs.append(sym)
                if len(underdogs) >= 2:
                    break
    if not underdogs:
        underdogs = [s for s in _SCAN_FALLBACK_UNDERDOGS if s not in top][:2]

    # Never scan already-held positions as fresh candidates
    top = [s for s in top if s not in held]
    underdogs = [s for s in underdogs if s not in held]
    return top, underdogs

PORTFOLIO_MAX_POSITIONS = 4
PORTFOLIO_MIN_ORDER_USDC = 10.0


def _count_recent_consecutive_losses(trade_history, symbol=None):
    """Count trailing consecutive losing SELLs from most recent, stopping at a win."""
    streak = 0
    for t in reversed(trade_history):
        if t.get("type") not in ("SELL",):
            continue
        if symbol and t.get("symbol") != symbol:
            continue
        pnl = t.get("pnl_pct")
        if pnl is None:
            continue
        if pnl < 0:
            streak += 1
        else:
            break
    return streak


def _is_buy_blocked_by_protections(live_state, symbol, now_ts, req):
    """Check halt and cooldown protections. Returns (blocked: bool, reason: str)."""
    halt_until = live_state.get("trading_halted_until_ts")
    if halt_until and now_ts < halt_until:
        return True, f"Trading-Halt aktiv (Verlustserie) — frei in {int(halt_until - now_ts)}s"
    cd = (live_state.get("cooldowns") or {}).get(symbol)
    if cd and now_ts < cd:
        return True, f"Cooldown {symbol} — frei in {int(cd - now_ts)}s"
    return False, ""


def _register_sell_outcome(live_state, symbol, pnl_pct, was_sl, interval_seconds, req,
                           log_fn=None):
    """Update cooldown and halt state after a sell. log_fn is optional callable(state, msg)."""
    now_ts = time.time()
    if was_sl and req.cooldown_candles > 0:
        live_state.setdefault("cooldowns", {})[symbol] = (
            now_ts + req.cooldown_candles * interval_seconds
        )
    if req.max_consecutive_losses > 0:
        streak = _count_recent_consecutive_losses(live_state.get("trade_history", []))
        live_state["loss_streak"] = streak
        if streak >= req.max_consecutive_losses:
            live_state["trading_halted_until_ts"] = (
                now_ts + req.halt_candles * interval_seconds
            )
            if log_fn is not None:
                log_fn(live_state,
                       f"⛔ Trading-Halt: {streak} Verluste in Folge — "
                       f"pausiere für {req.halt_candles} Kerzen")


def _portfolio_allocation_pct(confidence: int) -> float:
    """Confidence-tier allocation: signal confidence → fraction of free USDC committed."""
    if confidence >= 85: return 0.40
    if confidence >= 70: return 0.30
    if confidence >= 55: return 0.20
    return 0.0  # below min_confidence threshold; should not happen in caller


async def _fetch_scan_summaries(interval: str, symbols: list = None) -> list:
    if symbols is None:
        symbols = SCAN_SYMBOLS
    tasks = [fetch_latest_klines(sym, interval, limit=60) for sym in symbols]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    interval_mins = {"1h": 60, "4h": 240, "1d": 1440}.get(interval, 240)
    c24 = max(1, 1440 // interval_mins)
    c7d = max(1, 10080 // interval_mins)
    summaries = []
    for sym, raw in zip(symbols, results):
        if isinstance(raw, Exception) or not raw:
            continue
        enriched = compute_indicators(raw)
        cur = enriched[-1]
        price = cur["close"]
        def _chg(n, e=enriched, p=price):
            idx = max(0, len(e) - n)
            p0 = e[idx]["close"]
            return (p - p0) / p0 * 100 if p0 else 0
        summaries.append({
            "symbol": sym, "price": price,
            "h24": _chg(c24), "h7d": _chg(c7d),
            "atr_pct": (cur.get("atr") or 0) / price * 100 if price else 0,
            "rsi": cur.get("rsi") or 50,
            "macd": cur.get("macd") or 0,
            "vol_ratio": cur.get("volume_ratio") or 1.0,
        })
    return summaries


@app.post("/api/scan/symbols")
async def scan_symbols(body: dict, request: Request):
    user = _get_current_user(request)
    interval = body.get("interval", "4h")
    extra = [s.strip().upper() for s in body.get("extra_symbols", []) if s.strip()]
    symbols = SCAN_SYMBOLS + [s for s in extra if s not in SCAN_SYMBOLS]
    summaries = await _fetch_scan_summaries(interval, symbols)
    if not summaries:
        raise HTTPException(503, "Keine Marktdaten verfügbar")
    api_key, oauth_token = _claude_creds(user["username"])
    return await scan_market(summaries, interval, username=user["username"],
                             api_key=api_key, oauth_token=oauth_token)


@app.get("/api/symbols")
async def symbols():
    try:
        syms = await get_available_symbols()
        return {"symbols": syms}
    except Exception:
        return {"symbols": ["BTCUSDC", "ETHUSDC", "BNBUSDC", "SOLUSDC", "XRPUSDC"]}


# ── Simulation routes ──────────────────────────────────────────────────────────

@app.post("/api/simulate/start")
async def start_sim(req: SimRequest, background_tasks: BackgroundTasks,
                    request: Request):
    user = _get_current_user(request)
    username = user["username"]
    sim_state = _get_sim_state(username)
    if sim_state["running"]:
        raise HTTPException(409, "Simulation already running")
    if not _claude_configured(username):
        raise HTTPException(400, "Claude nicht konfiguriert. Bitte in Einstellungen API-Key oder OAuth-Token hinterlegen.")
    api_key, oauth_token = _claude_creds(username)

    sim_state.update({
        "running": True, "iteration": 0,
        "status": "starting", "results": [], "best_result": None, "log": [],
        "symbol": req.symbol, "interval": req.interval,
        "candle_prices": [], "candle_timestamps": [],
    })
    fee_pct = FEE_TIERS.get(req.fee_tier, 0.1)
    background_tasks.add_task(_sim_loop, req, fee_pct, username, api_key, oauth_token)
    return {"ok": True}


@app.post("/api/simulate/stop")
async def stop_sim(request: Request):
    user = _get_current_user(request)
    sim_state = _get_sim_state(user["username"])
    sim_state["running"] = False
    sim_state["status"] = "stopped"
    return {"ok": True}


@app.get("/api/simulate/status")
async def sim_status(request: Request):
    user = _get_current_user(request)
    sim_state = _get_sim_state(user["username"])
    return {k: v for k, v in sim_state.items()
            if k not in ("candle_prices", "candle_timestamps")}


@app.get("/api/simulate/chart-data")
async def sim_chart_data(request: Request):
    user = _get_current_user(request)
    sim_state = _get_sim_state(user["username"])
    return {
        "prices": sim_state["candle_prices"],
        "timestamps": sim_state["candle_timestamps"],
        "results": sim_state["results"],
        "best_result": sim_state["best_result"],
    }


@app.get("/api/simulations")
async def get_simulations(request: Request):
    user = _get_current_user(request)
    return {"simulations": _load_sims(user["username"])}


@app.get("/api/simulations/{sim_id}")
async def get_simulation_detail(sim_id: str, request: Request):
    user = _get_current_user(request)
    detail = load_simulation_detail(user["username"], sim_id)
    if detail is None:
        raise HTTPException(404, "Simulation not found")
    return detail


# ── Live trading routes ────────────────────────────────────────────────────────

@app.post("/api/live/start")
async def start_live(req: LiveRequest, background_tasks: BackgroundTasks,
                     request: Request):
    user = _get_current_user(request)
    username = user["username"]
    live_state = _get_live_state(username)
    if live_state["running"]:
        raise HTTPException(409, "Live trading already running")

    if not _claude_configured(username):
        raise HTTPException(400, "Claude nicht konfiguriert. Bitte in Einstellungen API-Key oder OAuth-Token hinterlegen.")
    api_key, oauth_token = _claude_creds(username)

    # Resolve Binance keys independently: form value wins, saved is fallback
    saved_key, saved_sec = get_binance_keys(username)
    bkey = req.api_key.strip() or saved_key
    bsec = req.api_secret.strip() or saved_sec
    if not bkey or not bsec:
        raise HTTPException(400, "Binance API-Key und Secret erforderlich")

    valid = await BinanceTrader(bkey, bsec).validate_keys()
    if not valid:
        raise HTTPException(400, "Ungültige Binance API-Keys")

    save_binance_keys(username, bkey, bsec)

    mode = req.mode if req.mode in ("single", "portfolio") else "single"

    # Rebuild req (sanitize) — keeps existing single-pair behavior identical
    req = LiveRequest(
        api_key=bkey, api_secret=bsec,
        symbol="" if mode == "portfolio" else req.symbol,
        interval=req.interval,
        trade_amount_usdt=req.trade_amount_usdt,
        compounding_mode=req.compounding_mode,
        analysis_weight=req.analysis_weight,
        min_confidence=req.min_confidence,
        min_confidence_sell=req.min_confidence_sell,
        sl_atr_mult=req.sl_atr_mult,
        tp_atr_mult=req.tp_atr_mult,
        mode=mode,
        max_per_position=req.max_per_position,
    )

    # Preserve trade history and accumulated capital across stop/start cycles
    saved_state = load_live_state(username)
    existing_history = (
        live_state.get("trade_history")
        or (saved_state.get("trade_history") if saved_state else None)
        or []
    )
    existing_capital = (
        (live_state.get("current_capital") or (saved_state.get("current_capital") if saved_state else None))
        if existing_history
        else None
    ) or req.trade_amount_usdt
    existing_calibration = (
        live_state.get("calibrated_thresholds")
        or (saved_state.get("calibrated_thresholds") if saved_state else None)
        or {}
    )

    kb_pct = 100 - req.analysis_weight
    weight_note = f"Wissensbasis {kb_pct}% / Markt {req.analysis_weight}%"
    session_token = str(uuid.uuid4())

    if mode == "portfolio":
        start_log = (f"Portfolio Trading gestartet — {req.interval}, max {PORTFOLIO_MAX_POSITIONS} "
                     f"Positionen, max ${req.max_per_position or 0:.0f}/Pos | {weight_note}")
    else:
        start_log = f"Live Trading gestartet — {req.interval}, ${req.trade_amount_usdt} USDC | {weight_note}"

    live_state.update({
        "running": True, "status": "active", "position": "FLAT",
        "symbol": "", "interval": req.interval,
        "trade_amount": req.trade_amount_usdt,
        "current_capital": existing_capital,
        "position_qty": 0,
        "compounding_mode": req.compounding_mode,
        "signals": [],
        "log": [start_log],
        "api_key": bkey, "api_secret": bsec,
        "next_check_ts": None, "next_check_str": None, "candle_count": 0,
        "analysis_weight": req.analysis_weight,
        "min_confidence": req.min_confidence,
        "min_confidence_sell": req.min_confidence_sell,
        "sl_atr_mult": req.sl_atr_mult,
        "tp_atr_mult": req.tp_atr_mult,
        "trade_history": existing_history, "live_candles": [], "buy_price": None,
        "calibrated_thresholds": existing_calibration,
        "mode": mode,
        "portfolio_positions": {},
        "max_per_position": req.max_per_position,
        "_session_token": session_token,
        "_username": username,
        "_cycle_running": False,
    })
    save_live_state(username, {
        "was_running": True,
        "api_key": bkey, "api_secret": bsec,
        "symbol": "", "interval": req.interval,
        "trade_amount": req.trade_amount_usdt, "current_capital": existing_capital,
        "position_qty": 0, "compounding_mode": req.compounding_mode, "position": "FLAT",
        "analysis_weight": req.analysis_weight,
        "min_confidence": req.min_confidence,
        "min_confidence_sell": req.min_confidence_sell,
        "sl_atr_mult": req.sl_atr_mult,
        "tp_atr_mult": req.tp_atr_mult,
        "trade_history": existing_history, "buy_price": None,
        "calibrated_thresholds": existing_calibration,
        "strategy_name": mode,   # repurpose existing column for mode
        "max_per_position": req.max_per_position,
        "last_regime": None, "last_risk": None, "last_news_score": None,
    })

    if mode == "portfolio":
        background_tasks.add_task(_portfolio_loop, req, username, api_key, oauth_token, session_token)
    else:
        background_tasks.add_task(_live_loop, req, username, api_key, oauth_token, session_token)
    return {"ok": True}


@app.post("/api/live/stop")
async def stop_live(request: Request):
    user = _get_current_user(request)
    username = user["username"]
    live_state = _get_live_state(username)
    live_state["running"] = False
    live_state["status"] = "stopped"
    live_state["log"].append("Live Trading gestoppt")
    deactivate_live_state(username)
    return {"ok": True}


@app.post("/api/live/topup")
async def topup_live(req: TopupRequest, request: Request):
    user = _get_current_user(request)
    username = user["username"]
    live_state = _get_live_state(username)

    if not live_state.get("running"):
        raise HTTPException(400, "Live Trading nicht aktiv")
    if req.amount < 1.0:
        raise HTTPException(400, "Mindestbetrag $1")

    old_capital = live_state.get("current_capital") or 0.0
    new_capital = round(old_capital + req.amount, 2)
    live_state["current_capital"] = new_capital
    live_state["trade_amount"] = new_capital  # update base for compounding

    position = live_state.get("position", "FLAT")
    symbol = live_state.get("symbol", "")

    if position == "FLAT":
        _log(live_state, f"💰 Kapital aufgestockt: ${old_capital:.2f} + ${req.amount:.2f} → ${new_capital:.2f} — wird beim nächsten Signal eingesetzt")
    elif position == "IN_POSITION" and symbol:
        _log(live_state, f"💰 Kapital aufgestockt: ${old_capital:.2f} + ${req.amount:.2f} → ${new_capital:.2f} — kaufe {symbol} nach…")
        bkey = live_state.get("api_key", "")
        bsec = live_state.get("api_secret", "")
        try:
            trader = BinanceTrader(bkey, bsec)
            balances = await trader.get_balances()
            usdc_available = balances.get("USDC", 0.0)
            buy_amount = min(req.amount, round(usdc_available * 0.995, 2))
            if buy_amount >= 10.0:
                order = await trader.place_market_order(symbol=symbol, side="BUY", quote_quantity=buy_amount)
                bought_qty = float(order.get("executedQty") or 0)
                buy_price = float(order.get("fills", [{}])[0].get("price") or 0) or float(order.get("cummulativeQuoteQty") or buy_amount) / bought_qty if bought_qty else 0
                if bought_qty > 0:
                    live_state["position_qty"] = (live_state.get("position_qty") or 0) + bought_qty
                    _log(live_state, f"✅ Nachkauf {symbol}: +{bought_qty:.6f} @ ${buy_price:,.4f} mit ${buy_amount:.2f} USDC")
                else:
                    _log(live_state, f"⚠ Nachkauf ergab 0 Menge — USDC {usdc_available:.2f} verfügbar")
            else:
                _log(live_state, f"⚠ Zu wenig USDC für Nachkauf ({usdc_available:.2f} verfügbar, Minimum $10) — Kapital erhöht, kein Kauf")
        except Exception as e:
            _log(live_state, f"⚠ Nachkauf fehlgeschlagen: {e} — Kapital trotzdem erhöht")
    else:
        _log(live_state, f"💰 Kapital aufgestockt: ${old_capital:.2f} + ${req.amount:.2f} → ${new_capital:.2f}")

    _persist_trade_history(username, live_state)
    return {"ok": True, "new_capital": new_capital}


@app.post("/api/live/trigger")
async def trigger_live(request: Request):
    user = _get_current_user(request)
    live_state = _get_live_state(user["username"])
    if not live_state.get("running"):
        raise HTTPException(400, "Live Trading nicht aktiv")
    if live_state.get("_cycle_running"):
        return {"ok": False, "reason": "cycle_running"}
    ev = live_state.get("_trigger_event")
    if ev is None:
        return {"ok": False, "reason": "loop_not_ready"}
    ev.set()
    return {"ok": True}


@app.post("/api/live/reset-history")
async def reset_live_history(request: Request):
    user = _get_current_user(request)
    username = user["username"]
    live_state = _get_live_state(username)
    if not live_state.get("running"):
        raise HTTPException(400, "Live Trading ist nicht aktiv")
    is_in_pos = (
        live_state.get("position") in ("IN_POSITION", "BUYING", "SELLING")
        or (live_state.get("mode") == "portfolio" and live_state.get("portfolio_open_count", 0) > 0)
        or bool(live_state.get("portfolio_positions"))
    )
    if is_in_pos:
        raise HTTPException(400, "Kann Historie nicht zurücksetzen während Positionen offen sind")
    start_capital = float(live_state.get("trade_amount") or 50.0)
    live_state["trade_history"] = []
    live_state["current_capital"] = start_capital
    live_state["portfolio_positions"] = {}
    live_state["portfolio_open_count"] = 0
    _persist_trade_history(username, live_state)
    save_live_state(username, {
        "trade_history": [],
        "current_capital": start_capital,
        "portfolio_positions": {},
    })
    _log(live_state, f"🔄 Historie zurückgesetzt — Startkapital ${start_capital:.2f}")
    return {"ok": True, "start_capital": start_capital}


@app.post("/api/live/reset-position")
async def reset_live_position(request: Request):
    user = _get_current_user(request)
    username = user["username"]
    live_state = _get_live_state(username)
    if not live_state.get("running"):
        raise HTTPException(400, "Live Trading ist nicht aktiv")
    if live_state.get("mode") == "portfolio":
        n = len(live_state.get("portfolio_positions") or {})
        live_state["portfolio_positions"] = {}
        live_state["position"] = "FLAT"
        live_state["position_qty"] = 0
        live_state["buy_price"] = None
        live_state["sl_pct"] = None
        live_state["tp_pct"] = None
        update_position(username, "FLAT")
        _log(live_state, f"🔄 Portfolio: {n} Position(en) intern auf FLAT zurückgesetzt (kein Binance-Order)")
        return {"ok": True, "position": "FLAT", "cleared": n}
    # Single-pair: existing behavior
    old_pos = live_state.get("position", "FLAT")
    live_state["position"] = "FLAT"
    live_state["position_qty"] = 0
    live_state["buy_price"] = None
    live_state["sl_pct"] = None
    live_state["tp_pct"] = None
    update_position(username, "FLAT")
    _log(live_state, f"🔄 Position manuell zurückgesetzt: {old_pos} → FLAT (kein Binance-Order)")
    save_live_state_snapshot(username, live_state.get("symbol", ""), live_state)
    return {"ok": True, "position": "FLAT"}


@app.get("/api/live/credentials")
async def get_live_credentials(request: Request):
    user = _get_current_user(request)
    bkey, bsec = get_binance_keys(user["username"])
    hint = f"{bkey[:4]}...{bkey[-4:]}" if len(bkey) >= 8 else ("✓" if bkey else "")
    return {
        "has_key": bool(bkey),
        "has_secret": bool(bsec),
        "key_hint": hint,
    }


@app.get("/api/live/credentials/reveal")
async def reveal_live_credentials(request: Request):
    user = _get_current_user(request)
    bkey, _ = get_binance_keys(user["username"])
    return {"api_key": bkey or ""}


class BinanceValidateRequest(BaseModel):
    api_key: str = ""
    api_secret: str = ""

@app.post("/api/live/validate-keys")
async def validate_binance_keys(req: BinanceValidateRequest, request: Request):
    user = _get_current_user(request)
    saved_key, saved_sec = get_binance_keys(user["username"])
    bkey = req.api_key.strip() or saved_key
    bsec = req.api_secret.strip() or saved_sec
    if not bkey or not bsec:
        return {"ok": False, "error": "API Key und Secret erforderlich"}
    try:
        trader = BinanceTrader(bkey, bsec)
        account = await trader.get_account()
        balances = {b["asset"]: float(b["free"]) for b in account.get("balances", []) if float(b["free"]) > 0}
        usdc = balances.get("USDC", 0.0)
        return {"ok": True, "usdc_balance": usdc}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/live/status")
async def live_status(request: Request):
    user = _get_current_user(request)
    live_state = _get_live_state(user["username"])
    result = {k: v for k, v in live_state.items()
              if k not in ("api_key", "api_secret", "live_candles")
              and not k.startswith("_")}
    result["calibration_meta"] = calibration_meta(live_state.get("trade_history", []))
    result["cycle_running"] = bool(live_state.get("_cycle_running"))
    # Portfolio aggregates (only meaningful in portfolio mode)
    if live_state.get("mode") == "portfolio":
        positions = live_state.get("portfolio_positions") or {}
        total_value = 0.0
        for slot in positions.values():
            qty   = float(slot.get("position_qty") or 0)
            price = float(slot.get("current_price") or slot.get("buy_price") or 0)
            total_value += qty * price
        result["portfolio_total_value"] = round(total_value, 2)
        result["portfolio_open_count"] = len(positions)
        result["portfolio_max_positions"] = PORTFOLIO_MAX_POSITIONS
        # free_usdc is updated by the loop on each cycle
        result["portfolio_free_usdc"] = float(live_state.get("portfolio_free_usdc") or 0.0)
    return result


@app.get("/api/live/holdings")
async def live_holdings(request: Request):
    user = _get_current_user(request)
    live_state = _get_live_state(user["username"])

    if not live_state.get("running"):
        return {"ok": False, "reason": "not_running"}

    api_key = live_state.get("api_key") or ""
    api_secret = live_state.get("api_secret") or ""
    symbol = live_state.get("symbol") or ""

    if not api_key or not symbol:
        return {"ok": False, "reason": "no_keys"}

    # Parse base/quote from symbol (e.g. BTCUSDC → BTC + USDC)
    quote, base = None, None
    for q in ("USDC", "USDT", "BTC", "ETH", "BNB"):
        if symbol.endswith(q):
            quote = q
            base = symbol[:-len(q)]
            break

    if not base or not quote:
        return {"ok": False, "reason": "unknown_symbol"}

    trader = BinanceTrader(api_key, api_secret)
    try:
        balances, current_price = await asyncio.gather(
            trader.get_balances(),
            trader.get_price(symbol),
            return_exceptions=True,
        )
        if isinstance(balances, Exception):
            raise balances
        if isinstance(current_price, Exception):
            current_price = None

        base_amount = balances.get(base, 0.0)
        quote_amount = balances.get(quote, 0.0)
        base_value = (base_amount * current_price) if (base_amount > 0 and current_price) else 0.0

        return {
            "ok": True,
            "base": base,
            "quote": quote,
            "base_amount": base_amount,
            "quote_amount": quote_amount,
            "current_price": current_price,
            "base_value_in_quote": base_value,
        }
    except Exception as e:
        return {"ok": False, "reason": str(e)}


@app.get("/api/live/performance")
async def live_performance(request: Request):
    user = _get_current_user(request)
    live_state = _get_live_state(user["username"])

    trade_history = live_state.get("trade_history", [])
    _raw_ta = live_state.get("trade_amount")
    trade_amount = float(_raw_ta) if _raw_ta else 50.0
    compounding_mode = live_state.get("compounding_mode", "compound")
    now_ms = int(time.time() * 1000)
    position = live_state.get("position", "FLAT")
    buy_price = live_state.get("buy_price")
    committed = float(live_state.get("current_capital") or trade_amount)
    current_symbol = live_state.get("symbol", "")

    is_portfolio = live_state.get("mode") == "portfolio"

    # Portfolio mode: aggregate mark-to-market across all open positions
    if is_portfolio:
        positions = live_state.get("portfolio_positions") or {}
        agg_value = 0.0
        for slot in positions.values():
            qty   = float(slot.get("position_qty") or 0)
            price = float(slot.get("current_price") or slot.get("buy_price") or 0)
            agg_value += qty * price
        free_usdc_now = float(live_state.get("portfolio_free_usdc") or 0.0)
        current_capital = round(free_usdc_now + agg_value, 2)
        symbol_candles = []
    else:
        # Mark-to-market: fetch recent candles for the held symbol so the chart
        # shows unrealized P&L even right after a restart (live_candles may be empty)
        symbol_candles: list = live_state.get("live_candles", [])
        if position == "IN_POSITION" and current_symbol and len(symbol_candles) < 2:
            try:
                symbol_candles = await fetch_latest_klines(current_symbol,
                    live_state.get("interval", "1h"), limit=100)
            except Exception:
                pass

        # If buy_price is missing (e.g. crash before it was saved), reconstruct from
        # Binance trade history so mark-to-market works correctly
        if position == "IN_POSITION" and not buy_price and current_symbol:
            try:
                bkey = live_state.get("api_key", "")
                bsec = live_state.get("api_secret", "")
                if bkey and bsec:
                    _trader = BinanceTrader(bkey, bsec)
                    my_trades = await _trader.get_my_trades(current_symbol, limit=10)
                    last_buy = next((t for t in reversed(my_trades)
                                     if t.get("isBuyer", False)), None)
                    if last_buy:
                        buy_price = float(last_buy["price"])
                        live_state["buy_price"] = buy_price
                        save_live_state(user["username"], {"buy_price": buy_price})
            except Exception:
                pass

        # current_capital: mark-to-market when IN_POSITION, otherwise stored value
        if position == "IN_POSITION" and buy_price and buy_price > 0 and symbol_candles:
            latest_price = symbol_candles[-1]["close"]
            current_capital = round(committed * (latest_price / buy_price), 2)
        else:
            current_capital = committed

    sorted_trades = sorted(trade_history, key=lambda x: x.get("timestamp", 0))
    start_ts = sorted_trades[0]["timestamp"] if sorted_trades else (now_ms - 86_400_000)

    cap_series_raw, sell_checkpoints = _build_capital_series(
        sorted_trades, trade_amount, compounding_mode,
        position, buy_price, committed, symbol_candles, now_ms, current_capital, start_ts,
    )
    capital_series = [{"ts": ts, "usdc": cap} for ts, cap in cap_series_raw]

    # Use first real point as baseline; fall back to trade_amount when no trades yet
    baseline_capital = capital_series[0]["usdc"] if capital_series else trade_amount
    baseline_ts = capital_series[0]["ts"] if capital_series else start_ts

    # Normalised % series for bot
    bot_pct_series = [
        {"ts": p["ts"], "pct": round((p["usdc"] / baseline_capital - 1) * 100, 2)}
        for p in capital_series
        if baseline_capital
    ]

    # Per-trade P&L bars
    trade_pnl = [
        {"ts": t["timestamp"], "pct": round(t["pnl_pct"], 2), "symbol": t.get("symbol", "")}
        for t in sorted_trades
        if t["type"] == "SELL" and t.get("pnl_pct") is not None
    ]

    # Paired BUY→SELL trade list for trade table display
    # Orphaned SELLs (no prior BUY in history) are shown with buy_price from live_state
    trade_pairs = []
    pending_buy = None
    for t in sorted_trades:
        if t.get("type") == "BUY":
            pending_buy = t
        elif t.get("type") == "SELL":
            sell_ts = t.get("timestamp", 0)
            if pending_buy is not None:
                buy_ts  = pending_buy.get("timestamp", 0)
                dur_h   = round((sell_ts - buy_ts) / 3_600_000, 1) if sell_ts > buy_ts else 0
                bp      = pending_buy.get("price")
                pending_buy = None
            else:
                # Orphaned SELL — use stored buy_price as best estimate
                buy_ts  = start_ts
                dur_h   = 0
                bp      = buy_price  # from live_state, may be None
            trade_pairs.append({
                "symbol":     t.get("symbol", ""),
                "buy_price":  bp,
                "sell_price": t.get("price"),
                "buy_ts":     buy_ts,
                "sell_ts":    sell_ts,
                "duration_h": dur_h,
                "pnl_pct":    t.get("pnl_pct"),
                "net_usdc":   t.get("net_usdc"),
            })
    # If still in position, add open trade
    if pending_buy is not None and position == "IN_POSITION":
        bp = buy_price or pending_buy.get("price")
        cur = symbol_candles[-1]["close"] if symbol_candles else None
        unrealised = round((cur - bp) / bp * 100, 2) if (bp and cur) else None
        open_dur_h  = round((now_ms - pending_buy.get("timestamp", now_ms)) / 3_600_000, 1)
        trade_pairs.append({
            "symbol":     pending_buy.get("symbol", live_state.get("symbol", "")),
            "buy_price":  pending_buy.get("price"),
            "sell_price": None,
            "buy_ts":     pending_buy.get("timestamp", 0),
            "sell_ts":    None,
            "duration_h": open_dur_h,
            "pnl_pct":    unrealised,
            "net_usdc":   None,
            "open":       True,
        })

    # BTC benchmark — interval chosen by duration relative to first real snapshot
    duration_h = (now_ms - baseline_ts) / 3_600_000
    if duration_h <= 48:
        btc_interval, btc_limit = "1h", 500
    elif duration_h <= 336:
        btc_interval, btc_limit = "4h", 500
    else:
        btc_interval, btc_limit = "1d", 500

    btc_pct_series: list = []
    try:
        btc_raw = await fetch_latest_klines("BTCUSDC", btc_interval, limit=btc_limit)
        if btc_raw:
            candles_before = [c for c in btc_raw if c["timestamp"] <= baseline_ts]
            ref_candle = candles_before[-1] if candles_before else btc_raw[0]
            ref_price = ref_candle["close"]
            series_src = [c for c in btc_raw if c["timestamp"] >= ref_candle["timestamp"]]
            btc_pct_series = [
                {"ts": baseline_ts if i == 0 else c["timestamp"],
                 "pct": round((c["close"] / ref_price - 1) * 100, 2)}
                for i, c in enumerate(series_src)
            ]
            if btc_pct_series and btc_pct_series[-1]["ts"] < now_ms:
                btc_pct_series.append({
                    "ts": now_ms,
                    "pct": round((btc_raw[-1]["close"] / ref_price - 1) * 100, 2),
                })
    except Exception as exc:
        logger.warning(f"BTC benchmark fetch failed [{user['username']}]: {exc}")

    bot_total_pct = round((current_capital / baseline_capital - 1) * 100, 2) if baseline_capital else 0
    btc_total_pct = btc_pct_series[-1]["pct"] if btc_pct_series else None

    return {
        "capital_series": capital_series,
        "bot_pct_series": bot_pct_series,
        "btc_pct_series": btc_pct_series,
        "trade_pnl": trade_pnl,
        "trade_pairs": trade_pairs,
        "summary": {
            "start_capital": baseline_capital,
            "current_capital": current_capital,
            "bot_pct": bot_total_pct,
            "btc_pct": btc_total_pct,
            "num_sells": len(trade_pnl),
        },
    }


@app.get("/api/live/chart-data")
async def live_chart_data(request: Request):
    user = _get_current_user(request)
    live_state = _get_live_state(user["username"])
    return {
        "candles": live_state.get("live_candles", []),
        "trade_history": live_state.get("trade_history", []),
    }


@app.get("/api/trades/{username}/{symbol}")
async def get_trade_history(username: str, symbol: str, request: Request,
                            limit: int = 20, offset: int = 0):
    current_user = _get_current_user(request)
    if current_user["username"] != username and current_user.get("role") != "admin":
        raise HTTPException(403, "Nicht berechtigt")
    return load_trade_log(username, symbol.upper(), limit=limit, offset=offset)


# ── Background tasks ───────────────────────────────────────────────────────────

async def _sim_loop(req: SimRequest, fee_pct: float, username: str,
                    api_key: Optional[str], oauth_token: str = ""):
    sim_state = _get_sim_state(username)
    try:
        _log(sim_state, f"Fetching {req.days}d of {req.interval} data for {req.symbol}…")
        sim_state["status"] = "fetching"

        candles = await fetch_klines(req.symbol, req.interval, req.days)
        sim_state["candle_prices"] = [c["close"] for c in candles]
        sim_state["candle_timestamps"] = [c["timestamp"] for c in candles]
        _log(sim_state, f"Fetched {len(candles)} candles")

        sim_state["status"] = "computing_indicators"
        enriched = compute_indicators(candles)
        _log(sim_state, "Technical indicators computed (RSI, MACD, BB, ATR, StochRSI)")

        if not sim_state["running"]:
            return

        sim_state["status"] = "analyzing"
        _log(sim_state, f"Asking Claude to generate signals (KB {100 - req.analysis_weight}% / Market {req.analysis_weight}%)…")

        analysis = await analyze_with_claude(
            symbol=req.symbol, interval=req.interval,
            candles=enriched, username=username,
            analysis_weight=req.analysis_weight,
            api_key=api_key, oauth_token=oauth_token,
        )

        signals = analysis.get("signals", [])
        _log(sim_state, f"Signals: {len(signals)} | Confidence: {analysis.get('confidence', 0)}%")
        _log(sim_state, f"Patterns: {', '.join(analysis.get('patterns_found', []))}")

        # Apply confidence gates (same as live trading)
        before = len(signals)
        signals = [
            s for s in signals
            if not (
                (s.get("action", "").upper() == "BUY"  and s.get("confidence", 0) < req.min_confidence)
                or
                (s.get("action", "").upper() == "SELL" and s.get("confidence", 0) < req.min_confidence_sell)
            )
        ]
        dropped = before - len(signals)
        if dropped:
            _log(sim_state, f"Konfidenz-Filter: {dropped} Signal(e) entfernt (BUY<{req.min_confidence}% / SELL<{req.min_confidence_sell}%)")

        sim_result = run_simulation(
            candles=enriched, signals=signals,
            initial_capital=req.initial_capital, fee_pct=fee_pct,
            compounding_mode=req.compounding_mode,
        )

        ret = sim_result["total_return_pct"]
        _log(sim_state, f"Return: {ret:+.2f}% | Win rate: {sim_result['win_rate']:.1f}% | "
             f"Trades: {sim_result['num_trades']} | Drawdown: {sim_result['max_drawdown']:.1f}% | "
             f"Fees: ${sim_result['total_fees_usdt']:.2f} ({sim_result['fee_drag_pct']:.2f}% drag)")

        if ret > 0:
            _log(sim_state, f"✅ Profitable! Return: +{ret:.2f}%")
            sim_state["status"] = "profitable"
        else:
            sim_state["status"] = "completed"
            _log(sim_state, f"✓ Simulation complete. Return: {ret:+.2f}%")

        result = {
            "analysis": analysis.get("analysis", ""),
            "patterns_found": analysis.get("patterns_found", []),
            "signals": signals, "confidence": analysis.get("confidence", 0),
            **sim_result, "profitable": ret > 0,
        }
        sim_state["results"].append(result)
        sim_state["best_result"] = result

        sim_id = f"sim_{int(time.time())}_{secrets.token_hex(4)}"
        entry = {
            "id": sim_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "symbol": req.symbol, "interval": req.interval,
            "days": req.days, "capital": req.initial_capital,
            "fee_tier": req.fee_tier,
            "total_return_pct": sim_result.get("total_return_pct", 0),
            "win_rate": sim_result.get("win_rate", 0),
            "num_trades": sim_result.get("num_trades", 0),
            "max_drawdown": sim_result.get("max_drawdown", 0),
            "total_fees_usdt": sim_result.get("total_fees_usdt", 0),
            "fee_drag_pct": sim_result.get("fee_drag_pct", 0),
            "analysis_weight": req.analysis_weight,
            "profitable": ret > 0,
            "compounding_mode": req.compounding_mode,
            "compounding_mode_label": sim_result.get("compounding_mode_label", req.compounding_mode),
        }
        full_result = {
            **result, "id": sim_id, "symbol": req.symbol, "interval": req.interval,
            "days": req.days, "capital": req.initial_capital, "fee_tier": req.fee_tier,
            "candle_prices": sim_state.get("candle_prices", []),
            "candle_timestamps": sim_state.get("candle_timestamps", []),
        }
        save_simulation(username, entry, full_result)

        _log(sim_state, "🧠 Aktualisiere Wissensbasis…")
        kb_ok, kb_msg = await synthesize_learnings(
            req.symbol, req.interval, entry,
            username=username,
            api_key=api_key, oauth_token=oauth_token,
        )
        if kb_ok:
            _log(sim_state, f"✅ {kb_msg}")
            async def _run_community():
                comm_ok, comm_msg = await synthesize_community_patterns(
                    req.symbol, req.interval,
                    api_key=api_key, oauth_token=oauth_token,
                )
                if comm_ok:
                    logger.info("Community KB: %s", comm_msg)
                else:
                    logger.debug("Community KB skipped: %s", comm_msg)
            asyncio.create_task(_run_community())
        else:
            _log(sim_state, f"⚠ Wissensbasis-Update fehlgeschlagen: {kb_msg}")

    except Exception as e:
        sim_state["status"] = "error"
        _log(sim_state, f"ERROR: {e}")
    finally:
        sim_state["running"] = False


async def _scan_and_maybe_switch(interval: str, current_symbol: str, position: str,
                                  position_symbol: str, username: str,
                                  api_key: Optional[str], oauth_token: str = "") -> str:
    live_state = _get_live_state(username)
    try:
        summaries = await _fetch_scan_summaries(interval)
        if not summaries:
            return current_symbol or "BTCUSDC"
        result = await scan_market(summaries, interval, api_key=api_key, oauth_token=oauth_token)
        best = result.get("best_symbol", "")
        rec = result.get("recommendation", "")
        if not best:
            return current_symbol or "BTCUSDC"
        if position == "FLAT":
            if best != current_symbol:
                if current_symbol:
                    _log(live_state, f"🔄 Wechsel: {current_symbol} → {best} | {rec[:100]}")
                else:
                    _log(live_state, f"🤖 Agent wählt: {best} | {rec[:100]}")
                live_state["symbol"] = best
                update_position(username, "FLAT")
                return best
            else:
                _log(live_state, f"✓ Scanner bestätigt {current_symbol} als bestes Setup")
        else:
            if best != position_symbol:
                _log(live_state, f"🔄 Besseres Setup gefunden: {best} — verkaufe {position_symbol} zuerst")
                live_state["pending_symbol_switch"] = best
            else:
                _log(live_state, f"✓ Scanner bestätigt {position_symbol} weiterhin stark")
        return current_symbol
    except Exception as e:
        _log(live_state, f"⚠ Scanner-Fehler: {e}")
        return current_symbol


async def _live_loop(req: LiveRequest, username: str, api_key: Optional[str],
                     oauth_token: str = "", session_token: str = ""):
    live_state = _get_live_state(username)
    trigger_event = asyncio.Event()
    live_state["_trigger_event"] = trigger_event

    def _still_active() -> bool:
        return live_state["running"] and live_state.get("_session_token") == session_token
    trader = BinanceTrader(req.api_key, req.api_secret)
    interval_seconds = _interval_to_seconds(req.interval)
    CLOSE_BUFFER = 10

    # symbol comes from saved state (resume) or will be picked by agent (fresh start)
    current_symbol = live_state.get("symbol") or ""
    position_symbol = current_symbol

    def _next_close() -> float:
        now = time.time()
        return (int(now / interval_seconds) + 1) * interval_seconds

    def _fmt_ts(ts: float) -> str:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    def _fmt_wait(secs: float) -> str:
        h, rem = divmod(int(secs), 3600)
        m, s = divmod(rem, 60)
        if h: return f"{h}h {m}m {s}s"
        if m: return f"{m}m {s}s"
        return f"{s}s"

    async def _do_buy(symbol: str, capital: float) -> bool:
        """Place BUY order. Returns True on success, False on failure. Updates live_state."""
        nonlocal position_symbol
        live_state["position"] = "BUYING"
        try:
            balances = await trader.get_balances()
            usdc_available = balances.get("USDC", 0.0)
            actual_capital = min(capital, round(usdc_available * 0.995, 2))
            if actual_capital < 10.0:
                _log(live_state, f"⚠ Zu wenig USDC ({usdc_available:.2f}) — Mindestorder $10 nicht erreicht")
                live_state["position"] = "FLAT"
                return False
            if actual_capital < capital:
                _log(live_state, f"Balance: {usdc_available:.2f} USDC — kaufe mit {actual_capital:.2f} USDC (verfügbar)")
            order = await trader.place_market_order(symbol=symbol, side="BUY", quote_quantity=actual_capital)
            bought_qty = float(order.get("executedQty", 0))
            if bought_qty <= 0:
                _log(live_state, f"⚠ Kauf: executedQty=0 — bleibt FLAT")
                live_state["position"] = "FLAT"
                return False
            cumm_quote = float(order.get("cummulativeQuoteQty", 0))
            buy_price = (cumm_quote / bought_qty) if bought_qty > 0 else (actual_capital / bought_qty)
            position_symbol = symbol
            live_state["position"]        = "IN_POSITION"
            live_state["symbol"]          = symbol
            live_state["buy_price"]       = buy_price
            live_state["position_qty"]    = bought_qty
            live_state["current_capital"] = actual_capital
            live_state["sl_pct"]          = None
            live_state["tp_pct"]          = None
            update_position(username, "IN_POSITION", symbol=symbol)
            live_state["trade_history"].append({
                "type": "BUY", "symbol": symbol,
                "price": buy_price, "timestamp": int(time.time() * 1000),
                "order_id": str(order.get("orderId", "")), "pnl_pct": None,
            })
            _persist_trade_history(username, live_state)
            append_trade_log(username, symbol, live_state["trade_history"][-1])
            save_live_state_snapshot(username, symbol, live_state)
            # Persist buy_price + position to DB so it survives container restarts
            save_live_state(username, {
                "position": "IN_POSITION", "symbol": symbol,
                "buy_price": buy_price, "current_capital": actual_capital,
                "position_qty": bought_qty,
                "trade_history": live_state.get("trade_history", []),
            })
            # Annotate the BUY trade record with the voting context for calibration
            for t in reversed(live_state["trade_history"]):
                if t["type"] == "BUY" and t.get("voting_score") is None:
                    t["voting_score"]  = live_state.get("_last_buy_voting_score")
                    t["voting_regime"] = live_state.get("_last_buy_voting_regime")
                    break
            _log(live_state, f"✅ KAUF {symbol} — {bought_qty:.6f} @ ${buy_price:,.4f} | Eingesetzt: ${actual_capital:.2f}")
            return True
        except Exception as e:
            live_state["position"] = "FLAT"
            _log(live_state, f"❌ KAUF fehlgeschlagen: {e}")
            logger.error(f"KAUF fehlgeschlagen [{username}]: {e}")
            return False

    async def _do_sell(force: bool = False, force_reason: str = "") -> tuple[bool, float]:
        """Place SELL order. Returns (success, net_usdc). Updates live_state."""
        nonlocal position_symbol
        qty = live_state.get("position_qty") or 0
        # Always verify actual balance before selling — position_qty can be stale
        base = position_symbol.replace("USDC", "").replace("USDT", "")
        try:
            actual_qty = await trader.get_asset_balance(base)
        except Exception:
            actual_qty = qty
        if actual_qty <= 0:
            _log(live_state, f"⚠ Kein {base} auf Binance (position_qty={qty:.6f}) — setze auf FLAT")
            live_state["position"] = "FLAT"
            live_state["position_qty"] = 0
            update_position(username, "FLAT")
            return False, 0.0
        if actual_qty < qty:
            _log(live_state, f"⚠ Binance-Balance {actual_qty:.6f} < position_qty {qty:.6f} — verwende echte Balance")
        qty = actual_qty
        if qty <= 0:
            _log(live_state, f"⚠ position_qty=0 — nichts zu verkaufen, setze auf FLAT")
            live_state["position"] = "FLAT"
            live_state["position_qty"] = 0
            update_position(username, "FLAT")
            return False, 0.0
        live_state["position"] = "SELLING"
        try:
            step = await trader.get_lot_step(position_symbol)
            qty = _floor_to_step(qty, step)
            if qty <= 0:
                _log(live_state, f"⚠ Menge nach LOT_SIZE-Rundung = 0 — nichts zu verkaufen")
                live_state["position"] = "IN_POSITION"
                return False, 0.0
            precision = max(0, -int(math.floor(math.log10(step))))
            order = await trader.place_market_order(symbol=position_symbol, side="SELL", quantity=qty, qty_precision=precision)
            gross_usdc = float(order.get("cummulativeQuoteQty", 0))
            usdc_fees  = sum(float(f["commission"]) for f in order.get("fills", []) if f.get("commissionAsset", "").upper() == "USDC")
            net_usdc   = (gross_usdc - usdc_fees) if gross_usdc > 0 else (qty * (candles[-1]["close"] if candles else 0))
            # Actual fill price from order (more accurate than candle close)
            sell_price = (gross_usdc / qty) if qty > 0 and gross_usdc > 0 else (candles[-1]["close"] if candles else 0)
            # Buy price: live_state wins, then last BUY trade record, then candle fallback
            last_buy_rec = next((t for t in reversed(live_state.get("trade_history", [])) if t.get("type") == "BUY"), None)
            buy_p = (live_state.get("buy_price")
                     or (last_buy_rec.get("price") if last_buy_rec else None)
                     or sell_price)
            pnl_pct = (sell_price - buy_p) / buy_p * 100 if buy_p else 0.0
            prev_capital = live_state.get("current_capital") or req.trade_amount_usdt
            mode = live_state.get("compounding_mode", "compound")
            if mode == "fixed":
                live_state["current_capital"] = req.trade_amount_usdt
            elif mode == "compound_wins":
                live_state["current_capital"] = net_usdc if net_usdc >= req.trade_amount_usdt else req.trade_amount_usdt
            else:
                live_state["current_capital"] = net_usdc
            live_state["position"]     = "FLAT"
            live_state["buy_price"]    = None
            live_state["position_qty"] = 0
            live_state["sl_pct"]       = None
            live_state["tp_pct"]       = None
            update_position(username, "FLAT")
            live_state["_sell_fail_count"] = 0
            delta = net_usdc - prev_capital
            reason_str = f" [{force_reason}]" if force_reason else ""
            # Fetch real USDC balance for accurate performance graph snapshots
            real_usdc_balance: Optional[float] = None
            try:
                post_sell_balances = await trader.get_balances()
                real_usdc_balance = round(post_sell_balances.get("USDC", 0.0), 4)
            except Exception:
                pass
            live_state["trade_history"].append({
                "type": "SELL", "symbol": position_symbol,
                "price": round(sell_price, 8), "timestamp": int(time.time() * 1000),
                "order_id": str(order.get("orderId", "")), "pnl_pct": round(pnl_pct, 3),
                "net_usdc": round(net_usdc, 4),
                **({"real_usdc_balance": real_usdc_balance} if real_usdc_balance is not None else {}),
            })
            _log(live_state, f"✅ VERKAUF {position_symbol}{reason_str} @ ${sell_price:,.4f} | P&L: {pnl_pct:+.2f}% | Kapital: ${net_usdc:.2f} ({delta:+.2f}$)")
            _persist_trade_history(username, live_state)
            append_trade_log(username, position_symbol, live_state["trade_history"][-1])
            save_live_state_snapshot(username, position_symbol, live_state)
            save_live_state(username, {
                "position": "FLAT", "symbol": live_state.get("symbol", ""),
                "buy_price": None, "position_qty": 0,
                "current_capital": live_state["current_capital"],
                "trade_history": live_state.get("trade_history", []),
            })
            return True, net_usdc
        except Exception as e:
            live_state["position"] = "IN_POSITION"
            fail_count = live_state.get("_sell_fail_count", 0) + 1
            live_state["_sell_fail_count"] = fail_count
            _log(live_state, f"❌ VERKAUF fehlgeschlagen ({fail_count}x): {e} — bleibt IN_POSITION")
            logger.error(f"VERKAUF fehlgeschlagen [{username}]: {e}")
            return False, 0.0

    try:
        # ── Startup ──────────────────────────────────────────────────────────────
        is_resume = live_state.pop("_is_resume", False)
        _log(live_state, "🔍 Startup: prüfe Guthaben…")
        try:
            balances = await trader.get_balances()
            usdc_available = balances.get("USDC", 0.0)

            # ── Reconcile IN_POSITION against actual holdings (always) ────────
            if current_symbol and live_state.get("position") == "IN_POSITION":
                base_asset = current_symbol.replace("USDC", "").replace("USDT", "")
                crypto_held = balances.get(base_asset, 0.0)
                if crypto_held > 0:
                    live_state["position_qty"] = crypto_held
                    position_symbol = current_symbol
                    _log(live_state, f"✓ {crypto_held:.6f} {base_asset} vorhanden — bleibe IN_POSITION")
                else:
                    _log(live_state, f"⚠ Zustand-Desync: IN_POSITION aber kein {base_asset} — setze auf FLAT")
                    live_state["position"] = "FLAT"
                    live_state["position_qty"] = 0
                    update_position(username, "FLAT")
                    if not is_resume:
                        current_symbol = ""  # trigger fresh scan below on fresh start only

            if is_resume:
                # Resume: do NOT scan or buy — wait for next candle close
                pos = live_state.get("position", "FLAT")
                sym_info = f"{current_symbol} " if current_symbol else ""
                _log(live_state, f"✓ Wiederaufnahme: {sym_info}{pos} — warte auf nächste Kerze")
            else:
                # ── Fresh start: agent picks symbol and buys ──────────────────
                if not current_symbol or live_state.get("position", "FLAT") == "FLAT":
                    _log(live_state, "🤖 Agent sucht bestes Symbol…")
                    chosen = await _scan_and_maybe_switch(
                        req.interval, "", "FLAT", "",
                        username, api_key, oauth_token,
                    )
                    current_symbol = chosen
                    position_symbol = chosen
                    live_state["symbol"] = chosen
                    base_asset = chosen.replace("USDC", "").replace("USDT", "")
                    crypto_held = balances.get(base_asset, 0.0)
                    target = live_state.get("current_capital") or req.trade_amount_usdt
                    if target < 10.0:
                        target = req.trade_amount_usdt

                    try:
                        crypto_price = await trader.get_price(chosen) if crypto_held > 0 else 0.0
                    except Exception:
                        crypto_price = 0.0
                    crypto_value = crypto_held * crypto_price

                    if usdc_available >= target:
                        _log(live_state, f"💰 {usdc_available:.2f} USDC — kaufe {chosen} (${target:.2f})")
                        await _do_buy(chosen, target)

                    elif crypto_held > 0:
                        need_usdc = max(0.0, target - crypto_value)
                        if need_usdc < 10.0:
                            _log(live_state, f"✓ {crypto_held:.6f} {base_asset} (~${crypto_value:.2f}) — setze IN_POSITION")
                            live_state.update({
                                "position": "IN_POSITION", "position_qty": crypto_held,
                                "buy_price": crypto_price or live_state.get("buy_price"),
                                "current_capital": crypto_value, "symbol": chosen,
                            })
                            update_position(username, "IN_POSITION", symbol=chosen)
                            _add_synthetic_buy_if_needed(live_state, username, chosen,
                                                         crypto_price or live_state.get("buy_price") or 0, crypto_held)
                            _persist_trade_history(username, live_state)
                        elif usdc_available >= 10.0:
                            buy_usdc = min(need_usdc, round(usdc_available * 0.995, 2))
                            _log(live_state, f"ℹ {crypto_held:.6f} {base_asset} (~${crypto_value:.2f}) + kaufe ${buy_usdc:.2f} nach")
                            live_state.update({
                                "position": "IN_POSITION", "position_qty": crypto_held,
                                "buy_price": crypto_price or live_state.get("buy_price"),
                                "current_capital": crypto_value, "symbol": chosen,
                            })
                            update_position(username, "IN_POSITION", symbol=chosen)
                            _add_synthetic_buy_if_needed(live_state, username, chosen,
                                                         crypto_price or live_state.get("buy_price") or 0, crypto_held)
                            _persist_trade_history(username, live_state)
                            try:
                                order = await trader.place_market_order(chosen, "BUY", quote_quantity=buy_usdc)
                                extra_qty = float(order.get("executedQty", 0))
                                if extra_qty > 0:
                                    live_state["position_qty"] += extra_qty
                                    live_state["current_capital"] += buy_usdc
                                    _log(live_state, f"✅ Aufgestockt +{extra_qty:.6f} | Gesamt: {live_state['position_qty']:.6f} {base_asset}")
                            except Exception as e:
                                _log(live_state, f"⚠ Aufstocken fehlgeschlagen: {e} — behalte bestehende Position")
                        else:
                            _log(live_state, f"ℹ {crypto_held:.6f} {base_asset} (~${crypto_value:.2f}), kein USDC — setze IN_POSITION")
                            live_state.update({
                                "position": "IN_POSITION", "position_qty": crypto_held,
                                "buy_price": crypto_price or live_state.get("buy_price"),
                                "current_capital": crypto_value, "symbol": chosen,
                            })
                            update_position(username, "IN_POSITION", symbol=chosen)
                            _add_synthetic_buy_if_needed(live_state, username, chosen,
                                                         crypto_price or live_state.get("buy_price") or 0, crypto_held)
                            _persist_trade_history(username, live_state)

                    elif usdc_available >= 10.0:
                        _log(live_state, f"💰 {usdc_available:.2f} USDC (< Ziel ${target:.2f}) — kaufe soviel wie möglich")
                        await _do_buy(chosen, target)

                    else:
                        _log(live_state, f"⚠ Kein {base_asset} und weniger als $10 USDC — warte auf Einzahlung.")

        except Exception as e:
            _log(live_state, f"⚠ Startup-Check fehlgeschlagen: {e}")
            logger.error(f"Startup-Check fehlgeschlagen [{username}]: {e}")

        # Pre-fetch candles immediately so the chart is not empty during the first wait
        first_run = True
        candles: list = []
        _init_sym = live_state.get("symbol") or current_symbol
        if _init_sym:
            try:
                _init_raw = await fetch_latest_klines(_init_sym, req.interval, limit=100)
                if _init_raw:
                    candles = compute_indicators(_init_raw)
                    live_state["live_candles"] = [
                        {"timestamp": c["timestamp"], "close": c["close"]} for c in _init_raw[-80:]
                    ]
            except Exception as _e:
                logger.warning(f"Initial candle pre-fetch failed [{username}]: {_e}")
        while _still_active():

            next_close_ts = _next_close()
            wake_at = next_close_ts + CLOSE_BUFFER
            wait_secs = wake_at - time.time()

            live_state["next_check_ts"] = wake_at
            live_state["next_check_str"] = _fmt_ts(next_close_ts)

            if first_run:
                _log(live_state, f"Live Trading aktiv — {current_symbol} {req.interval}")
                _log(live_state, f"Nächste Analyse: {_fmt_ts(next_close_ts)} (in {_fmt_wait(wait_secs)})")
                first_run = False

            manual_trigger = False
            if wait_secs > 0:
                slept = 0.0
                while slept < wait_secs and _still_active():
                    chunk = min(30.0, wait_secs - slept)
                    try:
                        await asyncio.wait_for(trigger_event.wait(), timeout=chunk)
                        manual_trigger = True
                        trigger_event.clear()
                        break
                    except asyncio.TimeoutError:
                        slept += chunk

            if not _still_active():
                break

            if manual_trigger:
                _log(live_state, "\n🖱 Manueller Trigger — Analyse läuft…")
            else:
                live_state["candle_count"] += 1
                _log(live_state, f"\n── Kerze #{live_state['candle_count']} ({_fmt_ts(next_close_ts)}) ──")

            live_state["_cycle_running"] = True
            _log(live_state, "🔍 Marktcheck…")
            current_symbol = await _scan_and_maybe_switch(
                req.interval, current_symbol, live_state["position"],
                position_symbol, username, api_key, oauth_token,
            )

            # ── Symbol-Switch: direkt oder über USDC ────────────────────────────
            pending_switch = live_state.pop("pending_symbol_switch", None)
            if pending_switch and live_state["position"] == "IN_POSITION":
                from_base   = position_symbol.replace("USDC", "").replace("USDT", "")
                to_base     = pending_switch.replace("USDC", "").replace("USDT", "")
                direct_pair = f"{to_base}{from_base}"
                switched    = False

                # Fetch actual balance — position_qty may be stale
                try:
                    actual_qty = await trader.get_asset_balance(from_base)
                    if actual_qty <= 0:
                        actual_qty = live_state.get("position_qty") or 0
                except Exception:
                    actual_qty = live_state.get("position_qty") or 0

                if actual_qty > 0 and await trader.symbol_exists(direct_pair):
                    try:
                        _log(live_state, f"🔀 Direktwechsel {from_base}→{to_base} via {direct_pair} (halbe Gebühren)…")
                        # direct_pair e.g. XRPBTC: base=XRP, quote=BTC → spend BTC = quoteOrderQty
                        order    = await trader.place_market_order(symbol=direct_pair, side="BUY", quote_quantity=actual_qty)
                        new_qty  = float(order.get("executedQty", 0))
                        if new_qty > 0:
                            new_price = await trader.get_price(pending_switch)
                            live_state["position_qty"]    = new_qty
                            live_state["buy_price"]       = new_price
                            live_state["current_capital"] = new_qty * new_price
                            live_state["sl_pct"]          = None
                            live_state["tp_pct"]          = None
                            position_symbol = pending_switch
                            current_symbol  = pending_switch
                            live_state["symbol"] = pending_switch
                            update_position(username, "IN_POSITION")
                            live_state["trade_history"].append({
                                "type": "SWAP", "symbol": direct_pair,
                                "price": new_price, "timestamp": int(time.time() * 1000),
                                "order_id": str(order.get("orderId", "")), "pnl_pct": None,
                            })
                            _persist_trade_history(username, live_state)
                            append_trade_log(username, direct_pair, live_state["trade_history"][-1])
                            save_live_state_snapshot(username, pending_switch, live_state)
                            _log(live_state, f"✅ SWAP {from_base}→{to_base} — {new_qty:.6f} {to_base} @ ${new_price:,.4f}")
                            switched = True
                    except Exception as e:
                        _log(live_state, f"Direktwechsel fehlgeschlagen ({e}) — Fallback über USDC")

                if not switched and actual_qty > 0:
                    # Two-step: sell → USDC → buy new symbol
                    sell_ok, usdc_net = await _do_sell(force=True, force_reason=f"Wechsel→{to_base}")
                    if sell_ok and usdc_net >= 10.0:
                        bought = await _do_buy(pending_switch, usdc_net)
                        if not bought:
                            _log(live_state, f"⚠ Schritt 2 (Kauf {to_base}) fehlgeschlagen — bleibe FLAT mit {usdc_net:.2f} USDC")
                    elif sell_ok:
                        _log(live_state, f"⚠ Zu wenig USDC nach Verkauf ({usdc_net:.2f}) — kein Kauf möglich")

                _persist_trade_history(username, live_state)
                # Re-fetch data for new symbol and continue to signal generation
                current_symbol = live_state.get("symbol", current_symbol)
                position_symbol = current_symbol if live_state["position"] == "IN_POSITION" else position_symbol

            # ── Fetch candle data ────────────────────────────────────────────────
            active_symbol = position_symbol if live_state["position"] == "IN_POSITION" else current_symbol
            _log(live_state, f"Lade {active_symbol} {req.interval} Daten…")
            candles = await fetch_latest_klines(active_symbol, req.interval, limit=100)
            enriched = compute_indicators(candles)
            price = candles[-1]["close"] if candles else 0
            _log(live_state, f"Schlusskurs: ${price:,.2f}")
            live_state["live_candles"] = [
                {"timestamp": c["timestamp"], "close": c["close"]} for c in candles[-80:]
            ]

            # ── Per-cycle reconciliation: correct state if Binance diverged ───────
            if live_state.get("position") == "IN_POSITION" and active_symbol:
                try:
                    base_asset = active_symbol.replace("USDC", "").replace("USDT", "")
                    cycle_bal = await trader.get_balances()
                    crypto_held = cycle_bal.get(base_asset, 0.0)
                    if crypto_held <= 0:
                        logger.warning(
                            f"[{username}] Cycle desync: IN_POSITION but {base_asset}=0 → auto FLAT"
                        )
                        _log(live_state,
                             f"⚠ Auto-Korrektur: intern IN_POSITION aber kein {base_asset} auf Binance — setze FLAT")
                        live_state["position"] = "FLAT"
                        live_state["position_qty"] = 0
                        live_state["buy_price"] = None
                        live_state["sl_pct"] = None
                        live_state["tp_pct"] = None
                        update_position(username, "FLAT")
                except Exception as _rec_e:
                    logger.warning(f"[{username}] Per-cycle reconcile failed: {_rec_e}")

            # ── Multi-TF: fetch 4h candles for regime ────────────────────────────
            if req.interval != "4h":
                try:
                    raw4h = await fetch_latest_klines(active_symbol, "4h", limit=100)
                    candles_4h = compute_indicators(raw4h)
                except Exception:
                    candles_4h = enriched
            else:
                candles_4h = enriched

            async def _get_1h_candles(sym, interval, enriched_primary):
                if interval == "1h":
                    return enriched_primary
                try:
                    raw = await fetch_latest_klines(sym, "1h", limit=100)
                    return compute_indicators(raw)
                except Exception:
                    return enriched_primary

            # ── Agent 1: Regime ──────────────────────────────────────────────────
            try:
                from .news_fetcher import _fetch_fear_greed
                fng_data = await _fetch_fear_greed()
                candles_1h = await _get_1h_candles(active_symbol, req.interval, enriched)
                regime_result = await get_regime(
                    symbol=active_symbol, interval=req.interval,
                    candles_1h=candles_1h, candles_4h=candles_4h,
                    fear_greed=fng_data, api_key=api_key, oauth_token=oauth_token,
                )
            except Exception as e:
                logger.warning(f"Regime Agent error [{username}]: {e}")
                regime_result = {"regime": "RANGING", "strength": 50,
                                 "recommended_strategy": "mean_revert",
                                 "signal_weight_technical": 70, "signal_weight_news": 30}
            live_state["last_regime"] = regime_result
            _log(live_state, f"Regime: {regime_result['regime']} ({regime_result['strength']}/100) | {regime_result['recommended_strategy']}")

            # ── Agent 3: News Score ──────────────────────────────────────────────
            news_score = get_news_score_for_symbol(active_symbol)
            live_state["last_news_score"] = news_score
            if news_score.get("veto"):
                _log(live_state, f"⚠ News-Veto aktiv für {active_symbol} (Score {news_score['sentiment_score']}/100)")

            # ── Stop-Loss / Take-Profit ──────────────────────────────────────────
            force_sell = False
            force_sell_reason = ""
            if live_state["position"] == "IN_POSITION":
                buy_p  = live_state.get("buy_price") or 0
                sl_pct = live_state.get("sl_pct") or 0
                tp_pct = live_state.get("tp_pct") or 0
                # Protection A: Trailing SL ratchet
                if req.trailing_stop and buy_p and sl_pct and price > buy_p * (1 + req.trailing_activate_pct / 100):
                    new_sl = round(price * (1 - sl_pct / 100), 8)
                    if live_state.get("sl_price") is None or new_sl > live_state["sl_price"]:
                        live_state["sl_price"] = new_sl
                        _log(live_state, f"⤴ Trailing-SL angehoben → ${new_sl:,.4f}")
                sl_trigger = live_state.get("sl_price") or (round(buy_p * (1 - sl_pct / 100), 8) if (buy_p and sl_pct) else 0)
                if buy_p and sl_pct and price <= sl_trigger:
                    force_sell = True
                    force_sell_reason = f"Stop-Loss {sl_pct}% — ${price:,.2f} ≤ ${sl_trigger:,.2f}"
                    _log(live_state, f"🛑 STOP-LOSS: {force_sell_reason}")
                elif buy_p and tp_pct and price >= buy_p * (1 + tp_pct / 100):
                    force_sell = True
                    force_sell_reason = f"Take-Profit {tp_pct}% — ${price:,.2f} ≥ ${buy_p*(1+tp_pct/100):,.2f}"
                    _log(live_state, f"🎯 TAKE-PROFIT: {force_sell_reason}")

            # ── Signal generation ────────────────────────────────────────────────
            if force_sell:
                action, confidence, reason = "SELL", 100, force_sell_reason
            else:
                _log(live_state, "Frage Claude nach Signal…")
                signal = await get_live_signal(
                    symbol=active_symbol, interval=req.interval,
                    candles=enriched, current_position=live_state["position"],
                    username=username,
                    signal_history=live_state["signals"][-10:],
                    trade_history=live_state.get("trade_history", []),
                    analysis_weight=req.analysis_weight,
                    api_key=api_key, oauth_token=oauth_token,
                    regime=regime_result,
                    news_score=news_score,
                )
                action     = signal.get("action", "HOLD")
                confidence = signal.get("confidence", 0)
                reason     = signal.get("reason", "")
                _log(live_state, f"Signal: {action} | Konfidenz: {confidence}% | {reason}")

            raw_action = action
            _d_vote = _d_news_mod = _d_regime_boost = _d_total = 0.0
            _d_overrides: list = []
            _d_green = None

            live_state["signals"].append({
                "action": action, "confidence": confidence, "reason": reason,
                "price": price, "symbol": active_symbol,
                "timestamp": candles[-1]["timestamp"] if candles else 0,
            })
            if len(live_state["signals"]) > 500:
                live_state["signals"] = live_state["signals"][-500:]

            # ── Voting matrix ────────────────────────────────────────────────────
            _BUY_THRESHOLDS = {"BULL_TREND": 0.6, "RANGING": 0.75, "BEAR_TREND": 1.2, "HIGH_VOLATILITY": 999.0}
            buy_threshold = 1.0  # default, overridden below after regime_str is set
            if not force_sell:
                regime_str = regime_result.get("regime", "RANGING")
                calibrated = live_state.get("calibrated_thresholds") or {}
                buy_threshold = calibrated.get(regime_str) or _BUY_THRESHOLDS.get(regime_str, 1.0)
                news_sent  = news_score.get("sentiment_score", 50)
                news_veto  = news_score.get("veto", False)
                news_w     = regime_result.get("signal_weight_news", 30) / 100.0
                vote = 1.0 if action == "BUY" else (-1.0 if action == "SELL" else 0.0)
                news_mod = ((news_sent / 100.0) - 0.5) * 2.0 * news_w   # bipolar: −news_w … +news_w
                regime_boost = {"BULL_TREND": 0.3, "RANGING": 0.0, "BEAR_TREND": -0.3, "HIGH_VOLATILITY": -0.5}.get(regime_str, 0.0)
                total_score = vote + news_mod + regime_boost
                _d_vote, _d_news_mod, _d_regime_boost, _d_total = vote, news_mod, regime_boost, total_score
                _log(live_state, f"Vote: Signal={vote:+.1f} News={news_mod:+.2f} Regime={regime_boost:+.1f} → {total_score:+.2f} (Schwelle {buy_threshold:.1f} für {regime_str})")

                if regime_str == "HIGH_VOLATILITY" and action == "BUY":
                    action = "HOLD"
                    _d_overrides.append("BUY blockiert: HIGH_VOLATILITY-Regime")
                    _log(live_state, "🚫 BUY blockiert: HIGH_VOLATILITY")
                if news_veto and action == "BUY":
                    action = "HOLD"
                    _d_overrides.append("BUY blockiert: News-Veto")
                    _log(live_state, f"🚫 BUY blockiert: News-Veto")
                if action == "BUY" and total_score < buy_threshold:
                    action = "HOLD"
                    _d_overrides.append(f"BUY→HOLD: Voting-Score {total_score:.2f} unter Schwellenwert {buy_threshold:.1f} ({regime_str})")
                    _log(live_state, f"→ HOLD: Score {total_score:.2f} < {buy_threshold:.1f} ({regime_str})")
                if action == "SELL" and live_state["position"] == "IN_POSITION" and total_score > -0.8:
                    if not force_sell:
                        action = "HOLD"
                        _d_overrides.append(f"SELL→HOLD: Score {total_score:.2f} über Schwellenwert −0.8")
                        _log(live_state, f"→ HOLD: SELL Score {total_score:.2f} > -0.8")
            else:
                _d_overrides.append(f"Zwangsverkauf: {force_sell_reason}")

            # ── Mindest-Konfidenz-Filter ──────────────────────────────────────────
            min_conf = live_state.get("min_confidence") or req.min_confidence
            min_conf_sell = live_state.get("min_confidence_sell") or req.min_confidence_sell
            if action == "BUY" and not force_sell and confidence < min_conf:
                action = "HOLD"
                _d_overrides.append(f"BUY→HOLD: Konfidenz {confidence}% unter Mindest-Schwelle {min_conf}%")
                _log(live_state, f"→ HOLD: Konfidenz {confidence}% < {min_conf}%")
            if action == "SELL" and not force_sell and confidence < min_conf_sell:
                action = "HOLD"
                _d_overrides.append(f"SELL→HOLD: Konfidenz {confidence}% unter Mindest-Schwelle {min_conf_sell}%")
                _log(live_state, f"→ HOLD: SELL Konfidenz {confidence}% < {min_conf_sell}%")

            # ── Protections B+C: halt / cooldown gate ────────────────────────────
            if action == "BUY" and live_state["position"] == "FLAT":
                _blocked, _why = _is_buy_blocked_by_protections(
                    live_state, current_symbol, time.time(), req)
                if _blocked:
                    action = "HOLD"
                    _d_overrides.append(f"BUY blockiert: {_why}")
                    _log(live_state, f"🚫 BUY blockiert: {_why}")

            # ── Agent 4: Risk sizing ─────────────────────────────────────────────
            risk_result = None
            # Stash voting context so _do_buy can annotate the trade record
            live_state["_last_buy_voting_score"]  = _d_total
            live_state["_last_buy_voting_regime"] = regime_str if not force_sell else None

            if action == "BUY" and live_state["position"] == "FLAT":
                green = sum([
                    vote > 0,
                    news_sent >= 50,
                    regime_str not in ("BEAR_TREND", "HIGH_VOLATILITY"),
                    total_score >= buy_threshold,
                ])
                _d_green = green
                capital = live_state.get("current_capital") or req.trade_amount_usdt
                sl_mult = live_state.get("sl_atr_mult") or req.sl_atr_mult
                tp_mult = live_state.get("tp_atr_mult") or req.tp_atr_mult
                risk_result = calculate_risk_params(enriched, capital, regime_str, green, sl_mult, tp_mult)
                live_state["last_risk"] = risk_result
                _log(live_state, f"Risk: {risk_result['position_size_pct']}% Kapital | SL {risk_result['stop_loss_pct']:.2f}% | TP {risk_result['take_profit_pct']:.2f}%")
                if risk_result["blocked"]:
                    action = "HOLD"
                    _d_overrides.append(f"BUY blockiert: Risk Agent ({green}/4 Signale grün)")
                    _log(live_state, f"🚫 Risk Agent blockiert ({green}/4 Signale grün)")

            # ── Execute signal ───────────────────────────────────────────────────
            if action == "BUY" and live_state["position"] == "FLAT":
                capital = live_state.get("current_capital") or req.trade_amount_usdt
                if risk_result and not risk_result["blocked"]:
                    sized_capital = round(capital * risk_result["position_size_pct"] / 100.0, 2)
                    # Protection D: confidence-scaled sizing cap
                    sized_capital = min(sized_capital,
                                        round(capital * _portfolio_allocation_pct(confidence), 2))
                else:
                    sized_capital = capital
                if sized_capital >= 10.0:
                    bought = await _do_buy(current_symbol, sized_capital)
                    if bought and risk_result:
                        live_state["sl_pct"] = risk_result["stop_loss_pct"]
                        live_state["tp_pct"] = risk_result["take_profit_pct"]
                        # Seed trailing SL anchor and record entry candle (Protections A, E)
                        live_state["sl_price"] = round(
                            live_state["buy_price"] * (1 - risk_result["stop_loss_pct"] / 100), 8
                        ) if live_state.get("buy_price") else None
                        live_state["entry_candle_count"] = live_state.get("candle_count", 0)
                        _log(live_state, f"SL: {live_state['sl_pct']:.2f}% / TP: {live_state['tp_pct']:.2f}% (ATR-basiert)")
                else:
                    _log(live_state, f"⚠ sized_capital {sized_capital:.2f} < $10 — kein Kauf")

            elif action == "SELL" and live_state["position"] == "IN_POSITION":
                # Protection E: minimum holding period gate (non-forced exits only)
                if not force_sell and req.min_hold_candles > 0:
                    entry_cc = live_state.get("entry_candle_count")
                    held_c = (live_state.get("candle_count", 0) - entry_cc) if entry_cc is not None else req.min_hold_candles
                    if held_c < req.min_hold_candles:
                        action = "HOLD"
                        _d_overrides.append(f"SELL→HOLD: min_hold_candles ({held_c}/{req.min_hold_candles})")
                        _log(live_state, f"⏳ SELL→HOLD: erst {held_c} von {req.min_hold_candles} Kerzen gehalten")
                if action == "SELL":
                    sold, _ = await _do_sell(force=force_sell, force_reason=force_sell_reason)
                    if sold:
                        # Protection A: clear trailing SL anchor on exit
                        live_state["sl_price"] = None
                        live_state["entry_candle_count"] = None
                        # Protection B+C: register outcome for cooldown / halt logic
                        _was_sl = "Stop-Loss" in (force_sell_reason or "")
                        _sell_pnl = (live_state.get("trade_history") or [{}])[-1].get("pnl_pct", 0.0) or 0.0
                        _register_sell_outcome(live_state, position_symbol, _sell_pnl,
                                               _was_sl, interval_seconds, req, log_fn=_log)
                        new_thresholds = calibrate_thresholds(live_state.get("trade_history", []))
                        if new_thresholds:
                            live_state["calibrated_thresholds"] = new_thresholds
                            save_live_state(username, {"calibrated_thresholds": new_thresholds,
                                                       "trade_history": live_state.get("trade_history", [])})
                            _log(live_state, f"📐 Kalibrierung aktualisiert: {new_thresholds}")

            elif action == "HOLD":
                _log(live_state, f"→ HALTEN (Konfidenz: {confidence}%, Position: {live_state['position']})")
            else:
                _log(live_state, f"→ {action} ignoriert (Position: {live_state['position']})")

            live_state["last_decision"] = {
                "ts": candles[-1]["timestamp"] if candles else int(time.time() * 1000),
                "symbol": active_symbol,
                "candle_num": live_state.get("candle_count", 0),
                "price": price,
                "regime": {
                    "type": regime_result.get("regime", "RANGING"),
                    "strength": regime_result.get("strength", 50),
                    "strategy": regime_result.get("recommended_strategy", ""),
                },
                "news": {
                    "score": news_score.get("sentiment_score", 50),
                    "veto": news_score.get("veto", False),
                },
                "raw_action": raw_action,
                "confidence": confidence,
                "reason": reason,
                "voting": {
                    "vote": _d_vote,
                    "news_mod": _d_news_mod,
                    "regime_boost": _d_regime_boost,
                    "total_score": _d_total,
                },
                "overrides": _d_overrides,
                "final_action": action,
                "risk": {
                    "position_size_pct": risk_result["position_size_pct"],
                    "stop_loss_pct": risk_result["stop_loss_pct"],
                    "take_profit_pct": risk_result["take_profit_pct"],
                    "blocked": risk_result["blocked"],
                    "green_signals": _d_green,
                } if risk_result else None,
                "force_sell": force_sell,
            }

            next2 = _next_close()
            live_state["next_check_ts"] = next2 + CLOSE_BUFFER
            live_state["next_check_str"] = _fmt_ts(next2)
            _log(live_state, f"Nächste Analyse: {_fmt_ts(next2)} (in {_fmt_wait(next2 - time.time())})")
            live_state["_cycle_running"] = False

    except Exception as e:
        live_state["status"] = "error"
        _log(live_state, f"FEHLER: {e}")
        clear_live_state(username)
    finally:
        live_state["running"] = False
        live_state["next_check_ts"] = None
        live_state["next_check_str"] = None
        live_state["status"] = "stopped"
        live_state["_cycle_running"] = False
        live_state["_trigger_event"] = None


async def _portfolio_loop(req: LiveRequest, username: str, api_key: Optional[str],
                          oauth_token: str = "", session_token: str = ""):
    live_state = _get_live_state(username)
    trigger_event = asyncio.Event()
    live_state["_trigger_event"] = trigger_event

    def _still_active() -> bool:
        return live_state["running"] and live_state.get("_session_token") == session_token

    trader = BinanceTrader(req.api_key, req.api_secret)
    interval_seconds = _interval_to_seconds(req.interval)
    CLOSE_BUFFER = 10
    from .news_fetcher import _fetch_fear_greed  # local import shared by both phases

    def _next_close() -> float:
        now = time.time()
        return (int(now / interval_seconds) + 1) * interval_seconds

    def _fmt_ts(ts: float) -> str:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    def _fmt_wait(secs: float) -> str:
        h, rem = divmod(int(secs), 3600); m, s = divmod(rem, 60)
        if h: return f"{h}h {m}m {s}s"
        if m: return f"{m}m {s}s"
        return f"{s}s"

    # ── inline helpers (mirror _live_loop's _do_buy/_do_sell, operate on a slot) ──
    async def _portfolio_buy(symbol: str, capital: float, sl_pct: float, tp_pct: float,
                             buy_price_for_levels: float) -> bool:
        try:
            balances = await trader.get_balances()
            usdc_avail = balances.get("USDC", 0.0)
            spend = min(capital, round(usdc_avail * 0.995, 2))
            if spend < PORTFOLIO_MIN_ORDER_USDC:
                _log(live_state, f"⚠ {symbol}: zu wenig USDC ({usdc_avail:.2f}) — übersprungen")
                return False
            order = await trader.place_market_order(symbol=symbol, side="BUY", quote_quantity=spend)
            qty = float(order.get("executedQty", 0))
            if qty <= 0:
                _log(live_state, f"⚠ {symbol}: executedQty=0 — übersprungen")
                return False
            cumm = float(order.get("cummulativeQuoteQty", 0))
            buy_price = (cumm / qty) if qty > 0 else buy_price_for_levels
            entry_ts = int(time.time() * 1000)
            sl_price = round(buy_price * (1 - sl_pct / 100), 8) if sl_pct else None
            tp_price = round(buy_price * (1 + tp_pct / 100), 8) if tp_pct else None
            live_state["portfolio_positions"][symbol] = {
                "symbol": symbol,
                "position_qty": qty, "buy_price": buy_price, "current_price": buy_price,
                "entry_ts": entry_ts,
                "sl_pct": sl_pct, "tp_pct": tp_pct,
                "sl_price": sl_price, "tp_price": tp_price,
                "allocated_usdc": spend,
                "order_id": str(order.get("orderId", "")),
                "last_signal": "BUY", "last_confidence": 0,
                "entry_candle_count": live_state.get("candle_count", 0),
            }
            live_state["trade_history"].append({
                "type": "BUY", "symbol": symbol,
                "price": buy_price, "timestamp": entry_ts,
                "order_id": str(order.get("orderId", "")), "pnl_pct": None,
            })
            _persist_trade_history(username, live_state)
            append_trade_log(username, symbol, live_state["trade_history"][-1])
            _log(live_state, f"✅ KAUF {symbol} — {qty:.6f} @ ${buy_price:,.4f} | ${spend:.2f} USDC | SL {sl_pct:.2f}% / TP {tp_pct:.2f}%")
            return True
        except Exception as e:
            _log(live_state, f"❌ {symbol} KAUF fehlgeschlagen: {e}")
            logger.error(f"Portfolio KAUF [{username}/{symbol}]: {e}")
            return False

    async def _portfolio_sell(symbol: str, force_reason: str = "") -> tuple[bool, float]:
        slot = live_state["portfolio_positions"].get(symbol)
        if not slot:
            return False, 0.0
        qty = float(slot.get("position_qty") or 0)
        if qty <= 0:
            base = symbol.replace("USDC", "").replace("USDT", "")
            try: qty = await trader.get_asset_balance(base)
            except Exception: pass
        if qty <= 0:
            live_state["portfolio_positions"].pop(symbol, None)
            return False, 0.0
        try:
            step = await trader.get_lot_step(symbol)
            qty = _floor_to_step(qty, step)
            if qty <= 0:
                _log(live_state, f"⚠ {symbol}: Menge nach LOT_SIZE = 0")
                return False, 0.0
            precision = max(0, -int(math.floor(math.log10(step))))
            order = await trader.place_market_order(symbol=symbol, side="SELL",
                                                     quantity=qty, qty_precision=precision)
            gross = float(order.get("cummulativeQuoteQty", 0))
            fees  = sum(float(f["commission"]) for f in order.get("fills", [])
                        if f.get("commissionAsset", "").upper() == "USDC")
            net   = (gross - fees) if gross > 0 else 0.0
            sell_price = (gross / qty) if qty > 0 and gross > 0 else slot.get("current_price") or 0.0
            buy_p = float(slot.get("buy_price") or sell_price or 1.0)
            pnl_pct = (sell_price - buy_p) / buy_p * 100 if (buy_p and sell_price > 0) else None
            reason_str = f" [{force_reason}]" if force_reason else ""
            live_state["portfolio_positions"].pop(symbol, None)
            # Snapshot real portfolio value: actual USDC balance + remaining positions
            real_portfolio_value: Optional[float] = None
            try:
                post_balances = await trader.get_balances()
                free_usdc = post_balances.get("USDC", 0.0)
                positions_value = sum(
                    float(s.get("position_qty", 0))
                    * float(s.get("current_price") or s.get("buy_price") or 0)
                    for s in live_state["portfolio_positions"].values()
                )
                real_portfolio_value = round(free_usdc + positions_value, 4)
            except Exception:
                pass
            live_state["trade_history"].append({
                "type": "SELL", "symbol": symbol,
                "price": round(sell_price, 8), "timestamp": int(time.time() * 1000),
                "order_id": str(order.get("orderId", "")),
                "pnl_pct": round(pnl_pct, 3), "net_usdc": round(net, 4),
                **({"real_usdc_balance": real_portfolio_value} if real_portfolio_value is not None else {}),
            })
            _persist_trade_history(username, live_state)
            append_trade_log(username, symbol, live_state["trade_history"][-1])
            pnl_str = f"{pnl_pct:+.2f}%" if pnl_pct is not None else "?"
            _log(live_state, f"✅ VERKAUF {symbol}{reason_str} @ ${sell_price:,.4f} | P&L: {pnl_str} | +${net:.2f}")
            return True, net
        except Exception as e:
            _log(live_state, f"❌ {symbol} VERKAUF fehlgeschlagen: {e}")
            logger.error(f"Portfolio VERKAUF [{username}/{symbol}]: {e}")
            return False, 0.0

    async def _portfolio_partial_sell(symbol: str, fraction: float,
                                       force_reason: str = "") -> tuple[bool, float]:
        fraction = max(0.05, min(0.95, float(fraction)))
        slot = live_state["portfolio_positions"].get(symbol)
        if not slot:
            return False, 0.0
        total_qty = float(slot.get("position_qty") or 0)
        if total_qty <= 0:
            return False, 0.0
        sell_qty = total_qty * fraction
        try:
            step = await trader.get_lot_step(symbol)
            sell_qty = _floor_to_step(sell_qty, step)
            if sell_qty <= 0:
                _log(live_state, f"⚠ {symbol}: Teilmenge nach LOT_SIZE = 0")
                return False, 0.0
            precision = max(0, -int(math.floor(math.log10(step))))
            order = await trader.place_market_order(symbol=symbol, side="SELL",
                                                     quantity=sell_qty, qty_precision=precision)
            gross = float(order.get("cummulativeQuoteQty", 0))
            fees  = sum(float(f["commission"]) for f in order.get("fills", [])
                        if f.get("commissionAsset", "").upper() == "USDC")
            net   = (gross - fees) if gross > 0 else 0.0
            sell_price = (gross / sell_qty) if sell_qty > 0 and gross > 0 else slot.get("current_price") or 0.0
            buy_p = float(slot.get("buy_price") or sell_price or 1.0)
            pnl_pct = (sell_price - buy_p) / buy_p * 100 if (buy_p and sell_price > 0) else None
            reason_str = f" [{force_reason}]" if force_reason else ""

            # Update slot in place (do NOT pop)
            remaining_qty = max(0.0, total_qty - sell_qty)
            slot["position_qty"] = remaining_qty
            slot["allocated_usdc"] = max(0.0, float(slot.get("allocated_usdc") or 0) - gross)

            live_state["trade_history"].append({
                "type": "PARTIAL_SELL", "symbol": symbol,
                "price": sell_price, "timestamp": int(time.time() * 1000),
                "order_id": str(order.get("orderId", "")),
                "pnl_pct": pnl_pct, "net_usdc": net,
                "fraction": round(fraction, 4), "qty_sold": sell_qty,
            })
            _persist_trade_history(username, live_state)
            append_trade_log(username, symbol, live_state["trade_history"][-1])
            pnl_str_p = f"{pnl_pct:+.2f}%" if pnl_pct is not None else "?"
            _log(live_state, f"✂ TEILVERKAUF{reason_str} {symbol} ({fraction*100:.0f}%) — "
                 f"{sell_qty:.6f} @ ${sell_price:,.4f} | P&L: {pnl_str_p} | +${net:.2f}")

            # Pop slot if remainder is below minimum order size (dust)
            remaining_value = remaining_qty * sell_price
            if remaining_value < PORTFOLIO_MIN_ORDER_USDC:
                live_state["portfolio_positions"].pop(symbol, None)
                _log(live_state, f"↩ {symbol}: Rest ({remaining_value:.2f}) unter Minimum — Position geschlossen")

            return True, net
        except Exception as e:
            _log(live_state, f"❌ {symbol} TEILVERKAUF fehlgeschlagen: {e}")
            logger.error(f"Portfolio TEILVERKAUF [{username}/{symbol}]: {e}")
            return False, 0.0

    try:
        # ── Startup: reconcile portfolio from Binance balances ─────────────
        is_resume = live_state.pop("_is_resume", False)
        _log(live_state, f"🔍 Portfolio-Modus Startup ({'Resume' if is_resume else 'Frisch'})…")
        try:
            balances = await trader.get_balances()
            detected = 0
            for asset, amt in balances.items():
                if asset == "USDC" or amt <= 0:
                    continue
                if len(live_state["portfolio_positions"]) >= PORTFOLIO_MAX_POSITIONS:
                    break
                sym = f"{asset}USDC"
                try:
                    sym_exists = await trader.symbol_exists(sym)
                except Exception:
                    sym_exists = False
                if not sym_exists:
                    continue
                try: cur_price = await trader.get_price(sym)
                except Exception: cur_price = 0.0
                value = amt * cur_price
                if value < PORTFOLIO_MIN_ORDER_USDC:
                    continue
                if sym in live_state["portfolio_positions"]:
                    continue
                live_state["portfolio_positions"][sym] = {
                    "symbol": sym, "position_qty": amt,
                    "buy_price": cur_price, "current_price": cur_price,
                    "entry_ts": int(time.time() * 1000),
                    "sl_pct": None, "tp_pct": None,
                    "sl_price": None, "tp_price": None,
                    "allocated_usdc": value,
                    "order_id": "startup_detected",
                    "last_signal": "", "last_confidence": 0,
                }
                _add_synthetic_buy_if_needed(live_state, username, sym, cur_price, amt)
                detected += 1
                _log(live_state, f"✓ Erkannt: {amt:.6f} {asset} (~${value:.2f}) — als Position übernommen")
            _persist_trade_history(username, live_state)
            free_usdc_start = balances.get("USDC", 0.0)
            live_state["portfolio_free_usdc"] = free_usdc_start
            # Compute real starting capital = free USDC + all position values
            pos_value = sum(
                float(s.get("position_qty", 0)) * float(s.get("current_price") or s.get("buy_price") or 0)
                for s in live_state["portfolio_positions"].values()
            )
            real_start = round(free_usdc_start + pos_value, 2)
            if real_start > 0:
                live_state["trade_amount"] = real_start
            _log(live_state, f"💰 Startkapital: ${real_start:.2f} USDC (frei: ${free_usdc_start:.2f}, Positionen: ${pos_value:.2f})")
            if detected == 0:
                _log(live_state, "Keine bestehenden Positionen erkannt — warte auf Signale")
        except Exception as e:
            _log(live_state, f"⚠ Portfolio-Startup-Check fehlgeschlagen: {e}")

        first_run = True
        while _still_active():
            next_close_ts = _next_close()
            wake_at = next_close_ts + CLOSE_BUFFER
            wait_secs = wake_at - time.time()
            live_state["next_check_ts"] = wake_at
            live_state["next_check_str"] = _fmt_ts(next_close_ts)

            if first_run:
                _log(live_state, f"Portfolio-Loop aktiv — {req.interval}, {len(live_state['portfolio_positions'])}/{PORTFOLIO_MAX_POSITIONS} Positionen")
                _log(live_state, f"Nächste Analyse: {_fmt_ts(next_close_ts)} (in {_fmt_wait(wait_secs)})")
                first_run = False

            manual_trigger = False
            if wait_secs > 0:
                slept = 0.0
                while slept < wait_secs and _still_active():
                    chunk = min(30.0, wait_secs - slept)
                    try:
                        await asyncio.wait_for(trigger_event.wait(), timeout=chunk)
                        manual_trigger = True
                        trigger_event.clear()
                        break
                    except asyncio.TimeoutError:
                        slept += chunk
            if not _still_active():
                break

            if manual_trigger:
                _log(live_state, "\n🖱 Manueller Trigger — Portfolio-Analyse läuft…")
            else:
                live_state["candle_count"] += 1
                _log(live_state, f"\n── Portfolio-Zyklus #{live_state['candle_count']} ({_fmt_ts(next_close_ts)}) ──")

            live_state["_cycle_running"] = True

            # ── Per-cycle reconciliation: sync portfolio_positions with Binance ─
            try:
                balances = await trader.get_balances()
                live_state["portfolio_free_usdc"] = balances.get("USDC", 0.0)

                # Remove positions where the asset is no longer held on Binance
                for sym in list(live_state["portfolio_positions"].keys()):
                    base = sym.replace("USDC", "").replace("USDT", "")
                    actual_qty = float(balances.get(base, 0.0))
                    expected_qty = float(live_state["portfolio_positions"][sym].get("position_qty") or 0)
                    if actual_qty < max(expected_qty * 0.1, 0.0001):
                        _log(live_state, f"↩ {sym}: nicht mehr auf Account (erwartet {expected_qty:.6f}, "
                             f"vorhanden {actual_qty:.6f}) — aus Portfolio entfernt")
                        live_state["portfolio_positions"].pop(sym, None)
                    else:
                        live_state["portfolio_positions"][sym]["position_qty"] = actual_qty

                # Detect newly-held assets not yet in portfolio_positions
                for asset, amt in balances.items():
                    if asset in ("USDC", "USDT") or float(amt) <= 0:
                        continue
                    sym = f"{asset}USDC"
                    if sym in live_state["portfolio_positions"]:
                        continue
                    if len(live_state["portfolio_positions"]) >= PORTFOLIO_MAX_POSITIONS:
                        continue
                    try:
                        sym_exists = await trader.symbol_exists(sym)
                    except Exception:
                        sym_exists = False
                    if not sym_exists:
                        continue
                    try:
                        cur_price = await trader.get_price(sym)
                    except Exception:
                        cur_price = 0.0
                    value = float(amt) * cur_price
                    if value < PORTFOLIO_MIN_ORDER_USDC:
                        continue
                    live_state["portfolio_positions"][sym] = {
                        "symbol": sym, "position_qty": float(amt),
                        "buy_price": cur_price, "current_price": cur_price,
                        "entry_ts": int(time.time() * 1000),
                        "sl_pct": None, "tp_pct": None,
                        "sl_price": None, "tp_price": None,
                        "allocated_usdc": value,
                        "order_id": "cycle_detected",
                        "last_signal": "", "last_confidence": 0,
                    }
                    _add_synthetic_buy_if_needed(live_state, username, sym, cur_price, float(amt))
                    _persist_trade_history(username, live_state)
                    _log(live_state, f"↺ {sym}: neu erkannt ({float(amt):.6f} ~${value:.2f}) — in Portfolio aufgenommen")
            except Exception as e:
                _log(live_state, f"⚠ Portfolio-Abgleich fehlgeschlagen: {e}")
                balances = {}

            # ── Phase 1: review existing positions ────────────────────────
            for sym in list(live_state["portfolio_positions"].keys()):
                slot = live_state["portfolio_positions"].get(sym)
                if not slot:
                    continue
                try:
                    raw = await fetch_latest_klines(sym, req.interval, limit=100)
                    enriched = compute_indicators(raw)
                    price = enriched[-1]["close"] if enriched else 0.0
                    slot["current_price"] = price
                except Exception as e:
                    _log(live_state, f"⚠ {sym}: Daten-Fetch fehlgeschlagen ({e})")
                    continue

                # Calculate SL/TP from candles if not set (startup/cycle-detected positions)
                if enriched and slot.get("sl_pct") is None:
                    try:
                        risk_default = calculate_risk_params(
                            enriched, float(slot.get("allocated_usdc") or 50),
                            "RANGING",
                            signals_count_green=3,
                            sl_atr_mult=(live_state.get("sl_atr_mult") or req.sl_atr_mult),
                            tp_atr_mult=(live_state.get("tp_atr_mult") or req.tp_atr_mult),
                        )
                        if not risk_default.get("blocked"):
                            bp = float(slot.get("buy_price") or price)
                            slot["sl_pct"] = risk_default["stop_loss_pct"]
                            slot["tp_pct"] = risk_default["take_profit_pct"]
                            slot["sl_price"] = round(bp * (1 - risk_default["stop_loss_pct"] / 100), 8)
                            slot["tp_price"] = round(bp * (1 + risk_default["take_profit_pct"] / 100), 8)
                            _log(live_state, f"ℹ {sym}: SL/TP berechnet — SL {slot['sl_pct']:.2f}% / TP {slot['tp_pct']:.2f}%")
                    except Exception:
                        pass

                # SL/TP check
                buy_p = float(slot.get("buy_price") or 0)
                sl_pct = float(slot.get("sl_pct") or 0)
                tp_pct = float(slot.get("tp_pct") or 0)
                force_sell = False
                force_reason = ""
                # Protection A: Trailing SL ratchet (portfolio)
                if req.trailing_stop and buy_p and sl_pct and price > buy_p * (1 + req.trailing_activate_pct / 100):
                    new_sl_p = round(price * (1 - sl_pct / 100), 8)
                    if slot.get("sl_price") is None or new_sl_p > slot["sl_price"]:
                        slot["sl_price"] = new_sl_p
                        _log(live_state, f"⤴ {sym}: Trailing-SL angehoben → ${new_sl_p:,.4f}")
                sl_trigger_p = slot.get("sl_price") or (round(buy_p * (1 - sl_pct / 100), 8) if (buy_p and sl_pct) else 0)
                if buy_p and sl_pct and price <= sl_trigger_p:
                    force_sell = True
                    force_reason = f"SL {sl_pct:.2f}% — ${price:,.4f} ≤ ${sl_trigger_p:,.4f}"
                    _log(live_state, f"🛑 {sym}: STOP-LOSS — {force_reason}")
                elif buy_p and tp_pct and price >= buy_p * (1 + tp_pct / 100):
                    force_sell = True
                    force_reason = f"TP {tp_pct:.2f}% — ${price:,.4f} ≥ ${buy_p*(1+tp_pct/100):,.4f}"
                    _log(live_state, f"🎯 {sym}: TAKE-PROFIT — {force_reason}")

                if force_sell:
                    sold, _net = await _portfolio_sell(sym, force_reason=force_reason)
                    if sold:
                        # Protection B+C: register outcome for cooldown / halt
                        _was_sl_p = "SL" in force_reason
                        _pnl_p = (live_state.get("trade_history") or [{}])[-1].get("pnl_pct", 0.0) or 0.0
                        _register_sell_outcome(live_state, sym, _pnl_p,
                                               _was_sl_p, interval_seconds, req, log_fn=_log)
                        new_thresh = calibrate_thresholds(live_state.get("trade_history", []))
                        if new_thresh:
                            live_state["calibrated_thresholds"] = new_thresh
                    continue

                # Signal agent for hold-or-sell decision
                try:
                    candles_4h = enriched if req.interval == "4h" else compute_indicators(
                        await fetch_latest_klines(sym, "4h", limit=100))
                    candles_1h = enriched if req.interval == "1h" else compute_indicators(
                        await fetch_latest_klines(sym, "1h", limit=100))
                    fng = await _fetch_fear_greed()
                    regime = await get_regime(symbol=sym, interval=req.interval,
                                              candles_1h=candles_1h, candles_4h=candles_4h,
                                              fear_greed=fng, api_key=api_key, oauth_token=oauth_token)
                except Exception:
                    regime = {"regime": "RANGING", "strength": 50,
                              "recommended_strategy": "mean_revert",
                              "signal_weight_technical": 70, "signal_weight_news": 30}
                news_score = get_news_score_for_symbol(sym)
                sym_history = [t for t in live_state.get("trade_history", [])
                               if t.get("symbol") == sym or not t.get("symbol")]
                signal = await get_live_signal(
                    symbol=sym, interval=req.interval, candles=enriched,
                    current_position="IN_POSITION", username=username,
                    signal_history=[], trade_history=sym_history,
                    analysis_weight=req.analysis_weight,
                    api_key=api_key, oauth_token=oauth_token,
                    regime=regime, news_score=news_score,
                    min_confidence=live_state.get("min_confidence") or req.min_confidence,
                )
                action     = signal.get("action", "HOLD")
                confidence = signal.get("confidence", 0)
                slot["last_signal"] = action
                slot["last_confidence"] = confidence
                _log(live_state, f"{sym}: {action} ({confidence}%) — {signal.get('reason','')[:80]}")
                min_sell = live_state.get("min_confidence_sell") or req.min_confidence_sell
                if action == "SELL" and confidence >= min_sell:
                    # Protection E: minimum holding period gate (signal-driven, non-forced)
                    _entry_cc = slot.get("entry_candle_count")
                    _held_c = (live_state.get("candle_count", 0) - _entry_cc) if _entry_cc is not None else req.min_hold_candles
                    if req.min_hold_candles > 0 and _held_c < req.min_hold_candles:
                        _log(live_state, f"⏳ {sym}: SELL→HOLD: erst {_held_c} von {req.min_hold_candles} Kerzen gehalten")
                    else:
                        ok_s, _ = await _portfolio_sell(sym, force_reason=f"Signal {confidence}%")
                        if ok_s:
                            _register_sell_outcome(live_state, sym,
                                                   (live_state.get("trade_history") or [{}])[-1].get("pnl_pct", 0.0) or 0.0,
                                                   False, interval_seconds, req, log_fn=_log)
                elif action == "PARTIAL_SELL" and confidence >= min_sell:
                    frac = float(signal.get("sell_fraction") or 0)
                    if 0 < frac < 1:
                        await _portfolio_partial_sell(sym, frac,
                                                       force_reason=f"Signal {confidence}%")

            # ── Phase 2: scan for new entries ─────────────────────────────
            open_count = len(live_state["portfolio_positions"])
            slots_free = PORTFOLIO_MAX_POSITIONS - open_count
            if slots_free <= 0:
                _log(live_state, f"Portfolio voll ({open_count}/{PORTFOLIO_MAX_POSITIONS}) — keine neuen Käufe")
            else:
                # Re-fetch USDC balance — Phase 1 sells may have freed capital
                try:
                    bal_p2 = await trader.get_balances()
                    live_state["portfolio_free_usdc"] = bal_p2.get("USDC", 0.0)
                except Exception:
                    pass
                free_usdc = live_state.get("portfolio_free_usdc", 0.0)

                # ── Scan always runs first to get candidates ──────────────
                # Pairs selected dynamically by News Agent each cycle
                held_set = set(live_state["portfolio_positions"].keys())
                cycle_top, cycle_underdogs = _get_scan_pairs_from_news(held_set)
                all_scan_symbols = cycle_top + [s for s in cycle_underdogs if s not in cycle_top]
                underdog_label = ", ".join(cycle_underdogs) if cycle_underdogs else "–"
                _log(live_state, f"🔍 Scanne {len(all_scan_symbols)} Pairs: {', '.join(cycle_top)} | 📰 Underdogs: {underdog_label}")
                try:
                    summaries = await _fetch_scan_summaries(req.interval, all_scan_symbols)
                    scan = await scan_market(summaries, req.interval,
                                             username=username,
                                             api_key=api_key, oauth_token=oauth_token,
                                             underdog_symbols=cycle_underdogs)
                except Exception as e:
                    _log(live_state, f"⚠ Scanner-Fehler: {e}")
                    scan = {"ranking": []}

                # ranking is list of {symbol, score, reason}; filter out already-held & take top N
                held = set(live_state["portfolio_positions"].keys())
                ranking = [r for r in (scan.get("ranking") or [])
                           if r.get("symbol") and r["symbol"] not in held]
                candidates = ranking[:slots_free * 2]  # consider 2x to allow signal rejections

                # ── Rebalancing: if not enough USDC but candidates exist, ask held positions ──
                if free_usdc < PORTFOLIO_MIN_ORDER_USDC and candidates and live_state["portfolio_positions"]:
                    _log(live_state, f"♻ Nur ${free_usdc:.2f} USDC frei — versuche Rebalancing für {len(candidates)} Kandidaten…")
                    # Build candidate summary once — used in per-position context below
                    cand_lines = []
                    for c in candidates[:3]:
                        cand_lines.append(
                            f"  • {c['symbol']}: Score {c.get('score', '?')}/100 — {c.get('reason', '')[:120]}"
                        )
                    cand_summary = "\n".join(cand_lines) or "  (keine)"

                    for held_sym in list(live_state["portfolio_positions"].keys()):
                        held_slot = live_state["portfolio_positions"].get(held_sym)
                        if not held_slot:
                            continue
                        try:
                            raw_h = await fetch_latest_klines(held_sym, req.interval, limit=100)
                            enriched_h = compute_indicators(raw_h)
                            news_score_h = get_news_score_for_symbol(held_sym)
                            sym_history_h = [t for t in live_state.get("trade_history", [])
                                             if t.get("symbol") == held_sym or not t.get("symbol")]
                            # Build rich per-position rebalancing context
                            buy_p_h   = float(held_slot.get("buy_price") or 0)
                            cur_p_h   = float(held_slot.get("current_price") or buy_p_h)
                            pnl_h     = (cur_p_h - buy_p_h) / buy_p_h * 100 if buy_p_h else 0
                            qty_h     = float(held_slot.get("position_qty") or 0)
                            val_h     = qty_h * cur_p_h
                            all_held  = {s: round(float((v.get("position_qty") or 0) *
                                                        (v.get("current_price") or v.get("buy_price") or 0)), 2)
                                         for s, v in live_state["portfolio_positions"].items()}
                            portfolio_context_str = (
                                f"REBALANCING-ANFRAGE:\n"
                                f"Der Bot hat kein freies USDC (${free_usdc:.2f}) und möchte eine neue Position "
                                f"eröffnen. Du entscheidest jetzt für {held_sym} ob Kapital freigegeben werden soll.\n\n"
                                f"AKTUELLES PORTFOLIO:\n"
                                f"  Freies USDC: ${free_usdc:.2f}\n"
                                f"  Positionen: { {s: f'${v}' for s, v in all_held.items()} }\n\n"
                                f"DEINE AKTUELLE POSITION ({held_sym}):\n"
                                f"  Kaufpreis: ${buy_p_h:,.4f} | Aktuell: ${cur_p_h:,.4f} | "
                                f"P&L: {pnl_h:+.2f}% | Wert: ${val_h:.2f}\n\n"
                                f"WARTENDE KANDIDATEN (vom Scanner als besser bewertet):\n"
                                f"{cand_summary}\n\n"
                                f"DEINE AUFGABE:\n"
                                f"Analysiere den Chart von {held_sym} UND bedenke die Kandidaten oben.\n"
                                f"- SELL: wenn {held_sym} technisch schwach ist ODER ein Kandidat klar besser ist\n"
                                f"- PARTIAL_SELL: wenn du Kapital teilweise freigeben willst ohne die Position aufzugeben\n"
                                f"- HOLD: wenn {held_sym} klar besser als alle Kandidaten ist und du die Position behältst"
                            )
                            sig_h = await get_live_signal(
                                symbol=held_sym, interval=req.interval, candles=enriched_h,
                                current_position="IN_POSITION", username=username,
                                signal_history=[], trade_history=sym_history_h,
                                analysis_weight=req.analysis_weight,
                                api_key=api_key, oauth_token=oauth_token,
                                news_score=news_score_h,
                                portfolio_context=portfolio_context_str,
                                min_confidence=live_state.get("min_confidence") or req.min_confidence,
                            )
                            act_h = sig_h.get("action", "HOLD")
                            conf_h = sig_h.get("confidence", 0)
                            min_sell_h = live_state.get("min_confidence_sell") or req.min_confidence_sell
                            if act_h == "SELL" and conf_h >= min_sell_h:
                                ok_h, net_h = await _portfolio_sell(
                                    held_sym, force_reason="Rebalancing→SELL")
                                if ok_h:
                                    _register_sell_outcome(
                                        live_state, held_sym,
                                        (live_state.get("trade_history") or [{}])[-1].get("pnl_pct", 0.0) or 0.0,
                                        False, interval_seconds, req, log_fn=_log)
                                    try:
                                        balances = await trader.get_balances()
                                        free_usdc = balances.get("USDC", 0.0)
                                        live_state["portfolio_free_usdc"] = free_usdc
                                    except Exception:
                                        pass
                                    if free_usdc >= PORTFOLIO_MIN_ORDER_USDC:
                                        break
                            elif act_h == "PARTIAL_SELL" and conf_h >= min_sell_h:
                                frac_h = float(sig_h.get("sell_fraction") or 0)
                                if 0 < frac_h < 1:
                                    ok_h, net_h = await _portfolio_partial_sell(
                                        held_sym, frac_h, force_reason="Rebalancing")
                                    if ok_h:
                                        try:
                                            balances = await trader.get_balances()
                                            free_usdc = balances.get("USDC", 0.0)
                                            live_state["portfolio_free_usdc"] = free_usdc
                                        except Exception:
                                            pass
                                        if free_usdc >= PORTFOLIO_MIN_ORDER_USDC:
                                            break
                        except Exception as e:
                            _log(live_state, f"⚠ Rebalancing {held_sym}: {e}")
                            continue

                # ── Stufe 2: Schwächste Position opfern wenn High-Confidence-Kandidat wartet ──
                if free_usdc < PORTFOLIO_MIN_ORDER_USDC and candidates and live_state["portfolio_positions"]:
                    best_cand_score = max((c.get("score", 0) for c in candidates[:3]), default=0)
                    if best_cand_score >= 75:
                        worst_sym = min(
                            live_state["portfolio_positions"].keys(),
                            key=lambda s: (
                                (live_state["portfolio_positions"][s].get("current_price", 0) or 0) /
                                (live_state["portfolio_positions"][s].get("buy_price", 1) or 1)
                            )
                        )
                        worst_slot = live_state["portfolio_positions"].get(worst_sym, {})
                        buy_p = float(worst_slot.get("buy_price") or 0)
                        cur_p = float(worst_slot.get("current_price") or buy_p)
                        pnl_w = (cur_p - buy_p) / buy_p * 100 if buy_p else 0
                        _log(live_state, f"♻ Stufe-2-Rebalancing: verkaufe schwächste Position {worst_sym} "
                             f"(P&L: {pnl_w:+.1f}%) für High-Score-Kandidaten (score={best_cand_score})")
                        ok_w, net_w = await _portfolio_sell(worst_sym, force_reason="Rebalancing→Rotation")
                        if ok_w:
                            _register_sell_outcome(
                                live_state, worst_sym,
                                (live_state.get("trade_history") or [{}])[-1].get("pnl_pct", 0.0) or 0.0,
                                False, interval_seconds, req, log_fn=_log)
                            try:
                                balances = await trader.get_balances()
                                free_usdc = balances.get("USDC", 0.0)
                                live_state["portfolio_free_usdc"] = free_usdc
                            except Exception:
                                pass

                if free_usdc < PORTFOLIO_MIN_ORDER_USDC:
                    _log(live_state, f"Nur ${free_usdc:.2f} USDC frei — keine neuen Käufe")
                else:
                    min_buy = live_state.get("min_confidence") or req.min_confidence
                    fng = await _fetch_fear_greed()
                    _scan_rounds = [candidates]
                    for _round_idx, _round_cands in enumerate(_scan_rounds):
                        _slots_before_round = slots_free
                        for cand in _round_cands:
                            if slots_free <= 0: break
                            sym = cand["symbol"]
                            # Re-fetch latest balance — earlier buys reduced it
                            try:
                                balances = await trader.get_balances()
                                free_usdc = balances.get("USDC", 0.0)
                                live_state["portfolio_free_usdc"] = free_usdc
                            except Exception:
                                pass
                            if free_usdc < PORTFOLIO_MIN_ORDER_USDC: break

                            try:
                                raw = await fetch_latest_klines(sym, req.interval, limit=100)
                                enriched = compute_indicators(raw)
                                candles_4h = enriched if req.interval == "4h" else compute_indicators(
                                    await fetch_latest_klines(sym, "4h", limit=100))
                                candles_1h = enriched if req.interval == "1h" else compute_indicators(
                                    await fetch_latest_klines(sym, "1h", limit=100))
                                regime = await get_regime(symbol=sym, interval=req.interval,
                                                          candles_1h=candles_1h, candles_4h=candles_4h,
                                                          fear_greed=fng,
                                                          api_key=api_key, oauth_token=oauth_token)
                            except Exception as e:
                                _log(live_state, f"⚠ {sym}: Setup-Check fehlgeschlagen ({e})")
                                continue

                            news_score = get_news_score_for_symbol(sym)
                            if regime.get("regime") == "HIGH_VOLATILITY":
                                _log(live_state, f"⏭ {sym}: HIGH_VOLATILITY — übersprungen")
                                continue
                            if news_score.get("veto"):
                                _log(live_state, f"⏭ {sym}: News-Veto — übersprungen")
                                continue
                            # Protection B+C: halt / cooldown gate
                            _blk, _why = _is_buy_blocked_by_protections(
                                live_state, sym, time.time(), req)
                            if _blk:
                                _log(live_state, f"🚫 {sym}: BUY blockiert: {_why}")
                                continue

                            sym_history = [t for t in live_state.get("trade_history", [])
                                           if t.get("symbol") == sym or not t.get("symbol")]
                            signal = await get_live_signal(
                                symbol=sym, interval=req.interval, candles=enriched,
                                current_position="FLAT", username=username,
                                signal_history=[], trade_history=sym_history,
                                analysis_weight=req.analysis_weight,
                                api_key=api_key, oauth_token=oauth_token,
                                regime=regime, news_score=news_score,
                                min_confidence=min_buy,
                            )
                            action     = signal.get("action", "HOLD")
                            confidence = signal.get("confidence", 0)
                            reason_str = signal.get("reason", "")[:100]
                            if action != "BUY" or confidence < min_buy:
                                if action != "BUY":
                                    _log(live_state, f"⏭ {sym}: {action} ({confidence}%) — {reason_str}")
                                else:
                                    _log(live_state, f"⏭ {sym}: BUY {confidence}% unter Min {min_buy}% — {reason_str}")
                                continue

                            # Confidence-tier sizing
                            tier_pct = _portfolio_allocation_pct(confidence)
                            sized = round(free_usdc * tier_pct, 2)
                            if req.max_per_position and req.max_per_position > 0:
                                sized = min(sized, req.max_per_position)
                            if sized < PORTFOLIO_MIN_ORDER_USDC:
                                _log(live_state, f"⏭ {sym}: sized {sized:.2f} < ${PORTFOLIO_MIN_ORDER_USDC} — kein Kauf")
                                continue

                            # Risk params for SL/TP
                            risk = calculate_risk_params(enriched, sized, regime.get("regime", "RANGING"),
                                                         signals_count_green=4,
                                                         sl_atr_mult=(live_state.get("sl_atr_mult") or req.sl_atr_mult),
                                                         tp_atr_mult=(live_state.get("tp_atr_mult") or req.tp_atr_mult))
                            if risk.get("blocked"):
                                _log(live_state, f"⏭ {sym}: Risk-Agent blockiert (SL {risk.get('stop_loss_pct', '?')}% ATR)")
                                continue

                            price_now = enriched[-1]["close"] if enriched else 0.0
                            ok = await _portfolio_buy(sym, sized,
                                                      sl_pct=risk["stop_loss_pct"],
                                                      tp_pct=risk["take_profit_pct"],
                                                      buy_price_for_levels=price_now)
                            if ok:
                                slots_free -= 1

                        # After first round: if no buys, scan 10 additional pairs
                        if (_round_idx == 0
                                and slots_free == _slots_before_round
                                and slots_free > 0
                                and free_usdc >= PORTFOLIO_MIN_ORDER_USDC):
                            _ext_pairs = _get_extended_scan_pairs(set(all_scan_symbols), held_set)
                            if _ext_pairs:
                                _log(live_state, f"🔎 Keine Kaufsignale — scanne {len(_ext_pairs)} weitere Pairs: {', '.join(_ext_pairs)}")
                                try:
                                    _ext_summ = await _fetch_scan_summaries(req.interval, _ext_pairs)
                                    _ext_result = await scan_market(
                                        _ext_summ, req.interval,
                                        username=username,
                                        api_key=api_key, oauth_token=oauth_token,
                                        underdog_symbols=[])
                                except Exception as _e_ext:
                                    _log(live_state, f"⚠ Erweiterter Scanner-Fehler: {_e_ext}")
                                    _ext_result = {"ranking": []}
                                _ext_held = set(live_state["portfolio_positions"].keys())
                                _ext_ranking = [
                                    r for r in (_ext_result.get("ranking") or [])
                                    if r.get("symbol") and r["symbol"] not in _ext_held
                                ]
                                _ext_cands = _ext_ranking[:slots_free * 2]
                                if _ext_cands:
                                    _scan_rounds.append(_ext_cands)

            # ── Cycle wrap-up ────────────────────────────────────────────
            try:
                balances = await trader.get_balances()
                live_state["portfolio_free_usdc"] = balances.get("USDC", 0.0)
            except Exception: pass

            next2 = _next_close()
            live_state["next_check_ts"] = next2 + CLOSE_BUFFER
            live_state["next_check_str"] = _fmt_ts(next2)
            _log(live_state, f"Nächste Analyse: {_fmt_ts(next2)} (in {_fmt_wait(next2 - time.time())})")
            live_state["_cycle_running"] = False

    except Exception as e:
        live_state["status"] = "error"
        _log(live_state, f"FEHLER: {e}")
        clear_live_state(username)
    finally:
        live_state["running"] = False
        live_state["next_check_ts"] = None
        live_state["next_check_str"] = None
        live_state["status"] = "stopped"
        live_state["_cycle_running"] = False
        live_state["_trigger_event"] = None


# ── Helpers ────────────────────────────────────────────────────────────────────

def _persist_trade_history(username: str, live_state: dict) -> None:
    saved = load_live_state(username)
    if saved:
        saved["trade_history"] = live_state.get("trade_history", [])
        saved["buy_price"] = live_state.get("buy_price")
        saved["current_capital"] = live_state.get("current_capital")
        saved["position_qty"] = live_state.get("position_qty")
        saved["compounding_mode"] = live_state.get("compounding_mode")
        saved["position"] = live_state.get("position", "FLAT")
        saved["symbol"] = live_state.get("symbol") or saved.get("symbol", "")
        saved["analysis_weight"] = live_state.get("analysis_weight", 70)
        save_live_state(username, saved)


def _log(state: dict, msg: str):
    state["log"].append(msg)
    if len(state["log"]) > 500:
        state["log"] = state["log"][-400:]
        if username := state.get("_username"):
            trim_live_log(username)
    if username := state.get("_username"):
        append_live_log(username, msg)


def _interval_to_seconds(interval: str) -> int:
    return INTERVAL_MINUTES.get(interval, 240) * 60
