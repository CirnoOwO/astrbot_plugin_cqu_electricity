import asyncio
import copy
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from cqu_plugin.calendar_model import (BJ, calendar_input, calendar_pages, calendar_summary,
                                       make_calendar_model, report_month)
from cqu_plugin.dashboard import calendar_html
from cqu_plugin.models import Room
from test_history_captures import env, capture, IDENTITY
from test_plugin import plugin, bind, Event


def row(day, meter, cash, *, hour="14:01:00", slot="14:00", **extra):
    at = f"{day}T{hour}+08:00"
    return dict(id=int(day.replace("-", "")) * 100000 + int(hour.replace(":", "")),
                started_at=at, finished_at=at, status="success", capture_slot=slot,
                total_balance_yuan=str(cash), cash_balance_yuan=str(cash),
                subsidy_balance_yuan="0", subsidy_kwh="0", unit_price_yuan="0.54",
                meter_reading_kwh=str(meter) if meter is not None else None, **extra)


def data(rows, *, scope="month", month="2026-09", campus="huxi", as_of="2026-10-03T16:00:00+08:00"):
    return dict(room=dict(campus=campus, building="竹园5栋", account="B5344"),
                scope=scope, month_key=month, as_of=as_of, rows=rows)


def by_day(model):
    return {d["day"]: d for d in model["days"]}


def test_month_boundary_end_sample_balance_and_default_current_month():
    source = data([row("2026-09-30", 100, 10), row("2026-10-01", 105, "7.30")])
    before = copy.deepcopy(source)
    m = make_calendar_model(source)
    day = by_day(m)["2026-09-30"]
    assert Decimal(day["value"]) == 5 and day["balance"] == "7.30"
    assert day["balance_captured_at"].startswith("2026-10-01")
    assert m["balance"] == "10" and m["balance_label"] == "该月最后记录余额"
    assert source == before
    assert report_month(now=datetime(2026, 1, 1, tzinfo=BJ)) == "2026-01"
    assert report_month("上月", now=datetime(2026, 1, 1, tzinfo=BJ)) == "2025-12"
    assert report_month("本月", now=datetime(2026, 1, 1, tzinfo=BJ)) == "2026-01"


@pytest.mark.parametrize("argument", ["2026-13", "2026-2", "2027-01", "1001", "0000-01"])
def test_month_validation(argument):
    with pytest.raises(ValueError):
        report_month(argument, now=datetime(2026, 10, 3, tzinfo=BJ))


def test_recharge_badge_paired_with_interval_end_not_calendar_capture_day():
    m = make_calendar_model(data([row("2026-10-01", 100, 10), row("2026-10-02", 105, 50),
                                  row("2026-10-03", 111, "46.76")], month="2026-10"))
    days = by_day(m)
    assert days["2026-10-01"]["balance_increase"]
    assert days["2026-10-01"]["balance"] == "50"
    assert not days["2026-10-02"]["balance_increase"]
    cycle = make_calendar_model(data([row("2026-10-01", 100, 10), row("2026-10-02", 105, 50),
                                      row("2026-10-03", 111, "46.76")], scope="cycle"))
    assert cycle["start_day"] == "2026-10-02" and Decimal(cycle["total_kwh"]) == 6


@pytest.mark.parametrize("kind", ["gap", "manual", "long", "reset", "missing"])
def test_invalid_intervals_not_filled_or_assigned_to_one_day(kind):
    a, b = row("2026-10-01", 100, 10), row("2026-10-02", 105, "7.30")
    if kind == "gap":
        b = row("2026-10-03", 105, "7.30")
    elif kind == "manual":
        a = row("2026-10-01", 100, 10, hour="21:00:00", slot=None)
    elif kind == "long":
        b = row("2026-10-02", 105, "7.30", hour="15:01:00")
    elif kind == "reset":
        b["meter_reading_kwh"] = "2"
    else:
        b["meter_reading_kwh"] = None
    m = make_calendar_model(data([a, b], month="2026-10"))
    assert m["total_kwh"] is None and by_day(m)["2026-10-01"]["balance"] is None


