import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from cqu_plugin import scheduler as scheduler_module
from cqu_plugin.scheduler import Scheduler, spread_plan
from cqu_plugin.models import Room, Reading, QueryError
from cqu_plugin.repository import Repository
from cqu_plugin.history import daily_history
from cqu_plugin.settlement import afternoon_sample, morning_snapshot
from cqu_plugin.dashboard import dashboard_input
from cqu_plugin.dashboard_model import make_model
from test_schedule_slots import environment, BJ


@pytest.mark.parametrize("count", [1, 2, 10, 16, 100, 1000])
def test_strata_are_reproducible_bounded_and_never_cluster(count):
    ids=list(range(1,count+1))
    plan=spread_plan(ids,"2026-10-19","09:00")
    assert plan==spread_plan(reversed(ids),"2026-10-19","09:00")
    assert len(plan)==count
    step=max(20,300/count)
    assert all(i*step<=offset<=i*step+10 for i,(_,offset) in enumerate(plan))
    assert all(b[1]-a[1]>=10 for a,b in zip(plan,plan[1:]))
    if count==10:assert 270<=plan[-1][1]<300
    if count>16:assert plan[-1][1]>300


def add_rooms(env, count):
    repo=env[0]
    for index in range(1,count):
        room=repo.room(Room("huxi","竹园5栋",f"B5{400+index}"))
        repo.bind(("qq","123",str(2000+index)),room["id"],"qq:GroupMessage:123")


def install_clock(env, monkeypatch):
    clock=env[4]
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return clock["now"].astimezone(tz or BJ)
    monkeypatch.setattr(scheduler_module,"datetime",Clock)


def simulate(env, until, scheduler=None):
    scheduler=scheduler or env[3]
    clock=env[4]
    async def run():
        while clock["now"]<=until:
            await scheduler.tick()
            clock["now"]+=timedelta(seconds=scheduler.next_delay)
    asyncio.run(run())


def test_ten_rooms_spread_over_minutes_and_shared_bindings_capture_once(environment,monkeypatch):
    env=environment;repo,client,service,scheduler,clock,room=env
    install_clock(env,monkeypatch);add_rooms(env,10)
    repo.bind(("qq","123","9999"),room["id"],"qq:GroupMessage:123")
    seen=[]
    async def fetch(room,save):
        start=clock["now"]
        clock["now"]+=timedelta(seconds=3)
        seen.append((start,clock["now"],room.room_number))
        return Reading(Decimal("20"))
    client.fetch=fetch
    simulate(env,clock["now"]+timedelta(minutes=6))
    assert len(seen)==10 and len({r[2] for r in seen})==10
    assert (seen[-1][0]-seen[0][0]).total_seconds()>=260
    assert all((b[0]-a[1]).total_seconds()>=10 for a,b in zip(seen,seen[1:]))
    assert len(repo.all("SELECT * FROM capture_plans WHERE query_id IS NOT NULL"))==10


def test_delayed_start_and_retry_share_spacing_without_catchup_burst(environment,monkeypatch):
    env=environment;repo,client,service,scheduler,clock,room=env
    install_clock(env,monkeypatch);add_rooms(env,4)
    clock["now"]+=timedelta(minutes=5)
    calls=[];failures=set()
    async def fetch(room,save):
        calls.append(clock["now"])
        clock["now"]+=timedelta(seconds=2)
        if room.room_number not in failures:
            failures.add(room.room_number)
            raise QueryError("TIMEOUT")
        return Reading(Decimal("20"))
    client.fetch=fetch
    simulate(env,clock["now"]+timedelta(minutes=8))
    assert len(calls)==8
    assert all((b-a).total_seconds()>=12 for a,b in zip(calls,calls[1:]))
    runs=repo.all("SELECT * FROM query_runs ORDER BY id")
    for rid in {r["room_id"] for r in runs}:
        first,second=[r for r in runs if r["room_id"]==rid]
        assert (datetime.fromisoformat(second["started_at"])-datetime.fromisoformat(first["finished_at"])).total_seconds()>=300


def test_restart_keeps_plans_spacing_and_completed_checkpoints(environment,monkeypatch):
    env=environment;repo,client,service,scheduler,clock,room=env
    install_clock(env,monkeypatch);add_rooms(env,10)
    simulate(env,clock["now"]+timedelta(minutes=2))
    before=repo.all("SELECT room_id,planned_at FROM capture_plans ORDER BY room_id")
    already=client.calls
    # A new connection and Scheduler, as on process restart.
    reopened=Repository(repo.db.execute("PRAGMA database_list").fetchone()[2])
    service.repo=reopened
    replacement=Scheduler(service,lambda *x:True,scheduler.sender,capture_times=("09:00","21:00"),reminder_enabled=False)
    try:
        simulate(env,clock["now"]+timedelta(minutes=5),replacement)
        assert 0<already<10 and client.calls==10
        assert reopened.all("SELECT room_id,planned_at FROM capture_plans ORDER BY room_id")==before
    finally:
        service.repo=repo;reopened.close()


