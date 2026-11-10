import copy
from datetime import datetime
from decimal import Decimal

import pytest

from cqu_plugin.dashboard_model import BJ, make_model
from cqu_plugin.settlement import morning_snapshot
from test_history_captures import env, capture, IDENTITY
from cqu_plugin.dashboard import dashboard_input


def fixture():
    # User-provided balance/delta sequence; cumulative meters reconstructed for
    # tests, not represented as a downloaded platform response.
    balances = ["12.47", "6.17", "2.20", "98.54", "92.16", "85.71"]
    increments = ["0", "11.66", "7.36", "6.76", "11.82", "11.94"]
    meter = Decimal("1000")
    rows = []
    for index, (balance, delta) in enumerate(zip(balances, increments)):
        meter += Decimal(delta)
        at = f"2026-10-{index+10:02d}T14:00:30+08:00"
        rows.append(sample(index+1, at, balance, str(meter)))
    rows.append(sample(7, "2026-10-16T09:00:30+08:00", "85.71", str(meter+Decimal("13.94"))))
    return {"schema_version":1,"room":{"campus":"huxi","building":"竹园5栋","account":"B5344"},
            "as_of":"2026-10-16T09:01:00+08:00", "schedule":{"time":"14:00","window_minutes":15},
            "captures":rows}


def sample(identifier, at, balance, meter):
    return {"id":identifier,"started_at":at,"finished_at":at,"status":"success","error_code":None,
            "reading":{"total_balance_yuan":balance,"cash_balance_yuan":balance,
                       "subsidy_kwh":"0","subsidy_balance_yuan":None,
                       "meter_reading_kwh":meter,"unit_price_yuan":"0.54"}}


def estimate(data):
    return morning_snapshot(data["captures"],datetime.fromisoformat(data["as_of"]))


def test_1394_kwh_projection_keeps_platform_money_and_overlays_chart_only():
    data=fixture();before=copy.deepcopy(data);model=make_model(data)
    pending=model["settlement"]
    assert model["balance"]=="85.71" and model["display_balance"]=="78.18"
    assert pending["pending_cost_yuan"]=="7.53" and pending["usage"]["kwh"]=="13.94"
    assert model["balance_points"][-1]["value"]=="78.18"
    assert model["balance_points"][-1]["platform_value"]=="85.71"
    assert model["balance_points"][-1]["kind"]=="estimated"
    assert model["reference_bars"][-1]["reference_day"]=="2026-10-15"
    assert model["reference_bars"][-1]["value"]=="13.94"
    assert model["window"]["last_day"]=="2026-10-16"
    assert data==before


def test_forecast_uses_projected_balance_and_complete_afternoon_intervals():
    model=make_model(fixture());forecast=model["forecast"]
    # The 19-hour morning observation is NOT scaled up to 24 hours or used as
    # one of the complete daily intervals in the consumption average.
    expected=(Decimal("6.76")+Decimal("11.82")+Decimal("11.94"))/3
    assert Decimal(forecast["mean_kwh_per_day"])==expected
    assert Decimal(forecast["days"])==Decimal("78.18")/(expected*Decimal("0.54"))
    assert forecast["balance_basis"]=="estimated_after_charge"


@pytest.mark.parametrize("clock",["08:59:59","14:00:00","23:00:00"])
def test_morning_projection_only_between_9_and_14(clock):
    data=fixture();data["as_of"]=f"2026-10-16T{clock}+08:00"
    assert estimate(data) is None


def test_morning_capture_must_have_completed():
    data=fixture();data["as_of"]="2026-10-16T09:00:00+08:00"
    assert estimate(data) is None


def test_record_before_9_is_not_a_completed_morning_slot():
    data=fixture();data["captures"][-1]["started_at"]=data["captures"][-1]["finished_at"]="2026-10-16T08:52:00+08:00"
    assert estimate(data) is None


@pytest.mark.parametrize("balance",["90.00","85.70","82.00","60.00"])
def test_balance_changed_does_not_get_subtracted_again(balance):
    data=fixture();data["captures"][-1]["reading"].update(total_balance_yuan=balance,cash_balance_yuan=balance)
    model=make_model(data)
    assert model["settlement"]["state"]=="balance_changed"
    assert model["settlement"]["estimated_balance_yuan"] is None
    assert model["display_balance"] == balance and model["forecast"] is None
    assert model["reference_bars"][-1]["value"] == "13.94"


@pytest.mark.parametrize("balance", ["78.18", "78.20", "78.00"])
def test_observed_debit_uses_actual_balance_for_forecast_without_second_deduction(balance):
    data = fixture()
    data["captures"][-1]["reading"].update(total_balance_yuan=balance, cash_balance_yuan=balance)
    before = copy.deepcopy(data)
    model = make_model(data)
    settled = model["settlement"]
    assert settled["state"] == "settled" and not settled["estimated"]
    assert settled["estimated_balance_yuan"] is None and settled["pending_cost_yuan"] is None
    assert model["balance"] == model["display_balance"] == balance
    forecast = model["forecast"]
    assert forecast["balance_basis"] == "platform_balance"
    assert Decimal(forecast["days"]) == Decimal(balance) / (Decimal(forecast["mean_kwh_per_day"]) * Decimal("0.54"))
    assert model["balance_points"][-1]["kind"] != "estimated"
    assert data == before


