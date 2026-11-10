import asyncio
import json
import sqlite3
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from types import SimpleNamespace
import pytest
from cqu_plugin.repository import Repository
from cqu_plugin.models import Room,Reading
from cqu_plugin.service import ElectricityService
from cqu_plugin.scheduler import Scheduler
from cqu_plugin import main
from test_plugin import Event,plugin,bind

BJ=timezone(timedelta(hours=8))

@pytest.fixture
def repo(tmp_path):
    value=Repository(tmp_path/"global.db")
    yield value
    value.close()

def room(repo,account="B5344"):
    return repo.room(Room.from_account(account))

def test_global_one_qq_across_groups_private_platform(repo):
    first=room(repo);other=room(repo,"A7511")
    binding=repo.bind(("qq","123","1001"),first["id"],"qq:GroupMessage:123")
    assert repo.binding(("qq","","1001"))["id"]==binding["id"]
    updated=repo.bind(("qq2","","1001"),other["id"],"qq2:FriendMessage:1001")
    assert updated["id"]==binding["id"]
    assert repo.binding(("qq","456","1001"))["room_id"]==other["id"]
    assert repo.one("SELECT COUNT(*) n FROM user_bindings")["n"]==1
    repo.bind(("qq","123","1002"),first["id"],"qq:GroupMessage:123")
    repo.unbind(("qq3","456","1001"))
    assert not repo.binding(("qq","","1001"))
    assert repo.binding(("qq","","1002"))

def test_reminder_independent_from_rebinding_and_global_disable(repo):
    row=room(repo)
    b=repo.bind(("qq","123","1001"),row["id"],"qq:GroupMessage:123")
    repo.subscribe(b["id"],True,10,platform_id="qq",group_id="")
    repo.bind(("qq","456","1001"),row["id"],"qq:GroupMessage:456")
    assert repo.room_subscribers(row["id"])[0]["origin"]=="qq:FriendMessage:1001"
    repo.subscribe(b["id"],True,5,platform_id="qq",group_id="456",origin="qq:GroupMessage:1001_456")
    assert repo.room_subscribers(row["id"])[0]["origin"]=="qq:GroupMessage:1001_456"
    repo.subscribe(b["id"],False,10,platform_id="qq",group_id="")
    assert not repo.room_subscribers(row["id"])
    assert repo.one("SELECT origin FROM subscriptions")["origin"]=="qq:GroupMessage:1001_456"
    repo.bind(("qq","","1001"),room(repo,"A7511")["id"],"qq:FriendMessage:1001")
    assert not repo.all("SELECT * FROM subscriptions")

def test_private_bind_query_tools_and_global_rate_limit(plugin):
    event=Event(group="",message="/电费 绑定 B5344")
    asyncio.run(plugin.electricity(event))
    assert "已绑定" in event.responses[0]
    assert plugin.service.repo.binding(("qq-test","123","1001"))["room_number"]=="B5344"
    assert json.loads(asyncio.run(plugin.get_my_electricity(Event())))["room"]["room_number"]=="B5344"
    assert json.loads(asyncio.run(plugin.get_my_electricity_history(Event(group=""))))["ok"]
    assert plugin._rate_limit(("qq-test","123","1001"))
    assert not plugin._rate_limit(("qq-test","123","1002"))

def test_private_reminder_migrates_on_enable_only(plugin):
    row=bind(plugin)
    asyncio.run(plugin.electricity(Event(group="",message="/电费 提醒 开启 10")))
    assert plugin.service.repo.room_subscribers(row["id"])[0]["origin"]=="qq-test:FriendMessage:1001"
    plugin.limits.clear()
    asyncio.run(plugin.electricity(Event(group="123",message="/电费 提醒 开启 5",msg_id="other")))
    assert plugin.service.repo.room_subscribers(row["id"])[0]["origin"]=="qq-test:GroupMessage:123"
    plugin.limits.clear()
    asyncio.run(plugin.electricity(Event(group="",message="/电费 提醒 关闭",msg_id="off")))
    assert not plugin.service.repo.room_subscribers(row["id"])

def test_private_disabled_invalid_origin_and_foreign_platform(plugin):
    plugin.config["enable_private"]=False
    assert plugin._identity(Event(group="")) is None
    plugin.config["enable_private"]=True
    bad=Event(group="");bad.unified_msg_origin="qq-test:FriendMessage:1002"
    assert plugin._identity(bad) is None
    assert not plugin._allowed("other","","other:FriendMessage:1001")

@pytest.mark.parametrize("group",["","123"])
def test_send_private_plain_group_at(plugin,group):
    async def send(origin,chain):
        parts=getattr(chain,"chain",chain)
        if group:
            assert len(parts)==2
        else:
            assert len(parts)==1
            assert getattr(parts[0],"text",parts[0])=="提醒"
        return True
    plugin.context.send_message=send
    assert asyncio.run(plugin._send(Repository.route("qq-test",group,"1001"),["1001"],"提醒"))

