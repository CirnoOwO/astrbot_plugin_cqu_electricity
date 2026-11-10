import argparse
import json
import math
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from pathlib import Path

try:
    from .settlement import morning_snapshot
except ImportError:  # Standalone HTML generation CLI.
    from settlement import morning_snapshot

BJ=timezone(timedelta(hours=8))

def stamp(value):
    dt=datetime.fromisoformat(value)
    if dt.tzinfo is None:raise ValueError("Timestamps require a timezone")
    return dt.astimezone(BJ)

def number(value,required=False):
    if value is None:
        if required:raise ValueError("Missing required monetary value")
        return None
    if not isinstance(value,str):raise ValueError("Numeric fields must be decimal strings")
    d=Decimal(value)
    if not d.is_finite():raise ValueError("Non-finite value")
    return d

def clock(dt):return dt.strftime("%m月%d日 %H:%M")

def weighted(intervals):
    return sum((i["kwh"] for i in intervals),Decimal(0))*24/sum((i["hours"] for i in intervals),Decimal(0))

def nice_axis(values,include_zero=True):
    values=[float(v) for v in values if v is not None]
    low=min(values+[0]) if include_zero else min(values,default=0)
    high=max(values+[0])
    span=max(high-low,1)
    raw=span/4
    power=10**math.floor(math.log10(raw))
    step=next(x*power for x in (1,2,2.5,5,10) if x*power>=raw)
    bottom=math.floor(low/step)*step
    top=math.ceil(high/step)*step
    if top<=bottom:top=bottom+step*4
    ticks=[round(bottom+i*step,8) for i in range(int(round((top-bottom)/step))+1)]
    return {"min":bottom,"max":top,"ticks":ticks}

