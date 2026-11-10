import asyncio
import copy
import importlib.util
import inspect
import json
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from cqu_plugin import messages
from cqu_plugin.actions import ElectricityActions, parse_reminder, reminder_value
from cqu_plugin.dashboard import dashboard_room_input
from cqu_plugin.dashboard_model import make_model
from cqu_plugin.main import CquElectricityPlugin
from cqu_plugin.models import QueryError, Reading, Room
from cqu_plugin.reminders import qualifies, reminder_snapshot
from cqu_plugin.repository import Repository, SCHEMA
from cqu_plugin.scheduler import Scheduler
from cqu_plugin.service import ElectricityService
from cqu_plugin.settlement import BJ
from test_core import repo
from test_morning_settlement import fixture as morning_data
from test_plugin import plugin, bind, Event
from test_shapingba_dashboard import data as shapingba_data

IDENTITY = ("qq", "123", "1001")
ORIGIN = "qq:GroupMessage:123"


def load(repo, data):
    info = data["room"]
    room = repo.room(Room(info["campus"], info["building"], info["account"]))
    for source in data["captures"]:
        q = repo.begin(room["id"], "manual")
        if source["status"] == "success":
            repo.finish(q, Reading(Decimal(source["reading"]["cash_balance_yuan"])))
            fields = source["reading"]
            repo.db.execute("UPDATE query_runs SET " + ",".join(k+"=?" for k in fields) + " WHERE id=?", (*fields.values(), q))
        else:
            repo.finish(q, error="TIMEOUT")
        repo.db.execute("UPDATE query_runs SET started_at=?,finished_at=?,status=? WHERE id=?",
                        (source["started_at"], source["finished_at"], source["status"], q))
    repo.db.commit()
    return room, datetime.fromisoformat(data["as_of"])


def sub(repo, room, *, user="1001", group="123", threshold="100", kind="balance"):
    identity = ("qq", group, user)
    binding = repo.bind(identity, room["id"], repo.route(*identity))
    repo.subscribe(binding["id"], True, threshold, reminder_type=kind)
    return binding


def scheduler(repo):
    service = ElectricityService(repo, SimpleNamespace(configured=lambda: True))
    sent = []
    async def send(origin, users, text):
        sent.append((origin, users, text))
        return True
    return Scheduler(service, lambda *args: True, send), sent


@pytest.mark.parametrize("args,expected", [(["10元"], ("set", "10", "元")),
    (["2.5", "天"], ("set", "2.5", "天")), ([".5天"], ("set", ".5", "天")),
    ([], ("get", None, None)), (["关闭"], ("off", None, None)),
    (["开启"], ("set", "10", "元")), (["开启", "8元"], ("set", "8", "元"))])
def test_reminder_parser(args, expected):
    assert parse_reminder(args) == expected


@pytest.mark.parametrize("value,unit", [(0,"天"),(-1,"天"),(-1,"元"),(10001,"元"),
    (True,"元"),(float("nan"),"天"),(float("inf"),"天"),("2元","天"),(2,"日")])
def test_invalid_thresholds(value, unit):
    with pytest.raises(ValueError):
        reminder_value(value, unit)


def test_exclusive_updates_preserve_day_and_reading_is_noop(repo):
    room, now = load(repo, morning_data())
    binding = sub(repo, room)
    with repo.db:
        repo.db.execute("UPDATE subscriptions SET last_notified_day='2026-10-16' WHERE binding_id=?", (binding["id"],))
    actions = ElectricityActions(ElectricityService(repo, None), {})
    result = actions.set_reminder(("qq", "", "1001"), "qq:FriendMessage:1001", 2.5, "天")
    current = repo.subscription(binding["id"])
    assert current["reminder_type"] == "days" and current["days_threshold"] == "2.5"
    assert current["last_notified_day"] == "2026-10-16"
    assert "100 元" in result["message"] and "不再生效" in result["message"]
    before = repo.db.total_changes
    result = actions.get_reminder(IDENTITY, ORIGIN)
    assert result["reminder"]["destination"] == "私聊" and repo.db.total_changes == before
    actions.set_reminder(IDENTITY, ORIGIN, 10, "元")
    current = repo.subscription(binding["id"])
    assert current["reminder_type"] == "balance" and current["days_threshold"] is None
    before = dict(current)
    with pytest.raises(ValueError): actions.set_reminder(IDENTITY, ORIGIN, -2, "天")
    assert repo.subscription(binding["id"]) == before
    assert "其他群聊" in actions.get_reminder(IDENTITY, "qq:FriendMessage:1001")["message"]
    assert ORIGIN not in actions.get_reminder(IDENTITY, "qq:FriendMessage:1001")["message"]
    actions.disable_reminder(IDENTITY, "qq:FriendMessage:1001")
    assert not repo.subscription(binding["id"])["enabled"]
    assert "没有开启" in actions.disable_reminder(IDENTITY, ORIGIN)["message"]