def stage():
    from astrbot.core.pipeline.whitelist_check.stage import WhitelistCheckStage
    value=WhitelistCheckStage()
    value.enable_whitelist_check=True;value.whitelist=["123"]
    value.wl_ignore_admin_on_group=False;value.wl_ignore_admin_on_friend=False;value.wl_log=False
    return value

def handler(name="electricity",module=None):
    return SimpleNamespace(handler_module_path=module or main.__name__,handler_name=name)

@pytest.mark.parametrize("private,matched,enabled,expected",[(True,True,True,False),(True,False,True,True),(True,True,False,True),(False,True,True,True)])
def test_whitelist_exemption_only_enabled_private_command(plugin,private,matched,enabled,expected):
    event=Event(group="" if private else "999")
    event.set_extra("activated_handlers",[handler()] if matched else [handler("other")])
    plugin.config["enable_private"]=enabled
    asyncio.run(stage().process(event))
    assert event.stopped==expected

def test_whitelist_exemption_preserves_group_and_excludes_other_handlers(plugin):
    allowed=Event();asyncio.run(stage().process(allowed));assert not allowed.stopped
    event=Event(group="",message="电费 绑定 B5344")  # after WakingCheck strips '/'
    own=handler();event.set_extra("activated_handlers",[handler("other"),own,handler(module="other_plugin")])
    asyncio.run(stage().process(event))
    assert not event.stopped and event.get_extra("activated_handlers")==[own]
    event=Event(group="");event.set_extra("activated_handlers",[])
    asyncio.run(stage().process(event));assert event.stopped

def test_whitelist_restored_on_termination(plugin):
    cls,original,wrapped=plugin._whitelist_patch
    plugin._remove_private_commands()
    assert cls.process is original
    event=Event(group="");event.set_extra("activated_handlers",[handler()])
    asyncio.run(stage().process(event));assert event.stopped

def test_whitelist_does_not_overwrite_later_wrapper(plugin):
    cls,original,wrapped=plugin._whitelist_patch
    async def later(stage,event):return await wrapped(stage,event)
    cls.process=later
    try:
        plugin._remove_private_commands()
        assert cls.process is later
    finally:cls.process=original

def notification_environment(repo):
    row=room(repo)
    query=repo.begin(row["id"],"manual");repo.finish(query,Reading(Decimal("5")))
    service=ElectricityService(repo,SimpleNamespace(configured=lambda:True))
    sent=[]
    async def send(origin,users,text):sent.append((origin,users));return True
    scheduler=Scheduler(service,lambda *x:True,send)
    return row,scheduler,sent,datetime.now(BJ).date().isoformat()

def test_two_private_recipients_and_group_merge(repo):
    row,scheduler,sent,day=notification_environment(repo)
    for user,group in (("1001",""),("1002",""),("1003","123"),("1004","123")):
        b=repo.bind(("qq",group,user),row["id"],Repository.route("qq",group,user))
        repo.subscribe(b["id"],True,10)
    scheduler.queue_low_balance(row,day)
    asyncio.run(scheduler.deliver());scheduler.queue_low_balance(row,day);asyncio.run(scheduler.deliver())
    assert sorted(sent)==[("qq:FriendMessage:1001",["1001"]),("qq:FriendMessage:1002",["1002"]),("qq:GroupMessage:123",["1003","1004"])]

def test_reminder_moved_pending_never_sent_to_old_destination(repo):
    row,scheduler,sent,day=notification_environment(repo)
    b=repo.bind(("qq","123","1001"),row["id"],"qq:GroupMessage:123")
    repo.subscribe(b["id"],True,10);scheduler.queue_low_balance(row,day)
    repo.subscribe(b["id"],True,10,platform_id="qq",group_id="")
    scheduler.queue_low_balance(row,day);asyncio.run(scheduler.deliver())
    assert sent==[("qq:FriendMessage:1001",["1001"])]
    repo.subscribe(b["id"],True,10,platform_id="qq",group_id="456")
    scheduler.queue_low_balance(row,day);asyncio.run(scheduler.deliver())
    assert len(sent)==1
    repo.reset_recovered(row["id"],Decimal("20"))
    assert repo.one("SELECT last_notified_day FROM subscriptions")["last_notified_day"]==day

def test_global_identity_lock_same_qq_different_sessions(repo):
    service=ElectricityService(repo,None)
    first=service.identity_lock(("qq","123","1001"))
    assert first is service.identity_lock(("qq2","","1001"))
    assert first is not service.identity_lock(("qq","123","1002"))

