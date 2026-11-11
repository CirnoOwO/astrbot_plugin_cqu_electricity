import asyncio
import json
from decimal import Decimal
import httpx
import pytest

from cqu_plugin.models import Room, Reading, QueryError
from cqu_plugin.client import ElectricityClient, parse_nh_reading
from cqu_plugin.repository import Repository
from cqu_plugin.service import ElectricityService
from cqu_plugin.scheduler import Scheduler

ROOM_B10 = Room("shapingba", "B区10舍", "B10S306")
IDENTITY = ("qq-test", "123", "1001")
ORIGIN = "qq-test:GroupMessage:123"

LOGIN_SUCCESS = {"code": "0", "msg": "", "SysLoginAcctID": 2054, "SysLoginAcctNT": "B10S306", "username": "B10S306"}
LOGIN_NOT_FOUND = {"code": "-3", "msg": "抱歉,查询不到当前房间号或手机号!"}
ACCT_DATA = [{"F_AcctID": 2054, "PointerName": "B区10号学生宿舍(271)Z", "F_AcctName": "B10S306", "B_F_InitBalance": 80.86, "B_F_InitAllowance": 2.50, "B_F_Totalace": 83.36}]
SACCT_DATA = {"iTotalRecords": 1, "iTotalDisplayRecords": 1, "aaData": [["正在运行", "已通电", "77275", "62000", "16-4", "B10S306(16-4)", "瑞德单相电表(10000)", "1", "B区10舍306", "家属区、学生电价0.54", "0.5400", "2024/5/26 0:00:00", "2026/11/11 12:00:00", "0", "7061", "0", "B区10号学生宿舍(271)Z"]]}


@pytest.fixture
def repo(tmp_path):
    value = Repository(tmp_path / "nh_test.db")
    yield value
    value.close()


def make_nh_transport(status=200, login_resp=None, acct_resp=None, sacct_resp=None):
    login = login_resp if login_resp is not None else LOGIN_SUCCESS
    acct = acct_resp if acct_resp is not None else ACCT_DATA
    sacct = sacct_resp if sacct_resp is not None else SACCT_DATA

    def handle(request):
        url = str(request.url)
        if "/df/Admin/Login" in url:
            return httpx.Response(status, json=login)
        elif "/df/Home/GetAcctDs" in url:
            return httpx.Response(status, json=acct)
        elif "/df/Home/GetSAcctDs" in url:
            return httpx.Response(status, json=sacct)
        return httpx.Response(404)

    return httpx.MockTransport(handle)


def test_parse_nh_reading_valid():
    reading = parse_nh_reading(ROOM_B10, ACCT_DATA, SACCT_DATA, Decimal("0.54"))
    assert reading.cash_balance_yuan == Decimal("80.86")
    assert reading.subsidy_balance_yuan == Decimal("2.50")
    assert reading.meter_reading_kwh == Decimal("7061")
    assert reading.unit_price_yuan == Decimal("0.5400")
    assert reading.resolved_building == "B区10舍"
    fields = reading.fields()
    assert Decimal(fields["total_balance_yuan"]) == Decimal("83.36")


def test_parse_nh_reading_room_mismatch():
    mismatched = [{"F_AcctID": 2054, "F_AcctName": "B3S624", "B_F_InitBalance": 10.0}]
    with pytest.raises(QueryError, match="ROOM_MISMATCH"):
        parse_nh_reading(ROOM_B10, mismatched, SACCT_DATA, Decimal("0.54"))


def test_parse_nh_reading_invalid_structure():
    with pytest.raises(QueryError, match="INVALID_RESPONSE"):
        parse_nh_reading(ROOM_B10, [], SACCT_DATA, Decimal("0.54"))
    with pytest.raises(QueryError, match="INVALID_RESPONSE"):
        parse_nh_reading(ROOM_B10, "not a list", SACCT_DATA, Decimal("0.54"))