@pytest.mark.parametrize("factory", [morning_data, lambda: shapingba_data(4)])
def test_reminder_days_exactly_matches_panel_and_amount_is_actual(repo, factory):
    room, now = load(repo, factory())
    before = repo.db.total_changes
    state = reminder_snapshot(repo, room, now=now)
    model = make_model(dashboard_room_input(repo, room, now=now))
    assert state["days"] == model["forecast"]["days"]
    assert state["actual_balance"] == model["balance"]
    assert repo.db.total_changes == before
    s = dict(reminder_type="days", days_threshold=state["days"])
    assert not qualifies(s, state)
    s["days_threshold"] = str(Decimal(state["days"]) + Decimal("0.00001"))
    assert qualifies(s, state)
    assert not qualifies(dict(reminder_type="balance", low_balance_threshold=state["actual_balance"]), state)


@pytest.mark.parametrize("cash", ["0", "-1"])
def test_actual_exhausted_days_without_prediction(repo, cash):
    data = morning_data()
    data["captures"] = [copy.deepcopy(data["captures"][-1])]
    data["captures"][0]["reading"].update(total_balance_yuan=cash, cash_balance_yuan=cash)
    room, now = load(repo, data)
    state = reminder_snapshot(repo, room, now=now)
    assert state["days"] is None
    assert qualifies(dict(reminder_type="days", days_threshold="2"), state)
    assert "请尽快充值" in messages.alert(room, state, include_days=True)
    assert "停电" not in messages.alert(room, state, include_days=True)


def test_settled_morning_reminder_uses_same_actual_balance_forecast_as_panel(repo):
    data = morning_data()
    data["captures"][-1]["reading"].update(total_balance_yuan="78.18", cash_balance_yuan="78.18")
    room, now = load(repo, data)
    before = repo.db.total_changes
    state = reminder_snapshot(repo, room, now=now)
    model = make_model(dashboard_room_input(repo, room, now=now))
    assert state["days"] == model["forecast"]["days"]
    assert state["estimated_balance"] is None and state["actual_balance"] == "78.18"
    threshold = Decimal(state["days"])
    assert not qualifies(dict(reminder_type="days", days_threshold=str(threshold)), state)
    assert qualifies(dict(reminder_type="days", days_threshold=str(threshold + Decimal("0.01"))), state)
    assert "若扣除" not in messages.alert(room, state, include_days=True)
    assert repo.db.total_changes == before


def test_projected_exhausted_triggers_days_but_not_amount(repo):
    data = morning_data()
    for source in data["captures"][-2:]:
        source["reading"].update(total_balance_yuan="5", cash_balance_yuan="5")
    room, now = load(repo, data)
    state = reminder_snapshot(repo, room, now=now)
    assert Decimal(state["estimated_balance"]) < 0 and state["days"] is None
    assert qualifies(dict(reminder_type="days", days_threshold="2"), state)
    assert not qualifies(dict(reminder_type="balance", low_balance_threshold="4"), state)
    text = messages.alert(room, state, include_days=True)
    assert "若扣除这次预估电费" in text and "尚未扣费" not in text


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_later_failed_or_interrupted_capture_suppresses_alert(repo, status):
    data = morning_data()
    source = copy.deepcopy(data["captures"][-1])
    source.update(id=100, status=status, started_at="2026-10-16T09:00:40+08:00", finished_at="2026-10-16T09:00:50+08:00")
    data["captures"].append(source)
    room, now = load(repo, data)
    assert reminder_snapshot(repo, room, now=now) is None


def test_stale_and_insufficient_prediction_not_invented(repo):
    data = morning_data()
    data["captures"] = data["captures"][-1:]
    room, now = load(repo, data)
    state = reminder_snapshot(repo, room, now=now)
    assert not qualifies(dict(reminder_type="days", days_threshold="100"), state)
    assert reminder_snapshot(repo, room, now=now+timedelta(days=1)) is None