def test_schema2_migration_real_legacy_tables(tmp_path):
    path=tmp_path/"legacy.db"
    # Actual pre-change table definitions; don't generate legacy data with v3.
    from cqu_plugin.repository import SCHEMA
    schema=SCHEMA.replace(" platform_id TEXT NOT NULL, group_id TEXT NOT NULL, origin TEXT NOT NULL,\n enabled", " enabled").replace("UNIQUE(room_id,origin,day)","UNIQUE(room_id,platform_id,group_id,day)")
    db=sqlite3.connect(path);db.executescript(schema)
    db.execute("INSERT INTO rooms VALUES(1,'huxi','竹园5栋','B5344','2026-10-01')")
    db.execute("INSERT INTO rooms VALUES(2,'huxi','梅园7栋','A7511','2026-10-01')")
    for bid,user,rid,group,date in [(1,'1001',1,'123','01'),(2,'1001',1,'456','02'),(3,'1002',1,'123','01'),(4,'1002',2,'456','03')]:
        db.execute("INSERT INTO user_bindings VALUES(?,?,?,?,?,?,?,?)",(bid,'qq',group,user,rid,'qq:GroupMessage:'+group,'2026-10-01','2026-10-'+date))
    db.execute("INSERT INTO subscriptions(binding_id,enabled,low_balance_threshold,last_notified_day) VALUES(1,1,'10','2026-10-03')")
    db.execute("INSERT INTO subscriptions(binding_id,enabled,low_balance_threshold) VALUES(3,1,'10')")
    db.execute("INSERT INTO query_runs(id,room_id,trigger_type,requested_by,started_at,status) VALUES(1,1,'manual',1,'2026-10-01','success')")
    db.execute("INSERT INTO query_runs(id,room_id,trigger_type,requested_by,started_at,status) VALUES(2,1,'manual',3,'2026-10-01','success')")
    db.execute("INSERT INTO query_responses VALUES(1,1,1,'reading','2026-10-01',200,'application/json',?)",(b'{"raw":true}',))
    for nid,status,date in [(1,'sent','01'),(2,'pending','02'),(3,'sending','03')]:
        db.execute("INSERT INTO notifications VALUES(?,1,'qq','123','qq:GroupMessage:123',?,1,'[1,3]',1,0,?)",(nid,'2026-10-'+date,status))
    db.execute("INSERT INTO capture_schedule VALUES(1,'2026-10-03','09:00',1,'done',0)")
    db.execute("PRAGMA user_version=2");db.commit();db.close()
    repo=Repository(path)
    try:
        assert repo.db.execute("PRAGMA user_version").fetchone()[0]==5
        assert repo.all("SELECT id FROM user_bindings ORDER BY id")==[{"id":2},{"id":4}]
        assert repo.binding(('qq','','1001'))['room_id']==1
        assert repo.binding(('qq','','1002'))['room_id']==2
        assert repo.all("SELECT requested_by FROM query_runs ORDER BY id")==[{"requested_by":2},{"requested_by":4}]
        sub=repo.one("SELECT * FROM subscriptions")
        assert sub['binding_id']==2 and sub['origin']=='qq:GroupMessage:123' and sub['last_notified_day']=='2026-10-03'
        assert repo.all("SELECT status,binding_ids FROM notifications ORDER BY id")==[{"status":"sent","binding_ids":"[2, 4]"},{"status":"superseded","binding_ids":"[2, 4]"},{"status":"uncertain","binding_ids":"[2, 4]"}]
        assert repo.one("SELECT raw_body FROM query_responses")['raw_body']==b'{"raw":true}'
        assert repo.one("SELECT status FROM capture_schedule")['status']=='done'
        assert repo.one("SELECT COUNT(*) n FROM rooms")['n']==2
        with pytest.raises(sqlite3.IntegrityError):
            repo.db.execute("INSERT INTO user_bindings VALUES(5,'qq','789','1001',1,'qq:GroupMessage:789','2026-10-01','2026-10-01')")
    finally:repo.close()
    repo=Repository(path);assert len(repo.all("SELECT * FROM user_bindings"))==2;repo.close()

@pytest.mark.parametrize("message,matched",[("电费 绑定 B5344",True),("electricity",True),("电费XYZ",False),("你好",False)])
def test_real_command_filter_controls_whitelist_exemption(plugin,message,matched):
    command=pytest.importorskip("astrbot.core.star.filter.command")
    from astrbot.core.star.star_handler import StarHandlerMetadata,EventType
    md=StarHandlerMetadata(EventType.AdapterMessageEvent,"electricity","electricity",main.__name__,main.CquElectricityPlugin.electricity,[])
    command_filter=command.CommandFilter("电费",alias={"electricity"},handler_md=md)
    event=Event(group="",message=message)
    event.is_at_or_wake_command=True
    event.get_message_str=lambda: event.message_str
    passed=command_filter.filter(event,{})
    assert passed==matched
    event.set_extra("activated_handlers",[md] if passed else [])
    asyncio.run(stage().process(event))
    assert event.stopped==(not matched)
