"""
=============================================================
  AUTONOMOUS RULES-BASED TRADING BOT v3 — SMALL ACCOUNT SCALER
  Alpaca Paper Trading — $500 → $100,000 Challenge
=============================================================

  TIER SYSTEM (auto-adjusts as account grows):
  ─────────────────────────────────────────────────────────────
  TIER 1 │ $500 – $1,000
    → Stocks ONLY (no options yet — budget too small)
    → Cheap, high-beta tickers ($5–$40 range)
    → 20% max per trade │ 25% cash reserve │ 3 max positions

  TIER 2 │ $1,000 – $5,000
    → Options INTRODUCED (contracts ≤ $1.00 premium)
    → Falls back to stock trade if no affordable options found
    → 18% max per trade │ 25% cash reserve │ 4 max positions

  TIER 3 │ $5,000 – $25,000
    → Options PRIMARY with stock fallback
    → Broader mid-cap watchlist
    → 15% max per trade │ 22% cash reserve │ 5 max positions

  TIER 4 │ $25,000 – $100,000+
    → Full options strategy (original bot behavior)
    → Full watchlist: SPY, QQQ, blue chips
    → 12% max per trade │ 20% cash reserve │ 6 max positions
  ─────────────────────────────────────────────────────────────

  SIGNALS:
  1. MOMENTUM    — Buy call when stock up 3%+ over 5 days + up 1%+ today
  2. REVERSION   — Buy put when stock down 5%+ over 5 days
  3. STOP LOSS   — Exit any position down 10%+ automatically

  DATA: Real-time SIP feed (Alpaca paper accounts support this)

=============================================================
  SETUP:
  1. Paste your Alpaca Paper Trading keys below
  2. Run: python3 Trade_Run_3_Scaler.py
  3. Schedule daily at 9:35 AM ET (6:35 AM Fresno time)
     → cron: 35 6 * * 1-5 cd /your/folder && python3 Trade_Run_3_Scaler.py
=============================================================
"""

import logging
import time
from datetime import datetime, timedelta
import requests as http

# =============================================================
#   !! PASTE YOUR ALPACA PAPER TRADING KEYS HERE !!
# =============================================================
import os
ALPACA_API_KEY    = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")

# =============================================================
#   BASE SETTINGS
# =============================================================
ALPACA_TRADE_URL = "https://paper-api.alpaca.markets"
ALPACA_DATA_URL  = "https://data.alpaca.markets"

DRY_RUN          = False   # Set True to simulate without placing orders

# Signal thresholds — shared across all tiers
MOMENTUM_THRESHOLD    =  3.0   # % gain over 5 days triggers call buy
REVERSION_THRESHOLD   = -5.0   # % drop over 5 days triggers put buy
ACCELERATION_REQUIRED =  1.0   # Last day must be up this % for momentum signal
STOP_LOSS_PCT         = -10.0  # Exit any position down this % (wider than v1 for small accounts)

