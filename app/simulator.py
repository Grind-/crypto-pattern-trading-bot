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


def run_simulation(
    candles: List[Dict],
    signals: List[Dict],
    initial_capital: float = 1000.0,
    fee_pct: float = 0.1,
) -> Dict:
    if not signals or not candles:
        history = [
            {"candle_index": i, "timestamp": c.get("timestamp", 0),
             "value": initial_capital, "close": c["close"]}
            for i, c in enumerate(candles)
        ]
        return _empty_result(initial_capital, history)

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
    if valid and valid[-1].get("action", "").upper() == "BUY":
        valid.pop()

    sig_map = {s["candle_index"]: s for s in valid}

    capital = initial_capital
    position = 0.0
    buy_price = 0.0
    buy_index = 0
    buy_capital = 0.0      # capital committed at buy time (after buy fee)
    buy_fee_paid = 0.0
    trades = []
    history = []
    peak_value = initial_capital
    max_drawdown = 0.0
    total_fees = 0.0

    for i, candle in enumerate(candles):
        price = candle["close"]
        current_value = capital + position * price

        history.append({
            "candle_index": i,
            "timestamp": candle.get("timestamp", 0),
            "value": round(current_value, 2),
            "close": price,
        })

        if current_value > peak_value:
            peak_value = current_value
        drawdown = (peak_value - current_value) / peak_value * 100
        max_drawdown = max(max_drawdown, drawdown)

        if i in sig_map:
            action = sig_map[i].get("action", "").upper()

            if action == "BUY" and capital > 0:
                buy_fee = capital * (fee_pct / 100)
                total_fees += buy_fee
                buy_capital = capital - buy_fee          # actual USDT used after fee
                position = buy_capital / price
                buy_price = price
                buy_index = i
                buy_fee_paid = buy_fee
                capital = 0.0

            elif action == "SELL" and position > 0:
                gross = position * price
                sell_fee = gross * (fee_pct / 100)
                total_fees += sell_fee
                net_received = gross - sell_fee
                capital = net_received

                # True net P&L: compare net received vs original capital committed
                original_committed = buy_capital + buy_fee_paid  # full capital before buy fee
                net_pnl_pct = (net_received - original_committed) / original_committed * 100
                # Raw price move (before fees)
                price_move_pct = ((price - buy_price) / buy_price) * 100

                trades.append({
                    "buy_index": buy_index,
                    "sell_index": i,
                    "buy_price": round(buy_price, 2),
                    "sell_price": round(price, 2),
                    "pnl_pct": round(net_pnl_pct, 3),          # net after both fees
                    "price_move_pct": round(price_move_pct, 3), # raw price move
                    "fee_buy": round(buy_fee_paid, 4),
                    "fee_sell": round(sell_fee, 4),
                    "fees_total": round(buy_fee_paid + sell_fee, 4),
                    "reason_buy": sig_map.get(buy_index, {}).get("reason", ""),
                    "reason_sell": sig_map[i].get("reason", ""),
                })
                position = 0.0

    # Close open position at last price
    final_value = capital
    if position > 0:
        last_price = candles[-1]["close"]
        close_fee = position * last_price * (fee_pct / 100)
        total_fees += close_fee
        final_value = position * last_price - close_fee

    total_return_usdt = final_value - initial_capital
    total_return_pct = (total_return_usdt / initial_capital) * 100
    winning = sum(1 for t in trades if t["pnl_pct"] > 0)
    win_rate = (winning / len(trades) * 100) if trades else 0.0

    # Fee impact: what return would be without any fees
    fee_drag_pct = (total_fees / initial_capital) * 100

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
        "portfolio_history": history,
    }


def _empty_result(initial_capital: float, history: List[Dict]) -> Dict:
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
        "portfolio_history": history,
    }
