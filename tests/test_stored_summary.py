import asyncio
import json
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import pytest
from cqu_plugin.models import Reading
from cqu_plugin.summary import summary
from test_history_captures import env,capture,IDENTITY
from test_plugin import plugin,Event,bind

BJ=timezone(timedelta(hours=8))

def at(hour,minute=0,second=0,day=3):return datetime(2026,10,day,hour,minute,second,tzinfo=BJ)

def snapshots(env,*,today=True):
    repo,service,room=env
    capture(repo,room,"2026-10-01T14:00:20","95","42")
    capture(repo,room,"2026-10-02T14:00:20","100","39.30")
    capture(repo,room,"2026-10-03T09:00:20","105","39.30")
    if today:capture(repo,room,"2026-10-03T14:00:20","105","36.60")
    return service,room

def test_before_1400_uses_morning_reading_and_hypothetical_balance(env):
    service,room=snapshots(env)
    data=service.stored_result(room,now=at(13,59,59))
    assert data["daily_brief"]["state"]=="morning_pending"
    assert data["reading"]["total_balance_yuan"]=="39.30"
    assert data["daily_brief"]["estimate"]["reference_day"]=="2026-10-02"
    text=summary(data)
    assert "昨天（10月02日）" in text and "电费约 2.70 元" in text
    assert "预计扣费后余额：36.60" in text and "09:00" in text
    assert "将在今天14:00刷新" not in text
    assert not data["stale"]

def test_exact_1400_waits_for_completed_capture(env):
    service,room=snapshots(env)
    data=service.stored_result(room,now=at(14))
    assert data["daily_brief"]["state"]=="awaiting_refresh"
    assert data["stale"] and "尚未完成" in summary(data)
    assert "昨天（" not in summary(data)

def test_after_success_yesterday_estimate_from_daily_not_last_manual(env):
    service,room=snapshots(env)
    data=service.stored_result(room,now=at(14,1))
    assert data["daily_brief"]["state"]=="ready"
    estimate=data["daily_brief"]["estimate"]
    assert estimate["reference_day"]=="2026-10-02" and Decimal(estimate["kwh"])==5
    assert estimate["estimated"] and "昨天（10月02日）" in summary(data)
    assert "实际扣费" in summary(data) and "36.60" in summary(data)
    assert not data["stale"]

def test_later_manual_capture_does_not_replace_daily_snapshot(env):
    service,room=snapshots(env)
    capture(env[0],room,"2026-10-03T18:00:00","106","36.06")
    data=service.stored_result(room,now=at(19))
    assert data["reading"]["total_balance_yuan"]=="36.60"
    assert Decimal(data["daily_brief"]["estimate"]["kwh"])==5

def test_scheduled_failure_returns_saved_snapshot_without_fetch(env):
    service,room=snapshots(env,today=False)
    q=capture(env[0],room,"2026-10-03T14:00:30","0",error="TIMEOUT")
    with env[0].db:env[0].db.execute("UPDATE query_runs SET trigger_type='scheduled' WHERE id=?",(q,))
    data=service.stored_result(room,now=at(14,1))
    assert data["daily_brief"]["state"]=="refresh_failed"
    assert data["stale"] and data["error_code"]=="TIMEOUT"
    assert "尚未成功" in summary(data) and "39.30" in summary(data)

def test_manual_failure_does_not_change_successful_daily_summary(env):
    service,room=snapshots(env)
    capture(env[0],room,"2026-10-03T15:00:00","0",error="TIMEOUT")
    data=service.stored_result(room,now=at(16))
    assert data["daily_brief"]["state"]=="ready" and not data["stale"]

@pytest.mark.parametrize("kind",["gap","no_meter","reset","single"])
def test_missing_interval_never_invents_yesterday_usage(env,kind):
    repo,service,room=env
    if kind!="single":capture(repo,room,"2026-10-01T14:00:20" if kind=="gap" else "2026-10-02T14:00:20","100")
    q=capture(repo,room,"2026-10-03T14:00:20","90" if kind=="reset" else "105")
    if kind=="no_meter":
        with repo.db:repo.db.execute("UPDATE query_runs SET meter_reading_kwh=NULL WHERE id=?",(q,))
    data=service.stored_result(room,now=at(15))
    assert data["daily_brief"]["estimate"] is None
    assert "暂无" in summary(data) and "电费约" not in summary(data)

def test_empty_storage_does_not_fetch_and_offers_wait_or_explicit_refresh(env):
    repo,service,room=env
    before=repo.db.total_changes
    data=asyncio.run(service.mine(IDENTITY))
    assert data["error_code"]=="NO_SAVED_READING"
    assert "14:00" in summary(data) and "/电费 刷新" in summary(data)
    assert repo.db.total_changes==before

def test_no_fixed_sample_returns_balance_only_without_fake_attribution(env):
    repo,service,room=env
    capture(repo,room,"2026-10-03T09:00:20","100","40")
    data=service.stored_result(room,now=at(13))
    assert data["reading"]["total_balance_yuan"]=="40.00"
    assert data["daily_brief"]["estimate"] is None
    assert data["daily_brief"]["source"]=="sqlite_morning_snapshot"

def test_older_record_is_labelled_historical_not_yesterday(env):
    repo,service,room=env
    capture(repo,room,"2026-10-01T14:00:20","100","40")
    capture(repo,room,"2026-10-02T14:00:20","105","37.30")
    data=service.stored_result(room,now=at(15,day=6))
    assert "历史（10月01日）" in summary(data) and data["stale"]

def test_reading_commands_and_llm_never_call_client_or_write_queries(plugin):
    bind(plugin)
    # This check exercises stored data and text fallback; browser lifecycle is
    # verified separately in a single event loop by deployment rendering checks.
    async def no_browser(data):raise RuntimeError("Browser unavailable in storage test")
    plugin.dashboard.render=no_browser
    class NoFetch:
        async def fetch(self,*args):raise AssertionError("Read commands must not fetch")
        def configured(self):return False
    plugin.service.client=NoFetch();plugin.service.cache_seconds=0
    before=plugin.service.repo.db.total_changes
    for index,command in enumerate(("/电费","/电费 详情","/电费 历史","/电费 历史 明细")):
        plugin.limits.clear()
        event=Event(group="",message=command,msg_id=str(index))
        asyncio.run(plugin.electricity(event))
        assert event.responses and "暂时未能处理" not in event.responses[0]
    data=json.loads(asyncio.run(plugin.get_my_electricity(Event())))
    assert "daily_brief" in data
    assert json.loads(asyncio.run(plugin.get_my_electricity_history(Event())))["ok"]
    assert plugin.service.repo.db.total_changes==before

def test_explicit_refresh_still_fetches_and_saves(plugin):
    bind(plugin)
    class Client:
        calls=0
        async def fetch(self,room,save):
            self.calls+=1
            return Reading(Decimal("50"),meter_reading_kwh=Decimal("105"))
    client=Client();plugin.service.client=client
    before=plugin.service.repo.one("SELECT COUNT(*) n FROM query_runs")["n"]
    event=Event(message="/电费 刷新")
    asyncio.run(plugin.electricity(event))
    assert client.calls==1 and "50.00" in event.responses[0]
    assert plugin.service.repo.one("SELECT COUNT(*) n FROM query_runs")["n"]==before+1