# =============================================================
#   TIER DEFINITIONS
#   Bot auto-selects the right tier based on live portfolio value
# =============================================================
TIERS = {
    1: {
        "name":              "Micro ($500–$1K)",
        "min":               0,
        "max":               1_000,
        "max_position_pct":  0.20,    # 20% of portfolio per trade
        "cash_reserve":      0.25,    # Always keep 25% as cash
        "max_positions":     3,
        "options_enabled":   False,   # Account too small for options
        "min_option_budget": None,
        "stock_fallback":    True,
        "watchlist": [
            # Cheap, liquid, high-beta stocks — ideal for momentum on small budgets
            "SOFI", "PLTR", "RIVN", "HOOD", "SNAP",
            "CLSK", "MARA", "RIOT", "SOUN", "IONQ",
        ],
    },
    2: {
        "name":              "Small ($1K–$5K)",
        "min":               1_000,
        "max":               5_000,
        "max_position_pct":  0.18,
        "cash_reserve":      0.25,
        "max_positions":     4,
        "options_enabled":   True,
        "min_option_budget": 100,     # Need at least $100 to buy 1 contract
        "stock_fallback":    True,    # Fall back to shares if no cheap options found
        "watchlist": [
            "SOFI", "PLTR", "RIVN", "HOOD", "SNAP",
            "CLSK", "MARA", "RIOT", "SOUN", "IONQ",
            "AMD",  "TSLA", "NVDA", "META",
        ],
    },
    3: {
        "name":              "Growing ($5K–$25K)",
        "min":               5_000,
        "max":               25_000,
        "max_position_pct":  0.15,
        "cash_reserve":      0.22,
        "max_positions":     5,
        "options_enabled":   True,
        "min_option_budget": 200,
        "stock_fallback":    True,
        "watchlist": [
            "AMD",  "TSLA", "NVDA",  "META",  "AMZN",
            "GOOGL","MSFT", "AAPL",  "SOFI",  "PLTR",
            "HOOD", "MARA", "RIOT",
        ],
    },
    4: {
        "name":              "Scale ($25K–$100K+)",
        "min":               25_000,
        "max":               float("inf"),
        "max_position_pct":  0.12,
        "cash_reserve":      0.20,
        "max_positions":     6,
        "options_enabled":   True,
        "min_option_budget": 500,
        "stock_fallback":    True,
        "watchlist": [
            "SPY",  "QQQ",  "AAPL", "MSFT", "NVDA",
            "AMD",  "TSLA", "AMZN", "META", "GOOGL",
            "SOFI", "PLTR",
        ],
    },
}

GOAL            = 100_000
MILESTONES      = [500, 1_000, 2_500, 5_000, 10_000, 25_000, 50_000, 75_000, 100_000]


def get_tier(portfolio_value: float) -> tuple:
    """Return (tier_number, tier_config) based on current portfolio value."""
    for num in sorted(TIERS.keys()):
        t = TIERS[num]
        if t["min"] <= portfolio_value < t["max"]:
            return num, t
    return 4, TIERS[4]


# =============================================================
#   LOGGING
# =============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("scaler_bot.log"),
    ],
)
log = logging.getLogger(__name__)


# =============================================================
#   ALPACA API HELPERS
# =============================================================
def _headers() -> dict:
    return {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        "accept":              "application/json",
        "content-type":        "application/json",
    }


def get_account() -> dict:
    r = http.get(f"{ALPACA_TRADE_URL}/v2/account", headers=_headers())
    r.raise_for_status()
    return r.json()


def get_positions() -> list:
    r = http.get(f"{ALPACA_TRADE_URL}/v2/positions", headers=_headers())
    r.raise_for_status()
    return r.json()


def get_stock_bars(symbol: str, days: int = 10) -> list:
    """Fetch recent daily OHLCV bars via SIP feed (real-time on paper accounts)."""
    start = (datetime.now() - timedelta(days=days + 7)).strftime("%Y-%m-%d")
    r = http.get(
        f"{ALPACA_DATA_URL}/v2/stocks/{symbol}/bars",
        headers=_headers(),
        params={
            "timeframe": "1Day",
            "start":     start,
            "limit":     days,
            "feed":      "sip",   # ← Real-time SIP feed (upgraded from IEX)
        },
    )
    if r.status_code != 200:
        log.debug(f"  Bar fetch failed {symbol}: {r.status_code}")
        return []
    return r.json().get("bars", [])


def get_latest_price(symbol: str) -> float | None:
    """
    Get best available price for a symbol.
    Priority: SIP mid-quote → IEX mid-quote → last trade price.
    Falls back gracefully when market is closed or SIP requires a subscription.
    """
    # 1. Try SIP quotes (real-time, may need subscription)
    for feed in ("sip", "iex"):
        r = http.get(
            f"{ALPACA_DATA_URL}/v2/stocks/quotes/latest",
            headers=_headers(),
            params={"symbols": symbol, "feed": feed},
        )
        if r.status_code == 200:
            q   = r.json().get("quotes", {}).get(symbol, {})
            bid = q.get("bp", 0)
            ask = q.get("ap", 0)
            if bid > 0 and ask > 0:
                return round((bid + ask) / 2, 2)

    # 2. Fall back to last trade price (works after hours & on free feeds)
    r = http.get(
        f"{ALPACA_DATA_URL}/v2/stocks/trades/latest",
        headers=_headers(),
        params={"symbols": symbol, "feed": "iex"},
    )
    if r.status_code == 200:
        trade = r.json().get("trades", {}).get(symbol, {})
        price = trade.get("p", 0)
        if price > 0:
            log.debug(f"  {symbol}: using last trade price ${price:.2f} (quotes unavailable)")
            return round(price, 2)

    return None