def test_mixed_group_merge_no_thresholds_private_isolation_and_repeat(repo):
    room, now = load(repo, morning_data())
    sub(repo, room, user="1001", threshold="100")
    sub(repo, room, user="1002", threshold="20", kind="days")
    sub(repo, room, user="1003", threshold="1", kind="days")
    sub(repo, room, user="1004", group="", threshold="100")
    s, sent = scheduler(repo)
    day = now.date().isoformat()
    async def run():
        s.queue_low_balance(room, day, now=now)
        await s.deliver(now=now)
        s.queue_low_balance(room, day, now=now)
        await s.deliver(now=now)
    asyncio.run(run())
    assert len(sent) == 2
    group = next(item for item in sent if item[0] == ORIGIN)
    assert group[1] == ["1001", "1002"] and "预估电费" in group[2]
    assert "阈值" not in group[2] and "100 元" not in group[2] and "关闭提醒" not in group[2]
    private = next(item for item in sent if item[0] != ORIGIN)
    assert private[1] == ["1004"] and "85.71" in private[2]
    assert "78.18" not in private[2]


def test_cancelled_queue_reused_after_switch_and_late_subscriber(repo):
    room, now = load(repo, morning_data())
    b = sub(repo, room)
    s, sent = scheduler(repo)
    day = now.date().isoformat()
    s.queue_low_balance(room, day, now=now)
    repo.subscribe(b["id"], True, "1", reminder_type="days")
    asyncio.run(s.deliver(now=now))
    assert not sent and repo.one("SELECT status FROM notifications")["status"] == "cancelled"
    repo.subscribe(b["id"], True, "20", reminder_type="days")
    s.queue_low_balance(room, day, now=now)
    asyncio.run(s.deliver(now=now))
    assert len(sent) == 1 and sent[0][1] == ["1001"]
    sub(repo, room, user="1002")
    s.queue_low_balance(room, day, now=now)
    asyncio.run(s.deliver(now=now))
    assert len(sent) == 2 and sent[1][1] == ["1002"]


@pytest.mark.parametrize("change", ["off", "move", "rebind", "revoke"])
def test_queue_rechecks_changed_subscription_and_permission(repo, change):
    room, now = load(repo, morning_data())
    b = sub(repo, room)
    s, sent = scheduler(repo)
    day = now.date().isoformat()
    s.queue_low_balance(room, day, now=now)
    if change == "off": repo.subscribe(b["id"], False, 100)
    elif change == "move": repo.subscribe(b["id"], True, 100, group_id="", origin="qq:FriendMessage:1001")
    elif change == "rebind": repo.bind(IDENTITY, repo.room(Room.from_account("A7511"))["id"], ORIGIN)
    else: s.allowed = lambda *args: False
    asyncio.run(s.deliver(now=now))
    assert not sent


def test_uncertain_not_replayed_and_next_day_can_notify(repo):
    room, now = load(repo, morning_data())
    b = sub(repo, room)
    s, sent = scheduler(repo)
    calls = []
    async def uncertain(*args):
        calls.append(1)
        raise TimeoutError()
    s.sender = uncertain
    for _ in range(2):
        s.queue_low_balance(room, now.date().isoformat(), now=now)
        asyncio.run(s.deliver(now=now))
    assert len(calls) == 1 and repo.one("SELECT status FROM notifications")["status"] == "uncertain"
    data = morning_data()
    for source in data["captures"]:
        for key in ["started_at", "finished_at"]:
            source[key] = (datetime.fromisoformat(source[key]) + timedelta(days=1)).isoformat()
    load(repo, data)
    tomorrow = now + timedelta(days=1)
    s.queue_low_balance(room, tomorrow.date().isoformat(), now=tomorrow)
    asyncio.run(s.deliver(now=tomorrow))
    assert len(calls) == 2


def test_type_and_destination_changes_do_not_reset_daily_dedup(repo):
    room, now = load(repo, morning_data())
    b = sub(repo, room)
    s, sent = scheduler(repo)
    s.queue_low_balance(room, now.date().isoformat(), now=now)
    asyncio.run(s.deliver(now=now))
    repo.subscribe(b["id"], True, "100", reminder_type="days", group_id="")
    s.queue_low_balance(room, now.date().isoformat(), now=now)
    asyncio.run(s.deliver(now=now))
    assert len(sent) == 1


