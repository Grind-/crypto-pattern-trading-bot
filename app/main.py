import asyncio
import hashlib
import os
import secrets
import time
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import FastAPI, BackgroundTasks, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from .data_fetcher import fetch_klines, fetch_latest_klines, get_available_symbols
from .indicators import compute_indicators
from .claude_analyst import analyze_with_claude, get_live_signal, scan_market
from .simulator import run_simulation, FEE_TIERS
from .binance_trader import BinanceTrader
from .state_store import save_live_state, load_live_state, clear_live_state, update_position
from .sim_store import save_simulation, load_simulations as _load_sims, load_simulation_detail

# ── Auth config ───────────────────────────────────────────────────────────────
_SALT = "cpa_salt_bioval_2026"
_USERS = {
    "admin": "700acb2e5e32e2cbdb1cc63418b0842ba87925541d9fe07a7193646bd563aa3a",
}
_SESSIONS: dict[str, float] = {}   # token → expiry timestamp
_SESSION_TTL = 86400 * 7           # 7 days

PUBLIC_PATHS = {"/login", "/auth/login", "/auth/logout"}


def _hash_pw(password: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), _SALT.encode(), 200000).hex()


def _valid_session(token: str) -> bool:
    exp = _SESSIONS.get(token)
    return exp is not None and time.time() < exp


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith("/static/"):
            return await call_next(request)
        token = request.cookies.get("session")
        if not _valid_session(token or ""):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Not authenticated"}, status_code=401)
            return RedirectResponse("/login", status_code=302)
        return await call_next(request)


app = FastAPI(title="Crypto Pattern AI")
app.add_middleware(AuthMiddleware)
app.mount("/static", StaticFiles(directory="frontend"), name="static")


@app.on_event("startup")
async def _auto_resume():
    """Resume live trading after container restart if it was active."""
    saved = load_live_state()
    if not saved or not saved.get("was_running"):
        return
    req = LiveRequest(
        api_key=saved["api_key"],
        api_secret=saved["api_secret"],
        symbol=saved["symbol"],
        interval=saved["interval"],
        trade_amount_usdt=saved["trade_amount"],
        strategy_name=saved.get("strategy_name", ""),
        strategy_analysis=saved.get("strategy_analysis", ""),
        strategy_patterns=saved.get("strategy_patterns", []),
    )
    valid = await BinanceTrader(req.api_key, req.api_secret).validate_keys()
    if not valid:
        clear_live_state()
        return
    live_state.update({
        "running": True,
        "status": "active",
        "position": saved.get("position", "FLAT"),
        "symbol": req.symbol,
        "interval": req.interval,
        "trade_amount": req.trade_amount_usdt,
        "signals": [],
        "log": ["⟳ Live Trading nach Neustart wiederhergestellt"],
        "api_key": req.api_key,
        "api_secret": req.api_secret,
        "next_check_ts": None,
        "next_check_str": None,
        "candle_count": 0,
        "strategy_name": req.strategy_name,
        "strategy_analysis": req.strategy_analysis,
        "strategy_patterns": req.strategy_patterns,
    })
    asyncio.create_task(_live_loop(req))

# ── Global state ──────────────────────────────────────────────────────────────

sim_state: dict = {
    "running": False,
    "iteration": 0,
    "max_iterations": 10,
    "status": "idle",
    "results": [],
    "best_result": None,
    "log": [],
    "symbol": None,
    "interval": None,
    "candle_prices": [],  # just close prices for chart
    "candle_timestamps": [],
}

live_state: dict = {
    "running": False,
    "status": "idle",
    "position": "FLAT",
    "symbol": None,
    "interval": None,
    "trade_amount": 0,
    "signals": [],
    "log": [],
    "api_key": None,
    "api_secret": None,
    "next_check_ts": None,
    "next_check_str": None,
    "candle_count": 0,
    "strategy_name": "",
    "strategy_analysis": "",
    "strategy_patterns": [],
}


# ── Pydantic models ────────────────────────────────────────────────────────────

class SimRequest(BaseModel):
    symbol: str = "BTCUSDC"
    interval: str = "4h"
    days: int = 30
    initial_capital: float = 1000.0
    max_iterations: int = 10
    fee_tier: str = "standard"   # standard | bnb | vip1 | vip2 | vip3 | vip4


