from __future__ import annotations

import asyncio
import weakref
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from .models import Room, QueryError
from .history import daily_history, stamp
from .summary import BEIJING, public_result, public_record, usage
from .settlement import morning_snapshot


class ElectricityService:
    def __init__(self, repository, client, *, cache_seconds=60, failure_seconds=30):
        self.repo,self.client=repository,client
        self.cache_seconds=max(0,min(int(cache_seconds),3600))
        self.failure_seconds=failure_seconds
        self.inflight={}
        self._closing=False
        self.gate=asyncio.Semaphore(2)
        self.identity_locks=weakref.WeakValueDictionary()

    def identity_lock(self, identity):
        # Commands are rare and locks only live until the operation completes.
        return self.identity_locks.setdefault(str(identity[-1]),asyncio.Lock())

    def cached(self, room_id, seconds):
        current=self.repo.latest(room_id)
        if current and (datetime.now(timezone.utc)-datetime.fromisoformat(current["finished_at"])).total_seconds()<seconds:
            return current
        return None

    async def query(self, room, *, force=False, trigger="manual", requested_by=None):
        if self._closing: raise QueryError("NOT_READY")
        room_id=room["id"]
        if not force:
            if self.cached(room_id,self.cache_seconds):
                failed=self.repo.latest(room_id,"failed")
                current=self.repo.latest(room_id)
                error=failed["error_code"] if failed and failed["finished_at"]>current["finished_at"] else None
                return self.result(room,True,error)
        failed=self.repo.latest(room_id,"failed")
        if failed and failed["error_code"] not in {"NO_AUTH","AUTH_INVALID","AUTH_EXPIRED"} and (datetime.now(timezone.utc)-datetime.fromisoformat(failed["finished_at"])).total_seconds()<self.failure_seconds:
            return self.result(room,True,failed["error_code"])
        task=self.inflight.get(room_id)
        if task is None:
            task=asyncio.create_task(self._capture(room,trigger,requested_by))
            self.inflight[room_id]=task
            def done(finished):
                if self.inflight.get(room_id) is finished: self.inflight.pop(room_id,None)
            task.add_done_callback(done)
        # One user cancelling a request must not cancel other users' shared query.
        error=await asyncio.shield(task)
        if self._closing: raise QueryError("NOT_READY")
        return self.result(room,False,error)

    async def _capture(self, room, trigger, requested_by):
        async with self.gate:
            query_id=self.repo.begin(room["id"],trigger,requested_by)
            try:
                reading=await self.client.fetch(Room(room["campus"],room["building"],room["room_number"]),lambda seq,stage,status,content_type,body:self.repo.response(query_id,seq,stage,status,content_type,body))
                if reading.resolved_building:
                    room.update(self.repo.resolve_room(room,reading.resolved_building))
                self.repo.finish(query_id,reading)
                return None
            except asyncio.CancelledError:
                self.repo.finish(query_id,error="INTERRUPTED")
                raise
            except QueryError as exc:
                self.repo.finish(query_id,error=exc.code)
                return exc.code
            except Exception:
                self.repo.finish(query_id,error="INTERNAL")
                return "INTERNAL"

    def result(self, room, cached=False, error=None):
        rows=self.repo.recent_runs(room["id"])
        return public_result(room,rows[0] if rows else None,rows[1] if len(rows)>1 else None,cached=cached,error=error)

    async def bind(self, identity, room, origin):
        row=self.repo.room(room)
        result=await self.query(row,force=True,trigger="binding")
        if not result["ok"]: raise QueryError(result["error_code"] or "INTERNAL")
        return self.repo.bind(identity,row["id"],origin)

    async def mine(self, identity, force=False, trigger="manual"):
        binding=self.repo.binding(identity)
        if not binding: return {"ok":False,"error_code":"NOT_BOUND","message":"尚未绑定宿舍。请使用 /电费 绑定 虎溪 楼栋 房号。"}
        room={"id":binding["room_id"],"campus":binding["campus"],"building":binding["building"],"room_number":binding["room_number"]}
        if force: return await self.query(room,force=True,trigger=trigger,requested_by=binding["id"])
        return self.stored_result(room)

    def stored_result(self, room, *, now=None):
        # Reading commands and both LLM tools never initiate a capture.
        now=(now or datetime.now(BEIJING)).astimezone(BEIJING)
        due=now.replace(hour=14,minute=0,second=0,microsecond=0)
        before=now<due
        expected=(now.date()-timedelta(days=1) if before else now.date()).isoformat()
        rows=[r for r in self.repo.history(room["id"],30) if stamp(r)<=now]
        daily,_=daily_history(rows,min((r["day"] for r in rows),default=expected))
        fixed=[r for r in daily if r["sample_kind"]=="fixed" and r["day"]<=expected]
        chosen=fixed[-1] if fixed else None
        current=chosen["reading"] if chosen else rows[-1] if rows else None
        ready=bool(chosen and chosen["day"]==now.date().isoformat())
        failure=self.repo.latest(room["id"],"failed")
        morning=morning_snapshot(self.repo.completed_runs(room["id"],now=now),now)
        if morning:
            current=next(r for r in rows if r["id"]==morning["reading_id"])
            error=morning["refresh_error"]
            result=public_result(room,current,cached=True,error=error)
            result["stale"]=not morning["fresh"]
            result["settlement"]=morning
            result["usage"]=morning["usage"]
            result["daily_brief"]={"state":"morning_pending" if morning["state"]=="possibly_unsettled" else "morning_actual",
                                   "snapshot_day":now.date().isoformat(),"expected_snapshot_day":now.date().isoformat(),
                                   "estimate":morning["usage"],"source":"sqlite_morning_snapshot",
                                   "today":now.date().isoformat(),"next_refresh_at":due.isoformat(),
                                   "refresh_error":error,"estimated_day_unavailable_reason":morning["unavailable_reason"]}
            return result
        scheduled_failure=bool(not before and not ready and failure and failure["trigger_type"]=="scheduled" and due<=stamp(failure)<=now)
        error=failure["error_code"] if scheduled_failure else None
        result=public_result(room,current,cached=True,error=error if current else "NO_SAVED_READING")
        stale=bool(not chosen or chosen["day"]<expected)
        result["stale"]=bool(current) and (stale or bool(error))
        state="before_refresh" if before else "ready" if ready else "refresh_failed" if scheduled_failure else "awaiting_refresh"
        estimate=None
        if chosen and len(fixed)>=2:
            previous=fixed[-2]
            delta=chosen.get("usage")
            adjacent=chosen.get("span_days")==1 and (datetime.fromisoformat(chosen["day"])-datetime.fromisoformat(previous["day"])).days==1
            if adjacent and delta and delta.get("available"):
                estimate={"reference_day":(datetime.fromisoformat(chosen["day"]).date()-timedelta(days=1)).isoformat(),
                          "kwh":delta["kwh"],"cost_yuan":str(Decimal(delta["kwh"])*Decimal(current["unit_price_yuan"])),
                          "estimated":True,"method":"daily_meter_difference_times_price",
                          "note":"按相邻日采集的读数差作前一日参考，不代表学校实际计量时段或实际扣费。"}
        result["usage"]=chosen.get("usage") if chosen else None
        result["daily_brief"]={"state":state,"snapshot_day":chosen["day"] if chosen else None,
                               "expected_snapshot_day":expected,"estimate":estimate,"source":"sqlite_daily_snapshot" if chosen else "sqlite_latest_capture",
                               "today":now.date().isoformat(),"next_refresh_at":(due if before else due+timedelta(days=1)).isoformat(),
                               "refresh_error":error,"estimated_day_unavailable_reason":None if estimate else "缺少连续的14:00日记录或有效电表读数"}
        return result

    def history(self, identity, days=7):
        if isinstance(days,bool) or not isinstance(days,(int,float)) or int(days)!=days or not 1<=days<=30:
            return {"ok":False,"error_code":"INVALID_DAYS","message":"历史天数应为 1～30 的整数。"}
        binding=self.repo.binding(identity)
        if not binding: return {"ok":False,"error_code":"NOT_BOUND","message":"尚未绑定宿舍。"}
        days=int(days)
        rows=self.repo.history(binding["room_id"],days)
        start=(datetime.now(timezone(timedelta(hours=8))).date()-timedelta(days=days-1)).isoformat()
        result=[]; previous=None
        for row in rows:
            if row["day"]>=start: result.append({"day":row["day"],"reading":public_record(row),"usage":usage(row,previous)})
            previous=row
        daily,stats=daily_history(rows,start)
        return {"ok":True,"daily_records":daily,"overview":stats,"room":{k:binding[k] for k in ("campus","building","room_number")},"days":days,"records":result,"timezone":"Asia/Shanghai"}

    async def close(self):
        self._closing=True
        tasks=list(self.inflight.values())
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        self.repo.close()