def test_delivery_rechecks_after_waiting_for_sender_lock(repo):
    room, now = load(repo, morning_data())
    b = sub(repo, room)
    s, sent = scheduler(repo)
    s.queue_low_balance(room, now.date().isoformat(), now=now)
    async def run():
        lock = s.service.identity_lock(IDENTITY)
        async with lock:
            delivery = asyncio.create_task(s.deliver(now=now))
            await asyncio.sleep(0)
            assert not delivery.done() and not sent
            repo.subscribe(b["id"], False, "100")
        await delivery
    asyncio.run(run())
    assert not sent


def test_current_reading_updated_while_notification_waits_is_used(repo):
    data = morning_data()
    room, now = load(repo, data)
    sub(repo, room)
    s, sent = scheduler(repo)
    s.queue_low_balance(room, now.date().isoformat(), now=now)
    last = copy.deepcopy(data["captures"][-1])
    last.update(started_at="2026-10-16T09:01:00+08:00", finished_at="2026-10-16T09:01:10+08:00")
    last["reading"].update(total_balance_yuan="200", cash_balance_yuan="200")
    load(repo, dict(data, captures=[last]))
    asyncio.run(s.deliver(now=now+timedelta(minutes=1)))
    assert not sent


def test_days_alert_suppressed_after_manual_recharge_until_panel_is_consistent(repo):
    data = shapingba_data(4)
    room, now = load(repo, data)
    last = copy.deepcopy(data["captures"][-1])
    later = datetime.fromisoformat(last["finished_at"]) + timedelta(minutes=10)
    last.update(started_at=later.isoformat(), finished_at=later.isoformat())
    last["reading"].update(total_balance_yuan="200", cash_balance_yuan="200")
    load(repo, dict(data, captures=[last]))
    state = reminder_snapshot(repo, room, now=max(now, later))
    assert state["actual_balance"] == "200" and state["days"] is None
    assert not qualifies(dict(reminder_type="days", days_threshold="1000"), state)


def test_new_day_with_fresh_reading_reminds_again_after_success(repo):
    data = morning_data()
    room, now = load(repo, data)
    sub(repo, room)
    s, sent = scheduler(repo)
    s.queue_low_balance(room, now.date().isoformat(), now=now)
    asyncio.run(s.deliver(now=now))
    last = copy.deepcopy(data["captures"][-1])
    for key in ["started_at", "finished_at"]:
        last[key] = (datetime.fromisoformat(last[key])+timedelta(days=1)).isoformat()
    load(repo, dict(data, captures=[last]))
    tomorrow = now+timedelta(days=1)
    s.queue_low_balance(room, tomorrow.date().isoformat(), now=tomorrow)
    asyncio.run(s.deliver(now=tomorrow))
    assert len(sent) == 2


def test_migration_v4_additive_and_downgrade_keeps_new_captures(tmp_path):
    path = tmp_path/"old.db"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute("INSERT INTO rooms VALUES(1,'huxi','竹园5栋','B5344','2026-10-01')")
    db.execute("INSERT INTO user_bindings VALUES(1,'qq','123','1001',1,?,'2026-10-01','2026-10-01')", (ORIGIN,))
    db.execute("INSERT INTO subscriptions(binding_id,platform_id,group_id,origin,enabled,low_balance_threshold,last_notified_day) VALUES(1,'qq','123',?,1,'12.5','2026-10-16')", (ORIGIN,))
    db.execute("PRAGMA user_version=4")
    before = db.execute("SELECT * FROM subscriptions").fetchall()
    db.commit(); db.close()
    repo = Repository(path, recover=False)
    after = repo.all("SELECT * FROM subscriptions")
    assert tuple(after[0].values())[:len(before[0])] == before[0]
    assert after[0]["reminder_type"] == "balance" and after[0]["days_threshold"] is None
    sub(repo, repo.one("SELECT * FROM rooms"), user="1002", kind="days", threshold="2")
    q = repo.begin(1, "manual"); repo.finish(q, Reading(Decimal("8")))
    captures = repo.all("SELECT * FROM query_runs")
    repo.close()
    spec = importlib.util.spec_from_file_location("downgrade", Path(__file__).parents[1]/"deploy"/"downgrade_v013.py")
    script = importlib.util.module_from_spec(spec); spec.loader.exec_module(script)
    backup = tmp_path/"schema5.db"
    script.downgrade(path, backup)
    db = sqlite3.connect(path); db.row_factory = sqlite3.Row
    assert db.execute("PRAGMA user_version").fetchone()[0] == 4
    assert [dict(r) for r in db.execute("SELECT * FROM query_runs")] == captures
    assert db.execute("SELECT enabled FROM subscriptions WHERE binding_id=1").fetchone()[0] == 1
    assert db.execute("SELECT enabled FROM subscriptions WHERE binding_id=2").fetchone()[0] == 0
    assert "reminder_type" not in {r[1] for r in db.execute("PRAGMA table_info(subscriptions)")}
    db.close()
    check = sqlite3.connect(backup)
    assert check.execute("PRAGMA user_version").fetchone()[0] == 5
    check.close()


