import asyncio
import json
from datetime import datetime,timezone,timedelta
from decimal import Decimal
import pytest
from cqu_plugin.history import daily_history
from cqu_plugin.summary import history_text
from test_history_captures import env,capture,IDENTITY
from test_plugin import plugin,Event,bind

BJ=timezone(timedelta(hours=8))

def sample(day,at,meter,balance,seq):
    when=datetime.fromisoformat(day+"T"+at).replace(tzinfo=BJ).astimezone(timezone.utc).isoformat()
    return {"id":seq,"day":day,"started_at":when,"finished_at":when,"meter_reading_kwh":str(meter) if meter is not None else None,
            "total_balance_yuan":str(balance),"cash_balance_yuan":str(balance),"subsidy_kwh":"0","unit_price_yuan":"0.54"}

def result(rows,start="2026-10-01"):
    daily,stats=daily_history(rows,start)
    return {"ok":True,"room":{"building":"竹园5栋","room_number":"B5344"},"days":7,"records":[{"day":r["day"],"reading":r} for r in rows if r["day"]>=start],"daily_records":daily,"overview":stats}

def test_real_b5344_daily_summary_totals_and_average():
    points=[("03","15:01:29","17697.91","42.03"),("03","21:34:12","17697.91","42.03"),
            ("04","09:00:46","17705.58","42.03"),("04","21:01:02","17705.58","37.88"),
            ("05","09:00:17","17713.70","37.88"),("05","13:41:16","17713.70","33.50"),
            ("05","14:00:23","17713.70","33.50"),("06","14:00:41","17719.80","30.21"),("07","14:00:22","17724.76","27.53")]
    data=result([sample("2026-10-"+day,at,meter,balance,i) for i,(day,at,meter,balance) in enumerate(points)])
    assert len(data["daily_records"])==5
    assert [d["sample_kind"] for d in data["daily_records"]]==["fallback","fallback","fixed","fixed","fixed"]
    assert [Decimal(d["usage"]["kwh"]) for d in data["daily_records"][1:]]==[Decimal("7.67"),Decimal("8.12"),Decimal("6.10"),Decimal("4.96")]
    assert Decimal(data["overview"]["meter_increase_kwh"])==Decimal("26.85")
    assert Decimal(data["overview"]["balance_change_yuan"])==Decimal("-14.50")
    assert data["overview"]["average"]["intervals"]==2
    text=history_text(data)
    for phrase in ("已有 5 天记录","26.85 度","14.50 元","5.53 度/日","10-03 21:34*","10-04 21:01*","10-07 · 27.53元 · 4.96度 · ↓2.68元"):
        assert phrase in text
    assert "0.00度 /" not in text

def test_fixed_snapshot_preferred_over_later_manual_latest_balance_separate():
    rows=[sample("2026-10-05","14:00:01",100,40,1),sample("2026-10-05","18:00:00",101,39,2),sample("2026-10-06","14:05:00",106,35,3),sample("2026-10-06","20:00:00",107,34,4)]
    data=result(rows)
    assert data["daily_records"][0]["reading"]["meter_reading_kwh"]=="100"
    assert data["daily_records"][1]["usage"]["kwh"]=="6"
    assert data["overview"]["latest_reading"]["total_balance_yuan"]=="34"
    assert "最新余额：34.00" in history_text(data)
    assert data["overview"]["average"]["intervals"]==1

def test_fixed_window_boundary_and_fallback():
    rows=[sample("2026-10-05","13:59:59",100,40,1),sample("2026-10-05","14:15:00",101,39,2),sample("2026-10-06","14:14:59",105,35,3)]
    data=result(rows)
    assert data["daily_records"][0]["sample_kind"]=="fallback"
    assert data["daily_records"][1]["sample_kind"]=="fixed"
    assert data["overview"]["average"] is None

