"""Pure, conservative morning estimates. Never change a stored reading."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

BJ = timezone(timedelta(hours=8))
MONEY_FIELDS = ("total_balance_yuan", "cash_balance_yuan", "subsidy_kwh", "subsidy_balance_yuan")


def _time(value):
    value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        raise ValueError("Timestamps require a timezone")
    return value.astimezone(BJ)


def _number(value):
    if value is None:
        return None
    value = Decimal(value)
    if not value.is_finite():
        raise ValueError("Non-finite reading")
    return value


def afternoon_sample(row, day, window_minutes=15):
    """Recognize persisted afternoon batches, including extended spread windows."""
    due = datetime.fromisoformat(str(day)).replace(hour=14, tzinfo=BJ)
    began = _time(row.get("started_at") or row["finished_at"])
    same_day = _time(row["finished_at"]).date() == due.date()
    return same_day and due <= began and (
        row.get("capture_slot") == "14:00" or began < due + timedelta(minutes=window_minutes))


def _settled_charge(readings, cost):
    """Recognize a debit, without treating a recharge or partial debit as paid.

    The platform has no settlement flag. Require a positive total debit close
    to the meter cost (within 5% or five cents), and no observed component gain
    or disappearing component. This tolerates platform rounding/synchronization
    differences, but does not reconstruct unobserved transactions.
    """
    for field in MONEY_FIELDS:
        values = [_number(row.get(field)) for row in readings]
        if field in ("total_balance_yuan", "cash_balance_yuan") and any(v is None for v in values):
            return False
        for before, after in zip(values, values[1:]):
            if before is None or after is None:
                if before != after:
                    return False
            elif after > before:
                return False
    debit = _number(readings[0]["total_balance_yuan"]) - _number(readings[-1]["total_balance_yuan"])
    return debit > 0 and abs(debit - cost) <= max(Decimal("0.05"), cost * Decimal("0.05"))


def morning_snapshot(rows, now, *, window_minutes=15):
    """Latest success started today between 09:00 and 14:00, if available.

    Both paths require yesterday's first afternoon success, a stable positive
    price and uninterrupted positive meter growth. Unchanged money permits one
    hypothetical deduction; an approximately matching debit uses actual money.
    """
    now = now.astimezone(BJ)
    morning = now.replace(hour=9, minute=0, second=0, microsecond=0)
    due = morning.replace(hour=14)
    if not morning <= now < due:
        return None
    finished = [r for r in rows if r.get("finished_at") and _time(r["finished_at"]) <= now]
    successes = sorted((r for r in finished if r["status"] == "success"),
                       key=lambda r: (_time(r["finished_at"]), r["id"]))
    candidates = [r for r in successes if morning <= _time(r["started_at"]) < due]
    if not candidates:
        return None
    current = candidates[-1]
    reading = lambda row: row.get("reading", row)
    actual = reading(current)
    base_due = due - timedelta(days=1)
    bases = [r for r in successes if afternoon_sample(r,base_due.date(),window_minutes)]
    baseline = min(bases, key=lambda r: (_time(r["started_at"]), r["id"])) if bases else None
    later_failures = [r for r in finished if r["status"] != "success"
                      and (_time(r["finished_at"]), r["id"]) > (_time(current["finished_at"]), current["id"])]
    latest_failure = max(later_failures, key=lambda r: (_time(r["finished_at"]), r["id"]), default=None)
    result = {"state": "unavailable", "reading_id": current["id"],
              "baseline_id": baseline["id"] if baseline else None,
              "captured_at": current["finished_at"], "fresh": not later_failures,
              "refresh_error": (latest_failure.get("error_code") or latest_failure["status"].upper()) if latest_failure else None,
              "platform_balance_yuan": str(_number(actual["total_balance_yuan"])),
              "estimated_balance_yuan": None, "pending_cost_yuan": None,
              "estimated": True, "usage": None, "unavailable_reason": None}
    if baseline is None:
        result["unavailable_reason"] = "missing_previous_afternoon"
        return result
    trail = [baseline] + [r for r in successes if
            (_time(baseline["finished_at"]), baseline["id"]) < (_time(r["finished_at"]), r["id"])
            <= (_time(current["finished_at"]), current["id"])]
    meters = [_number(reading(r).get("meter_reading_kwh")) for r in trail]
    if any(value is None for value in meters):
        result["unavailable_reason"] = "missing_meter"
        return result
    if any(b < a for a, b in zip(meters, meters[1:])):
        result["unavailable_reason"] = "meter_reset"
        return result
    delta = meters[-1] - meters[0]
    price = _number(actual.get("unit_price_yuan"))
    cost = delta * price if price is not None and price > 0 else None
    result["usage"] = {"available": True, "kwh": str(delta),
                       "cost_yuan": str(cost) if cost is not None else None,
                       "reference_day": base_due.date().isoformat(),
                       "interval_hours": round((_time(current["finished_at"]) - _time(baseline["finished_at"])).total_seconds() / 3600, 2),
                       "estimated": True, "method": "previous_afternoon_to_morning_meter_difference"}
    if later_failures:
        result.update(state="refresh_failed", unavailable_reason="later_capture_failed")
    elif delta <= 0:
        result["unavailable_reason"] = "meter_not_updated"
    elif price is None or price <= 0 or any(_number(reading(r).get("unit_price_yuan")) != price for r in trail):
        result["unavailable_reason"] = "missing_or_changed_price"
    elif any(_number(reading(r).get(field)) != _number(reading(baseline).get(field))
             for r in trail for field in MONEY_FIELDS):
        if _settled_charge([reading(r) for r in trail], cost):
            result.update(state="settled", estimated=False)
        else:
            result.update(state="balance_changed", unavailable_reason="monetary_components_changed")
    else:
        # Round the hypothetical charge once, then subtract it. This keeps the
        # displayed operands and the forecast balance exactly consistent.
        charge = cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        projected = _number(actual["total_balance_yuan"]) - charge
        result.update(state="possibly_unsettled", pending_cost_yuan=f"{charge:.2f}",
                      estimated_balance_yuan=f"{projected:.2f}")
    return result