class LiveRequest(BaseModel):
    api_key: str
    api_secret: str
    symbol: str = "BTCUSDC"
    interval: str = "4h"
    trade_amount_usdt: float = 50.0
    strategy_name: str = ""
    strategy_analysis: str = ""
    strategy_patterns: List[str] = []


# ── Routes ─────────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str


@app.get("/login")
async def login_page():
    return FileResponse("frontend/login.html")


@app.post("/auth/login")
async def do_login(req: LoginRequest, response: Response):
    expected = _USERS.get(req.username)
    if not expected or not secrets.compare_digest(_hash_pw(req.password), expected):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = secrets.token_hex(32)
    _SESSIONS[token] = time.time() + _SESSION_TTL
    response.set_cookie("session", token, httponly=True, samesite="lax", max_age=_SESSION_TTL)
    return {"ok": True}


@app.post("/auth/logout")
async def do_logout(request: Request, response: Response):
    token = request.cookies.get("session", "")
    _SESSIONS.pop(token, None)
    response.delete_cookie("session")
    return RedirectResponse("/login", status_code=302)


@app.get("/")
async def index():
    return FileResponse("frontend/index.html")


SCAN_SYMBOLS = [
    "BTCUSDC", "ETHUSDC", "BNBUSDC", "SOLUSDC", "XRPUSDC",
    "ADAUSDC", "AVAXUSDC", "DOGEUSDC", "DOTUSDC", "LINKUSDC",
]