def test_reported_978_kwh_rounded_debit_regression():
    data = fixture()
    base, current = data["captures"][-2:]
    base["reading"].update(total_balance_yuan="81.88", cash_balance_yuan="81.88")
    current["reading"].update(total_balance_yuan="76.60", cash_balance_yuan="76.60",
                              meter_reading_kwh=str(Decimal(base["reading"]["meter_reading_kwh"]) + Decimal("9.78")))
    model = make_model(data)
    assert model["settlement"]["state"] == "settled"
    assert model["display_balance"] == "76.60"
    assert Decimal(model["forecast"]["days"]) > 0


@pytest.mark.parametrize("field", ["total_balance_yuan", "cash_balance_yuan", "subsidy_kwh", "subsidy_balance_yuan"])
def test_intermediate_component_gain_blocks_apparently_matching_final_debit(field):
    data = fixture()
    data["captures"][-1]["reading"].update(total_balance_yuan="78.18", cash_balance_yuan="78.18")
    row = copy.deepcopy(data["captures"][-2])
    row.update(id=8, started_at="2026-10-15T20:00:00+08:00", finished_at="2026-10-15T20:00:00+08:00")
    row["reading"][field] = "100"
    data["captures"].append(row)
    model = make_model(data)
    assert model["settlement"]["state"] == "balance_changed"
    assert model["forecast"] is None


@pytest.mark.parametrize("kind", ["missing_meter", "reset", "missing_price", "changed_price", "failed", "interrupted", "component_missing"])
def test_matching_debit_does_not_bypass_data_guards(kind):
    data = fixture()
    data["captures"][-1]["reading"].update(total_balance_yuan="78.18", cash_balance_yuan="78.18")
    current = data["captures"][-1]
    if kind == "missing_meter": current["reading"]["meter_reading_kwh"] = None
    elif kind == "reset": current["reading"]["meter_reading_kwh"] = "0"
    elif kind == "missing_price": current["reading"]["unit_price_yuan"] = None
    elif kind == "changed_price": current["reading"]["unit_price_yuan"] = "0.55"
    elif kind == "component_missing": current["reading"]["subsidy_kwh"] = None
    else:
        data["captures"].append({"id":8,"started_at":"2026-10-16T09:00:40+08:00","finished_at":"2026-10-16T09:00:50+08:00","status":kind,"error_code":"TIMEOUT"})
    model = make_model(data)
    assert model["settlement"]["state"] != "settled"
    assert model["forecast"] is None


def test_compensating_cash_and_subsidy_changes_block_projection():
    data=fixture();data["captures"][-1]["reading"].update(cash_balance_yuan="80.31",subsidy_kwh="10")
    assert estimate(data)["state"]=="balance_changed"


def test_intermediate_recharge_returning_to_same_balance_still_blocks():
    data=fixture();row=copy.deepcopy(data["captures"][-2]);row.update(id=8,started_at="2026-10-15T20:00:00+08:00",finished_at="2026-10-15T20:00:00+08:00")
    row["reading"].update(total_balance_yuan="185.71",cash_balance_yuan="185.71")
    data["captures"].append(row)
    assert estimate(data)["estimated_balance_yuan"] is None


@pytest.mark.parametrize("kind",["missing_baseline","missing_meter","reset","intermediate_reset","zero","missing_price","changed_price","failed","interrupted"])
def test_uncertain_data_never_produces_a_deduction(kind):
    data=fixture();rows=data["captures"]
    if kind=="missing_baseline":rows.pop(-2)
    elif kind=="missing_meter":rows[-1]["reading"]["meter_reading_kwh"]=None
    elif kind=="reset":rows[-1]["reading"]["meter_reading_kwh"]="0"
    elif kind=="zero":rows[-1]["reading"]["meter_reading_kwh"]=rows[-2]["reading"]["meter_reading_kwh"]
    elif kind=="missing_price":rows[-1]["reading"]["unit_price_yuan"]=None
    elif kind=="changed_price":rows[-1]["reading"]["unit_price_yuan"]="0.60"
    elif kind=="intermediate_reset":
        row=sample(8,"2026-10-15T22:00:00+08:00","85.71","1");rows.append(row)
    else:
        rows.append({"id":8,"started_at":"2026-10-16T09:00:40+08:00","finished_at":"2026-10-16T09:00:50+08:00","status":kind,"error_code":"TIMEOUT"})
    assert estimate(data)["estimated_balance_yuan"] is None
    assert make_model(data)["forecast"] is None


def test_manual_refresh_recalculates_from_baseline_not_previous_estimate():
    data=fixture();row=copy.deepcopy(data["captures"][-1]);row.update(id=8,started_at="2026-10-16T10:00:00+08:00",finished_at="2026-10-16T10:00:00+08:00")
    data["captures"].append(row);data["as_of"]="2026-10-16T10:01:00+08:00"
    assert make_model(data)["display_balance"]=="78.18"
    row["reading"].update(total_balance_yuan="78.18",cash_balance_yuan="78.18")
    model=make_model(data)
    assert model["display_balance"]=="78.18" and model["settlement"]["estimated_balance_yuan"] is None


