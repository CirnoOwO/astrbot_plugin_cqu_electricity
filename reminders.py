"""One read-only eligibility calculation for queueing and delivery."""
from datetime import datetime
from decimal import Decimal

from .dashboard import dashboard_room_input
from .dashboard_model import make_model
from .settlement import BJ, _time


def reminder_snapshot(repo, room, *, now=None, window_minutes=15):
    now = now or datetime.now(BJ)
    data = dashboard_room_input(repo, room, now=now, window_minutes=window_minutes)
    rows = [r for r in data["captures"] if _time(r["finished_at"]) <= now]
    latest = max(rows, key=lambda r: (_time(r["finished_at"]), r["id"]), default=None)
    if not latest or latest["status"] != "success" or _time(latest["finished_at"]).date() != now.astimezone(BJ).date():
        return None
    actual = latest["reading"]["total_balance_yuan"]
    try:
        if not Decimal(actual).is_finite():
            return None
    except (ValueError, TypeError, ArithmeticError):
        return None
    state = dict(query_id=latest["id"], actual_balance=actual, days=None, estimated_balance=None)
    try:
        model = make_model(data)
    except (ValueError, TypeError, ArithmeticError, KeyError):
        # An invalid forecast must not suppress a valid actual-balance alert.
        return state
    if not model["fresh"] or model["balance"] is None or Decimal(model["balance"]) != Decimal(actual):
        return state
    morning = model.get("settlement")
    if morning and morning["fresh"]:
        state["estimated_balance"] = morning.get("estimated_balance_yuan")
    if model["forecast"]:
        state["days"] = model["forecast"]["days"]
    return state


def qualifies(sub, state):
    if state is None:
        return False
    actual = Decimal(state["actual_balance"])
    if sub["reminder_type"] == "balance":
        return actual < Decimal(sub["low_balance_threshold"])
    if actual <= 0:
        return True
    projected = state["estimated_balance"]
    if projected is not None and Decimal(projected) <= 0:
        return True
    return state["days"] is not None and Decimal(state["days"]) < Decimal(sub["days_threshold"])
