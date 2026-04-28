import asyncio
import logging
import math
import os
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
                         save_binance_keys, get_binance_keys)
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
        req = LiveRequest(
            api_key=bkey,
            api_secret=bsec,
            symbol=saved_symbol,
            interval=saved["interval"],
            trade_amount_usdt=saved["trade_amount"],
            compounding_mode=saved.get("compounding_mode", "compound"),
            analysis_weight=int(saved.get("analysis_weight") or 70),
        )
        trader = BinanceTrader(bkey, bsec)
        valid = await trader.validate_keys()
        if not valid:
            deactivate_live_state(username)
            continue

        # Reconcile state against actual Binance holdings (only when symbol is known)
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

        api_key, oauth_token = _claude_creds(username)
        session_token = str(uuid.uuid4())
        state = _get_live_state(username)
        snapshot = load_live_state_snapshot(username, saved_symbol) if saved_symbol else None
        resumed_log = load_live_log(username)
        resumed_log.append("── Bot neu gestartet, Trading fortgesetzt ──")
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
    }


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


class LiveRequest(BaseModel):
    api_key: str = ""
    api_secret: str = ""
    symbol: str = ""          # empty = agent decides on startup
    interval: str = "4h"
    trade_amount_usdt: float = 50.0
    compounding_mode: str = "compound"   # "fixed" | "compound" | "compound_wins"
    analysis_weight: int = 70            # 0=pure KB, 100=pure market analysis


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


# ── Page routes ────────────────────────────────────────────────────────────────

@app.get("/")
async def index(request: Request):
    token = request.cookies.get("session", "")
    if _valid_session(token):
        return FileResponse("frontend/index.html")
    return FileResponse("frontend/landing.html")


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
    return {
        "username": user["username"],
        "role": user["role"],
        "claude_mode": user.get("claude_mode", "api_key"),
        "has_api_key": bool(user.get("claude_api_key")),
        "has_oauth_token": bool(user.get("claude_oauth_token")),
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
        })
    return {"users": safe}


@app.post("/api/admin/users")
async def admin_create_user(body: dict, request: Request):
    _require_admin(request)
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
    if not create_user(username, password, role=role, claude_mode=claude_mode):
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