async def _fetch_scan_summaries(interval: str) -> list:
    tasks = [fetch_latest_klines(sym, interval, limit=60) for sym in SCAN_SYMBOLS]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    interval_mins = {"1h": 60, "4h": 240, "1d": 1440}.get(interval, 240)
    c24 = max(1, 1440 // interval_mins)
    c7d = max(1, 10080 // interval_mins)
    summaries = []
    for sym, raw in zip(SCAN_SYMBOLS, results):
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
async def scan_symbols(body: dict):
    interval = body.get("interval", "4h")
    summaries = await _fetch_scan_summaries(interval)
    if not summaries:
        raise HTTPException(503, "Keine Marktdaten verfügbar")
    return await scan_market(summaries, interval)


@app.get("/api/symbols")
async def symbols():
    try:
        syms = await get_available_symbols()
        return {"symbols": syms}
    except Exception as e:
        return {"symbols": ["BTCUSDC", "ETHUSDC", "BNBUSDC", "SOLUSDC", "XRPUSDC"]}


@app.post("/api/simulate/start")
async def start_sim(req: SimRequest, background_tasks: BackgroundTasks):
    if sim_state["running"]:
        raise HTTPException(409, "Simulation already running")

    sim_state.update({
        "running": True,
        "iteration": 0,
        "max_iterations": req.max_iterations,
        "status": "starting",
        "results": [],
        "best_result": None,
        "log": [],
        "symbol": req.symbol,
        "interval": req.interval,
        "candle_prices": [],
        "candle_timestamps": [],
    })
    fee_pct = FEE_TIERS.get(req.fee_tier, 0.1)
    background_tasks.add_task(_sim_loop, req, fee_pct)
    return {"ok": True}


@app.post("/api/simulate/stop")
async def stop_sim():
    sim_state["running"] = False
    sim_state["status"] = "stopped"
    return {"ok": True}


@app.get("/api/simulate/status")
async def sim_status():
    return {k: v for k, v in sim_state.items() if k not in ("candle_prices", "candle_timestamps")}


@app.get("/api/simulations")
async def get_simulations():
    return {"simulations": _load_sims()}


@app.get("/api/simulations/{sim_id}")
async def get_simulation_detail(sim_id: str):
    detail = load_simulation_detail(sim_id)
    if detail is None:
        raise HTTPException(404, "Simulation not found")
    return detail


@app.get("/api/simulate/chart-data")
async def sim_chart_data():
    return {
        "prices": sim_state["candle_prices"],
        "timestamps": sim_state["candle_timestamps"],
        "results": sim_state["results"],
        "best_result": sim_state["best_result"],
    }


@app.post("/api/live/start")
async def start_live(req: LiveRequest, background_tasks: BackgroundTasks):
    if live_state["running"]:
        raise HTTPException(409, "Live trading already running")

    trader = BinanceTrader(req.api_key, req.api_secret)
    valid = await trader.validate_keys()
    if not valid:
        raise HTTPException(400, "Invalid Binance API keys")

    strategy_note = f" | Strategie: {req.strategy_name}" if req.strategy_name else ""
    live_state.update({
        "running": True,
        "status": "active",
        "position": "FLAT",
        "symbol": req.symbol,
        "interval": req.interval,
        "trade_amount": req.trade_amount_usdt,
        "signals": [],
        "log": [f"Live Trading gestartet: {req.symbol} {req.interval}, ${req.trade_amount_usdt} pro Trade{strategy_note}"],
        "api_key": req.api_key,
        "api_secret": req.api_secret,
        "next_check_ts": None,
        "next_check_str": None,
        "candle_count": 0,
        "strategy_name": req.strategy_name,
        "strategy_analysis": req.strategy_analysis,
        "strategy_patterns": req.strategy_patterns,
    })
    save_live_state({
        "was_running": True,
        "api_key": req.api_key,
        "api_secret": req.api_secret,
        "symbol": req.symbol,
        "interval": req.interval,
        "trade_amount": req.trade_amount_usdt,
        "position": "FLAT",
        "strategy_name": req.strategy_name,
        "strategy_analysis": req.strategy_analysis,
        "strategy_patterns": req.strategy_patterns,
    })
    background_tasks.add_task(_live_loop, req)
    return {"ok": True}


@app.post("/api/live/stop")
async def stop_live():
    live_state["running"] = False
    live_state["status"] = "stopped"
    live_state["log"].append("Live Trading gestoppt")
    clear_live_state()
    return {"ok": True}


@app.get("/api/live/status")
async def live_status():
    return {k: v for k, v in live_state.items() if k not in ("api_key", "api_secret")}


# ── Background tasks ───────────────────────────────────────────────────────────

async def _sim_loop(req: SimRequest, fee_pct: float = 0.1):
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

        feedback = None
        best_return = float("-inf")

        for iteration in range(req.max_iterations):
            if not sim_state["running"]:
                break

            sim_state["iteration"] = iteration + 1
            sim_state["status"] = f"iteration_{iteration + 1}"
            _log(sim_state, f"\n──── Iteration {iteration + 1}/{req.max_iterations} ────")
            _log(sim_state, "Asking Claude to analyze patterns and generate signals…")

            analysis = await analyze_with_claude(
                symbol=req.symbol,
                interval=req.interval,
                candles=enriched,
                feedback=feedback,
            )

            strategy = analysis.get("strategy_name", f"Strategy {iteration + 1}")
            signals = analysis.get("signals", [])
            _log(sim_state, f"Strategy: {strategy}")
            _log(sim_state, f"Signals: {len(signals)} | Confidence: {analysis.get('confidence', 0)}%")
            _log(sim_state, f"Patterns: {', '.join(analysis.get('patterns_found', []))}")

            sim_result = run_simulation(
                candles=enriched,
                signals=signals,
                initial_capital=req.initial_capital,
                fee_pct=fee_pct,
            )

            ret = sim_result["total_return_pct"]
            _log(sim_state, f"Return: {ret:+.2f}% | Win rate: {sim_result['win_rate']:.1f}% | Trades: {sim_result['num_trades']} | Drawdown: {sim_result['max_drawdown']:.1f}% | Fees: ${sim_result['total_fees_usdt']:.2f} ({sim_result['fee_drag_pct']:.2f}% drag)")

            result = {
                "iteration": iteration + 1,
                "strategy_name": strategy,
                "analysis": analysis.get("analysis", ""),
                "patterns_found": analysis.get("patterns_found", []),
                "signals": signals,
                "confidence": analysis.get("confidence", 0),
                **sim_result,
                "profitable": ret > 0,
            }
            sim_state["results"].append(result)

            if ret > best_return:
                best_return = ret
                sim_state["best_result"] = result

            if ret > 0:
                _log(sim_state, f"✅ PROFITABLE! Return: +{ret:.2f}% with {sim_result['num_trades']} trades")
                sim_state["status"] = "profitable"
                break
            else:
                _log(sim_state, f"❌ Not profitable ({ret:.2f}%). Sending feedback to Claude…")
                feedback = {
                    "strategy_name": strategy,
                    "previous_return": ret,
                    "patterns_found": analysis.get("patterns_found", []),
                    "trades": sim_result["trades"][:8],
                }

        if sim_state["status"] not in ("profitable", "stopped"):
            sim_state["status"] = "completed"
            _log(sim_state, f"\n✓ Simulation complete. Best return: {best_return:+.2f}%")

        if sim_state.get("best_result"):
            br = sim_state["best_result"]
            sim_id = f"sim_{int(time.time())}"
            entry = {
                "id": sim_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "symbol": req.symbol,
                "interval": req.interval,
                "days": req.days,
                "capital": req.initial_capital,
                "fee_tier": req.fee_tier,
                "total_return_pct": br.get("total_return_pct", 0),
                "win_rate": br.get("win_rate", 0),
                "num_trades": br.get("num_trades", 0),
                "max_drawdown": br.get("max_drawdown", 0),
                "total_fees_usdt": br.get("total_fees_usdt", 0),
                "fee_drag_pct": br.get("fee_drag_pct", 0),
                "strategy_name": br.get("strategy_name", "Unknown"),
                "strategy_analysis": br.get("analysis", ""),
                "strategy_patterns": br.get("patterns_found", []),
                "profitable": br.get("profitable", False),
                "iterations": sim_state.get("iteration", 0),
            }
            full_result = {
                **br,
                "id": sim_id,
                "symbol": req.symbol,
                "interval": req.interval,
                "days": req.days,
                "capital": req.initial_capital,
                "fee_tier": req.fee_tier,
                "candle_prices": sim_state.get("candle_prices", []),
                "candle_timestamps": sim_state.get("candle_timestamps", []),
            }
            save_simulation(entry, full_result)

    except Exception as e:
        sim_state["status"] = "error"
        _log(sim_state, f"ERROR: {e}")
    finally:
        sim_state["running"] = False


async def _scan_and_switch(interval: str, current_symbol: str) -> str:
    """Run market scanner; return new best symbol (may be same as current)."""
    try:
        summaries = await _fetch_scan_summaries(interval)
        if not summaries:
            return current_symbol
        result = await scan_market(summaries, interval)
        best = result.get("best_symbol", "")
        rec = result.get("recommendation", "")
        if best and best != current_symbol:
            _log(live_state, f"🔄 Scanner: {current_symbol} → {best} | {rec[:80]}")
            live_state["symbol"] = best
            update_position(live_state["position"])  # persist updated symbol too
        else:
            _log(live_state, f"🔍 Scanner bestätigt: {current_symbol} weiterhin bestes Setup")
        return best or current_symbol
    except Exception as e:
        _log(live_state, f"⚠ Scanner-Fehler: {e} — bleibe bei {current_symbol}")
        return current_symbol


async def _live_loop(req: LiveRequest):
    trader = BinanceTrader(req.api_key, req.api_secret)
    interval_seconds = _interval_to_seconds(req.interval)
    CLOSE_BUFFER = 10

    # current_symbol can change between trades when FLAT
    # position_symbol is locked once we buy (used for selling)
    current_symbol = req.symbol
    position_symbol = req.symbol

    def _next_close() -> float:
        now = time.time()
        return (int(now / interval_seconds) + 1) * interval_seconds

    def _fmt_ts(ts: float) -> str:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    def _fmt_wait(secs: float) -> str:
        h, rem = divmod(int(secs), 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}h {m}m {s}s"
        if m:
            return f"{m}m {s}s"
        return f"{s}s"

    try:
        first_run = True
        while live_state["running"]:

            # ── Auto-scan when FLAT (at start and after each sell) ────────────
            if live_state["position"] == "FLAT":
                _log(live_state, "🔍 Scanne Markt nach bestem Symbol…")
                current_symbol = await _scan_and_switch(req.interval, current_symbol)

            # ── Wait until next candle close ──────────────────────────────────
            next_close_ts = _next_close()
            wake_at = next_close_ts + CLOSE_BUFFER
            wait_secs = wake_at - time.time()

            live_state["next_check_ts"] = wake_at
            live_state["next_check_str"] = _fmt_ts(next_close_ts)

            if first_run:
                _log(live_state, f"Live Trading aktiv — {current_symbol} {req.interval}")
                _log(live_state, f"Erste Analyse: {_fmt_ts(next_close_ts)} (in {_fmt_wait(wait_secs)})")
                first_run = False

            if wait_secs > 0:
                slept = 0.0
                while slept < wait_secs and live_state["running"]:
                    chunk = min(30.0, wait_secs - slept)
                    await asyncio.sleep(chunk)
                    slept += chunk

            if not live_state["running"]:
                break

            # Use position_symbol when holding, current_symbol when flat
            active_symbol = position_symbol if live_state["position"] == "IN_POSITION" else current_symbol

            live_state["candle_count"] += 1
            _log(live_state, f"\n── Kerze #{live_state['candle_count']} geschlossen ({_fmt_ts(next_close_ts)}) ──")

            _log(live_state, f"Lade {active_symbol} {req.interval} Daten…")
            candles = await fetch_latest_klines(active_symbol, req.interval, limit=100)
            enriched = compute_indicators(candles)
            price = candles[-1]["close"] if candles else 0
            _log(live_state, f"Schlusskurs: ${price:,.2f}")

            _log(live_state, "Frage Claude nach Signal…")
            signal = await get_live_signal(
                symbol=active_symbol,
                interval=req.interval,
                candles=enriched,
                current_position=live_state["position"],
                signal_history=live_state["signals"][-10:],
                strategy_name=req.strategy_name,
                strategy_analysis=req.strategy_analysis,
                strategy_patterns=req.strategy_patterns,
            )

            action = signal.get("action", "HOLD")
            confidence = signal.get("confidence", 0)
            reason = signal.get("reason", "")
            _log(live_state, f"Signal: {action} | Konfidenz: {confidence}% | {reason}")

            live_state["signals"].append({
                "action": action, "confidence": confidence,
                "reason": reason, "price": price,
                "symbol": active_symbol,
                "timestamp": candles[-1]["timestamp"] if candles else 0,
            })

            # ── Execute trades ────────────────────────────────────────────────
            if action == "BUY" and live_state["position"] == "FLAT" and confidence >= 60:
                try:
                    order = await trader.place_market_order(
                        symbol=current_symbol, side="BUY",
                        quote_quantity=req.trade_amount_usdt,
                    )
                    position_symbol = current_symbol  # lock symbol for this trade
                    live_state["position"] = "IN_POSITION"
                    live_state["symbol"] = current_symbol
                    update_position("IN_POSITION")
                    _log(live_state, f"✅ KAUF {current_symbol} — Order {order.get('orderId', '?')} @ ${price:,.2f}")
                except Exception as e:
                    _log(live_state, f"❌ KAUF fehlgeschlagen: {e}")

            elif action == "SELL" and live_state["position"] == "IN_POSITION" and confidence >= 55:
                try:
                    base_asset = position_symbol.replace("USDC", "").replace("USDT", "")
                    balances = await trader.get_balances()
                    qty = balances.get(base_asset, 0)
                    if qty > 0:
                        order = await trader.place_market_order(
                            symbol=position_symbol, side="SELL", quantity=qty,
                        )
                        live_state["position"] = "FLAT"
                        update_position("FLAT")
                        _log(live_state, f"✅ VERKAUF {position_symbol} — Order {order.get('orderId', '?')} @ ${price:,.2f}")
                        # Scanner runs at next loop iteration (FLAT branch)
                    else:
                        _log(live_state, f"⚠ Kein {base_asset}-Guthaben zum Verkaufen")
                except Exception as e:
                    _log(live_state, f"❌ VERKAUF fehlgeschlagen: {e}")

            elif action == "HOLD":
                _log(live_state, f"→ HALTEN (Position: {live_state['position']})")
            else:
                _log(live_state, f"→ {action} ignoriert (Konfidenz {confidence}% unter Schwellwert)")

            next2 = _next_close()
            live_state["next_check_ts"] = next2 + CLOSE_BUFFER
            live_state["next_check_str"] = _fmt_ts(next2)
            _log(live_state, f"Nächste Analyse: {_fmt_ts(next2)} (in {_fmt_wait(next2 - time.time())})")

    except Exception as e:
        live_state["status"] = "error"
        _log(live_state, f"FEHLER: {e}")
        clear_live_state()
    finally:
        live_state["running"] = False
        live_state["next_check_ts"] = None
        live_state["next_check_str"] = None
        live_state["status"] = "stopped"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _log(state: dict, msg: str):
    state["log"].append(msg)
    if len(state["log"]) > 500:
        state["log"] = state["log"][-400:]


def _interval_to_seconds(interval: str) -> int:
    mapping = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800,
               "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400}
    return mapping.get(interval, 14400)