def test_valid_zero_and_extended_batch_first_success_selection():
    rows = [row("2026-10-01", 100, 10, hour="14:20:00"),
            row("2026-10-02", 100, 10, hour="14:21:00"),
            row("2026-10-02", 105, "7.3", hour="15:00:00", slot=None)]
    m = make_calendar_model(data(rows, month="2026-10"))
    assert Decimal(m["total_kwh"]) == 0 and m["recorded_days"] == 1
    assert by_day(m)["2026-10-01"]["balance"] == "10"


@pytest.mark.parametrize("invalid", [None, "price", "recharge", "subsidy", "meter_irrelevant"])
def test_shapingba_estimation_checks_intermediate_successes(invalid):
    rows = [row("2026-10-01", None, 10), row("2026-10-02", None, "7.30")]
    if invalid == "price":
        rows.insert(1, row("2026-10-01", None, 9, hour="21:00:00", slot=None))
        rows[1]["unit_price_yuan"] = "0.60"
    elif invalid == "recharge":
        rows.insert(1, row("2026-10-01", None, 15, hour="21:00:00", slot=None))
    elif invalid == "subsidy":
        rows.insert(1, row("2026-10-01", None, 9, hour="21:00:00", slot=None))
        rows[1]["subsidy_balance_yuan"] = "2"
    elif invalid == "meter_irrelevant":
        rows[1]["meter_reading_kwh"] = "1"
    m = make_calendar_model(data(rows, month="2026-10", campus="shapingba"))
    assert m["inferred"]
    if invalid in {"price", "recharge", "subsidy"}:
        assert m["total_kwh"] is None
    else:
        assert Decimal(m["total_kwh"]) == 5


def test_empty_month_cycle_and_future_captures_are_unknown():
    future = row("2026-10-04", 100, 10)
    m = make_calendar_model(data([future]))
    assert m["balance"] is None and m["total_kwh"] is None and m["captured_at"] is None
    assert len(m["days"]) == 30
    cycle = make_calendar_model(data([], scope="cycle"))
    assert cycle["balance"] is None and cycle["total_kwh"] is None
    assert "无完整日用电记录" in calendar_summary(cycle)
    assert "无余额记录" in calendar_summary(cycle)


def test_pagination_preserves_cycle_totals_and_missing_dates():
    rows = [row("2026-08-31", 100, 20), row("2026-09-01", 105, "17.30"),
            row("2026-09-30", 110, "14.60"), row("2026-10-01", 115, "11.90")]
    m = make_calendar_model(data(rows, scope="cycle"))
    pages = calendar_pages(m)
    assert len(pages) == 3 and [p["page_index"] for p in pages] == [1, 2, 3]
    assert all(p["total_kwh"] == m["total_kwh"] and len(p["months"]) == 1 for p in pages)
    assert m["recorded_days"] == 2 and Decimal(m["total_kwh"]) == 10
    assert by_day(m)["2026-09-01"]["value"] is None


def test_cycle_manual_increase_starts_at_observation_and_excludes_partial_day():
    rows = [row("2026-10-01", 100, 2),
            row("2026-10-01", 101, 20, hour="19:24:00", slot=None),
            row("2026-10-02", 105, "17.30"), row("2026-10-03", 110, "14.60")]
    m = make_calendar_model(data(rows, scope="cycle"))
    assert m["start_balance"] == "20" and Decimal(m["balance_net_change"]) == Decimal("-5.40")
    assert by_day(m)["2026-10-01"]["status"] == "不足整日"
    assert Decimal(m["total_kwh"]) == 5 and m["recorded_days"] == 1


def test_huxi_transient_meter_reset_not_hidden_by_recovery():
    rows = [row("2026-10-01", 100, 10),
            row("2026-10-01", 1, 9, hour="21:00:00", slot=None),
            row("2026-10-02", 105, "7.30")]
    m = make_calendar_model(data(rows, month="2026-10"))
    assert m["total_kwh"] is None and by_day(m)["2026-10-01"]["status"] == "读数变化"