SCAN_SYMBOLS = [
    "BTCUSDC", "ETHUSDC", "BNBUSDC", "SOLUSDC", "XRPUSDC",
    "ADAUSDC", "AVAXUSDC", "DOGEUSDC", "DOTUSDC", "LINKUSDC",
]


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

    req = LiveRequest(
        api_key=bkey, api_secret=bsec,
        symbol="", interval=req.interval,
        trade_amount_usdt=req.trade_amount_usdt,
        compounding_mode=req.compounding_mode,
        analysis_weight=req.analysis_weight,
    )

    kb_pct = 100 - req.analysis_weight
    weight_note = f"Wissensbasis {kb_pct}% / Markt {req.analysis_weight}%"
    session_token = str(uuid.uuid4())
    live_state.update({
        "running": True, "status": "active", "position": "FLAT",
        "symbol": "", "interval": req.interval,
        "trade_amount": req.trade_amount_usdt,
        "current_capital": req.trade_amount_usdt,
        "position_qty": 0,
        "compounding_mode": req.compounding_mode,
        "signals": [],
        "log": [f"Live Trading gestartet — {req.interval}, ${req.trade_amount_usdt} USDC | {weight_note}"],
        "api_key": bkey, "api_secret": bsec,
        "next_check_ts": None, "next_check_str": None, "candle_count": 0,
        "analysis_weight": req.analysis_weight,
        "trade_history": [], "live_candles": [], "buy_price": None,
        "_session_token": session_token,
        "_username": username,
    })
    save_live_state(username, {
        "was_running": True,
        "api_key": bkey, "api_secret": bsec,
        "symbol": "", "interval": req.interval,
        "trade_amount": req.trade_amount_usdt, "current_capital": req.trade_amount_usdt,
        "position_qty": 0, "compounding_mode": req.compounding_mode, "position": "FLAT",
        "analysis_weight": req.analysis_weight,
        "trade_history": [], "buy_price": None,
        "last_regime": None, "last_risk": None, "last_news_score": None,
    })
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
    return {k: v for k, v in live_state.items()
            if k not in ("api_key", "api_secret", "live_candles")}


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
            buy_price = candles[-1]["close"] if candles else actual_capital / bought_qty
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
        if qty <= 0:
            # Reconcile: check actual balance
            base = position_symbol.replace("USDC", "").replace("USDT", "")
            try:
                qty = await trader.get_asset_balance(base)
            except Exception:
                pass
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
            buy_p     = live_state.get("buy_price") or (candles[-1]["close"] if candles else 0)
            cur_price = candles[-1]["close"] if candles else 0
            pnl_pct   = (cur_price - buy_p) / buy_p * 100 if buy_p else 0.0
            gross_usdc = float(order.get("cummulativeQuoteQty", 0))
            usdc_fees  = sum(float(f["commission"]) for f in order.get("fills", []) if f.get("commissionAsset", "").upper() == "USDC")
            net_usdc   = (gross_usdc - usdc_fees) if gross_usdc > 0 else (qty * cur_price)
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
            live_state["trade_history"].append({
                "type": "SELL", "symbol": position_symbol,
                "price": cur_price, "timestamp": int(time.time() * 1000),
                "order_id": str(order.get("orderId", "")), "pnl_pct": round(pnl_pct, 3),
            })
            _log(live_state, f"✅ VERKAUF {position_symbol}{reason_str} @ ${cur_price:,.4f} | P&L: {pnl_pct:+.2f}% | Kapital: ${net_usdc:.2f} ({delta:+.2f}$)")
            _persist_trade_history(username, live_state)
            append_trade_log(username, position_symbol, live_state["trade_history"][-1])
            save_live_state_snapshot(username, position_symbol, live_state)
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

                    try:
                        crypto_price = await trader.get_price(chosen) if crypto_held > 0 else 0.0
                    except Exception:
                        crypto_price = 0.0
                    crypto_value = crypto_held * crypto_price

                    if usdc_available >= target:
                        _log(live_state, f"💰 {usdc_available:.2f} USDC — kaufe {chosen}")
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
                        elif usdc_available >= 10.0:
                            buy_usdc = min(need_usdc, round(usdc_available * 0.995, 2))
                            _log(live_state, f"ℹ {crypto_held:.6f} {base_asset} (~${crypto_value:.2f}) + kaufe ${buy_usdc:.2f} nach")
                            live_state.update({
                                "position": "IN_POSITION", "position_qty": crypto_held,
                                "buy_price": crypto_price or live_state.get("buy_price"),
                                "current_capital": crypto_value, "symbol": chosen,
                            })
                            update_position(username, "IN_POSITION", symbol=chosen)
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

                    elif usdc_available >= 10.0:
                        _log(live_state, f"💰 {usdc_available:.2f} USDC (< Ziel ${target:.2f}) — kaufe soviel wie möglich")
                        await _do_buy(chosen, target)

                    else:
                        _log(live_state, f"⚠ Kein {base_asset} und weniger als $10 USDC — warte auf Einzahlung.")

        except Exception as e:
            _log(live_state, f"⚠ Startup-Check fehlgeschlagen: {e}")
            logger.error(f"Startup-Check fehlgeschlagen [{username}]: {e}")

        first_run = True
        candles: list = []
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

            if wait_secs > 0:
                slept = 0.0
                while slept < wait_secs and _still_active():
                    chunk = min(30.0, wait_secs - slept)
                    await asyncio.sleep(chunk)
                    slept += chunk

            if not _still_active():
                break

            live_state["candle_count"] += 1
            _log(live_state, f"\n── Kerze #{live_state['candle_count']} ({_fmt_ts(next_close_ts)}) ──")

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
                if buy_p and sl_pct and price <= buy_p * (1 - sl_pct / 100):
                    force_sell = True
                    force_sell_reason = f"Stop-Loss {sl_pct}% — ${price:,.2f} ≤ ${buy_p*(1-sl_pct/100):,.2f}"
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
            if not force_sell:
                regime_str = regime_result.get("regime", "RANGING")
                news_sent  = news_score.get("sentiment_score", 50)
                news_veto  = news_score.get("veto", False)
                news_w     = regime_result.get("signal_weight_news", 30) / 100.0
                vote = 1.0 if action == "BUY" else (-1.0 if action == "SELL" else 0.0)
                news_mod = (news_sent / 100.0) * news_w
                regime_boost = {"BULL_TREND": 0.3, "RANGING": 0.0, "BEAR_TREND": -0.3, "HIGH_VOLATILITY": -0.5}.get(regime_str, 0.0)
                total_score = vote + news_mod + regime_boost
                _d_vote, _d_news_mod, _d_regime_boost, _d_total = vote, news_mod, regime_boost, total_score
                _log(live_state, f"Vote: Signal={vote:+.1f} News={news_mod:+.2f} Regime={regime_boost:+.1f} → {total_score:+.2f}")

                if regime_str == "HIGH_VOLATILITY" and action == "BUY":
                    action = "HOLD"
                    _d_overrides.append("BUY blockiert: HIGH_VOLATILITY-Regime")
                    _log(live_state, "🚫 BUY blockiert: HIGH_VOLATILITY")
                if news_veto and action == "BUY":
                    action = "HOLD"
                    _d_overrides.append("BUY blockiert: News-Veto")
                    _log(live_state, f"🚫 BUY blockiert: News-Veto")
                if action == "BUY" and total_score < 1.3:
                    action = "HOLD"
                    _d_overrides.append(f"BUY→HOLD: Voting-Score {total_score:.2f} unter Schwellenwert 1.3")
                    _log(live_state, f"→ HOLD: Score {total_score:.2f} < 1.3")
                if action == "SELL" and live_state["position"] == "IN_POSITION" and total_score > -0.8:
                    if not force_sell:
                        action = "HOLD"
                        _d_overrides.append(f"SELL→HOLD: Score {total_score:.2f} über Schwellenwert −0.8")
                        _log(live_state, f"→ HOLD: SELL Score {total_score:.2f} > -0.8")
            else:
                _d_overrides.append(f"Zwangsverkauf: {force_sell_reason}")

            # ── Agent 4: Risk sizing ─────────────────────────────────────────────
            risk_result = None
            if action == "BUY" and live_state["position"] == "FLAT":
                regime_str = regime_result.get("regime", "RANGING")
                news_sent  = news_score.get("sentiment_score", 50)
                vote = 1.0 if action == "BUY" else (-1.0 if action == "SELL" else 0.0)
                news_w     = regime_result.get("signal_weight_news", 30) / 100.0
                news_mod = (news_sent / 100.0) * news_w
                regime_boost = {"BULL_TREND": 0.3, "RANGING": 0.0, "BEAR_TREND": -0.3, "HIGH_VOLATILITY": -0.5}.get(regime_str, 0.0)
                total_score = vote + news_mod + regime_boost
                green = sum([
                    vote > 0,
                    news_sent >= 50,
                    regime_str not in ("BEAR_TREND", "HIGH_VOLATILITY"),
                    total_score >= 1.3,
                ])
                _d_green = green
                capital = live_state.get("current_capital") or req.trade_amount_usdt
                risk_result = calculate_risk_params(enriched, capital, regime_str, green)
                live_state["last_risk"] = risk_result
                _log(live_state, f"Risk: {risk_result['position_size_pct']}% Kapital | SL {risk_result['stop_loss_pct']:.2f}% | TP {risk_result['take_profit_pct']:.2f}%")
                if risk_result["blocked"]:
                    action = "HOLD"
                    _d_overrides.append(f"BUY blockiert: Risk Agent ({green}/4 Signale grün)")
                    _log(live_state, f"🚫 Risk Agent blockiert ({green}/4 Signale grün)")

            # ── Execute signal ───────────────────────────────────────────────────
            if action == "BUY" and live_state["position"] == "FLAT" and confidence >= 0:
                capital = live_state.get("current_capital") or req.trade_amount_usdt
                if risk_result and not risk_result["blocked"]:
                    sized_capital = round(capital * risk_result["position_size_pct"] / 100.0, 2)
                else:
                    sized_capital = capital
                if sized_capital >= 10.0:
                    bought = await _do_buy(current_symbol, sized_capital)
                    if bought and risk_result:
                        live_state["sl_pct"] = risk_result["stop_loss_pct"]
                        live_state["tp_pct"] = risk_result["take_profit_pct"]
                        _log(live_state, f"SL: {live_state['sl_pct']:.2f}% / TP: {live_state['tp_pct']:.2f}% (ATR-basiert)")
                else:
                    _log(live_state, f"⚠ sized_capital {sized_capital:.2f} < $10 — kein Kauf")

            elif action == "SELL" and live_state["position"] == "IN_POSITION" and (force_sell or confidence >= 0):
                await _do_sell(force=force_sell, force_reason=force_sell_reason)

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

    except Exception as e:
        live_state["status"] = "error"
        _log(live_state, f"FEHLER: {e}")
        clear_live_state(username)
    finally:
        live_state["running"] = False
        live_state["next_check_ts"] = None
        live_state["next_check_str"] = None
        live_state["status"] = "stopped"


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
