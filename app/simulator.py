from typing import List, Dict

# Binance fee tiers (taker rate, used for market orders)
FEE_TIERS = {
    "standard":  0.10,   # Standard spot
    "bnb":       0.075,  # BNB fee discount (25% off)
    "vip1":      0.09,
    "vip2":      0.08,
    "vip3":      0.07,
    "vip4":      0.05,
}

COMPOUNDING_MODES = {
    "fixed":          "Fixes Volumen",
    "compound":       "Volles Compounding",
    "compound_wins":  "Gewinne compounding",
}


def run_simulation(
    candles: List[Dict],
    signals: List[Dict],
    initial_capital: float = 1000.0,
    fee_pct: float = 0.1,
    compounding_mode: str = "compound",
) -> Dict:
    """
    compounding_mode:
      "fixed"         — always trade with initial_capital; P&L accumulates in wallet_base
      "compound"      — reinvest everything (wins AND losses compound)
      "compound_wins" — compound wins; reset to initial_capital after a loss
    """
    if not signals or not candles:
        history = [
            {"candle_index": i, "timestamp": c.get("timestamp", 0),
             "value": initial_capital, "close": c["close"]}
            for i, c in enumerate(candles)
        ]
        return _empty_result(initial_capital, history, compounding_mode)

    sorted_sigs = sorted(signals, key=lambda x: x.get("candle_index", 0))
    sorted_sigs = [s for s in sorted_sigs if 0 <= s.get("candle_index", -1) < len(candles)]

    # Enforce strict BUY → SELL → BUY alternation
    valid = []
    expecting = "BUY"
    for s in sorted_sigs:
        action = s.get("action", "").upper()
        if action == expecting:
            valid.append(s)
            expecting = "SELL" if expecting == "BUY" else "BUY"
    # Trailing BUY (no closing SELL) is kept — the position will be
    # marked-to-market at the last candle price rather than being dropped.

    sig_map = {s["candle_index"]: s for s in valid}

    # active_capital: USDC available for the current/next trade (0 while IN_POSITION)
    # wallet_base:    P&L accumulated outside the active trade pool (used by fixed/compound_wins)
    active_capital = initial_capital
    wallet_base = 0.0
    position = 0.0
    buy_price = 0.0
    buy_index = 0
    entry_capital = 0.0    # capital committed at buy time (before fee)
    buy_capital_net = 0.0  # capital after buy fee
    buy_fee_paid = 0.0
    trades = []
    history = []
    peak_value = initial_capital
    max_drawdown = 0.0
    total_fees = 0.0

    for i, candle in enumerate(candles):
        price = candle["close"]
        current_value = active_capital + position * price + wallet_base

        history.append({
            "candle_index": i,
            "timestamp": candle.get("timestamp", 0),
            "value": round(current_value, 2),
            "close": price,
        })

        if current_value > peak_value:
            peak_value = current_value
        drawdown = (peak_value - current_value) / peak_value * 100 if peak_value > 0 else 0
        max_drawdown = max(max_drawdown, drawdown)

        if i in sig_map:
            action = sig_map[i].get("action", "").upper()

            if action == "BUY" and active_capital > 0:
                buy_fee = active_capital * (fee_pct / 100)
                total_fees += buy_fee
                entry_capital = active_capital
                buy_capital_net = active_capital - buy_fee
                position = buy_capital_net / price
                buy_price = price
                buy_index = i
                buy_fee_paid = buy_fee
                active_capital = 0.0

            elif action == "SELL" and position > 0:
                gross = position * price
                sell_fee = gross * (fee_pct / 100)
                total_fees += sell_fee
                net_received = gross - sell_fee

                original_committed = entry_capital
                net_pnl_pct = (net_received - original_committed) / original_committed * 100 if original_committed else 0
                price_move_pct = ((price - buy_price) / buy_price) * 100 if buy_price else 0

                trades.append({
                    "buy_index": buy_index,
                    "sell_index": i,
                    "buy_price": round(buy_price, 2),
                    "sell_price": round(price, 2),
                    "pnl_pct": round(net_pnl_pct, 3),
                    "price_move_pct": round(price_move_pct, 3),
                    "fee_buy": round(buy_fee_paid, 4),
                    "fee_sell": round(sell_fee, 4),
                    "fees_total": round(buy_fee_paid + sell_fee, 4),
                    "reason_buy": sig_map.get(buy_index, {}).get("reason", ""),
                    "reason_sell": sig_map[i].get("reason", ""),
                    "capital_used": round(entry_capital, 2),
                    "capital_after": round(net_received, 2),
                })
                position = 0.0

                # Apply compounding mode
                if compounding_mode == "compound":
                    active_capital = net_received
                elif compounding_mode == "fixed":
                    wallet_base += net_received - entry_capital
                    active_capital = initial_capital
                else:  # compound_wins
                    if net_received >= initial_capital:
                        active_capital = net_received
                    else:
                        wallet_base += net_received - initial_capital
                        active_capital = initial_capital

    # Close open position at last price (mark-to-market)
    if position > 0:
        last_price = candles[-1]["close"]
        close_fee = position * last_price * (fee_pct / 100)
        total_fees += close_fee
        final_value = position * last_price - close_fee + wallet_base
    else:
        final_value = active_capital + wallet_base

    total_return_usdt = final_value - initial_capital
    total_return_pct = (total_return_usdt / initial_capital) * 100 if initial_capital else 0
    winning = sum(1 for t in trades if t["pnl_pct"] > 0)
    win_rate = (winning / len(trades) * 100) if trades else 0.0
    fee_drag_pct = (total_fees / initial_capital) * 100 if initial_capital else 0

    return {
        "trades": trades,
        "total_return_pct": round(total_return_pct, 2),
        "total_return_usdt": round(total_return_usdt, 2),
        "final_capital": round(final_value, 2),
        "win_rate": round(win_rate, 1),
        "max_drawdown": round(max_drawdown, 2),
        "num_trades": len(trades),
        "total_fees_usdt": round(total_fees, 4),
        "fee_drag_pct": round(fee_drag_pct, 3),
        "fee_pct_used": fee_pct,
        "compounding_mode": compounding_mode,
        "compounding_mode_label": COMPOUNDING_MODES.get(compounding_mode, compounding_mode),
        "portfolio_history": history,
    }


def _empty_result(initial_capital: float, history: List[Dict], compounding_mode: str = "compound") -> Dict:
    return {
        "trades": [],
        "total_return_pct": 0.0,
        "total_return_usdt": 0.0,
        "final_capital": initial_capital,
        "win_rate": 0.0,
        "max_drawdown": 0.0,
        "num_trades": 0,
        "total_fees_usdt": 0.0,
        "fee_drag_pct": 0.0,
        "fee_pct_used": 0.1,
        "compounding_mode": compounding_mode,
        "compounding_mode_label": COMPOUNDING_MODES.get(compounding_mode, compounding_mode),
        "portfolio_history": history,
    }