def test_restart_preserves_global_gap_even_if_all_tasks_overdue(environment,monkeypatch):
    env=environment;repo,client,service,scheduler,clock,room=env
    install_clock(env,monkeypatch);add_rooms(env,3)
    clock["now"]+=timedelta(minutes=5)
    asyncio.run(scheduler.tick())
    assert client.calls==1
    replacement=Scheduler(service,lambda *x:True,scheduler.sender,reminder_enabled=False)
    clock["now"]+=timedelta(seconds=9)
    asyncio.run(replacement.tick())
    assert client.calls==1
    clock["now"]+=timedelta(seconds=1)
    asyncio.run(replacement.tick())
    assert client.calls==2


def test_new_binding_does_not_reshuffle_persisted_plan(environment,monkeypatch):
    env=environment;repo,client,service,scheduler,clock,room=env
    install_clock(env,monkeypatch);add_rooms(env,3)
    asyncio.run(scheduler.tick())
    before=repo.all("SELECT room_id,planned_at FROM capture_plans ORDER BY room_id")
    add_rooms(env,4)
    asyncio.run(scheduler.tick())
    after=repo.all("SELECT room_id,planned_at FROM capture_plans ORDER BY room_id")
    assert all(row in after for row in before)
    assert len(after)==4
    assert max(r["planned_at"] for r in after)>=max(r["planned_at"] for r in before)+20


def test_large_batch_extends_window_and_retains_retry_room(environment,monkeypatch):
    env=environment;repo,client,service,scheduler,clock,room=env
    install_clock(env,monkeypatch);add_rooms(env,60)
    asyncio.run(scheduler.tick())
    plans=repo.all("SELECT * FROM capture_plans ORDER BY planned_at")
    assert len(plans)==60
    due=clock["now"].timestamp()
    assert plans[-1]["planned_at"]>due+15*60
    assert plans[-1]["deadline"]>=plans[-1]["planned_at"]+350


def test_expired_and_revoked_rooms_never_generate_catchup(environment,monkeypatch):
    env=environment;repo,client,service,scheduler,clock,room=env
    install_clock(env,monkeypatch);add_rooms(env,10)
    asyncio.run(scheduler.tick())
    scheduler.allowed=lambda *x:False
    simulate(env,clock["now"]+timedelta(minutes=6))
    assert client.calls<=1
    scheduler.allowed=lambda *x:True
    clock["now"]=clock["now"].replace(hour=9,minute=16)
    asyncio.run(scheduler.tick())
    assert client.calls<=1


def test_extended_afternoon_capture_used_by_history_dashboard_and_morning(environment):
    repo,client,service,scheduler,clock,room=environment
    at=datetime(2026,10,3,14,21,tzinfo=BJ)
    clock["now"]=at
    asyncio.run(service.query(room,force=True))
    q=repo.latest(room["id"])["id"]
    with repo.db:
        repo.db.execute("UPDATE query_runs SET meter_reading_kwh='100' WHERE id=?",(q,))
        repo.db.execute("INSERT INTO capture_plans VALUES(?,?,?,?,?,?)",(room["id"],"2026-10-03","14:00",at.timestamp(),at.timestamp()+600,q))
    rows=repo.completed_runs(room["id"],now=at)
    assert rows[0]["capture_slot"]=="14:00"
    daily,_=daily_history([{**r,"day":"2026-10-03"} for r in rows],"2026-10-03")
    assert daily[0]["sample_kind"]=="fixed"
    identity=("qq","123","1001")
    data=dashboard_input(repo,identity,now=at)
    assert make_model(data)["fresh"]
    assert service.stored_result(room,now=at)["daily_brief"]["state"]=="ready"
    clock["now"]=at+timedelta(days=1,hours=-5)
    asyncio.run(service.query(room,force=True))
    with repo.db:
        repo.db.execute("UPDATE query_runs SET meter_reading_kwh='105' WHERE id=(SELECT MAX(id) FROM query_runs)")
    rows=repo.completed_runs(room["id"],now=clock["now"])
    assert morning_snapshot(rows,clock["now"])["estimated_balance_yuan"]=="17.30"
    # Unmarked late manual samples still do not qualify as the daily baseline.
    assert not afternoon_sample({**rows[0],"capture_slot":None},"2026-10-03")


def test_schema3_upgrade_is_additive_and_preserves_records(tmp_path):
    path=tmp_path/"upgrade.db"
    repo=Repository(path)
    room=repo.room(Room("huxi","竹园5栋","B5344"))
    q=repo.begin(room["id"],"manual")
    repo.response(q,1,"reading",200,"application/json",b'{"raw":true}')
    repo.finish(q,Reading(Decimal("20")))
    with repo.db:
        repo.db.execute("DROP TABLE capture_plans")
        repo.db.execute("DROP TABLE capture_pacing")
        repo.db.execute("PRAGMA user_version=3")
    before=repo.all("SELECT * FROM query_runs")
    repo.close()
    repo=Repository(path)
    try:
        assert repo.db.execute("PRAGMA user_version").fetchone()[0]==5
        assert repo.all("SELECT * FROM query_runs")==before
        assert repo.one("SELECT raw_body FROM query_responses")["raw_body"]==b'{"raw":true}'
        assert repo.all("PRAGMA foreign_key_check")==[]
    finally:repo.close()