def test_repository_calendar_reader_isolated_readonly_no_30day_cutoff(env):
    repo, service, room = env
    old = capture(repo, room, "2026-08-01T14:01:00", "10", "20")
    capture(repo, room, "2026-09-30T14:01:00", "100", "10")
    endpoint = capture(repo, room, "2026-10-01T14:01:00", "105", "7.30")
    capture(repo, room, "2026-10-04T14:01:00", "110", "4.60")
    other = repo.room(Room.from_account("A7511"))
    capture(repo, other, "2026-10-01T14:01:00", "999", "987")
    now = datetime(2026, 10, 3, 16, tzinfo=BJ)
    before = repo.db.total_changes
    cycle = calendar_input(repo, IDENTITY, now=now)
    assert cycle["rows"][0]["id"] == old
    monthly = calendar_input(repo, IDENTITY, now=now, scope="month", month_key="2026-09")
    assert monthly["rows"][-1]["id"] == endpoint
    assert "987" not in str(monthly) and "A7511" not in str(monthly)
    assert calendar_input(repo, ("qq", "123", "unknown"), now=now) is None
    assert repo.db.total_changes == before
    assert Decimal(make_calendar_model(monthly)["total_kwh"]) == 5


def test_template_injection_escaped():
    m = make_calendar_model(data([]))
    m["room"]["building"] = "</script><script>bad()</script>"
    html = calendar_html(m)
    assert "</script><script>bad()" not in html and r"\u003c/script" in html
    assert "<nav" not in html and "__REPORT_DATA__" not in html


@pytest.mark.parametrize("group", ["123", ""])
@pytest.mark.parametrize("command", ["本轮", "月报", "月报 本月", "月报 2026-09"])
def test_calendar_commands_bound_sender_no_query_or_writes(plugin, tmp_path, group, command):
    bind(plugin)
    class NoFetch:
        async def fetch(self, *args):
            raise AssertionError("Calendar must not collect")
    plugin.service.client = NoFetch()
    path = tmp_path / "calendar.png"
    path.write_bytes(b"fixture")
    async def render(model):
        assert model["room"]["account"] == "D1102"
        return [str(path)]
    plugin.dashboard.render_calendar = render
    before = plugin.service.repo.db.total_changes
    event = Event(group=group, message="/电费 " + command)
    event.chain_result = lambda components: components
    asyncio.run(plugin.electricity(event))
    assert len(event.responses) == 1 and len(event.responses[0]) == 1
    assert plugin.service.repo.db.total_changes == before


def test_calendar_fallback_uses_model_not_platform_query(plugin):
    bind(plugin)
    async def unavailable(model):
        raise RuntimeError("Browser unavailable")
    plugin.dashboard.render_calendar = unavailable
    before = plugin.service.repo.db.total_changes
    event = Event(message="/电费 本轮")
    asyncio.run(plugin.electricity(event))
    assert "本轮用电日历" in event.responses[0] and "5.00 元" in event.responses[0]
    assert "无完整日用电记录" in event.responses[0]
    assert plugin.service.repo.db.total_changes == before


@pytest.mark.parametrize("command", ["本轮 A7511", "月报 本月 1002", "月报 1002", "月报 2027-01"])
def test_calendar_rejects_identity_or_bad_arguments(plugin, command):
    bind(plugin)
    async def forbidden(model):
        raise AssertionError("Must reject before render")
    plugin.dashboard.render_calendar = forbidden
    event = Event(message="/电费 " + command)
    asyncio.run(plugin.electricity(event))
    assert event.responses and "暂时未能" not in event.responses[0]


def test_calendar_unbound_and_disallowed_groups(plugin):
    for index, group in enumerate(["123", "999"]):
        event = Event(group=group, message="/电费 本轮", msg_id=str(index))
        asyncio.run(plugin.electricity(event))
        assert "绑定" in event.responses[0] if group == "123" else "仅支持" in event.responses[0]
