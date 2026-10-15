"""
main.py
=======
    python3 main.py                 paper trading (default)
    python3 main.py --check         config, Kite, NIFTY expiry
    python3 main.py --dry-run       one minute of OI, no orders
    python3 main.py --mode live     real orders, and only if LIVE_TRADING_CONFIRMED=true

Live trading needs both switches. One flag cannot send a real order by itself.
"""

from __future__ import annotations

import argparse
import os
import sys


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="NIFTY option-selling bot")
    parser.add_argument("--mode", choices=["paper", "live"], help="override TRADING_MODE")
    parser.add_argument("--lots", type=int, help="override TRADE lots")
    parser.add_argument("--log-level", help="DEBUG / INFO / WARNING")
    parser.add_argument("--check", action="store_true", help="validate config and connectivity, then exit")
    parser.add_argument("--dry-run", action="store_true", help="one minute of OI, no orders")
    return parser.parse_args()


_args = _parse_args()
if _args.mode:
    os.environ["TRADING_MODE"] = _args.mode
if _args.lots:
    os.environ["LOTS"] = str(_args.lots)
if _args.log_level:
    os.environ["LOG_LEVEL"] = _args.log_level

import config                                                     # noqa: E402
from core import clock                                            # noqa: E402
from core.logging_setup import setup                              # noqa: E402

log = setup("main")


BANNER = r"""
╔══════════════════════════════════════════════════════════════════╗
║   NIFTY OPTION SELLING  ·  paper until you arm live              ║
║   Skip 10m  ·  analyse 15m from 09:26  ·  trade from 09:41       ║
╚══════════════════════════════════════════════════════════════════╝
"""


def _print_config_summary() -> None:
    mode = "LIVE" if config.is_live() else "PAPER"
    log.info("Mode            : %s", mode)
    if config.TRADING_MODE == "live" and not config.LIVE_TRADING_CONFIRMED:
        log.warning("TRADING_MODE=live but LIVE_TRADING_CONFIRMED is not true — staying in PAPER")
    log.info("Symbol          : %s", ", ".join(config.active_symbols()) or "none")
    log.info("Lots            : %d", config.TRADE["lots"])
    log.info(
        "Signal          : ATM dominance above 30%% and the nearby strike above 15%% since %s",
        config.SESSION["analysis_start"],
    )
    log.info(
        "Strikes         : ATM ±%d       scan every %d min       note every %d min",
        config.SYMBOLS["NIFTY"]["chain_strikes"],
        config.ENGINE["scan_minutes"],
        config.ENGINE["report_minutes"],
    )
    log.info(
        "Flip cooldown   : %d min, then the signal is read again",
        config.ENGINE["flip_cooldown_min"],
    )
    log.info(
        "Trade window    : %s to %s    square-off %s",
        config.SESSION["first_trade"], config.SESSION["last_entry"],
        config.SESSION["hard_square_off"],
    )
    log.info(
        "Stop / target   : +%.0f on the sold premium, target when premium ≤ %.0f. Hedge has no stop.",
        config.TRADE["sl_points"], config.TRADE["target_premium"],
    )
    log.info(
        "Hedge           : nearest OTM ask under ₹%.0f",
        config.TRADE["hedge_max_premium"],
    )
    log.info("Now (IST)       : %s", clock.now().strftime("%Y-%m-%d %H:%M:%S"))


def run_check() -> int:
    from data.instruments import InstrumentMaster
    from data.kite_gateway import KiteGateway, TokenExpired

    log.info("CONFIGURATION CHECK")
    problems = config.validate()
    for problem in problems:
        log.error("• %s", problem)
    if not problems:
        log.info("Configuration OK")

    missing = [name for name, value in {
        "ZERODHA_API_KEY": config.ZERODHA_API_KEY,
        "ZERODHA_ACCESS_TOKEN": config.ZERODHA_ACCESS_TOKEN,
        "TELEGRAM_BOT_TOKEN": config.TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": config.TELEGRAM_CHAT_ID,
    }.items() if not value]
    if missing:
        log.error("Missing in .env: %s", ", ".join(missing))
        return 1

    try:
        gateway = KiteGateway()
        if not gateway.check_connection():
            log.error("Kite connection failed — run python3 login_zerodha.py")
            return 1
    except TokenExpired:
        log.error("Access token expired — run python3 login_zerodha.py")
        return 1
    except Exception as exc:
        log.error("Could not create the Kite client: %s", exc)
        return 1

    instruments = InstrumentMaster(gateway)
    if not instruments.ensure_loaded():
        log.error("Instrument master could not be loaded")
        return 1

    symbol = "NIFTY"
    if not instruments.has_symbol(symbol):
        log.error("NIFTY contracts were not found")
        return 1
    expiry = instruments.select_expiry(symbol)
    log.info(
        "NIFTY lot=%d expiry=%s (%sd) spot_token=%s",
        instruments.lot_size(symbol), expiry,
        clock.dte(expiry) if expiry else "?",
        instruments.spot_token(symbol),
    )
    return 0 if not problems else 1


def run_dry() -> int:
    from engine import TradingEngine

    engine = TradingEngine()
    if not engine.start():
        return 1
    try:
        if not clock.is_market_open():
            log.info("Market is closed. Dry run connected, but there is no live book to read.")
            return 0
        end = clock.now().replace(second=0, microsecond=0)
        engine._on_scan(end, clock.now())
        if not engine.samples:
            log.error("The scan did not produce a reading")
            return 1
        log.info("Dry run finished — nothing was ordered")
        return 0
    finally:
        engine.stop("dry run finished")
        engine.shutdown()


def main() -> int:
    print(BANNER)
    _print_config_summary()
    if _args.check:
        return run_check()
    if _args.dry_run:
        return run_dry()

    from engine import TradingEngine

    if config.is_live():
        log.critical("LIVE TRADING IS ON — REAL ORDERS WILL BE PLACED")

    engine = TradingEngine()
    if not engine.start():
        log.critical("Engine failed to start")
        return 1
    engine.install_signal_handlers()
    engine.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