def test_gaps_not_filled_or_used_for_daily_average():
    rows=[sample("2026-10-05","14:00:00",100,40,1),sample("2026-10-07","14:00:00",110,34,2)]
    data=result(rows)
    assert len(data["daily_records"])==2 and data["overview"]["average"] is None
    assert data["daily_records"][1]["span_days"]==2
    assert "跨 2 天" in history_text(data)

def test_hidden_intraday_balance_increase_not_called_spending():
    rows=[sample("2026-10-05","14:00:00",100,40,1),sample("2026-10-05","18:00:00",101,50,2),sample("2026-10-06","14:00:00",110,39,3)]
    data=result(rows)
    assert data["overview"]["balance_increase_events"]==1
    assert data["daily_records"][1]["balance_increase_observed"]
    assert "净变化不代表实际扣费" in history_text(data)
    assert "期间余额曾增加" in history_text(data)

def test_net_balance_increase_has_positive_label():
    data=result([sample("2026-10-05","14:00:00",100,40,1),sample("2026-10-06","14:00:00",105,50,2)])
    assert "余额净增加 10.00" in history_text(data) and "↑10.00元" in history_text(data)

@pytest.mark.parametrize("middle",[None,"50"])
def test_hidden_meter_gap_or_reset_prevents_fake_total_and_average(middle):
    rows=[sample("2026-10-05","14:00:00",100,40,1),sample("2026-10-05","18:00:00",middle,39,2),sample("2026-10-06","14:00:00",110,38,3)]
    data=result(rows)
    assert data["overview"]["meter_increase_kwh"] is None
    assert data["overview"]["average"] is None
    assert not data["daily_records"][1]["usage"]["available"]

@pytest.mark.parametrize("meters",[(None,None),(100,90)])
def test_no_meter_or_meter_reset_no_consumption(meters):
    data=result([sample("2026-10-05","14:00:00",meters[0],40,1),sample("2026-10-06","14:00:00",meters[1],35,2)])
    assert data["overview"]["meter_increase_kwh"] is None
    assert data["overview"]["average"] is None
    assert "度/日" not in history_text(data)

def test_preceding_day_fixed_snapshot_even_when_last_record_is_later(env):
    repo,service,room=env
    capture(repo,room,"2026-10-02T14:00:00","100")
    capture(repo,room,"2026-10-02T23:50:00","101")
    capture(repo,room,"2026-10-03T14:00:00","105")
    before=repo.db.total_changes
    data=service.history(IDENTITY,1)
    assert repo.db.total_changes==before
    assert len(data["records"])==1 and len(data["daily_records"])==1
    assert data["records"][0]["usage"]["kwh"]=="4"
    assert data["daily_records"][0]["usage"]["kwh"]=="5"
    assert data["overview"]["average"] is None
    assert data["overview"]["balance_change_yuan"] is None

def test_daily_pagination_and_detail_timestamp_meter():
    rows=[sample(f"2026-10-{day:02d}","14:00:00",100+day,40-day,day) for day in range(1,23)]
    data=result(rows)
    page=history_text(data,page=2)
    assert "第 2/2 页" in page and "10-01 ·" in page
    assert "下一页：/电费 历史 7 2" in history_text(data)
    detail=history_text(data,detail=True)
    assert "累计电表读数" in detail and "2026-10-22 14:00:00" in detail
    assert "122.00度" in detail
    assert "下一页：/电费 历史 明细 7 2" in detail

def test_command_daily_and_detail_and_llm_read_only(plugin):
    bind(plugin)
    before=plugin.service.repo.db.total_changes
    event=Event(group="",message="/电费 历史")
    asyncio.run(plugin.electricity(event))
    assert "已有 1 天记录" in event.responses[0]
    plugin.limits.clear()
    event=Event(group="",message="/电费 历史 明细 7",msg_id="detail-message")
    asyncio.run(plugin.electricity(event))
    assert "累计电表读数" in event.responses[0]
    data=json.loads(asyncio.run(plugin.get_my_electricity_history(Event())))
    assert len(data["daily_records"])==1 and len(data["records"])==1 and "overview" in data
    assert plugin.service.repo.db.total_changes==before