def tool(plugin, name, event=None, *args):
    return json.loads(asyncio.run(getattr(plugin, name)(event or Event(), *args)))


def test_operation_tool_signatures_cannot_choose_identity():
    expected = {"set_my_electricity_reminder": ["value", "unit"], "get_my_electricity_reminder": [],
                "disable_my_electricity_reminder": [], "bind_my_electricity": ["room"],
                "unbind_my_electricity": [], "send_my_electricity_panel": []}
    for name, args in expected.items():
        assert list(inspect.signature(getattr(CquElectricityPlugin, name)).parameters) == ["self", "event", *args]


@pytest.mark.parametrize("group", ["123", ""])
def test_tool_reminders_sender_isolation_no_capture(plugin, group):
    bind(plugin); bind(plugin, "1002", room="D1103")
    class NoFetch:
        async def fetch(self, *args): raise AssertionError("No school access")
    plugin.service.client = NoFetch()
    event = Event(group=group)
    before = plugin.service.repo.one("SELECT COUNT(*) n FROM query_runs")["n"]
    assert tool(plugin, "set_my_electricity_reminder", event, 2, "天")["ok"]
    status = tool(plugin, "get_my_electricity_reminder", event)
    assert status["reminder"] == dict(enabled=True, value="2", unit="天", destination="本会话")
    assert "1002" not in json.dumps(status)
    assert tool(plugin, "disable_my_electricity_reminder", event)["ok"]
    assert not tool(plugin, "get_my_electricity_reminder", event)["reminder"]["enabled"]
    assert plugin.service.repo.one("SELECT COUNT(*) n FROM query_runs")["n"] == before


@pytest.mark.parametrize("name,args", [("set_my_electricity_reminder", (2,"天")),
    ("get_my_electricity_reminder", ()), ("disable_my_electricity_reminder", ()),
    ("bind_my_electricity", ("B5344",)), ("unbind_my_electricity", ()), ("send_my_electricity_panel", ())])
def test_tools_reject_disallowed_conversation(plugin, name, args):
    assert tool(plugin, name, Event(group="999"), *args)["error_code"] == "FORBIDDEN"
    plugin.config["enable_private"] = False
    assert tool(plugin, name, Event(group=""), *args)["error_code"] == "FORBIDDEN"


def test_bind_tool_validates_preserves_old_on_failure_and_deduplicates(plugin):
    old = bind(plugin)
    tool(plugin, "set_my_electricity_reminder", Event(), 10, "元")
    class Failing:
        async def fetch(self, *args): raise QueryError("ROOM_NOT_FOUND")
    plugin.service.client = Failing()
    result = tool(plugin, "bind_my_electricity", Event(), "B5344")
    assert not result["ok"] and "都保留" in result["message"]
    assert plugin.service.repo.binding(("qq-test","123","1001"))["room_id"] == old["id"]
    assert plugin.service.repo.room_subscribers(old["id"])
    class Success:
        calls = 0
        async def fetch(self, *args):
            self.calls += 1
            return Reading(Decimal("8"))
    client = Success(); plugin.service.client = client
    # A different room avoids the prior room's intentional failure cooldown.
    event = Event()
    a = tool(plugin, "bind_my_electricity", event, "虎溪 梅园7栋 511")
    b = tool(plugin, "bind_my_electricity", event, "虎溪 梅园7栋 511")
    assert a == b and a["ok"] and client.calls == 1
    assert "原宿舍的提醒已关闭" in a["message"]
    assert not plugin.service.repo.all("SELECT * FROM subscriptions")
    assert tool(plugin, "unbind_my_electricity")["ok"]
    assert plugin.service.repo.all("SELECT * FROM query_runs")