def test_parse_nh_reading_missing_device_falls_back():
    empty_sacct = {"iTotalRecords": 0, "aaData": []}
    reading = parse_nh_reading(ROOM_B10, ACCT_DATA, empty_sacct, Decimal("0.60"))
    assert reading.cash_balance_yuan == Decimal("80.86")
    assert reading.meter_reading_kwh is None
    assert reading.unit_price_yuan == Decimal("0.60")
    assert reading.resolved_building == "B区10舍"


def test_nh_fetch_success_without_auth(repo, tmp_path):
    client = ElectricityClient(tmp_path / "missing", nh_enabled=True, transport=make_nh_transport())
    assert client.configured() is False
    assert client.configured("shapingba") is True

    service = ElectricityService(repo, client)
    room = repo.room(ROOM_B10)
    result = asyncio.run(service.query(room, force=True))
    assert result["ok"]
    assert result["reading"]["cash_balance_yuan"] == "80.86"
    assert result["reading"]["meter_reading_kwh"] == "7061"

    responses = repo.all("SELECT stage, http_status FROM query_responses ORDER BY sequence_no")
    assert len(responses) == 3
    assert [r["stage"] for r in responses] == ["nh_login", "nh_account", "nh_device"]


def test_nh_fetch_room_not_found(repo, tmp_path):
    client = ElectricityClient(tmp_path / "missing", nh_enabled=True, transport=make_nh_transport(login_resp=LOGIN_NOT_FOUND))
    service = ElectricityService(repo, client)
    room = repo.room(ROOM_B10)
    result = asyncio.run(service.query(room, force=True))
    assert not result["ok"]
    assert result["error_code"] == "ROOM_NOT_FOUND"


def test_nh_fetch_http_error(repo, tmp_path):
    client = ElectricityClient(tmp_path / "missing", nh_enabled=True, transport=make_nh_transport(status=500))
    service = ElectricityService(repo, client)
    room = repo.room(ROOM_B10)
    result = asyncio.run(service.query(room, force=True))
    assert not result["ok"]
    assert result["error_code"] == "HTTP_ERROR"


def test_nh_fallback_to_payment_when_configured(repo, tmp_path):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({"access_token": "valid-token"}))

    def hybrid_handler(request):
        url = str(request.url)
        if "nh.cqu.edu.cn" in url:
            return httpx.Response(500)
        form = dict(httpx.QueryParams(request.content.decode()))
        if form["level"] == "0":
            data = {"total": [{"code": "b"}, {"code": "r"}], "data": [{"name": "B区10号学生宿舍", "value": "b10"}]}
        elif form["type"] == "select":
            data = {"data": [{"name": "B10S306", "value": "r306"}]}
        else:
            data = {"data": {"roomID": "B10S306", "cashBalance": "12.34", "subsidiesBalance": "0", "price": "0.54"}, "showData": {"现金余额": "12.34"}}
        return httpx.Response(200, json={"code": 200, "map": data})

    client = ElectricityClient(auth_file, nh_enabled=True, transport=httpx.MockTransport(hybrid_handler))
    assert client.configured() is True
    service = ElectricityService(repo, client)
    room = repo.room(ROOM_B10)
    result = asyncio.run(service.query(room, force=True))
    print("FALLBACK RESULT:", result)
    assert result["ok"]
    assert result["reading"]["cash_balance_yuan"] == "12.34"
    assert result["reading"]["meter_reading_kwh"] is None


def test_scheduler_runs_for_shapingba_without_token(repo, tmp_path):
    client = ElectricityClient(tmp_path / "missing", nh_enabled=True, transport=make_nh_transport())
    service = ElectricityService(repo, client)
    room = repo.room(ROOM_B10)
    repo.bind(IDENTITY, room["id"], ORIGIN)

    async def send(*args):
        pass

    scheduler = Scheduler(service, lambda *x: True, send, capture_times=("09:00",), reminder_enabled=False)
    clock_now = Scheduler(service, lambda *x: True, send).capture_times
    # Verify tick does not abort early when client has nh_enabled and shapingba rooms
    can_run = client.configured()
    if not can_run and client.nh_enabled:
        rooms = scheduler._eligible_rooms()
        can_run = any(r.get("campus") == "shapingba" for r in rooms.values())
    assert can_run is True