def test_afternoon_replaces_morning_bar_instead_of_adding_another_day():
    data=fixture();row=copy.deepcopy(data["captures"][-1]);row.update(id=8,started_at="2026-10-16T14:00:30+08:00",finished_at="2026-10-16T14:00:30+08:00")
    row["reading"].update(total_balance_yuan="78.18",cash_balance_yuan="78.18")
    data["captures"].append(row);data["as_of"]="2026-10-16T14:01:00+08:00"
    model=make_model(data)
    assert model["settlement"] is None and model["balance"]=="78.18"
    assert model["reference_bars"][-1]["value"]=="13.94"
    assert len({b["reference_day"] for b in model["reference_bars"]})==len(model["reference_bars"])


def test_no_morning_record_preserves_original_snapshot_behavior():
    data=fixture();data["captures"].pop()
    model=make_model(data)
    assert model["settlement"] is None and model["window"]["last_day"]=="2026-10-15"
    assert model["captured_at"].startswith("2026-10-15T14:")


def test_negative_projected_balance_is_visible_without_forecast():
    data=fixture()
    for row in data["captures"][-2:]:row["reading"].update(total_balance_yuan="2.20",cash_balance_yuan="2.20")
    model=make_model(data)
    assert model["display_balance"]=="-5.33" and model["forecast"] is None
    assert model["balance_points"][-1]["value"]=="-5.33"
    assert model["balance_points"][-1]["platform_value"]=="2.20"


def test_service_and_dashboard_share_estimate_and_do_not_write(env):
    repo,service,room=env
    capture(repo,room,"2026-10-02T14:00:30","100","85.71")
    capture(repo,room,"2026-10-03T09:00:30","113.94","85.71")
    now=datetime(2026,10,3,9,1,tzinfo=BJ);before=repo.db.total_changes
    result=service.stored_result(room,now=now)
    model=make_model(dashboard_input(repo,IDENTITY,now=now))
    assert result["settlement"]==model["settlement"]
    assert result["reading"]["total_balance_yuan"]=="85.71"
    assert result["settlement"]["estimated_balance_yuan"]=="78.18"
    assert repo.db.total_changes==before
    daily=service.history(IDENTITY,7)
    assert daily["daily_records"][-1]["reading"]["total_balance_yuan"]=="85.71"
    assert "78.18" not in str(daily)


@pytest.mark.parametrize("status",["failed","interrupted"])
def test_service_and_dashboard_both_drop_projection_after_failed_refresh(env,status):
    repo,service,room=env
    capture(repo,room,"2026-10-02T14:00:30","100","85.71")
    capture(repo,room,"2026-10-03T09:00:30","113.94","85.71")
    q=capture(repo,room,"2026-10-03T10:00:30","0",error="TIMEOUT")
    if status=="interrupted":
        with repo.db:repo.db.execute("UPDATE query_runs SET status='interrupted' WHERE id=?",(q,))
    now=datetime(2026,10,3,10,1,tzinfo=BJ)
    result=service.stored_result(room,now=now)
    model=make_model(dashboard_input(repo,IDENTITY,now=now))
    assert result["settlement"]==model["settlement"]
    assert result["stale"] and not model["fresh"]
    assert result["settlement"]["estimated_balance_yuan"] is None


def test_morning_does_not_duplicate_daily_history_and_keeps_raw_9_and_14(env):
    repo,service,room=env
    capture(repo,room,"2026-10-02T14:00:30","100","85.71")
    capture(repo,room,"2026-10-03T09:00:30","113.94","85.71")
    capture(repo,room,"2026-10-03T14:00:30","113.94","78.18")
    history=service.history(IDENTITY,7)
    assert len(history["records"])==3 and len(history["daily_records"])==2
    assert history["daily_records"][-1]["reading"]["total_balance_yuan"]=="78.18"
    assert Decimal(history["daily_records"][-1]["usage"]["kwh"])==Decimal("13.94")
    assert history["records"][-2]["reading"]["total_balance_yuan"]=="85.71"


def test_latest_interrupted_capture_does_not_report_an_older_failure(env):
    repo,service,room=env
    capture(repo,room,"2026-10-02T14:00:30","100","85.71")
    capture(repo,room,"2026-10-03T09:00:30","113.94","85.71")
    capture(repo,room,"2026-10-03T09:30:30","0",error="TIMEOUT")
    q=capture(repo,room,"2026-10-03T10:00:30","0",error="INTERRUPTED")
    with repo.db:
        repo.db.execute("UPDATE query_runs SET status='interrupted' WHERE id=?",(q,))
    result=service.stored_result(room,now=datetime(2026,10,3,10,1,tzinfo=BJ))
    assert result["stale"]
    assert result["daily_brief"]["refresh_error"]=="INTERRUPTED"
    assert result["settlement"]["estimated_balance_yuan"] is None
