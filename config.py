"""
config.py
=========
SINGLE CONTROL SURFACE for the NIFTY option-selling bot.

The strategy is one structure only:

    BULLISH  ->  buy an OTM put under ₹10, then sell the ATM put
    BEARISH  ->  buy an OTM call under ₹10, then sell the ATM call

The side comes from open interest alone. Minutes from 09:15 through 09:25
are ignored. From 09:26 the bot stores ATM and seven strikes either side,
so a later ATM still has the open interest it had at the open. Dominance
is the ATM put-OI change against the ATM call-OI change since 09:26.
That analysis runs for 15 minutes. Nothing is sold before 09:41. ATM dominance past 30%, and the strike one
step away on that side past 15%, is the only reason a spread is opened.
The same pair of checks closes it. The next spread waits two minutes and
is taken only if the signal still qualifies. Nothing is opened after 15:00.
Whatever is open is squared off at 15:15. On expiry day the bot trades the
contract that expires today. Until you set both live switches, nothing is
sent to the exchange.

Edit the dictionaries below. Environment variables override the switches
marked with _env_*.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def _env_str(key: str, default: str) -> str:
    return os.getenv(key, default).strip()


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(float(os.getenv(key, str(default))))
    except (TypeError, ValueError):
        return default


# ══════════════════════════════════════════════════════════════════════════════
#  1. CREDENTIALS  (.env only — never hard-code secrets here)
# ══════════════════════════════════════════════════════════════════════════════
ZERODHA_API_KEY      = _env_str("ZERODHA_API_KEY", "")
ZERODHA_API_SECRET   = _env_str("ZERODHA_API_SECRET", "")
ZERODHA_ACCESS_TOKEN = _env_str("ZERODHA_ACCESS_TOKEN", "")
ZERODHA_USER_ID      = _env_str("ZERODHA_USER_ID", "")

TELEGRAM_BOT_TOKEN   = _env_str("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID     = _env_str("TELEGRAM_CHAT_ID", "")


# ══════════════════════════════════════════════════════════════════════════════
#  2. MODE
# ══════════════════════════════════════════════════════════════════════════════
# paper -> fills are simulated on the live bid/ask book. This is the default.
# live  -> real orders. Also requires LIVE_TRADING_CONFIRMED=true.
TRADING_MODE = _env_str("TRADING_MODE", "paper").lower()
LIVE_TRADING_CONFIRMED = _env_bool("LIVE_TRADING_CONFIRMED", False)

# Paper bookkeeping only. There is no margin cap and no daily loss cap.
PAPER_STARTING_CAPITAL = _env_float("PAPER_STARTING_CAPITAL", 1_000_000.0)
CAPITAL = _env_float("CAPITAL", 1_000_000.0)


# ══════════════════════════════════════════════════════════════════════════════
#  3. UNDERLYING — NIFTY only
# ══════════════════════════════════════════════════════════════════════════════
SYMBOLS: dict = {
    "NIFTY": {
        "enabled":          True,
        "display_name":     "NIFTY 50",
        "spot_key":         "NSE:NIFTY 50",
        "opt_exchange":     "NFO",
        "instrument_name":  "NIFTY",
        "strike_step":      50,
        "lot_size":         65,          # fallback; the instrument dump wins
        "freeze_qty":       1800,
        "tick_size":        0.05,
        # ATM and this many strikes each side are stored from 09:26.
        # Dominance stays on ATM. The band is what keeps a later ATM's morning OI.
        "chain_strikes":    7,
    },
}

SYMBOL_FILTER = _env_str("SYMBOLS", "NIFTY")


# ══════════════════════════════════════════════════════════════════════════════
#  4. SESSION (IST, "HH:MM")
# ══════════════════════════════════════════════════════════════════════════════
# 09:15–09:25 is the 10-minute skip. The 09:26 print is the baseline.
# The first change is 09:27. 09:26–09:40 is the 15-minute analysis.
# From 09:41 each minute can sell when ATM dominance is past 30% and the
# strike one step away agrees at 15%.
# 15:00 is the last entry. The same pair of checks closes a spread. The next
# one waits out the flip cooldown. 15:15 squares off.
SESSION: dict = {
    "market_open":            "09:15",
    "market_close":           "15:30",
    "analysis_start":         "09:26",
    "first_trade":            "09:41",
    "last_entry":             "15:00",
    "hard_square_off":        "15:15",
    "expiry_hard_square_off": "15:15",
    "blackout_dates":         [],       # "YYYY-MM-DD"
    "blackout_windows":       [],       # [("10:00", "10:20")]
}


# ══════════════════════════════════════════════════════════════════════════════
#  5. HOW OFTEN THE BOT LOOKS
# ══════════════════════════════════════════════════════════════════════════════
ENGINE: dict = {
    "monitor_interval_sec":   0.5,     # stop and target, on the live book
    "scan_minutes":           1,       # one OI print per minute
    "report_minutes":         5,       # Telegram note. The minute pass is not posted.
    "state_save_sec":         10.0,
    "feed_stale_sec":         15.0,    # no new trade while the feed is this quiet
    "feed_dead_panic_sec":    90.0,    # flatten if the feed dies with a position open
    "kite_rps":               _env_float("KITE_RPS", 2.5),
    # 09:26 through 09:40 is the 15-minute analysis. 09:41 is the first sell.
    "min_open_samples":       15,
    "flip_cooldown_min":      2,       # after a signal exit, wait, then read the signal again
    "scan_retry_sec":         5.0,     # a failed minute is tried again, without waiting the minute out
}


# ══════════════════════════════════════════════════════════════════════════════
#  6. EXPIRY
# ══════════════════════════════════════════════════════════════════════════════
EXPIRY: dict = {
    "min_dte":            0,
    "max_dte":            8,       # next weekly, including a one-day holiday shift
    "preferred_min_dte":  1,
    "allow_expiry_day":   True,    # expiry day trades the contract that expires today
}


# ══════════════════════════════════════════════════════════════════════════════
#  7. OPEN INTEREST — the only reason a trade is opened or flipped
# ══════════════════════════════════════════════════════════════════════════════
# The check is the ATM call and the ATM put.
# dominance = (put change - call change) / (|put change| + |call change|)
# More than +30% is put writing (bullish, sell the put). The strike one step
# below must also be put writing, at half that threshold.
# More than -30% is call writing (bearish, sell the call). The strike one step
# above must also be call writing, at half that threshold.
# A ratio sitting on the line does not vote. The same pair closes a spread.
OI: dict = {
    "min_ratio":            0.30,
    "neighbor_ratio":       0.15,    # half of min_ratio, on the first strike beside ATM
    "min_strikes":          1,       # the ATM call and the ATM put must both be present
}


# ══════════════════════════════════════════════════════════════════════════════
#  8. THE TRADE
# ══════════════════════════════════════════════════════════════════════════════
TRADE: dict = {
    "lots":                 _env_int("LOTS", 1),

    # Hedge is bought FIRST (so the short is never naked), then the ATM is sold.
    # The wing is the nearest OTM strike whose ask is strictly below this.
    # It can sit far from ATM. A ₹10 option is usually well outside the at-the-money pair.
    "hedge_max_premium":    10.0,
    "hedge_min_premium":    0.05,
    "hedge_search_steps":   40,          # 40 x 50 = 2,000 points from ATM

    # Stop is on the SOLD premium only. Sold at 120 -> stop when it trades at 170.
    # The hedge has no stop of its own. It is closed when the spread is closed.
    "sl_points":            50.0,

    # Target is an absolute premium, not a profit percent.
    # Sold at 120 -> target when that option's premium is back at 10.
    "target_premium":       10.0,

    # Do not sell an ATM option that is already at the target.
    "min_short_premium":    15.0,

    # 0 DTE only. Sell ATM when its bid is greater than this. Otherwise walk
    # ITM1, ITM2, ITM3 and sell the first bid that is greater than this.
    # A missing ATM quote does not fall through to an ITM strike.
    "zero_dte_min_premium": 100.0,
    "zero_dte_itm_steps":   3,
}


# ══════════════════════════════════════════════════════════════════════════════
#  9. EXECUTION — how a buy and a sell use the book
# ══════════════════════════════════════════════════════════════════════════════
# A BUY lifts the ask ladder until the order quantity is covered.
# A SELL hits the bid ladder the same way.
# The limit is the price of that covering level, so the size is priced off
# the depth instead of the top line alone.
EXECUTION: dict = {
    "product":                  "NRML",
    "variety":                  "regular",
    "entry_order_type":         "LIMIT",
    "entry_limit_ticks":        0,
    "fill_poll_sec":            0.6,
    "fill_timeout_sec":         5.0,
    "modify_attempts":          3,
    "market_fallback":          True,
    "exit_limit_ticks":         0,
    "exit_fill_timeout_sec":    3.0,
    "exit_modify_attempts":     4,
    "exit_market_fallback":     True,
    "hedge_first_on_entry":     True,
    "shorts_first_on_exit":     True,
    "rollback_on_partial_entry": True,
    "slice_above_freeze":       True,
    "slice_sleep_sec":          0.35,
    "use_iceberg":              False,
    "use_broker_margin_api":    False,

    # Backup broker stop on the SHORT leg only, wider than the bot's 50 points,
    # in case this process dies. Off in paper so the 50-point rule is the one
    # you are measuring.
    "protective_sl": {
        "enabled":              False,
        "order_type":           "SL-M",
        "extra_points":         15.0,
        "sl_limit_buffer_pct":  5.0,
    },

    "paper": {
        "slippage_ticks":        1,
        "assumed_spread_ticks":  6,
        "thin_book_extra_ticks": 2,    # remainder past the visible book
        "latency_ms":            250,
        "cost_per_order":        20.0,
        "min_depth_lots":        0,
    },
    "cost_per_order_live":      20.0,
}


# ══════════════════════════════════════════════════════════════════════════════
#  10. TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════
NOTIFY: dict = {
    "enabled":              True,
    "send_startup":         True,
    "send_shutdown":        True,
    "send_signals":         False,    # the 1-minute pass is collected, not posted
    "send_consolidated":    True,     # one combined note every 5 minutes
    "send_entries":         True,
    "send_exits":           True,
    "send_errors":          True,
    "send_daily_summary":   True,
    "max_msgs_per_min":     18,
    "retry_attempts":       3,
    "timeout_sec":          20,
}


# ══════════════════════════════════════════════════════════════════════════════
#  11. PATHS
# ══════════════════════════════════════════════════════════════════════════════
BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BASE_DIR, "state")
LOG_DIR   = os.path.join(BASE_DIR, "logs")

PATHS: dict = {
    "state_file":     os.path.join(STATE_DIR, "engine_state.json"),
    "day_stats_file": os.path.join(STATE_DIR, "day_stats.json"),
    "trade_journal":  os.path.join(STATE_DIR, "trades.csv"),
    "ledger_daily":   os.path.join(STATE_DIR, "ledger_daily.csv"),
    "ledger_monthly": os.path.join(STATE_DIR, "ledger_monthly.csv"),
    "log_dir":        LOG_DIR,
}

LOGGING: dict = {
    "level":      _env_str("LOG_LEVEL", "INFO").upper(),
    "console":    True,
    "file":       True,
    "tick_debug": False,
}


# ══════════════════════════════════════════════════════════════════════════════
#  12. HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def active_symbols() -> list:
    wanted = [s.strip().upper() for s in SYMBOL_FILTER.split(",") if s.strip()]
    out = []
    for name, cfg in SYMBOLS.items():
        if not cfg.get("enabled", False):
            continue
        if wanted and name.upper() not in wanted:
            continue
        out.append(name)
    return out


def is_live() -> bool:
    """Live orders require both the mode switch and the explicit confirmation."""
    return TRADING_MODE == "live" and LIVE_TRADING_CONFIRMED


def symbol_cfg(symbol: str) -> dict:
    return SYMBOLS[symbol]


def oi_span(minutes: int) -> str:
    """Label for how long the combined series has been running. Not a slice."""
    return f"{max(1, int(minutes))}m"


def validate() -> list:
    """Human-readable problems. Empty means the config can run."""
    errs = []

    if TRADING_MODE not in ("paper", "live"):
        errs.append(f"TRADING_MODE must be 'paper' or 'live', got {TRADING_MODE!r}")

    if TRADING_MODE == "live" and not LIVE_TRADING_CONFIRMED:
        errs.append(
            "TRADING_MODE=live but LIVE_TRADING_CONFIRMED is False — "
            "the engine will stay in paper mode. Set LIVE_TRADING_CONFIRMED=true "
            "only when you want real orders."
        )

    if active_symbols() != ["NIFTY"]:
        errs.append("This strategy trades NIFTY only. Set SYMBOLS=NIFTY and leave SENSEX out.")

    nifty = SYMBOLS.get("NIFTY") or {}
    tracked = int(nifty.get("chain_strikes") or 0)
    if tracked < 1:
        errs.append(
            "NIFTY chain_strikes must be at least 1 so the strike beside ATM is stored. "
            "7 keeps ATM and seven strikes either side."
        )

    if not (SESSION["market_open"] <= SESSION["analysis_start"] < SESSION["first_trade"]):
        errs.append("analysis_start must sit at or after the open and before the first trade.")
    if not (SESSION["first_trade"] < SESSION["last_entry"] < SESSION["hard_square_off"]):
        errs.append("first trade, last entry, and square-off must be in that order.")
    if SESSION["hard_square_off"] > SESSION["market_close"]:
        errs.append("square-off must be at or before the market close.")

    if TRADE["hedge_max_premium"] <= TRADE["hedge_min_premium"]:
        errs.append("hedge_max_premium must be above hedge_min_premium.")
    if TRADE["sl_points"] <= 0:
        errs.append("TRADE['sl_points'] must be > 0.")
    if TRADE["target_premium"] <= 0:
        errs.append("TRADE['target_premium'] must be > 0.")
    if TRADE["lots"] < 1:
        errs.append("TRADE['lots'] must be at least 1.")
    if TRADE["min_short_premium"] <= TRADE["target_premium"]:
        errs.append("min_short_premium should sit above target_premium, or the target is already hit.")
    if TRADE["zero_dte_min_premium"] <= TRADE["target_premium"]:
        errs.append("zero_dte_min_premium should sit above target_premium.")
    if int(TRADE["zero_dte_itm_steps"]) < 1:
        errs.append("zero_dte_itm_steps must be at least 1.")

    if ENGINE["scan_minutes"] <= 0:
        errs.append("ENGINE['scan_minutes'] must be > 0.")
    if int(ENGINE["report_minutes"]) < int(ENGINE["scan_minutes"]):
        errs.append("ENGINE['report_minutes'] must be at least the scan interval.")
    ratio = float(OI["min_ratio"])
    neighbor = float(OI["neighbor_ratio"])
    if not 0 < ratio < 1:
        errs.append("OI['min_ratio'] must sit between 0 and 1 (0.30 means more than 30%).")
    if not 0 < neighbor < ratio:
        errs.append("OI['neighbor_ratio'] must sit between 0 and min_ratio.")
    if abs(neighbor * 2 - ratio) > 1e-9:
        errs.append("OI['neighbor_ratio'] must be half of min_ratio (15% when dominance is 30%).")
    if int(OI["min_strikes"]) < 1:
        errs.append("OI['min_strikes'] must be at least 1.")
    if int(ENGINE["min_open_samples"]) < 2:
        errs.append("ENGINE['min_open_samples'] must be at least 2.")
    if int(ENGINE["flip_cooldown_min"]) < 1:
        errs.append("ENGINE['flip_cooldown_min'] must be at least 1.")
    if float(ENGINE["scan_retry_sec"]) <= 0:
        errs.append("ENGINE['scan_retry_sec'] must be > 0.")

    if EXECUTION["product"] not in ("NRML", "MIS"):
        errs.append("EXECUTION['product'] must be NRML or MIS.")

    if NOTIFY["enabled"] and not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        errs.append("Telegram is on but TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are missing in .env")

    return errs
