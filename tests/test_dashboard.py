import asyncio
from datetime import datetime, timezone, timedelta
import pytest
from cqu_plugin.dashboard import dashboard_input, dashboard_html
from cqu_plugin.dashboard_model import make_model
from cqu_plugin.models import Room
from test_history_captures import env, capture, IDENTITY
from test_plugin import plugin, bind, Event

BJ=timezone(timedelta(hours=8))

def test_dashboard_adapter_bound_room_only_and_no_writes(env):
    repo,service,room=env
    other=repo.room(Room.from_account("A7511"))
    capture(repo,room,"2026-10-02T14:00:20","100","10")
    capture(repo,room,"2026-10-03T14:00:20","103","8.38")
    capture(repo,other,"2026-10-03T14:00:20","999","987")
    capture(repo,room,"2026-10-03T15:00:00","0",error="TIMEOUT")
    before=repo.db.total_changes
    data=dashboard_input(repo,IDENTITY,now=datetime(2026,10,3,16,tzinfo=BJ))
    assert len(data["captures"])==3 and make_model(data)["balance"]=="8.38"
    assert repo.db.total_changes==before
    assert "987" not in str(data) and "A7511" not in str(data)
    assert dashboard_input(repo,("qq","123","unknown")) is None

def test_dashboard_html_escapes_room_input(env):
    repo,service,room=env
    capture(repo,room,"2026-10-03T14:00:20","100")
    model=make_model(dashboard_input(repo,IDENTITY,now=datetime(2026,10,3,16,tzinfo=BJ)))
    model["room"]["building"]="</script><script>bad()</script>"
    html=dashboard_html(model)
    assert "</script><script>bad()" not in html and r"\u003c/script" in html

@pytest.mark.parametrize("group",["123",""])
def test_command_returns_local_image_without_fetch_or_writes(plugin,tmp_path,group):
    bind(plugin)
    class NoFetch:
        async def fetch(self,*args):raise AssertionError("Dashboard must not collect")
    plugin.service.client=NoFetch()
    output=tmp_path/"report.png";output.write_bytes(b"fixture")
    async def render(data):
        assert data["room"]["account"]=="D1102"
        return str(output)
    plugin.dashboard.render=render
    before=plugin.service.repo.db.total_changes
    event=Event(group=group);event.chain_result=lambda components:components
    asyncio.run(plugin.electricity(event))
    assert len(event.responses)==1 and len(event.responses[0])==1
    assert plugin.service.repo.db.total_changes==before

def test_renderer_failure_falls_back_to_saved_text(plugin):
    bind(plugin)
    async def unavailable(data):raise RuntimeError("Browser unavailable")
    plugin.dashboard.render=unavailable
    event=Event();before=plugin.service.repo.db.total_changes
    asyncio.run(plugin.electricity(event))
    assert "5.00" in event.responses[0] and plugin.service.repo.db.total_changes==before

def test_unbound_command_does_not_render(plugin):
    async def forbidden(data):raise AssertionError("Unbound user must not render")
    plugin.dashboard.render=forbidden
    event=Event();asyncio.run(plugin.electricity(event))
    assert "尚未绑定" in event.responses[0]
