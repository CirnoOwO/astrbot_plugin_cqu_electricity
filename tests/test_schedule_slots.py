import asyncio
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import sqlite3
import pytest

from cqu_plugin.models import Room,Reading,QueryError
from cqu_plugin.repository import Repository
from cqu_plugin import repository as repository_module
from cqu_plugin.service import ElectricityService
from cqu_plugin import service as service_module
from cqu_plugin.scheduler import Scheduler,parse_capture_times

BJ=timezone(timedelta(hours=8))


class Client:
    def __init__(self):self.calls=0;self.error=None
    def configured(self):return True
    async def fetch(self,room,save):
        self.calls+=1
        if self.error:raise QueryError(self.error)
        return Reading(Decimal("20"))


@pytest.fixture
def environment(tmp_path,monkeypatch):
    clock={"now":datetime(2026,10,3,9,0,tzinfo=BJ)}
    monkeypatch.setattr(repository_module,"utc_now",lambda:clock["now"].astimezone(timezone.utc).isoformat())
    class SimulatedDateTime(datetime):
        @classmethod
        def now(cls,tz=None):return clock["now"].astimezone(tz or BJ)
    monkeypatch.setattr(service_module,"datetime",SimulatedDateTime)
    repo=Repository(tmp_path/"slots.db")
    room=repo.room(Room("huxi","竹园5栋","B5344"))
    repo.bind(("qq","123","1001"),room["id"],"qq:GroupMessage:123")
    client=Client();service=ElectricityService(repo,client,failure_seconds=0)
    async def send(*args):raise AssertionError("No notifications in slot tests")
    scheduler=Scheduler(service,lambda *x:True,send,capture_times=("09:00","21:00"),reminder_enabled=False)
    yield repo,client,service,scheduler,clock,room
    repo.close()


def tick(env,hour,minute,day=3):
    repo,client,service,scheduler,clock,room=env
    clock["now"]=datetime(2026,10,day,hour,minute,tzinfo=BJ)
    asyncio.run(scheduler.tick(clock["now"]))


def test_morning_evening_next_day_independent(environment):
    tick(environment,9,0);tick(environment,9,1)
    tick(environment,21,0);tick(environment,21,1)
    repo,client,*_=environment
    assert client.calls==2
    assert repo.all("SELECT day,slot,attempts,status FROM capture_schedule ORDER BY slot")==[
        {"day":"2026-10-03","slot":"09:00","attempts":1,"status":"done"},
        {"day":"2026-10-03","slot":"21:00","attempts":1,"status":"done"}]
    tick(environment,9,0,day=4)
    assert client.calls==3


def test_retry_once_five_minutes_then_next_slot(environment):
    repo,client,*_=environment
    client.error="TIMEOUT"
    tick(environment,21,0);tick(environment,21,1)
    assert client.calls==1
    tick(environment,21,5);tick(environment,21,10)
    assert client.calls==2
    assert repo.one("SELECT attempts,status FROM capture_schedule")=={"attempts":2,"status":"failed"}
    client.error=None
    tick(environment,9,0,day=4)
    assert client.calls==3


@pytest.mark.parametrize("hour,minute",[(8,59),(9,15),(12,0),(20,59),(21,15),(23,0),(0,0)])
def test_no_capture_outside_window(environment,hour,minute):
    tick(environment,hour,minute)
    assert environment[1].calls==0
    assert not environment[0].all("SELECT * FROM capture_schedule")


def test_reuses_only_current_slot_manual_record(environment):
    repo,client,service,scheduler,clock,room=environment
    clock["now"]=datetime(2026,10,3,9,1,tzinfo=BJ)
    asyncio.run(service.query(room,force=True))
    tick(environment,9,2)
    assert client.calls==1
    tick(environment,21,0)
    assert client.calls==2


def test_does_not_reuse_record_started_before_slot(environment):
    repo,client,service,scheduler,clock,room=environment
    clock["now"]=datetime(2026,10,3,8,59,tzinfo=BJ)
    asyncio.run(service.query(room,force=True))
    tick(environment,9,0)
    assert client.calls==2


def test_retry_checkpoint_survives_scheduler_restart(environment):
    repo,client,service,scheduler,clock,room=environment
    client.error="TIMEOUT"
    tick(environment,21,0)
    async def send(*args):return True
    replacement=Scheduler(service,lambda *x:True,send,capture_times=("09:00","21:00"),reminder_enabled=False)
    clock["now"]=datetime(2026,10,3,21,1,tzinfo=BJ)
    asyncio.run(replacement.tick(clock["now"]))
    assert client.calls==1
    clock["now"]=datetime(2026,10,3,21,5,tzinfo=BJ)
    asyncio.run(replacement.tick(clock["now"]))
    assert client.calls==2


def test_schema_migration_preserves_data_and_legacy_state(tmp_path):
    path=tmp_path/"migration.db";repo=Repository(path)
    room=repo.room(Room("huxi","竹园5栋","B5344"))
    binding=repo.bind(("qq","123","1001"),room["id"],"qq:GroupMessage:123")
    repo.subscribe(binding["id"],True,10)
    query=repo.begin(room["id"],"manual",binding["id"])
    repo.response(query,1,"reading",200,"application/json",b'{"sample":true}')
    repo.finish(query,Reading(Decimal("20")))
    with repo.db:
        repo.db.execute("DROP TABLE capture_schedule")
        repo.db.execute("CREATE TABLE capture_schedule(room_id INTEGER NOT NULL REFERENCES rooms(id),day TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL,PRIMARY KEY(room_id,day))")
        repo.db.execute("INSERT INTO capture_schedule VALUES(?, ?,1,'done')",(room["id"],"2026-10-03"))
        repo.db.execute("PRAGMA user_version=1")
    repo.close()
    repo=Repository(path)
    assert repo.db.execute("PRAGMA user_version").fetchone()[0]==5
    assert repo.one("SELECT slot,status FROM capture_schedule")=={"slot":"legacy","status":"done"}
    assert repo.binding(("qq","123","1001"))["room_id"]==room["id"]
    assert repo.latest(room["id"])["total_balance_yuan"]=="20.00"
    assert repo.one("SELECT raw_body FROM query_responses")["raw_body"]==b'{"sample":true}'
    assert repo.one("SELECT enabled FROM subscriptions")["enabled"]==1
    repo.capture_attempt(room["id"],"2026-10-03","09:00",0)
    repo.capture_done(room["id"],"2026-10-03","09:00",True)
    repo.close()
    reopened=Repository(path)
    assert reopened.one("SELECT COUNT(*) AS n FROM capture_schedule")["n"]==2
    reopened.close()


def test_times_sorted_and_deduplicated():
    assert parse_capture_times(["21:00","09:00","9:00"])==[(9,0),(21,0)]


def test_new_default_collects_9_and_14_once_each(environment):
    repo,client,service,old,clock,room=environment
    async def send(*args):raise AssertionError("No notifications in slot test")
    scheduler=Scheduler(service,lambda *x:True,send,reminder_enabled=False)
    assert scheduler.capture_times==[(9,0),(14,0)]
    for hour in (9,9,14,14,21):
        clock["now"]=datetime(2026,10,3,hour,1,tzinfo=BJ)
        asyncio.run(scheduler.tick(clock["now"]))
    assert client.calls==2
    assert [r["slot"] for r in repo.all("SELECT slot FROM capture_schedule ORDER BY slot")]==["09:00","14:00"]

@pytest.mark.parametrize("values",[[],None,"09:00",["25:00"],["09:60"]])
def test_invalid_times_rejected(values):
    with pytest.raises(ValueError):parse_capture_times(values)