def get_option_contracts(
    symbol: str, option_type: str,
    strike_min: float, strike_max: float,
    exp_min: str, exp_max: str,
) -> list:
    params = {
        "underlying_symbols":  symbol,
        "type":                option_type,
        "strike_price_gte":    str(int(strike_min)),
        "strike_price_lte":    str(int(strike_max)),
        "expiration_date_gte": exp_min,
        "expiration_date_lte": exp_max,
        "status":              "active",
        "limit":               10,
    }
    r = http.get(
        f"{ALPACA_TRADE_URL}/v2/options/contracts",
        headers=_headers(),
        params=params,
    )
    if r.status_code != 200:
        return []
    return r.json().get("option_contracts", [])


def get_option_quote(option_symbol: str) -> float | None:
    r = http.get(
        f"{ALPACA_DATA_URL}/v1beta1/options/quotes/latest",
        headers=_headers(),
        params={"symbols": option_symbol, "feed": "indicative"},
    )
    if r.status_code != 200:
        return None
    q = r.json().get("quotes", {}).get(option_symbol)
    if not q:
        return None
    bid, ask = q.get("bp", 0), q.get("ap", 0)
    return round((bid + ask) / 2, 2) if bid > 0 and ask > 0 else None


def place_stock_order(symbol: str, side: str, qty: int) -> dict | None:
    order = {
        "symbol":        symbol,
        "qty":           str(qty),
        "side":          side,
        "type":          "market",
        "time_in_force": "day",
    }
    r = http.post(f"{ALPACA_TRADE_URL}/v2/orders", headers=_headers(), json=order)
    if r.status_code in (200, 201):
        data = r.json()
        log.info(f"  📬 Stock order placed: {side.upper()} {qty}x {symbol} | ID: {data['id']}")
        return data
    log.error(f"  ❌ Stock order failed: {r.status_code} — {r.text[:200]}")
    return None


def place_option_order(symbol: str, side: str, qty: int, limit_price: float) -> dict | None:
    order = {
        "symbol":        symbol,
        "qty":           str(qty),
        "side":          side,
        "type":          "limit",
        "limit_price":   str(round(limit_price, 2)),
        "time_in_force": "day",
    }
    r = http.post(f"{ALPACA_TRADE_URL}/v2/orders", headers=_headers(), json=order)
    if r.status_code in (200, 201):
        data = r.json()
        log.info(
            f"  📬 Option order placed: {side.upper()} {qty}x {symbol} "
            f"@ ${limit_price:.2f}/share | ID: {data['id']}"
        )
        return data
    log.error(f"  ❌ Option order failed: {r.status_code} — {r.text[:200]}")
    return None


# =============================================================
#   TRADING SIGNALS
# =============================================================
def analyze_stock(symbol: str) -> tuple:
    """
    Returns (signal, current_price, 5d_change) where signal is:
    'call'  → strong uptrend, ride momentum
    'put'   → strong downtrend, mean reversion
    None    → no signal
    """
    bars = get_stock_bars(symbol, days=10)
    if len(bars) < 6:
        log.debug(f"  {symbol}: not enough bars")
        return None, 0, 0

    closes    = [b["c"] for b in bars]
    price_now = closes[-1]
    price_5d  = closes[-6]
    price_1d  = closes[-2]

    change_5d = ((price_now - price_5d) / price_5d) * 100
    change_1d = ((price_now - price_1d) / price_1d) * 100

    log.info(f"  {symbol:<6} ${price_now:>8.2f} │ 5d: {change_5d:>+6.2f}% │ 1d: {change_1d:>+6.2f}%")

    if change_5d >= MOMENTUM_THRESHOLD and change_1d >= ACCELERATION_REQUIRED:
        log.info(f"  ✅ MOMENTUM → {symbol} ({change_5d:+.2f}% over 5 days, {change_1d:+.2f}% today)")
        return "call", price_now, change_5d

    if change_5d <= REVERSION_THRESHOLD:
        log.info(f"  ✅ REVERSION → {symbol} ({change_5d:+.2f}% over 5 days)")
        return "put", price_now, change_5d

    return None, price_now, change_5d


