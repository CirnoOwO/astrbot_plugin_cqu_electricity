import asyncio
import json
from datetime import datetime,timezone,timedelta
from decimal import Decimal
import pytest
from cqu_plugin.repository import Repository
from cqu_plugin.service import ElectricityService
from cqu_plugin.models import Room,Reading
from cqu_plugin.summary import history_text
from cqu_plugin import repository as repo_module,service as service_module
from test_plugin import Event,plugin,bind

BJ=timezone(timedelta(hours=8))
IDENTITY=("qq","123","1001")

@pytest.fixture
def env(tmp_path,monkeypatch):
    class FixedTime(datetime):
        @classmethod
        def now(cls,tz=None):return cls(2026,10,3,23,30,tzinfo=BJ).astimezone(tz or BJ)
    monkeypatch.setattr(repo_module,"datetime",FixedTime)
    monkeypatch.setattr(service_module,"datetime",FixedTime)
    repo=Repository(tmp_path/"history.db")
    room=repo.room(Room.from_account("B5344"))
    repo.bind(IDENTITY,room["id"],Repository.route(*IDENTITY))
    service=ElectricityService(repo,None)
    yield repo,service,room
    repo.close()

def capture(repo,room,local_time,meter,cash="20",*,started=None,error=None):
    query=repo.begin(room["id"],"manual")
    repo.finish(query,Reading(Decimal(cash),meter_reading_kwh=Decimal(meter)) if not error else None,error=error)
    at=datetime.fromisoformat(local_time).replace(tzinfo=BJ).astimezone(timezone.utc).isoformat()
    began=datetime.fromisoformat(started).replace(tzinfo=BJ).astimezone(timezone.utc).isoformat() if started else at
    with repo.db:repo.db.execute("UPDATE query_runs SET started_at=?,finished_at=? WHERE id=?",(began,at,query))
    return query

def test_same_day_captures_preserved_adjacent_usage_and_read_only(env):
    repo,service,room=env
    capture(repo,room,"2026-09-25T21:00:00","98")
    capture(repo,room,"2026-09-26T21:00:00","100")
    for at,meter in [("09:00:00","101"),("15:00:00","102"),("21:00:00","103.5")]:
        capture(repo,room,"2026-10-03T"+at,meter)
    capture(repo,room,"2026-10-03T21:01:00","0",error="TIMEOUT")
    before=repo.db.total_changes
    result=service.history(IDENTITY,7)
    assert repo.db.total_changes==before
    assert len(result["records"])==3
    assert [r["usage"]["kwh"] for r in result["records"]]==["1","1","1.5"]
    assert result["records"][0]["usage"]["interval_hours"]==156
    text=history_text(result,detail=True)
    assert "共 3 次成功采集" in text
    assert all("2026-10-03 "+at in text for at in ("09:00:00","15:00:00","21:00:00"))
    assert text.index("21:00:00")<text.index("15:00:00")<text.index("09:00:00")
    assert "2026-09-26" not in text and "21:01:00" not in text

def test_beijing_midnight_uses_finish_time_and_nearest_predecessor(env):
    repo,service,room=env
    capture(repo,room,"2026-10-02T23:50:00","100")
    capture(repo,room,"2026-10-03T00:00:05","100.2",started="2026-10-02T23:59:55")
    capture(repo,room,"2026-10-03T00:10:05","100.3")
    records=service.history(IDENTITY,1)["records"]
    assert len(records)==2 and all(r["day"]=="2026-10-03" for r in records)
    assert [r["usage"]["kwh"] for r in records]==["0.2","0.1"]

def test_same_timestamp_stable_order_and_no_fake_interval(env):
    repo,service,room=env
    capture(repo,room,"2026-10-03T09:00:00","100")
    capture(repo,room,"2026-10-03T09:00:00","101")
    records=service.history(IDENTITY,1)["records"]
    assert [r["reading"]["meter_reading_kwh"] for r in records]==["100","101"]
    assert all(r["usage"] is None for r in records)

def test_pages_have_all_records_once_latest_first(env):
    repo,service,room=env
    for minute in range(22):capture(repo,room,f"2026-10-03T09:{minute:02d}:00",str(100+minute))
    result=service.history(IDENTITY,1)
    first=history_text(result,detail=True);second=history_text(result,detail=True,page=2)
    assert "共 22 次成功采集 · 第 1/2 页" in first
    assert "下一页：/电费 历史 明细 1 2" in first
    assert "2026-10-03 09:21:00" in first and "2026-10-03 09:00:00" not in first
    assert "2026-10-03 09:00:00" in second and "2026-10-03 09:01:00" in second
    for minute in range(22):assert (first+second).count(f"2026-10-03 09:{minute:02d}:00")==1
    with pytest.raises(ValueError,match="只有 2 页"):history_text(result,detail=True,page=3)

@pytest.mark.parametrize("page",[0,-1,True,1.5])
def test_invalid_page_rejected(env,page):
    with pytest.raises(ValueError):history_text(env[1].history(IDENTITY,1),page=page)

def test_no_success_history_empty(env):
    repo,service,room=env
    capture(repo,room,"2026-10-03T09:00:00","0",error="TIMEOUT")
    assert "暂无成功采集记录" in history_text(service.history(IDENTITY,1))

def test_shapingba_history_does_not_invent_usage(env):
    repo,service,room=env
    for at,cash in [("09:00:00","20"),("21:00:00","15")]:
        query=capture(repo,room,"2026-10-03T"+at,"0",cash)
        with repo.db:repo.db.execute("UPDATE query_runs SET meter_reading_kwh=NULL WHERE id=?",(query,))
    result=service.history(IDENTITY,1)
    assert not result["records"][1]["usage"]["available"]
    assert Decimal(result["records"][1]["usage"]["balance_change_yuan"])==Decimal("-5")
    assert "度 /" not in history_text(result,detail=True)

def test_llm_returns_every_capture_command_paginates_private(plugin):
    row=bind(plugin)
    for i in range(22):
        query=plugin.service.repo.begin(row["id"],"manual")
        plugin.service.repo.finish(query,Reading(Decimal("20"),meter_reading_kwh=Decimal(100+i)))
    data=json.loads(asyncio.run(plugin.get_my_electricity_history(Event(group=""))))
    assert len(data["records"])==23
    assert all(r["reading"]["finished_at"] for r in data["records"])
    event=Event(group="",message="/电费 历史 明细 7 2")
    asyncio.run(plugin.electricity(event))
    assert "第 2/2 页" in event.responses[0]
