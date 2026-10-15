"""
data/kite_gateway.py
====================
The one place that touches the Kite REST API. Everything is routed through the
rate limiter, and every method degrades gracefully (returns empty/None) instead
of raising — except TokenException, which is escalated because only a fresh
login can fix it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from kiteconnect import KiteConnect
from kiteconnect import exceptions as kite_exc

import config
from core.logging_setup import get
from data.rate_limiter import kite_call, kite_order_call

log = get("kite")


class TokenExpired(RuntimeError):
    """Raised when the Kite access token is no longer valid."""


class KiteGateway:
    def __init__(self) -> None:
        if not config.ZERODHA_API_KEY:
            raise RuntimeError("ZERODHA_API_KEY missing — check .env")
        self.kite = KiteConnect(api_key=config.ZERODHA_API_KEY)
        if config.ZERODHA_ACCESS_TOKEN:
            self.kite.set_access_token(config.ZERODHA_ACCESS_TOKEN)
        self.profile_name = ""
        self.user_id = config.ZERODHA_USER_ID

    # ─── Session ──────────────────────────────────────────────────────────────

    def check_connection(self) -> bool:
        try:
            profile = kite_call(self.kite.profile)
            self.profile_name = profile.get("user_name", "")
            self.user_id = profile.get("user_id", self.user_id)
            log.info("Kite connected as %s (%s)", self.profile_name, self.user_id)
            return True
        except kite_exc.TokenException as exc:
            raise TokenExpired(str(exc)) from exc
        except Exception as exc:
            log.error("Kite connection check failed: %s", exc)
            return False

    def available_margin(self) -> float:
        """Free cash usable for F&O (live mode sizing)."""
        try:
            data = kite_call(self.kite.margins, "equity")
            available = (data or {}).get("available", {})
            return float(available.get("live_balance", available.get("cash", 0.0)) or 0.0)
        except kite_exc.TokenException as exc:
            raise TokenExpired(str(exc)) from exc
        except Exception as exc:
            log.warning("Could not read margins: %s", exc)
            return 0.0

    # ─── Reference data ───────────────────────────────────────────────────────

    def instruments(self, exchange: str) -> List[Dict[str, Any]]:
        try:
            rows = kite_call(self.kite.instruments, exchange)
            log.info("Downloaded %d instruments for %s", len(rows or []), exchange)
            return rows or []
        except kite_exc.TokenException as exc:
            raise TokenExpired(str(exc)) from exc
        except Exception as exc:
            log.error("instruments(%s) failed: %s", exchange, exc)
            return []

    def ltp(self, keys: List[str]) -> Dict[str, Any]:
        if not keys:
            return {}
        try:
            return kite_call(self.kite.ltp, keys) or {}
        except kite_exc.TokenException as exc:
            raise TokenExpired(str(exc)) from exc
        except Exception as exc:
            log.error("ltp(%s) failed: %s", keys[:3], exc)
            return {}

    def quote(self, keys: List[str]) -> Dict[str, Any]:
        """Full quotes, automatically batched to Kite's 500-instrument limit."""
        out: Dict[str, Any] = {}
        for start in range(0, len(keys), 500):
            batch = keys[start:start + 500]
            try:
                out.update(kite_call(self.kite.quote, batch) or {})
            except kite_exc.TokenException as exc:
                raise TokenExpired(str(exc)) from exc
            except Exception as exc:
                log.error("quote batch at %d failed: %s", start, exc)
        return out

    def historical(
        self,
        token: int,
        from_dt: datetime,
        to_dt: datetime,
        interval: str,
        oi: bool = False,
    ) -> List[Dict[str, Any]]:
        try:
            return kite_call(
                self.kite.historical_data,
                instrument_token=token,
                from_date=from_dt,
                to_date=to_dt,
                interval=interval,
                continuous=False,
                oi=oi,
            ) or []
        except kite_exc.TokenException as exc:
            raise TokenExpired(str(exc)) from exc
        except Exception as exc:
            log.warning("historical(%s, %s) failed: %s", token, interval, exc)
            return []

    # ─── Margins ──────────────────────────────────────────────────────────────

    def basket_margin(self, orders: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """
        Real SPAN+exposure requirement for an exact basket, including the hedge
        benefit. This is a read-only API so it is safe (and used) in paper mode.
        """
        if not orders:
            return None
        try:
            result = kite_call(
                self.kite.basket_order_margins,
                orders,
                consider_positions=True,
                mode="compact",
            )
            return result
        except kite_exc.TokenException as exc:
            raise TokenExpired(str(exc)) from exc
        except Exception as exc:
            log.warning("basket_order_margins failed (%s) — using fallback estimate", exc)
            return None

    # ─── Orders ───────────────────────────────────────────────────────────────

    def place_order(self, **params) -> Optional[str]:
        try:
            return kite_order_call(self.kite.place_order, **params)
        except kite_exc.TokenException as exc:
            raise TokenExpired(str(exc)) from exc
        except Exception as exc:
            log.error("place_order failed (%s): %s", params.get("tradingsymbol"), exc)
            return None

    def modify_order(self, order_id: str, critical: bool = False, **params) -> bool:
        try:
            kite_order_call(
                self.kite.modify_order,
                variety=params.pop("variety", config.EXECUTION["variety"]),
                order_id=order_id,
                critical=critical,
                **params,
            )
            return True
        except kite_exc.TokenException as exc:
            raise TokenExpired(str(exc)) from exc
        except Exception as exc:
            log.error("modify_order %s failed: %s", order_id, exc)
            return False

    def cancel_order(self, order_id: str, variety: Optional[str] = None) -> bool:
        try:
            kite_order_call(
                self.kite.cancel_order,
                variety=variety or config.EXECUTION["variety"],
                order_id=order_id,
                critical=True,
            )
            return True
        except kite_exc.TokenException as exc:
            raise TokenExpired(str(exc)) from exc
        except Exception as exc:
            log.error("cancel_order %s failed: %s", order_id, exc)
            return False

    def order_history(self, order_id: str) -> List[Dict[str, Any]]:
        try:
            return kite_order_call(self.kite.order_history, order_id, critical=True) or []
        except kite_exc.TokenException as exc:
            raise TokenExpired(str(exc)) from exc
        except Exception as exc:
            log.warning("order_history %s failed: %s", order_id, exc)
            return []

    def orders(self) -> List[Dict[str, Any]]:
        try:
            return kite_call(self.kite.orders) or []
        except kite_exc.TokenException as exc:
            raise TokenExpired(str(exc)) from exc
        except Exception as exc:
            log.warning("orders() failed: %s", exc)
            return []

    def positions(self) -> Dict[str, Any]:
        try:
            return kite_call(self.kite.positions) or {}
        except kite_exc.TokenException as exc:
            raise TokenExpired(str(exc)) from exc
        except Exception as exc:
            log.warning("positions() failed: %s", exc)
            return {}