def find_best_option(
    symbol: str, option_type: str,
    current_price: float, budget: float,
) -> tuple:
    """Find the cheapest viable option contract that fits within budget."""
    max_premium = budget / 100   # Each contract = 100 shares

    if option_type == "call":
        strike_min = round(current_price * 1.01, 0)
        strike_max = round(current_price * 1.07, 0)
    else:
        strike_min = round(current_price * 0.93, 0)
        strike_max = round(current_price * 0.99, 0)

    exp_min = (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d")
    exp_max = (datetime.now() + timedelta(days=45)).strftime("%Y-%m-%d")

    contracts = get_option_contracts(
        symbol, option_type, strike_min, strike_max, exp_min, exp_max
    )
    if not contracts:
        log.warning(f"  ⚠️  No {option_type} contracts available for {symbol}")
        return None, None

    best_symbol  = None
    best_premium = float("inf")

    for c in contracts:
        mid = get_option_quote(c["symbol"])
        if mid and 0.05 < mid <= max_premium:
            if mid < best_premium:
                best_premium = mid
                best_symbol  = c["symbol"]

    if best_symbol:
        cost_per_contract = best_premium * 100
        log.info(
            f"  🎯 Best {option_type}: {best_symbol} @ ${best_premium:.2f}/share "
            f"(${cost_per_contract:.2f}/contract)"
        )
    else:
        log.warning(
            f"  ⚠️  No affordable {option_type} found for {symbol} "
            f"within ${budget:.0f} budget (max premium: ${max_premium:.2f})"
        )

    return best_symbol, best_premium


# =============================================================
#   STOP LOSS CHECKER
# =============================================================
def check_stop_losses(positions: list) -> list:
    """Return positions that have breached the stop-loss threshold."""
    exits = []
    for p in positions:
        plpc = float(p.get("unrealized_plpc", 0)) * 100
        if plpc <= STOP_LOSS_PCT:
            log.warning(
                f"  🛑 STOP LOSS triggered: {p['symbol']} │ "
                f"P&L: {plpc:.1f}% (threshold: {STOP_LOSS_PCT}%)"
            )
            exits.append(p)
    return exits


# =============================================================
#   PROGRESS DISPLAY
# =============================================================
def log_progress(portfolio_val: float, tier_num: int, tier: dict) -> None:
    """Display a milestone progress bar toward the $100K goal."""
    progress       = min((portfolio_val / GOAL) * 100, 100)
    next_milestone = next((m for m in MILESTONES if m > portfolio_val), GOAL)
    gap            = next_milestone - portfolio_val
    bar_filled     = int(progress / 5)
    bar            = "█" * bar_filled + "░" * (20 - bar_filled)

    log.info(f"\n{'─'*65}")
    log.info(f"  🏆  $500 → $100K PROGRESS")
    log.info(f"  Current value : ${portfolio_val:>10,.2f}  ({progress:.1f}% of goal)")
    log.info(f"  Next milestone: ${next_milestone:>10,.0f}  (${gap:>10,.2f} to go)")
    log.info(f"  Active tier   : {tier_num} — {tier['name']}")
    log.info(f"  [{bar}] {progress:.1f}%")
    log.info(f"{'─'*65}\n")


# =============================================================
#   MAIN BOT LOGIC
# =============================================================
def run() -> None:
    log.info("=" * 65)
    log.info("  SMALL ACCOUNT SCALER BOT v3 — $500 → $100K CHALLENGE")
    log.info(f"  Mode      : {'🟡 DRY RUN (no real orders)' if DRY_RUN else '🟢 LIVE PAPER TRADING'}")
    log.info(f"  Date/Time : {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    log.info("=" * 65)

    # ── 1. Connect & fetch account ──────────────────────────────
    try:
        account       = get_account()
        portfolio_val = float(account["portfolio_value"])
        cash          = float(account["cash"])
        buying_power  = float(account["buying_power"])
        log.info(
            f"\n✅ Connected │ Portfolio: ${portfolio_val:,.2f} │ "
            f"Cash: ${cash:,.2f} │ Buying Power: ${buying_power:,.2f}"
        )
    except Exception as e:
        log.error(f"❌ Connection failed: {e}")
        return

    # ── 2. Determine tier ───────────────────────────────────────
    tier_num, tier = get_tier(portfolio_val)

    log.info(f"\n⚙️  TIER {tier_num} — {tier['name']}")
    log.info(f"   Options     : {'✅ Enabled' if tier['options_enabled'] else '❌ Disabled (stocks only this tier)'}")
    log.info(f"   Max trade   : {tier['max_position_pct']*100:.0f}% of portfolio")
    log.info(f"   Cash reserve: {tier['cash_reserve']*100:.0f}%")
    log.info(f"   Max positions: {tier['max_positions']}")
    log.info(f"   Watchlist   : {', '.join(tier['watchlist'])}")

    log_progress(portfolio_val, tier_num, tier)

    # ── 3. Fetch current positions ──────────────────────────────
    try:
        positions = get_positions()
        log.info(f"📊 Open positions: {len(positions)}")
        for p in positions:
            plpc = float(p.get("unrealized_plpc", 0)) * 100
            mv   = float(p.get("market_value", 0))
            log.info(f"   {p['symbol']:<8} MV: ${mv:>9,.2f} │ P&L: {plpc:>+6.1f}%")
    except Exception as e:
        log.error(f"❌ Failed to fetch positions: {e}")
        return

    # ── 4. Check stop losses ────────────────────────────────────
    log.info("\n🛑 STEP 1 — Stop Loss Check")
    exits      = check_stop_losses(positions)
    exits_done = 0

    for pos in exits:
        sym  = pos["symbol"]
        qty  = abs(int(float(pos["qty"])))
        plpc = float(pos["unrealized_plpc"]) * 100
        log.info(f"  Exiting {sym} ({plpc:.1f}% loss)...")

        if DRY_RUN:
            log.info(f"  [DRY RUN] Would close {qty}x {sym}")
            exits_done += 1
        else:
            asset_class = pos.get("asset_class", "us_equity")
            side        = "sell" if float(pos["qty"]) > 0 else "buy"
            if asset_class == "us_option":
                mid = get_option_quote(sym)
                if mid:
                    place_option_order(sym, side, qty, mid)
            else:
                place_stock_order(sym, side, qty)
            exits_done += 1

    # ── 5. Scan watchlist ───────────────────────────────────────
    log.info(f"\n📡 STEP 2 — Watchlist Scan ({len(tier['watchlist'])} symbols)")

    open_count = len(positions) - exits_done
    if open_count >= tier["max_positions"]:
        log.info(f"  ⏸️  At max positions ({tier['max_positions']}). Skipping new signals.")
        signals = []
    else:
        signals    = []
        already_in = {p["symbol"] for p in positions}

        for symbol in tier["watchlist"]:
            if symbol in already_in:
                log.info(f"  ⏭️  {symbol} — already in portfolio, skipping")
                continue
            signal, price, change = analyze_stock(symbol)
            if signal:
                signals.append((symbol, signal, price, change))
            time.sleep(0.25)   # gentle rate limiting

    # ── 6. Execute new trades ───────────────────────────────────
    log.info(f"\n⚡ STEP 3 — Execute {len(signals)} Signal(s)")
    trades_done      = 0
    max_deploy       = buying_power * (1 - tier["cash_reserve"])
    budget_per_trade = min(
        portfolio_val * tier["max_position_pct"],
        max_deploy / max(len(signals), 1),
    )
    log.info(f"  Budget per trade: ${budget_per_trade:,.2f}")

    for symbol, signal, price, change in signals:
        log.info(f"\n  ┌─ {symbol} │ Signal: {signal.upper()} │ 5d Change: {change:+.2f}%")
        log.info(f"  │  Budget: ${budget_per_trade:,.2f}")
        traded = False

        # ── Try options first (if tier allows and budget is sufficient) ──
        if (
            tier["options_enabled"]
            and tier["min_option_budget"] is not None
            and budget_per_trade >= tier["min_option_budget"]
        ):
            log.info(f"  │  🔎 Searching for {signal.upper()} option...")
            contract, premium = find_best_option(symbol, signal, price, budget_per_trade)

            if contract and premium:
                qty        = max(1, int(budget_per_trade / (premium * 100)))
                total_cost = premium * 100 * qty
                log.info(f"  │  → {qty}x {contract} @ ${premium:.2f} = ${total_cost:,.2f} total")

                if DRY_RUN:
                    log.info(f"  │  [DRY RUN] Would BUY {qty}x {contract}")
                    trades_done += 1
                    traded = True
                else:
                    result = place_option_order(contract, "buy", qty, premium)
                    if result:
                        trades_done += 1
                        traded = True

        # ── Fallback: buy shares directly ───────────────────────
        if not traded and tier["stock_fallback"]:
            log.info(f"  │  📈 Option not available/affordable — trying stock fallback...")
            live_price = get_latest_price(symbol)

            # If quotes are unavailable (market closed, feed issue), use the
            # last known bar close price that came back from analyze_stock()
            if not live_price or live_price <= 0:
                if price and price > 0:
                    live_price = price
                    log.info(f"  │  ℹ️  Using last bar close price: ${live_price:.2f} (live quote unavailable)")
                else:
                    log.warning(f"  └  ⚠️  No price available for {symbol}. Skipping.")
                    continue

            qty = max(1, int(budget_per_trade / live_price))
            if qty < 1 or live_price > budget_per_trade:
                log.warning(
                    f"  └  ⚠️  ${live_price:.2f}/share exceeds budget "
                    f"${budget_per_trade:.2f}. Skipping {symbol}."
                )
                continue

            cost = qty * live_price
            log.info(f"  │  → {qty}x {symbol} @ ${live_price:.2f} = ${cost:.2f} total")

            if DRY_RUN:
                log.info(f"  │  [DRY RUN] Would BUY {qty}x {symbol} @ ${live_price:.2f}")
                trades_done += 1
                traded = True
            else:
                result = place_stock_order(symbol, "buy", qty)
                if result:
                    trades_done += 1
                    traded = True

        if not traded:
            log.info(f"  └  ⚠️  No trade executed for {symbol}.")
        else:
            log.info(f"  └  ✅ Trade complete for {symbol}.")

    # ── 7. Summary ──────────────────────────────────────────────
    log.info("\n" + "=" * 65)
    log.info("  RUN COMPLETE — SUMMARY")
    log.info(f"  Tier active      : {tier_num} — {tier['name']}")
    log.info(f"  Signals found    : {len(signals)}")
    log.info(f"  Trades executed  : {trades_done}{' (Dry Run)' if DRY_RUN else ''}")
    log.info(f"  Stop losses hit  : {len(exits)}")
    log.info(f"  Portfolio value  : ${portfolio_val:,.2f}")
    log.info(f"  Cash remaining   : ${cash:,.2f}")
    log.info("─" * 65)
    log.info("  CURRENT TIER RULES:")
    log.info(f"  Buy CALL  : stock up {MOMENTUM_THRESHOLD}%+ over 5 days AND up {ACCELERATION_REQUIRED}%+ today")
    log.info(f"  Buy PUT   : stock down {abs(REVERSION_THRESHOLD)}%+ over 5 days")
    log.info(f"  SELL      : any position down {abs(STOP_LOSS_PCT)}%+ (stop loss)")
    log.info(f"  Approach  : {'Options preferred, stocks as fallback' if tier['options_enabled'] else 'Stocks only (options unlock at Tier 2 / $1K)'}")
    log.info("─" * 65)
    log.info("  TIER UPGRADE THRESHOLDS:")
    log.info("  $1,000  → Tier 2: Options trading unlocked")
    log.info("  $5,000  → Tier 3: Broader watchlist + bigger budgets")
    log.info("  $25,000 → Tier 4: Full strategy (SPY, QQQ, blue chips)")
    log.info("  $100,000 → 🎯 GOAL REACHED!")
    log.info("=" * 65)
    log.info("  Tip: Run daily after 9:35 AM ET (6:35 AM Fresno time)")
    log.info("       The bot auto-upgrades its tier as your account grows 🚀")
    log.info("=" * 65)


if __name__ == "__main__":
    run()
