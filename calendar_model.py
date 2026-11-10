"""Read-only calendar reports; a labelled day is a complete afternoon interval."""
from __future__ import annotations

import calendar
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal

from .settlement import BJ, MONEY_FIELDS, _number, _time, afternoon_sample


def report_month(argument=None, *, now=None):
    """Default to this calendar month, never a rolling 30-day window."""
    today = (now or datetime.now(BJ)).astimezone(BJ).date()
    if argument is None or argument == "本月":
        return today.strftime("%Y-%m")
    if argument == "上月":
        return (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", argument):
        raise ValueError("用法：/电费 月报 [本月、上月或 YYYY-MM]")
    try:
        first = date.fromisoformat(argument + "-01")
    except ValueError:
        raise ValueError("月份格式应为 YYYY-MM，例如 2026-10。") from None
    if first > today:
        raise ValueError("该月份尚未开始。")
    return argument


def calendar_input(repo, identity, *, scope="cycle", month_key=None, now=None,
                   window_minutes=15):
    """The only source of the room ID is the current sender's global binding."""
    binding = repo.binding(identity)
    if not binding:
        return None
    now = now or datetime.now(BJ)
    if scope not in {"cycle", "month"}:
        raise ValueError("Unknown calendar scope")
    start = end = None
    if scope == "month":
        month_key = report_month(month_key, now=now)
        first = date.fromisoformat(month_key + "-01")
        following = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        start = datetime.combine(first, datetime.min.time(), tzinfo=BJ)
        # The last labelled day needs the following day's afternoon observation.
        end = datetime.combine(following + timedelta(days=1), datetime.min.time(), tzinfo=BJ)
    return {"room": {"campus": binding["campus"], "building": binding["building"],
                     "account": binding["room_number"]},
            "rows": repo.calendar_runs(binding["room_id"], now=now, start=start, end=end),
            "as_of": now.isoformat(), "scope": scope, "month_key": month_key,
            "window_minutes": window_minutes}


def _increased(a, b):
    return any(_number(a.get(k)) is not None and _number(b.get(k)) is not None
               and _number(b[k]) > _number(a[k]) for k in MONEY_FIELDS)


def make_calendar_model(source):
    now = _time(source["as_of"])
    today = now.date()
    scope = source["scope"]
    if scope not in {"cycle", "month"}:
        raise ValueError("Unknown calendar scope")
    rows = [dict(r, t=_time(r["finished_at"])) for r in source["rows"]
            if r["status"] == "success" and _time(r["finished_at"]) <= now]
    rows.sort(key=lambda r: (r["t"], r["id"]))
    # Normalize/validate all emitted numbers, including balances, before JSON.
    for row in rows:
        for key in (*MONEY_FIELDS, "unit_price_yuan", "meter_reading_kwh"):
            value = _number(row.get(key))
            row[key] = str(value) if value is not None else None
    increases = [b for a, b in zip(rows, rows[1:]) if _increased(a, b)]
    anchor = increases[-1] if scope == "cycle" and increases else rows[0] if rows else None
    if scope == "month":
        key = report_month(source["month_key"], now=now)
        start = date.fromisoformat(key + "-01")
        end = start.replace(day=calendar.monthrange(start.year, start.month)[1])
    else:
        start = anchor["t"].date() if anchor else today
        end = today
    buckets = defaultdict(list)
    for row in rows:
        buckets[row["t"].date()].append(row)
    selected = []
    for day, items in sorted(buckets.items()):
        fixed = [r for r in items if afternoon_sample(r, day, source.get("window_minutes", 15))]
        row = min(fixed, key=lambda r: (_time(r["started_at"]), r["id"])) if fixed else items[-1]
        selected.append(dict(row, fixed=bool(fixed)))

    inferred = source["room"]["campus"] == "shapingba"
    values = {}
    # Selected endpoints are ordered successes; inspect *all* successes between
    # them so a transient top-up, subsidy, price change or meter reset isn't lost.
    positions = {r["id"]: i for i, r in enumerate(rows)}
    for a, b in zip(selected, selected[1:]):
        day = a["t"].date()
        if not start <= day <= end or day >= today:
            continue
        trail = rows[positions[a["id"]]:positions[b["id"]] + 1]
        hours = Decimal(str((b["t"] - a["t"]).total_seconds())) / 3600
        reason, value = None, None
        if scope == "cycle" and anchor and (a["t"], a["id"]) < (anchor["t"], anchor["id"]):
            reason = "不足整日"
        elif (b["t"].date() - day).days != 1:
            reason = "缺少记录"
        elif not a["fixed"] or not b["fixed"] or not Decimal("23.5") <= hours <= Decimal("24.5"):
            reason = "不足整日"
        elif inferred:
            prices = [_number(r["unit_price_yuan"]) for r in trail]
            balances = [_number(r["total_balance_yuan"]) for r in trail]
            if any(p is None or p <= 0 or p != prices[-1] for p in prices) or any(v is None for v in balances):
                reason = "无法估算"
            elif any(_increased(x, y) for x, y in zip(trail, trail[1:])):
                reason = "余额增加"
            else:
                value = (balances[0] - balances[-1]) / prices[-1]
        else:
            meters = [_number(r["meter_reading_kwh"]) for r in trail]
            if any(v is None for v in meters):
                reason = "缺少记录"
            elif any(y < x for x, y in zip(meters, meters[1:])):
                reason = "读数变化"
            else:
                value = meters[-1] - meters[0]
        values[day] = dict(day=day.isoformat(), value=str(value) if value is not None else None,
                           status=reason or "recorded", from_id=a["id"], to_id=b["id"],
                           from_time=a["t"].isoformat(), to_time=b["t"].isoformat(), hours=str(hours),
                           balance=b["total_balance_yuan"] if value is not None else None,
                           balance_captured_at=b["t"].isoformat() if value is not None else None,
                           balance_increase=any(_increased(x, y) for x, y in zip(trail, trail[1:])))
    days = []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        status = "尚未到日期" if day > today else "待后续采集" if day == today else "缺少记录"
        item = values.get(day, dict(day=day.isoformat(), value=None, status=status,
                                   balance=None, balance_captured_at=None, balance_increase=False))
        days.append(dict(item, start=day == start and scope == "cycle", today=day == today,
                         future=day > today))
    valid = [d for d in days if d["value"] is not None]
    period = [r for r in rows if start <= r["t"].date() <= end]
    if scope == "cycle" and anchor:
        period = [r for r in period if (r["t"], r["id"]) >= (anchor["t"], anchor["id"])]
    first, latest = (period[0], period[-1]) if period else (None, None)
    net = None
    if latest and first["total_balance_yuan"] is not None and latest["total_balance_yuan"] is not None:
        net = str(_number(latest["total_balance_yuan"]) - _number(first["total_balance_yuan"]))
    months, month = [], start.replace(day=1)
    while month <= end:
        months.append(dict(year=month.year, month=month.month, first_weekday=month.weekday(),
                           days_in_month=calendar.monthrange(month.year, month.month)[1]))
        # date.max's December still has a valid report; don't overflow to year 10000.
        if month.year == end.year and month.month == end.month:
            break
        month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    return dict(schema_version=1, room=dict(source["room"], campus_label="沙坪坝" if inferred else "虎溪"),
                scope=scope, as_of=now.isoformat(), start_day=start.isoformat(), end_day=end.isoformat(),
                start_reason="余额增加" if scope == "cycle" and increases else "首次记录",
                anchor_id=anchor["id"] if anchor else None, anchor_time=anchor["t"].isoformat() if anchor else None,
                start_balance=first["total_balance_yuan"] if first else None,
                balance=latest["total_balance_yuan"] if latest else None, balance_net_change=net,
                captured_at=latest["t"].isoformat() if latest else None,
                total_kwh=str(sum((Decimal(d["value"]) for d in valid), Decimal(0))) if valid else None,
                month_key=start.strftime("%Y-%m") if scope == "month" else None,
                title_label="月度用电日历" if scope == "month" else "本轮用电日历",
                range_label=f"{start.year}年{start.month}月" if scope == "month" else f"{start.year}年{start.month}月{start.day}日 — {end.year}年{end.month}月{end.day}日",
                basis_label="自然月统计" if scope == "month" else "从最近一次余额增加开始" if increases else "从首次记录开始",
                balance_label="该月最后记录余额" if scope == "month" and end < today else "最新平台余额",
                recorded_days=len(valid), inferred=inferred, days=days, months=months)


def calendar_pages(model):
    """One readable image per month; the first image owns the overall summary."""
    count = len(model["months"])
    return [dict(model, months=[month], page_index=i + 1, page_count=count)
            for i, month in enumerate(model["months"])]


def calendar_summary(model):
    total = "无完整日用电记录" if model["total_kwh"] is None else (
        f"已记录{'估算' if model['inferred'] else ''}用电 {Decimal(model['total_kwh']):.2f} 度 · {model['recorded_days']} 天")
    balance = "无余额记录" if model["balance"] is None else (
        f"{model['balance_label']}：{Decimal(model['balance']):.2f} 元"
        + (f"（{_time(model['captured_at']):%m月%d日 %H:%M}）" if model["captured_at"] else ""))
    return (f"📅 {model['room']['building']} · {model['room']['account']}\n"
            f"{model['title_label']} · {model['range_label']}\n{total}\n{balance}\n"
            "用量标在相邻下午采集的前一天，不等同自然日用电。")
