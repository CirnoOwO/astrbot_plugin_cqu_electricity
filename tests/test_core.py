import asyncio
import json
from datetime import datetime,timezone,timedelta
from decimal import Decimal
import httpx
import pytest

from cqu_plugin.models import Room,Reading,QueryError,number
from cqu_plugin.client import ElectricityClient,parse_reading,redact_body,load_token
from cqu_plugin.repository import Repository
from cqu_plugin.service import ElectricityService
from cqu_plugin.scheduler import Scheduler,daily_time
from cqu_plugin.summary import summary,usage

ROOM=Room("huxi","兰园1栋","D1102")
IDENTITY=("qq-test","123","1001")
ORIGIN="qq-test:GroupMessage:123"

@pytest.fixture
def repo(tmp_path):
    value=Repository(tmp_path/"electricity.db")
    yield value
    value.close()

@pytest.fixture
def auth(tmp_path):
    p=tmp_path/"auth.json";p.write_text(json.dumps({"access_token":"offline-token"}),encoding="utf-8")
    return p


def transport(final=None,status=200):
    def handle(request):
        form=dict(httpx.QueryParams(request.content.decode()))
        if form["type"]=="IEC":
            data=final or {"data":{"balance":"84.57","meterreading":"1000.25","subsidy":"20"},"showData":{"余额":"84.57"}}
        elif form["level"]=="0":
            data={"total":[{"code":"building"},{"code":"room"}],"data":[{"name":"兰园1栋","value":"b1"}]}
        else: data={"data":[{"name":"D1102","value":"r1"}]}
        return httpx.Response(status,json={"code":200,"map":data})
    return httpx.MockTransport(handle)

@pytest.mark.parametrize("bad",["NaN","Infinity",True,"12abc","1.2.3","约12元"])
def test_numeric_rejection(bad):
    with pytest.raises(ValueError):number(bad)

@pytest.mark.parametrize("value,expected",[("84.57元","84.57"),("20 度","20"),("-1.50","-1.50")])
def test_numeric(value,expected):assert number(value)==Decimal(expected)


def test_huxi_fields():
    reading=parse_reading(ROOM,{"data":{"balance":"84.57","reading":"1000.25","subsidy":"20"},"showData":{}},Decimal("0.54"))
    assert reading.fields()["total_balance_yuan"]=="95.37"
    assert reading.meter_reading_kwh==Decimal("1000.25")


def test_labelled_huxi():
    r=parse_reading(ROOM,{"data":{},"showData":[{"name":"余额","value":"5.00"},{"label":"电表读数","value":"1000.25"}]},Decimal("0.54"))
    assert r.cash_balance_yuan==5 and r.meter_reading_kwh==Decimal("1000.25")


def test_shapingba_fields():
    room=Room("shapingba","A区1号学生宿舍","A1S123")
    r=parse_reading(room,{"data":{"roomID":"A1S123","cashBalance":"84.57","subsidiesBalance":"10","price":"0.54"},"showData":{}},Decimal("0.54"))
    assert r.fields()["total_balance_yuan"]=="94.57" and r.meter_reading_kwh is None


def test_wrong_room_rejected():
    with pytest.raises(QueryError,match="ROOM_MISMATCH"):
        parse_reading(ROOM,{"data":{"room":"OTHER","balance":"10"},"showData":{}},Decimal("0.54"))


def test_optional_meter_not_zero():
    r=parse_reading(ROOM,{"data":{"balance":"10"},"showData":{}},Decimal("0.54"))
    assert r.meter_reading_kwh is None and r.subsidy_kwh is None


def test_token_missing(tmp_path):
    with pytest.raises(QueryError,match="NO_AUTH"):load_token(tmp_path/"missing")


def test_token_never_stored(repo,auth):
    final={"data":{"balance":"10","access_token":"response-secret","subsidy":"0"},"showData":{"余额":"10"},"echo":"offline-token"}
    client=ElectricityClient(auth,transport=transport(final))
    room=repo.room(ROOM);service=ElectricityService(repo,client)
    result=asyncio.run(service.query(room))
    assert result["ok"]
    responses=repo.all("SELECT raw_body FROM query_responses")
    assert len(responses)==3
    raw=b"".join(r["raw_body"] for r in responses)
    assert b"offline-token" not in raw and b"response-secret" not in raw and b"[REDACTED]" in raw
    assert "raw_body" not in json.dumps(result)


