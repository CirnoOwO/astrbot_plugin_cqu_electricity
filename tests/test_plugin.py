import asyncio
import ast
import json
import inspect
from pathlib import Path
from types import SimpleNamespace
import pytest

from cqu_plugin import main
from cqu_plugin.main import CquElectricityPlugin
from cqu_plugin.models import Room,Reading


class Context:
    def __init__(self):self.white=["123"]
    def get_config(self,umo=None):return {"platform_settings":{"enable_id_white_list":True,"id_whitelist":self.white}}
    def get_platform_inst(self,platform):return SimpleNamespace(meta=lambda:SimpleNamespace(name="aiocqhttp")) if platform=="qq-test" else None


class Event:
    def __init__(self,user="1001",group="123",message="/电费",msg_id="test-message"):
        self.user,self.group,self.message_str=user,group,message
        self.unified_msg_origin="qq-test:GroupMessage:"+group if group else "qq-test:FriendMessage:"+user
        self.message_obj=SimpleNamespace(message_id=msg_id)
        self.role="member";self.responses=[];self.stopped=False
    def is_private_chat(self):return not self.group
    def get_message_type(self):
        from astrbot.core.platform.message_type import MessageType
        return MessageType.FRIEND_MESSAGE if not self.group else MessageType.GROUP_MESSAGE
    def get_extra(self,key,default=None):return getattr(self,"extras",{}).get(key,default)
    def set_extra(self,key,value):
        if not hasattr(self,"extras"):self.extras={}
        self.extras[key]=value
    def get_platform_name(self):return "aiocqhttp"
    def get_platform_id(self):return "qq-test"
    def get_group_id(self):return self.group
    def get_sender_id(self):return self.user
    def stop_event(self):self.stopped=True
    def plain_result(self,text):return text
    async def send(self,result):self.responses.append(result)


class Client:
    def configured(self):return True
    async def fetch(self,room,save):
        save(1,"reading",200,"application/json",b'{"fixture":true}')
        return Reading(__import__("decimal").Decimal("5"))

@pytest.fixture
def plugin(tmp_path,monkeypatch):
    monkeypatch.setattr(main,"get_astrbot_data_path",lambda:str(tmp_path))
    value=CquElectricityPlugin(Context(),{"auto_capture_enabled":False})
    asyncio.run(value.initialize())
    value.service.client=Client()
    yield value
    asyncio.run(value.terminate())


def bind(plugin,user="1001",group="123",room="D1102"):
    row=plugin.service.repo.room(Room("huxi","兰园1栋",room))
    plugin.service.repo.bind(("qq-test",group,user),row["id"],"qq-test:GroupMessage:"+group)
    asyncio.run(plugin.service.query(row))
    return row


def test_tools_accept_no_identity():
    assert list(inspect.signature(CquElectricityPlugin.get_my_electricity).parameters)==["self","event"]
    assert list(inspect.signature(CquElectricityPlugin.get_my_electricity_history).parameters)==["self","event","days"]
    tree=ast.parse(Path(main.__file__).read_text(encoding="utf-8"))
    decorators=[d for n in ast.walk(tree) if isinstance(n,ast.AsyncFunctionDef) for d in n.decorator_list if isinstance(d,ast.Call) and isinstance(d.func,ast.Attribute) and d.func.attr=="llm_tool"]
    assert len(decorators)==8


def test_llm_current_user_only(plugin):
    bind(plugin);bind(plugin,"1002",room="D1103")
    a=json.loads(asyncio.run(plugin.get_my_electricity(Event())))
    b=json.loads(asyncio.run(plugin.get_my_electricity(Event("1002"))))
    assert a["room"]["room_number"]=="D1102" and b["room"]["room_number"]=="D1103"
    assert "1002" not in json.dumps(a)
    with pytest.raises(TypeError):asyncio.run(plugin.get_my_electricity(Event(),user_id="1002"))


def test_llm_unbound_does_not_bind(plugin):
    data=json.loads(asyncio.run(plugin.get_my_electricity(Event())))
    assert data["error_code"]=="NOT_BOUND"
    assert not plugin.service.repo.all("SELECT * FROM user_bindings")


def test_llm_group_whitelist_enforced(plugin):
    bind(plugin,group="999")
    data=json.loads(asyncio.run(plugin.get_my_electricity(Event(group="999"))))
    assert data["error_code"]=="FORBIDDEN"


def test_llm_history_read_only(plugin):
    bind(plugin)
    before=plugin.service.repo.one("SELECT COUNT(*) AS n FROM query_runs")["n"]
    data=json.loads(asyncio.run(plugin.get_my_electricity_history(Event(),7)))
    assert data["ok"] and len(data["records"])==1
    assert plugin.service.repo.one("SELECT COUNT(*) AS n FROM query_runs")["n"]==before
    assert not plugin.service.repo.all("SELECT * FROM subscriptions")


def test_command_binding(plugin):
    event=Event(message="/电费 绑定 虎溪 兰园1栋 D1102")
    asyncio.run(plugin.electricity(event))
    assert event.stopped and "已绑定" in event.responses[0]
    assert plugin.service.repo.binding(("qq-test","123","1001"))["room_number"]=="D1102"


def test_command_reminder_only_self(plugin):
    bind(plugin);bind(plugin,"1002")
    event=Event(message="/电费 提醒 开启 10")
    asyncio.run(plugin.electricity(event))
    rows=plugin.service.repo.all("SELECT b.user_id FROM subscriptions s JOIN user_bindings b ON b.id=s.binding_id WHERE s.enabled=1")
    assert rows==[{"user_id":"1001"}]


def test_command_duplicate_message(plugin):
    bind(plugin)
    event=Event(message="/电费 解绑")
    async def run():await plugin.electricity(event);await plugin.electricity(event)
    asyncio.run(run())
    assert len(event.responses)==1


def test_revoked_whitelist_blocks_tools(plugin):
    bind(plugin);plugin.context.white=["999"]
    result=json.loads(asyncio.run(plugin.get_my_electricity(Event())))
    assert result["error_code"]=="FORBIDDEN"


def test_tool_history_days_rejected(plugin):
    bind(plugin)
    result=json.loads(asyncio.run(plugin.get_my_electricity_history(Event(),1.5)))
    assert result["error_code"]=="INVALID_DAYS"


@pytest.mark.parametrize("command,account",[("/电费 绑定 B10S306","B10S306"),("/电费 绑定 沙坪坝 A1S123","A1S123"),("/电费 绑定 沙坪坝 A区12舍 3单元112","A12S3112"),("/电费 绑定 沙坪坝 A区12舍 3单元 112","A12S3112"),("/电费 绑定 B区7舍 7003","B7S003")])
def test_shapingba_command_formats(plugin,command,account):
    event=Event(message=command)
    asyncio.run(plugin.electricity(event))
    binding=plugin.service.repo.binding(("qq-test","123","1001"))
    assert binding["campus"]=="shapingba" and binding["room_number"]==account
    assert "已绑定" in event.responses[0]


def test_explicit_wrong_campus_does_not_bind(plugin):
    event=Event(message="/电费 绑定 虎溪 A1S123")
    asyncio.run(plugin.electricity(event))
    assert not plugin.service.repo.binding(("qq-test","123","1001"))
    assert "校区不一致" in event.responses[0]