def test_same_room_rebinding_keeps_reminder(plugin):
    row = bind(plugin)
    tool(plugin, "set_my_electricity_reminder", Event(), 10, "元")
    result = tool(plugin, "bind_my_electricity", Event(group=""), "D1102")
    assert result["ok"] and "保持不变" in result["message"]
    assert plugin.service.repo.room_subscribers(row["id"])[0]["origin"] == "qq-test:GroupMessage:123"


def test_panel_tool_sends_once_no_path_leak_and_no_capture(plugin, tmp_path):
    bind(plugin)
    path = tmp_path/"panel.png"; path.write_bytes(b"fixture")
    async def render(data): return str(path)
    plugin.dashboard.render = render
    event = Event(); event.chain_result = lambda parts: parts
    before = plugin.service.repo.db.total_changes
    a = tool(plugin, "send_my_electricity_panel", event)
    b = tool(plugin, "send_my_electricity_panel", event)
    assert a == b and a["image_sent"] and len(event.responses) == 1
    assert str(path) not in json.dumps(a) and plugin.service.repo.db.total_changes == before


def test_panel_tool_failure_sends_summary_and_send_error_is_not_success(plugin):
    bind(plugin)
    async def render(data): raise RuntimeError("Unavailable")
    plugin.dashboard.render = render
    event = Event()
    result = tool(plugin, "send_my_electricity_panel", event)
    assert result["ok"] and not result["image_sent"] and result["text_sent"]
    assert "5.00" in event.responses[0]
    async def failed_send(result): raise TimeoutError()
    event = Event(); event.send = failed_send
    result = tool(plugin, "send_my_electricity_panel", event)
    assert not result["ok"] and result["error_code"] == "INTERNAL"


def test_command_new_syntax_and_tool_share_results(plugin):
    row = bind(plugin)
    event = Event(message="/电费 提醒 2.5 天")
    asyncio.run(plugin.electricity(event))
    before = tool(plugin, "get_my_electricity_reminder")["reminder"]
    assert before["unit"] == "天" and before["value"] == "2.5"
    plugin.limits.clear()
    event = Event(message="/电费 提醒 10元", msg_id="amount")
    asyncio.run(plugin.electricity(event))
    assert "不再生效" in event.responses[0] and "改好了" not in event.responses[0]
    assert tool(plugin, "get_my_electricity_reminder")["reminder"]["unit"] == "元"
    saved = plugin.service.repo.subscription(plugin.service.repo.binding(("qq-test","123","1001"))["id"])
    plugin.limits.clear()
    event = Event(message="/电费 提醒 10元 2天", msg_id="bad")
    asyncio.run(plugin.electricity(event))
    assert plugin.service.repo.subscription(saved["binding_id"]) == saved


def test_admin_disabled_and_unbound_tools_dont_write_or_claim_success(plugin):
    assert tool(plugin, "set_my_electricity_reminder", Event(), 2, "天")["error_code"] == "NOT_BOUND"
    assert tool(plugin, "send_my_electricity_panel")["error_code"] == "NOT_BOUND"
    row = bind(plugin)
    plugin.config["reminder_enabled"] = False
    before = plugin.service.repo.db.total_changes
    assert tool(plugin, "set_my_electricity_reminder", Event(), 2, "天")["error_code"] == "REMINDERS_DISABLED"
    assert plugin.service.repo.db.total_changes == before


def test_delivery_does_not_send_to_reused_binding_id_under_old_user_lock(repo):
    room, now = load(repo, morning_data())
    old = sub(repo, room)
    s, sent = scheduler(repo)
    s.queue_low_balance(room, now.date().isoformat(), now=now)

    async def run():
        async with s.service.identity_lock(IDENTITY):
            delivery = asyncio.create_task(s.deliver(now=now))
            await asyncio.sleep(0)
            assert not delivery.done()
            repo.unbind(IDENTITY)
            new = sub(repo, room, user="1002")
            assert new["id"] == old["id"]
        await delivery

    asyncio.run(run())
    assert not sent