def test_unmodified_raw_bytes():
    body=b'{ "code" : 200, "map" : {} }'
    assert redact_body(body,"offline-token")==body


def test_html_secret_redaction():
    raw=redact_body(b'<html>access_token="another-secret" offline-token</html>',"offline-token")
    assert b"another-secret" not in raw and b"offline-token" not in raw

@pytest.mark.parametrize("status,body",[(401,{"code":200}),(200,{"code":401}),(403,{"code":403})])
def test_auth_failure_keeps_raw(repo,auth,status,body):
    client=ElectricityClient(auth,transport=httpx.MockTransport(lambda r:httpx.Response(status,json=body)))
    room=repo.room(ROOM);service=ElectricityService(repo,client)
    result=asyncio.run(service.query(room))
    assert result["error_code"]=="AUTH_EXPIRED"
    assert repo.one("SELECT COUNT(*) AS n FROM query_responses")["n"]==1
    assert repo.latest(room["id"],"failed")["error_code"]=="AUTH_EXPIRED"


def test_login_html(repo,auth):
    client=ElectricityClient(auth,transport=httpx.MockTransport(lambda r:httpx.Response(200,text='<html>login password</html>')))
    service=ElectricityService(repo,client)
    result=asyncio.run(service.query(repo.room(ROOM)))
    assert result["error_code"]=="AUTH_EXPIRED"


class FakeClient:
    def __init__(self):self.calls=0;self.error=None;self.balance=Decimal("5")
    def configured(self):return True
    async def fetch(self,room,save):
        self.calls+=1
        await asyncio.sleep(.01)
        save(1,"reading",200,"application/json",b'{"offline":true}')
        if self.error:raise QueryError(self.error)
        return Reading(self.balance,meter_reading_kwh=Decimal(self.calls))


def setup(repo):
    client=FakeClient();service=ElectricityService(repo,client)
    row=repo.room(ROOM)
    return client,service,row


def test_user_bindings_and_rooms_isolated(repo):
    client,service,row=setup(repo)
    second=repo.room(Room("huxi","竹园2栋","B2203"))
    a=repo.bind(IDENTITY,row["id"],ORIGIN)
    b=repo.bind(("qq-test","123","1002"),second["id"],ORIGIN)
    async def run():
        await service.query(row);await service.query(second)
        return await service.mine(IDENTITY),await service.mine(("qq-test","123","1002"))
    mine,other=asyncio.run(run())
    assert mine["room"]["room_number"]=="D1102" and other["room"]["room_number"]=="B2203"
    assert set(mine)==set(other)


def test_failed_rebind_preserves_old_binding(repo):
    client,service,row=setup(repo)
    repo.bind(IDENTITY,row["id"],ORIGIN)
    client.error="ROOM_NOT_FOUND"
    with pytest.raises(QueryError):asyncio.run(service.bind(IDENTITY,Room("huxi","兰园1栋","D1103"),ORIGIN))
    assert repo.binding(IDENTITY)["room_id"]==row["id"]


def test_shared_query_and_cache(repo):
    client,service,row=setup(repo)
    async def run():
        await asyncio.gather(service.query(row,force=True),service.query(row,force=True))
        await service.query(row)
    asyncio.run(run())
    assert client.calls==1
    assert repo.one("SELECT COUNT(*) AS n FROM query_runs")["n"]==1


def test_failure_after_success_is_stale(repo):
    client,service,row=setup(repo)
    async def run():
        await service.query(row)
        client.error="NETWORK"
        first=await service.query(row,force=True)
        return first,await service.query(row)
    first,second=asyncio.run(run())
    assert first["stale"] and second["stale"] and not second["ok"]
    assert "上次成功" in summary(first)


