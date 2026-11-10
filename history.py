from __future__ import annotations

from datetime import datetime,timedelta
from decimal import Decimal
from .summary import BEIJING,public_record,usage
from .settlement import afternoon_sample


def stamp(row):
    return datetime.fromisoformat(row["finished_at"]).astimezone(BEIJING)


def balance_change(current,previous):
    return str(Decimal(current["total_balance_yuan"])-Decimal(previous["total_balance_yuan"])) if previous else None


def fixed_average(daily):
    # Only consecutive dates with fixed 14:00 samples and valid meter differences.
    runs=[];run=[]
    for row in daily:
        value=row["reading"]
        if row["sample_kind"]!="fixed" or value.get("meter_reading_kwh") is None:
            if len(run)>=2:runs.append(run)
            run=[];continue
        if run:
            last=run[-1]
            adjacent=(datetime.fromisoformat(row["day"])-datetime.fromisoformat(last["day"])).days==1
            interval=(stamp(value)-stamp(last["reading"])).total_seconds()/3600
            valid=bool(row.get("usage") and row["usage"].get("available")) and Decimal(value["meter_reading_kwh"])>=Decimal(last["reading"]["meter_reading_kwh"])
            if not (adjacent and 23.5<=interval<=24.5 and valid):
                if len(run)>=2:runs.append(run)
                run=[]
        run.append(row)
    if len(run)>=2:runs.append(run)
    if not runs:return None
    chosen=runs[-1];first,last=chosen[0],chosen[-1]
    seconds=Decimal(str((stamp(last["reading"])-stamp(first["reading"])).total_seconds()))
    increase=Decimal(last["reading"]["meter_reading_kwh"])-Decimal(first["reading"]["meter_reading_kwh"])
    return {"kwh_per_day":str(increase/(seconds/Decimal(86400))),"intervals":len(chosen)-1,
            "start_day":first["day"],"end_day":last["day"],"method":"consecutive_1400_meter_difference"}


def daily_history(rows,start):
    ordered=sorted(rows,key=lambda r:(stamp(r),r["id"]))
    days={}
    for row in ordered:days.setdefault(row["day"],[]).append(row)
    daily=[];previous=None
    for day,samples in sorted(days.items()):
        fixed=[]
        for row in samples:
            if afternoon_sample(row,day):fixed.append(row)
        chosen=min(fixed,key=lambda r:(r.get("started_at") or r["finished_at"],r["id"])) if fixed else samples[-1]
        if day>=start:
            between=[r for r in ordered if previous and stamp(previous)<stamp(r)<=stamp(chosen)]
            trail=([previous] if previous else [])+between
            gains=sum(1 for a,b in zip(trail,trail[1:]) if Decimal(b["total_balance_yuan"])>Decimal(a["total_balance_yuan"]))
            delta=usage(chosen,previous)
            if delta and delta.get("available"):
                meters=[r.get("meter_reading_kwh") for r in trail]
                if any(v is None for v in meters):delta={"available":False,"reason":"期间缺少累计电表读数"}
                elif any(Decimal(b)<Decimal(a) for a,b in zip(meters,meters[1:])):delta={"available":False,"reason":"电表读数回退，无法计算用量"}
            daily.append({"day":day,"reading":public_record(chosen),"sample_kind":"fixed" if fixed else "fallback",
                          "usage":delta,"balance_change_yuan":balance_change(chosen,previous),
                          "span_days":(datetime.fromisoformat(day)-datetime.fromisoformat(previous["day"])).days if previous else None,
                          "balance_increase_observed":bool(gains)})
        previous=chosen
    captures=[r for r in ordered if r["day"]>=start]
    stats={"capture_count":len(captures),"recorded_days":len(daily),"average":fixed_average(daily)}
    if captures:
        first,last=captures[0],captures[-1]
        meters=[r.get("meter_reading_kwh") for r in captures]
        valid=len(captures)>=2 and all(v is not None for v in meters)
        if valid:valid=all(Decimal(b)>=Decimal(a) for a,b in zip(meters,meters[1:]))
        gains=[Decimal(b["total_balance_yuan"])-Decimal(a["total_balance_yuan"]) for a,b in zip(captures,captures[1:]) if Decimal(b["total_balance_yuan"])>Decimal(a["total_balance_yuan"])]
        stats.update({"start_at":first["finished_at"],"end_at":last["finished_at"],"latest_reading":public_record(last),
                      "meter_increase_kwh":str(Decimal(meters[-1])-Decimal(meters[0])) if valid else None,
                      "meter_increase_unavailable_reason":None if valid else "缺少读数、读数回退或不足两次记录",
                      "balance_change_yuan":balance_change(last,first) if len(captures)>=2 else None,
                      "balance_increase_events":len(gains),"balance_increase_total_yuan":str(sum(gains,Decimal(0)))})
    return daily,stats