def make_model(data,days=7):
    if data.get("schema_version")!=1:raise ValueError("Unsupported input schema")
    if not 1<=days<=14:raise ValueError("Dashboard window must be 1..14 days")
    room=data["room"]
    if room["campus"] not in ("huxi","shapingba"):raise ValueError("Unknown campus")
    if any(not isinstance(room.get(k),str) or not room[k].strip() for k in ("building","account")):raise ValueError("Invalid room")
    now=stamp(data["as_of"])
    hour,minute=map(int,data["schedule"]["time"].split(":"))
    if not (0<=hour<=23 and 0<=minute<=59):raise ValueError("Invalid daily time")
    window=data["schedule"]["window_minutes"]
    if not isinstance(window,int) or isinstance(window,bool) or not 1<=window<=60:raise ValueError("Invalid capture window")
    due=now.replace(hour=hour,minute=minute,second=0,microsecond=0)
    expected=now.date()-(timedelta(days=1) if now<due else timedelta())
    start=expected-timedelta(days=days-1)
    captures=[];ids=set();failures=[]
    for source in data["captures"]:
        if not isinstance(source["id"],int) or isinstance(source["id"],bool) or source["id"] in ids:raise ValueError("Invalid or duplicate capture id")
        ids.add(source["id"])
        began,finished=stamp(source["started_at"]),stamp(source["finished_at"])
        if finished<began:raise ValueError("Capture finishes before it starts")
        if finished>now:continue
        if source["status"]!="success":failures.append({**source,"time":finished});continue
        reading=source["reading"]
        balance=number(reading["total_balance_yuan"],True)
        number(reading["cash_balance_yuan"],True)
        meter=number(reading.get("meter_reading_kwh"))
        price=number(reading.get("unit_price_yuan"))
        if meter is not None and meter<0:raise ValueError("Negative meter reading")
        if price is not None and price<=0:raise ValueError("Nonpositive unit price")
        captures.append({"id":source["id"],"began":began,"time":finished,"day":finished.date(),"balance":balance,"meter":meter,"price":price,"reading":reading,"capture_slot":source.get("capture_slot")})
    captures.sort(key=lambda r:(r["time"],r["id"]))
    morning=morning_snapshot(data["captures"],now,window_minutes=window)
    if morning:
        expected=now.date()
        start=expected-timedelta(days=days-1)
    buckets={}
    for row in captures:buckets.setdefault(row["day"],[]).append(row)
    selected=[]
    for day,rows in sorted(buckets.items()):
        day_due=datetime.combine(day,datetime.min.time(),tzinfo=BJ).replace(hour=hour,minute=minute)
        fixed=[r for r in rows if day_due<=r["began"] and (r.get("capture_slot")==data["schedule"]["time"] or r["began"]<day_due+timedelta(minutes=window))]
        chosen=min(fixed,key=lambda r:(r["began"],r["id"])) if fixed else rows[-1]
        selected.append({**chosen,"kind":"fixed" if fixed else "fallback"})
    eligible=[r for r in selected if r["day"]<=expected]
    fixed=[r for r in eligible if r["kind"]=="fixed"]
    current=fixed[-1] if fixed else eligible[-1] if eligible else None
    if morning:current=next(r for r in selected if r["id"]==morning["reading_id"])
    fresh=bool(current and current["day"]==expected and current["kind"]=="fixed")
    if morning:fresh=morning["fresh"]
    display_balance=Decimal(morning["estimated_balance_yuan"]) if morning and morning["estimated_balance_yuan"] is not None else current["balance"] if current else None
    inferred=room["campus"]=="shapingba"
    intervals=[];valid_by_day={};reasons={}
    for previous,current_day in zip(selected,selected[1:]):
        if current_day["day"]>expected:continue
        reason=None
        duration=Decimal(str((current_day["time"]-previous["time"]).total_seconds()))/3600
        trail=[previous]+[r for r in captures if previous["time"]<r["time"]<=current_day["time"]]
        if previous["kind"]!="fixed" or current_day["kind"]!="fixed":reason="非固定采集时点"
        elif (current_day["day"]-previous["day"]).days!=1:reason="缺失采集日期"
        elif not Decimal("23.5")<=duration<=Decimal("24.5"):reason="采集间隔偏离一日"
        elif inferred:
            if any(r["price"] is None or r["price"]!=current_day["price"] for r in trail):reason="缺少电价或电价变化"
            elif any((number(b["reading"].get(field)) or Decimal(0))>(number(a["reading"].get(field)) or Decimal(0))
                     for a,b in zip(trail,trail[1:])
                     for field in ("total_balance_yuan","cash_balance_yuan","subsidy_balance_yuan")):reason="余额或补贴增加"
        elif any(r["meter"] is None for r in trail):reason="缺少累计电表读数"
        elif any(b["meter"]<a["meter"] for a,b in zip(trail,trail[1:])):reason="电表读数回退"
        if reason:reasons[current_day["day"]]=reason;continue
        cost=previous["balance"]-current_day["balance"] if inferred else None
        kwh=cost/current_day["price"] if inferred else current_day["meter"]-previous["meter"]
        item={"capture_day":current_day["day"],"reference_day":current_day["day"]-timedelta(days=1),"kwh":kwh,"cost":cost,"hours":duration,"first":previous,"last":current_day}
        intervals.append(item);valid_by_day[current_day["day"]]=item
    run=[]
    if current:
        day=current["day"]-timedelta(days=1) if morning else current["day"]
        while day in valid_by_day:
            item=valid_by_day[day]
            if item["reference_day"]<start:break
            run.insert(0,item);day-=timedelta(days=1)
    minimum=2 if inferred else 3
    forecast_run=run
    if current and len(forecast_run)<minimum:
        # Sampling gaps may hide a recent baseline, but semantic anomalies
        # (meter resets, recharge, price changes) must still break its use.
        sampling_reasons={"非固定采集时点","缺失采集日期","采集间隔偏离一日"}
        candidate=[]
        last_day=current["day"]-timedelta(days=1) if morning else current["day"]
        for offset in range((last_day-start).days+1):
            day=last_day-timedelta(days=offset)
            if day in reasons and reasons[day] not in sampling_reasons:break
            item=valid_by_day.get(day)
            if item is None or item["reference_day"]<start:
                candidate=[]
                continue
            if not candidate and (expected-day).days>3:break
            candidate.insert(0,item)
            if len(candidate)>=minimum:
                trail=[r for r in captures if candidate[0]["first"]["time"]<=r["time"]<=current["time"]]
                stable_price=all(r["price"]==current["price"] for r in trail)
                if inferred:
                    stable_usage=all((number(b["reading"].get(field)) or Decimal(0))<=(number(a["reading"].get(field)) or Decimal(0))
                                     for a,b in zip(trail,trail[1:])
                                     for field in ("total_balance_yuan","cash_balance_yuan","subsidy_balance_yuan"))
                else:
                    stable_usage=all(r["meter"] is not None for r in trail) and all(b["meter"]>=a["meter"] for a,b in zip(trail,trail[1:]))
                if stable_price and stable_usage:forecast_run=candidate
                break
    forecast=None;forecast_reason=f"不足{minimum}个连续有效日间隔"
    if current and not fresh:forecast_reason="日记录尚未更新，不外推旧余额"
    elif current and current["price"] is None:forecast_reason="缺少有效电价"
    elif current and display_balance<=0:forecast_reason="余额非正，不估算可用时间"
    elif morning and morning["estimated_balance_yuan"] is None and morning["state"]!="settled" and not inferred:forecast_reason="早间结算状态不明确，不估算可用时间"
    elif current and fresh and len(forecast_run)>=minimum:
        recent=forecast_run[-3:];average=weighted(recent)
        if inferred and any(i["last"]["price"]!=current["price"] for i in recent):average=Decimal(0)
        if average>0 and current["price"]:
            forecast={"days":str(display_balance/(average*current["price"])),"mean_kwh_per_day":str(average),"interval_count":len(recent),"first_reference_day":recent[0]["reference_day"].isoformat(),"last_reference_day":recent[-1]["reference_day"].isoformat(),"estimated":True,"balance_basis":"estimated_after_charge" if morning and morning["estimated_balance_yuan"] is not None else "platform_balance","method":"balance_decrease" if inferred else "meter_difference"}
        else:forecast_reason="近期读数没有增加，不显示无限可用时间"
    else:
        if current and current["meter"] is None:forecast_reason="接口没有累计电表读数"
    comparison=None
    if len(run)>=5:
        recent,baseline=weighted(run[-3:]),weighted(run[-5:-3])
        if baseline>0:comparison={"percent":str((recent/baseline-1)*100),"recent_count":3,"baseline_count":2}
    dates=[start+timedelta(days=i) for i in range(days)]
    by_day={r["day"]:r for r in selected}
    points=[{"day":day.isoformat(),"value":str(by_day[day]["balance"]) if day in by_day else None,"kind":by_day[day]["kind"] if day in by_day else "missing","captured_at":by_day[day]["time"].isoformat() if day in by_day else None} for day in dates]
    if morning and morning["estimated_balance_yuan"] is not None:
        points[-1].update(platform_value=points[-1]["value"],value=str(display_balance),kind="estimated")
    bars=[]
    for i in range(days):
        reference=start+timedelta(days=i);capture_day=reference+timedelta(days=1)
        if capture_day>expected:continue
        item=valid_by_day.get(capture_day)
        if morning and capture_day==expected:
            value=morning["usage"]["kwh"] if morning["usage"] else None
            bars.append({"reference_day":reference.isoformat(),"capture_day":capture_day.isoformat(),"value":value,"reason":morning["unavailable_reason"] if value is None else None,"kind":"morning"})
        else:bars.append({"reference_day":reference.isoformat(),"capture_day":capture_day.isoformat(),"value":str(item["kwh"]) if item else None,"reason":reasons.get(capture_day,"缺少采集记录") if not item else None})
    any_meter=inferred or any(r["meter"] is not None for r in captures)
    last_failure=max(failures,key=lambda r:r["time"],default=None)
    warning=None
    if not fresh:
        warning=(expected.strftime("%m月%d日")+"的日采集尚未成功；以上为已保存记录。") if current else "暂无已保存的成功日记录。"
        if last_failure and last_failure["time"].date()>=expected:warning+=" 最近采集失败："+str(last_failure.get("error_code") or last_failure["status"])+"。"
    if now<due and not morning:warning=(warning+" " if warning else "")+"昨天的数据将在今天"+data["schedule"]["time"]+"刷新。"
    increases=sum(1 for a,b in zip(captures,captures[1:]) if b["day"]>=start and b["day"]<=expected and b["balance"]>a["balance"])
    notice="余额增加可能来自充值或补助，不解释为负用电。" if increases else None
    return {"schema_version":1,"room":{**room,"campus_label":{"huxi":"虎溪","shapingba":"沙坪坝"}[room["campus"]]},"generated_at":now.isoformat(),"schedule":data["schedule"],"window":{"days":days,"first_day":start.isoformat(),"last_day":expected.isoformat()},"balance":str(current["balance"]) if current else None,"display_balance":str(display_balance) if display_balance is not None else None,"settlement":morning,"price":str(current["price"]) if current and current["price"] else None,"captured_at":current["time"].isoformat() if current else None,"fresh":fresh,"warning":warning,"forecast":forecast,"forecast_reason":forecast_reason,"comparison":comparison,"balance_points":points,"balance_axis":nice_axis([p["value"] for p in points]),"reference_bars":bars if any_meter else [],"meter_axis":nice_axis([b["value"] for b in bars]) if any_meter else None,"notice":notice,"provenance":data.get("provenance",{})}

def build(input_path,output,days=7):
    data=json.loads(Path(input_path).read_text(encoding="utf-8-sig"))
    model=make_model(data,days)
    output=Path(output);output.parent.mkdir(parents=True,exist_ok=True)
    encoded=json.dumps(model,ensure_ascii=False,separators=(",",":"),allow_nan=False).replace("<",r"\u003c").replace(">",r"\u003e").replace("&",r"\u0026")
    template=Path(__file__).with_name("template.html").read_text(encoding="utf-8")
    if template.count("__REPORT_DATA__")!=1:raise ValueError("Invalid template")
    output.write_text(template.replace("__REPORT_DATA__",encoded),encoding="utf-8")
    output.with_suffix(".model.json").write_text(json.dumps(model,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    return model

if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("input");parser.add_argument("output");parser.add_argument("--days",type=int,default=7)
    args=parser.parse_args();model=build(args.input,args.output,args.days)
    print(json.dumps({"room":model["room"]["account"],"captured_at":model["captured_at"],"day_points":sum(p["value"] is not None for p in model["balance_points"]),"valid_intervals":sum(b["value"] is not None for b in model["reference_bars"]),"forecast":model["forecast"]},ensure_ascii=False))