def test_unbind_deletes_subscription_preserves_history(repo):
    client,service,row=setup(repo)
    binding=repo.bind(IDENTITY,row["id"],ORIGIN)
    repo.subscribe(binding["id"],True,10)
    asyncio.run(service.query(row,requested_by=binding["id"]))
    repo.unbind(IDENTITY)
    assert repo.binding(IDENTITY) is None
    assert not repo.all("SELECT * FROM subscriptions")
    assert repo.latest(row["id"])["requested_by"] is None


def test_restart_marks_unfinished(tmp_path):
    path=tmp_path/"db.sqlite";repo=Repository(path);row=repo.room(ROOM)
    repo.begin(row["id"],"manual");repo.close()
    repo=Repository(path)
    assert repo.one("SELECT status FROM query_runs")["status"]=="interrupted"
    assert repo.db.execute("PRAGMA user_version").fetchone()[0]==5
    repo.close()


def test_readings_delta_not_balance():
    a={"finished_at":"2026-10-03T04:00:00+00:00","meter_reading_kwh":"1001.8","total_balance_yuan":"5"}
    b={"finished_at":"2026-10-02T04:00:00+00:00","meter_reading_kwh":"1000","total_balance_yuan":"10"}
    delta=usage(a,b)
    assert Decimal(delta["kwh"])==Decimal("1.8") and delta["interval_hours"]==24 and not delta["estimated"]
    a["meter_reading_kwh"]=None
    assert not usage(a,b)["available"] and usage(a,b)["balance_change_yuan"]=="-5"


def test_meter_reset_not_negative_usage():
    a={"finished_at":"2026-10-03T04:00:00+00:00","meter_reading_kwh":"0"}
    b={"finished_at":"2026-10-02T04:00:00+00:00","meter_reading_kwh":"1000"}
    assert not usage(a,b)["available"]

@pytest.mark.parametrize("days",[0,31,-1,True,1.5,"7",None])
def test_history_days_validation(repo,days):
    client,service,row=setup(repo)
    assert service.history(IDENTITY,days)["error_code"]=="INVALID_DAYS"


