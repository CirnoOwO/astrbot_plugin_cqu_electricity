import copy,json,tempfile,unittest
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from pathlib import Path
from cqu_plugin.dashboard_model import make_model,build

BJ=timezone(timedelta(hours=8))

def fake(count=8):
    first=datetime(2030,5,1,14,0,30,tzinfo=BJ)
    captures=[]
    for i in range(count):
        at=first+timedelta(days=i)
        captures.append({"id":i+1,"started_at":(at-timedelta(seconds=3)).isoformat(),"finished_at":at.isoformat(),"status":"success","error_code":None,"reading":{"total_balance_yuan":str(Decimal("70")-i*Decimal("2.16")),"cash_balance_yuan":str(Decimal("70")-i*Decimal("2.16")),"meter_reading_kwh":str(Decimal("1000")+i*4),"unit_price_yuan":"0.54","subsidy_kwh":"0","subsidy_balance_yuan":None}})
    return {"schema_version":1,"room":{"campus":"huxi","building":"测试园9栋","account":"Z9001"},"as_of":(first+timedelta(days=count-1,hours=1)).isoformat(),"schedule":{"time":"14:00","window_minutes":15},"captures":captures,"provenance":{"label":"模拟数据，设计验证"}}

class ModelTests(unittest.TestCase):
    def test_valid_forecast_and_generated_axis(self):
        m=make_model(fake());self.assertEqual(Decimal(m["forecast"]["mean_kwh_per_day"]),4)
        self.assertAlmostEqual(float(m["forecast"]["days"]),54.88/2.16)
        self.assertEqual(m["comparison"]["percent"],"0")
        self.assertEqual(m["balance_points"][-1]["day"],"2030-05-08")
    def test_change_account_and_value_not_static(self):
        d=fake();d["room"]["account"]="NEW88";d["captures"][-1]["reading"]["total_balance_yuan"]="300"
        m=make_model(d);self.assertEqual(m["balance"],"300");self.assertEqual(m["room"]["account"],"NEW88");self.assertTrue(m["notice"])
        self.assertGreaterEqual(m["balance_axis"]["max"],300)
    def test_missing_day_uses_recent_baseline_without_interpolation(self):
        d=fake();d["captures"].pop(-2);m=make_model(d)
        self.assertEqual(m["forecast"]["first_reference_day"],"2030-05-03")
        self.assertEqual(m["forecast"]["last_reference_day"],"2030-05-05")
        self.assertEqual(Decimal(m["forecast"]["mean_kwh_per_day"]),4)
        self.assertIsNone(m["balance_points"][-2]["value"])
        self.assertTrue(all(b["value"] is None for b in m["reference_bars"][-2:]))

    def test_short_latest_run_uses_recent_complete_baseline(self):
        for latest_count in (0,1):
            d=fake(10);d["captures"].pop(-(latest_count+2))
            m=make_model(d)
            self.assertEqual(m["forecast"]["interval_count"],3)
            self.assertEqual(m["forecast"]["last_reference_day"],f"2030-05-{7-latest_count:02d}")
            self.assertIsNone(m["comparison"])

    def test_gap_with_no_complete_recent_run_does_not_forecast(self):
        d=fake(10);d["captures"].pop(-4)
        self.assertIsNone(make_model(d)["forecast"])

    def test_synthetic_gap_replay_uses_current_morning_balance(self):
        data=json.loads((Path(__file__).parent/"fixtures/recent_gap_replay.json").read_text(encoding="utf-8"))
        original=copy.deepcopy(data);m=make_model(data);forecast=m["forecast"]
        self.assertEqual(data,original)
        self.assertEqual(m["display_balance"],"68.56")
        self.assertEqual(forecast["interval_count"],3)
        self.assertEqual(forecast["first_reference_day"],"2026-10-31")
        self.assertEqual(forecast["last_reference_day"],"2026-11-02")
        self.assertAlmostEqual(float(forecast["mean_kwh_per_day"]),6.0)
        self.assertAlmostEqual(float(forecast["days"]),21.160493827)
        self.assertEqual(forecast["balance_basis"],"estimated_after_charge")
        missing=[b for b in m["reference_bars"] if b["capture_day"] in ("2026-11-04","2026-11-05")]
        self.assertEqual(len(missing),2)
        self.assertTrue(all(b["value"] is None for b in missing))

    def test_fallback_baseline_must_end_within_three_days(self):
        d=fake(12);d["captures"]=[r for r in d["captures"] if r["id"]<=8 or r["id"]==12]
        self.assertIsNone(make_model(d,days=10)["forecast"])

    def test_fallback_cannot_use_intervals_before_window(self):
        d=fake(10);d["captures"]=[r for r in d["captures"] if r["id"]<=5 or r["id"]==10]
        self.assertIsNone(make_model(d)["forecast"])

    def test_fallback_does_not_cross_reset_hidden_by_sampling_gap(self):
        d=fake();d["captures"][-2]["started_at"]=d["captures"][-2]["finished_at"]="2030-05-07T15:00:00+08:00"
        d["captures"][-2]["reading"]["meter_reading_kwh"]="20"
        self.assertIsNone(make_model(d)["forecast"])

    def test_fallback_does_not_cross_price_change(self):
        d=fake();d["captures"].pop(-2);d["captures"][-1]["reading"]["unit_price_yuan"]="0.60"
        self.assertIsNone(make_model(d)["forecast"])

    def test_shapingba_fallback_does_not_cross_recharge_hidden_by_gap(self):
        d=fake();d["room"]["campus"]="shapingba";d["captures"].pop(-2)
        for r in d["captures"]:r["reading"]["meter_reading_kwh"]=None
        d["captures"][-1]["reading"]["cash_balance_yuan"]="100"
        d["captures"][-1]["reading"]["total_balance_yuan"]="100"
        self.assertIsNone(make_model(d)["forecast"])
    def test_no_meter_shapingba_estimates_from_daily_debit(self):
        d=fake();d["room"]["campus"]="shapingba"
        for r in d["captures"]:r["reading"]["meter_reading_kwh"]=None
        m=make_model(d);self.assertEqual(Decimal(m["forecast"]["mean_kwh_per_day"]),4)
        self.assertEqual(m["forecast"]["method"],"balance_decrease")
        self.assertTrue(all(Decimal(b["value"])==4 for b in m["reference_bars"]))
    def test_zero_increase_no_infinity(self):
        d=fake()
        for r in d["captures"]:r["reading"]["meter_reading_kwh"]="1000"
        self.assertIsNone(make_model(d)["forecast"])
    def test_reset_during_interval_blocks_forecast(self):
        d=fake();r=copy.deepcopy(d["captures"][-2]);r["id"]=100;r["started_at"]=r["finished_at"]="2030-05-07T18:00:00+08:00";r["reading"]["meter_reading_kwh"]="20";d["captures"].append(r)
        self.assertIsNone(make_model(d)["forecast"])
    def test_stale_balance_no_prediction(self):
        d=fake();d["as_of"]="2030-05-09T15:00:00+08:00";m=make_model(d)
        self.assertFalse(m["fresh"]);self.assertIsNone(m["forecast"]);self.assertIn("尚未成功",m["warning"])
    def test_before_due_uses_previous_day(self):
        d=fake();d["as_of"]="2030-05-08T12:00:00+08:00";m=make_model(d)
        self.assertEqual(m["window"]["last_day"],"2030-05-07");self.assertEqual(m["captured_at"][:10],"2030-05-07");self.assertIn("今天14:00",m["warning"])
    def test_fallback_not_used_for_interval(self):
        d=fake();d["captures"][-1]["started_at"]=d["captures"][-1]["finished_at"]="2030-05-08T15:00:00+08:00"
        m=make_model(d);self.assertEqual(m["balance_points"][-1]["kind"],"fallback");self.assertIsNone(m["forecast"])
    def test_negative_balance_visible_axis_no_forecast(self):
        d=fake();d["captures"][-1]["reading"]["total_balance_yuan"]="-2.50";m=make_model(d)
        self.assertEqual(m["balance"],"-2.50");self.assertLess(m["balance_axis"]["min"],0);self.assertIsNone(m["forecast"])
    def test_empty_captures(self):
        d=fake();d["captures"]=[];m=make_model(d);self.assertIsNone(m["balance"]);self.assertIsNone(m["forecast"])
    def test_only_failed_captures(self):
        d=fake();d["captures"]=[{**r,"status":"failed","error_code":"AUTH_EXPIRED"} for r in d["captures"]]
        m=make_model(d);self.assertIsNone(m["balance"]);self.assertIn("AUTH_EXPIRED",m["warning"])
    def test_no_future_timestamp_use(self):
        d=fake();d["captures"][-1]["started_at"]=d["captures"][-1]["finished_at"]="2031-01-01T14:00:00+08:00"
        m=make_model(d);self.assertFalse(m["fresh"])
    def test_bad_numbers_and_naive_times_rejected(self):
        for field,value in [("total_balance_yuan","NaN"),("unit_price_yuan","0")]:
            d=fake();d["captures"][0]["reading"][field]=value
            with self.assertRaises(ValueError):make_model(d)
        d=fake();d["as_of"]="2030-05-08T15:00:00"
        with self.assertRaises(ValueError):make_model(d)
    def test_no_script_injection_from_room(self):
        d=fake();d["room"]["building"]="</script><script>bad()</script>"
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);(p/"in.json").write_text(json.dumps(d),encoding="utf-8");build(p/"in.json",p/"out.html")
            html=(p/"out.html").read_text(encoding="utf-8");self.assertNotIn("</script><script>bad()",html)
            self.assertIn("\\u003c/script",html)

if __name__=="__main__":unittest.main()