def test_low_reminder_once_per_group(repo):
    client,service,row=setup(repo)
    for user in ("1001","1002"):
        b=repo.bind(("qq-test","123",user),row["id"],ORIGIN)
        repo.subscribe(b["id"],True,10)
    sent=[]
    async def send(origin,users,text):sent.append((origin,users,text));return True
    scheduler=Scheduler(service,lambda *x:True,send,capture_times=[datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M")])
    async def run():await scheduler.tick();await scheduler.tick()
    asyncio.run(run())
    assert len(sent)==1 and sent[0][1]==["1001","1002"]
    assert client.calls==1
    assert repo.one("SELECT status FROM notifications")["status"]=="sent"


def test_no_subscribers_no_send(repo):
    client,service,row=setup(repo)
    repo.bind(IDENTITY,row["id"],ORIGIN)
    async def send(*args):raise AssertionError("should not send")
    asyncio.run(Scheduler(service,lambda *x:True,send,capture_times=[datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M")]).tick())
    assert not repo.all("SELECT * FROM notifications")


def test_uncertain_send_not_retried(repo):
    client,service,row=setup(repo)
    b=repo.bind(IDENTITY,row["id"],ORIGIN);repo.subscribe(b["id"],True,10)
    sends=[]
    async def send(*args):sends.append(1);raise TimeoutError()
    scheduler=Scheduler(service,lambda *x:True,send,capture_times=[datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M")])
    async def run():await scheduler.tick();await scheduler.tick()
    asyncio.run(run())
    assert len(sends)==1 and repo.one("SELECT status FROM notifications")["status"]=="uncertain"


def test_revoked_group_does_not_capture(repo):
    client,service,row=setup(repo)
    repo.bind(IDENTITY,row["id"],ORIGIN)
    async def send(*args):raise AssertionError()
    asyncio.run(Scheduler(service,lambda *x:False,send,capture_times=[datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M")]).tick())
    assert client.calls==0

@pytest.mark.parametrize("value",["25:00","12:60","12",None])
def test_invalid_schedule(value):
    with pytest.raises(ValueError):daily_time(value)


@pytest.mark.parametrize("building,room,expected",[("竹园5栋","344","B5344"),("梅园二栋","308","A2308"),("竹园1栋","406","B1406"),("荷园2栋","306","F2306"),("菊园A1栋","612","EA612"),("菊园B2栋","521","EB521")])
def test_full_room_account_rules(building,room,expected):
    assert Room.validated("虎溪",building,room).room_number==expected

@pytest.mark.parametrize("account,building",[("B5344","竹园5栋"),("A2308","梅园2栋"),("B1406","竹园1栋"),("EA612","菊园A区"),("EB521","菊园B区")])
def test_account_shorthand(account,building):
    room=Room.from_account(account)
    assert room.room_number==account and room.building==building


def test_mismatched_building_account():
    with pytest.raises(ValueError):Room.validated("虎溪","竹园5栋","A2308")


def test_chrysanthemum_resolves_unique_building(repo,auth):
    def handler(request):
        form=dict(httpx.QueryParams(request.content.decode()))
        if form["level"]=="0":
            data={"total":[{"code":"building"},{"code":"room"}],"data":[{"name":"菊园A1栋","value":"a1"},{"name":"菊园A2栋","value":"a2"}]}
        elif form["type"]=="select":
            data={"data":[{"name":"EA612","value":"ea"}]} if form["building"]=="a1" else {"data":[]}
        else:data={"data":{"balance":"5","room":"EA612"},"showData":{"余额":"5"}}
        return httpx.Response(200,json={"code":200,"map":data})
    service=ElectricityService(repo,ElectricityClient(auth,transport=httpx.MockTransport(handler)))
    binding=asyncio.run(service.bind(IDENTITY,Room.from_account("EA612"),ORIGIN))
    assert binding["building"]=="菊园A1栋"
    assert repo.latest(binding["room_id"])
    assert repo.one("SELECT COUNT(*) AS n FROM query_responses")["n"]==4


def test_chrysanthemum_ambiguous_does_not_bind(repo,auth):
    def handler(request):
        form=dict(httpx.QueryParams(request.content.decode()))
        data={"total":[{},{}],"data":[{"name":"菊园A1栋","value":"a1"},{"name":"菊园A2栋","value":"a2"}]} if form["level"]=="0" else {"data":[{"name":"EA612","value":"ea"}]}
        return httpx.Response(200,json={"code":200,"map":data})
    service=ElectricityService(repo,ElectricityClient(auth,transport=httpx.MockTransport(handler)))
    with pytest.raises(QueryError,match="AMBIGUOUS_ROOM"):asyncio.run(service.bind(IDENTITY,Room.from_account("EA612"),ORIGIN))
    assert repo.binding(IDENTITY) is None


@pytest.mark.parametrize("account,building",[("A1S123","A区1舍"),("B10S306","B区10舍"),("A12S3112","A区12舍"),("A10S5F1","A区10舍"),("B12S024","B区12舍"),("B7S003","B区7舍"),("C5SF339","C区5舍")])
def test_shapingba_all_user_examples(account,building):
    room=Room.from_account(account)
    assert room.campus=="shapingba" and room.building==building and room.room_number==account

@pytest.mark.parametrize("building,raw,account",[("A区1舍","123","A1S123"),("B区10舍","306","B10S306"),("A区12舍","3单元112","A12S3112"),("A区12号学生宿舍","3112","A12S3112"),("A区10舍","5-1","A10S5F1"),("B区12舍","12024","B12S024"),("B区7舍","7003","B7S003"),("C区5舍","负339","C5SF339"),("C区5舍","-339","C5SF339")])
def test_shapingba_raw_room_rules(building,raw,account):
    room=Room.validated("沙坪坝",building,raw)
    assert room.room_number==account

@pytest.mark.parametrize("account,expected",[("a1s123","A1S123"),("A10S5-1","A10S5F1"),("C5S负339","C5SF339")])
def test_shapingba_account_copy_formats(account,expected):
    assert Room.from_account(account).room_number==expected

@pytest.mark.parametrize("building,raw",[("A区12舍","112"),("B区10舍","A1S123"),("A区1舍","B10S306"),("A区1舍","123F"),("A区12舍","0单元112")])
def test_shapingba_missing_unit_and_conflicts_rejected(building,raw):
    with pytest.raises(ValueError):Room.validated("沙坪坝",building,raw)


def test_shapingba_real_building_names_and_saving(repo,auth):
    calls=[]
    def handler(request):
        form=dict(httpx.QueryParams(request.content.decode())); calls.append(form)
        assert form["feeitemid"]=="449"
        if form["level"]=="0":
            data={"total":[{"code":"building"},{"code":"room"}],"data":[{"name":"B区1号学生宿舍","value":"b1"},{"name":"B区10号学生宿舍","value":"b10"}]}
        elif form["type"]=="select":
            assert form["building"]=="b10"
            data={"data":[{"name":"B10S306","value":"r306"}]}
        else:
            data={"data":{"roomID":"B10S306","cashBalance":"5.20","subsidiesBalance":"2.50","price":"0.54"},"showData":{"现金余额":"5.20"}}
        return httpx.Response(200,json={"code":200,"map":data})
    service=ElectricityService(repo,ElectricityClient(auth,transport=httpx.MockTransport(handler)))
    binding=asyncio.run(service.bind(IDENTITY,Room.from_account("B10S306"),ORIGIN))
    assert binding["building"]=="B区10号学生宿舍"
    assert binding["room_number"]=="B10S306"
    reading=repo.latest(binding["room_id"])
    assert reading["total_balance_yuan"]=="7.70" and reading["subsidy_balance_yuan"]=="2.50"
    assert reading["meter_reading_kwh"] is None
    assert len(calls)==3 and repo.one("SELECT COUNT(*) AS n FROM query_responses")["n"]==3


def test_shapingba_units_unique_match(repo,auth):
    def handler(request):
        form=dict(httpx.QueryParams(request.content.decode()))
        if form["level"]=="0":
            data={"total":[{},{}],"data":[{"name":"A区12号学生宿舍（1单元）","value":"a1"},{"name":"A区12号学生宿舍（3单元）","value":"a3"}]}
        elif form["type"]=="select":
            data={"data":[{"name":"A12S3112","value":"r"}]} if form["building"]=="a3" else {"data":[]}
        else:data={"data":{"roomID":"A12S3112","cashBalance":"5","subsidiesBalance":"0","price":"0.54"},"showData":{"现金余额":"5"}}
        return httpx.Response(200,json={"code":200,"map":data})
    service=ElectricityService(repo,ElectricityClient(auth,transport=httpx.MockTransport(handler)))
    binding=asyncio.run(service.bind(IDENTITY,Room.from_account("A12S3112"),ORIGIN))
    assert binding["building"]=="A区12号学生宿舍（3单元）"



def test_maintenance_probe_does_not_interrupt_live_query(tmp_path):
    path=tmp_path/"probe.db"
    live=Repository(path)
    room=live.room(ROOM)
    query_id=live.begin(room["id"],"manual")
    probe=Repository(path,recover=False)
    assert probe.one("SELECT status FROM query_runs WHERE id=?",(query_id,))["status"]=="running"
    probe.close()
    live.finish(query_id,Reading(Decimal("5")))
    live.close()


@pytest.mark.parametrize("value,expected",[("梅园七幢","梅园7栋"),("梅园三幢","梅园3栋"),("梅园二幢","梅园2栋"),("梅园四幢","梅园4栋"),("梅园五幢","梅园5栋"),("梅园八幢","梅园8栋"),("梅园7幢","梅园7栋"),("竹园十栋","竹园10栋"),("兰园十一幢","兰园11栋"),("松园二十幢","松园20栋"),("菊园A一幢","菊园A1栋")])
def test_chinese_building_aliases(value,expected):
    from cqu_plugin.models import building_name
    assert building_name(value)==expected

@pytest.mark.parametrize("legacy",[False,True])
def test_a7511_platform_chinese_name_and_existing_history(repo,auth,legacy):
    def handler(request):
        form=dict(httpx.QueryParams(request.content.decode()))
        if form["level"]=="0":
            data={"total":[{},{}],"data":[{"name":"梅园七幢","value":"m7"},{"name":"梅园1栋","value":"m1"}]}
        elif form["type"]=="select":
            assert form["building"]=="m7"
            data={"data":[{"name":"A7511","value":"r511"}]}
        else:data={"data":{"balance":"20","meterreading":"1000"},"showData":{"余额":"20"}}
        return httpx.Response(200,json={"code":200,"map":data})
    service=ElectricityService(repo,ElectricityClient(auth,transport=httpx.MockTransport(handler)))
    if legacy:
        row=repo.room(Room("huxi","梅园七幢","A7511"))
        repo.bind(IDENTITY,row["id"],ORIGIN)
        result=asyncio.run(service.mine(IDENTITY,force=True))
        assert result["ok"] and repo.binding(IDENTITY)["room_id"]==row["id"]
    else:
        b=asyncio.run(service.bind(IDENTITY,Room.from_account("A7511"),ORIGIN))
        assert b["building"]=="梅园7栋" and b["room_number"]=="A7511"
    assert repo.one("SELECT COUNT(*) AS n FROM query_responses")["n"]==3


def test_huxi_two_equivalent_input_forms():
    assert Room.from_account("A7511")==Room.validated("虎溪","梅园七幢","511")
    assert Room.from_account("A7511")==Room.validated("虎溪","梅园7栋","511")
# ==============================================================================
# v0.1.17 Open-Source Enhancements: Session Auth, HTTPS, and Closing Protection
# ==============================================================================

@pytest.mark.parametrize(
    "raw_session",
    [
        "clean-token",
        " 'clean-token' ",
        '"clean-token"',
        "‘clean-token’",
        "“clean-token”",
        " Bearer clean-token ",
        " 'Bearer clean-token' ",
        '"Bearer clean-token"',
        " ‘Bearer clean-token’ ",
        " \"Bearer 'clean-token'\" ",
    ],
)
def test_session_token_formats_and_outbound_headers(tmp_path, raw_session):
    requests = []
    responses = []
    mock_api = transport({
        "data": {"balance": "84.57", "echo": "clean-token"},
        "showData": {"余额": "84.57"},
    })

    def handle(request):
        requests.append(request)
        return mock_api.handle_request(request)

    # Use a non-existent auth file path to ensure credentials come strictly from session
    client = ElectricityClient(
        tmp_path / "non_existent_auth.json",
        session=raw_session,
        transport=httpx.MockTransport(handle),
    )

    assert client.configured() is True
    reading = asyncio.run(client.fetch(ROOM, lambda *args: responses.append(args[-1])))
    assert reading.cash_balance_yuan == Decimal("84.57")
    assert len(requests) == 3

    # Outbound headers and cookies must contain clean token without outer quotes
    for req in requests:
        assert req.headers["synjones-auth"] == "bearer clean-token"
        assert req.headers["cookie"] == "synjones-auth=bearer clean-token"

    # Verify secret redaction
    raw_response = b"".join(responses)
    assert b"clean-token" not in raw_response
    assert b"[REDACTED]" in raw_response


@pytest.mark.parametrize("session", ["", "   ", None])
def test_empty_session_falls_back_to_token_file(auth, session):
    assert load_token(auth, session=session) == "offline-token"
    client = ElectricityClient(auth, session=session)
    assert client.configured() is True


@pytest.mark.parametrize(
    "invalid_session",
    [
        123,
        {"access_token": "foo"},
        "token with spaces",
        "x" * 16385,
        "'unmatched-double\"",
        '"unmatched-single\'',
        "‘unmatched-single'",
        "token;injection=1",
        "token\x00null",
        "token\r\nnewline",
        "token\x1fcontrol",
        "token\x7fdel",
        "token\u200bzerowidth",
        "token\u3000fullwidth",
        "中文token测试",
    ],
)
def test_invalid_session_fails_fast_without_network(auth, invalid_session):
    requests = []
    client = ElectricityClient(
        auth,
        session=invalid_session,
        transport=httpx.MockTransport(lambda r: requests.append(r)),
    )
    assert client.configured() is False
    with pytest.raises(QueryError, match="^AUTH_INVALID$"):
        asyncio.run(client.fetch(ROOM, lambda *args: None))
    assert len(requests) == 0


def test_empty_session_and_missing_file_raises_no_auth(tmp_path):
    client = ElectricityClient(tmp_path / "missing.json", session="")
    assert client.configured() is False
    with pytest.raises(QueryError, match="^NO_AUTH$"):
        asyncio.run(client.fetch(ROOM, lambda *args: None))

    # Also test whitespace-only session with quotes stripped to empty
    client_empty_quotes = ElectricityClient(tmp_path / "missing.json", session=" '   ' ")
    assert client_empty_quotes.configured() is False
    with pytest.raises(QueryError, match="^NO_AUTH$"):
        asyncio.run(client_empty_quotes.fetch(ROOM, lambda *args: None))


@pytest.mark.parametrize(
    "scheme, expected_scheme",
    [(None, "https"), ("http", "http"), ("https", "https")],
)
def test_default_https_and_explicit_http_scheme(auth, scheme, expected_scheme):
    requests = []
    mock_api = transport()

    def handle(request):
        requests.append(request)
        return mock_api.handle_request(request)

    kwargs = {} if scheme is None else {"scheme": scheme}
    client = ElectricityClient(auth, transport=httpx.MockTransport(handle), **kwargs)
    reading = asyncio.run(client.fetch(ROOM, lambda *args: None))
    assert reading.cash_balance_yuan == Decimal("84.57")
    assert len(requests) == 3
    assert all(r.url.scheme == expected_scheme for r in requests)


def test_tls_failure_does_not_retry_over_http(auth):
    requests = []

    def handle(request):
        requests.append(request)
        raise httpx.ConnectError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed",
            request=request,
        )

    client = ElectricityClient(auth, transport=httpx.MockTransport(handle))
    with pytest.raises(QueryError, match="NETWORK"):
        asyncio.run(client.fetch(ROOM, lambda *args: None))

    assert len(requests) == 1
    assert requests[0].url.scheme == "https"


def test_service_closing_rejects_new_queries_and_cancels_inflight(tmp_path):
    repo = Repository(tmp_path / "closing_test.db")
    room_first = repo.room(ROOM)
    room_second = repo.room(Room("huxi", "兰园1栋", "D1103"))

    fetch_entered = asyncio.Event()
    cleanup_entered = asyncio.Event()
    release_cleanup = asyncio.Event()

    class StallingClient:
        async def fetch(self, room, save):
            fetch_entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_entered.set()
                await release_cleanup.wait()

    service = ElectricityService(repo, StallingClient())

    async def scenario():
        # Start inflight query on room_first
        query_task = asyncio.create_task(service.query(room_first))
        await fetch_entered.wait()

        # Initiate service closing
        close_task = asyncio.create_task(service.close())
        await cleanup_entered.wait()

        try:
            # Query on room_second while closing must be rejected immediately
            with pytest.raises(QueryError, match="NOT_READY"):
                await asyncio.wait_for(service.query(room_second), timeout=1.0)
            assert room_second["id"] not in service.inflight
        finally:
            release_cleanup.set()
            await close_task

        # Original in-flight task was cancelled during close
        with pytest.raises(asyncio.CancelledError):
            await query_task

        # Query after close must also be rejected
        with pytest.raises(QueryError, match="NOT_READY"):
            await service.query(room_second)

    asyncio.run(asyncio.wait_for(scenario(), timeout=5.0))


def test_redact_body_json_unicode_escaped_token():
    # Tokens escaped in JSON strings (e.g. \u002d for '-') must be redacted
    # when decoded by json.loads in redact_body.
    body = b'{"echo": "clean\\u002dtoken", "nested": {"val": "clean\\u002dtoken"}}'
    redacted = redact_body(body, "clean-token")
    assert b"clean-token" not in redacted
    assert b"\\u002d" not in redacted
    assert b"[REDACTED]" in redacted
    data = json.loads(redacted)
    assert data["echo"] == "[REDACTED]"
    assert data["nested"]["val"] == "[REDACTED]"
