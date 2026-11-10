from __future__ import annotations

# Query protocol and field aliases adapted from ChrisKimZHT/CQU-Electricity-Bill.
# Copyright (c) 2026 Haotian Zou; MIT license, see LICENSE.
import asyncio
import json
import re
from decimal import Decimal
from dataclasses import replace
from html import unescape, escape
from pathlib import Path
from urllib.parse import quote
import httpx
from .models import Room, Reading, QueryError, number, building_name, shapingba_building

HOST = "payment.cqu.edu.cn"
SENSITIVE = {"token", "accesstoken", "refreshtoken", "authorization", "synjonesauth", "cookie", "setcookie", "password", "secret", "appsecret"}


def redact_body(body, token):
    # Preserve response bytes when no secret is present. Never store request headers.
    target_values = {token, quote(token, safe=""), escape(token)} - {""}
    for value in target_values:
        body = body.replace(value.encode(), b"[REDACTED]")
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError):
        return re.sub(rb'(?i)((?:access_token|refresh_token|password|authorization|synjones-auth)[\s"\']*[:=][\s"\']*)([^"\'<>\s,}]+)', rb'\1[REDACTED]', body)
    changed = False
    def clean(item):
        nonlocal changed
        if isinstance(item, dict):
            result = {}
            for key, val in item.items():
                if re.sub(r"[^a-z]", "", key.lower()) in SENSITIVE:
                    result[key] = "[REDACTED]"
                    changed = True
                else:
                    result[key] = clean(val)
            return result
        if isinstance(item, list):
            return [clean(v) for v in item]
        if isinstance(item, str):
            res_str = item
            for tv in target_values:
                if tv in res_str:
                    res_str = res_str.replace(tv, "[REDACTED]")
                    changed = True
            return res_str
        return item
    cleaned = clean(value)
    return json.dumps(cleaned, ensure_ascii=False).encode() if changed else body


def load_token(path, session=""):
    try:
        token = ""
        if isinstance(session, str):
            token = session.strip()
        elif session is not None:
            raise ValueError("session must be a string")
        if not token:
            data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
            token = data.get("access_token", "") if isinstance(data, dict) else ""
        if not isinstance(token, str):
            raise ValueError()
        token = token.strip()
        pairs = {("'", "'"), ('"', '"'), ("‘", "’"), ("“", "”")}
        while len(token) >= 2 and (token[0], token[-1]) in pairs:
            token = token[1:-1].strip()
        token = re.sub(r"^bearer\s+", "", token, flags=re.I).strip()
        while len(token) >= 2 and (token[0], token[-1]) in pairs:
            token = token[1:-1].strip()
        if not token:
            raise QueryError("NO_AUTH")
        if not token.isascii():
            raise ValueError("Non-ASCII character in token")
        if any(c in token for c in "'\"‘’“”"):
            raise ValueError("Unmatched quotes in token")
        if ";" in token:
            raise ValueError("Semicolon in token")
        if any(ord(c) < 32 or ord(c) == 127 for c in token):
            raise ValueError("Control character in token")
        if len(token) > 16384 or any(c.isspace() for c in token):
            raise ValueError("Whitespace or excessive length in token")
        return token
    except FileNotFoundError:
        raise QueryError("NO_AUTH") from None
    except QueryError:
        raise
    except (OSError, ValueError):
        raise QueryError("AUTH_INVALID") from None


def parse_reading(room, response_map, price):
    data, display = response_map.get("data") or {}, response_map.get("showData") or {}
    if not isinstance(data, dict) or not isinstance(display, (dict,list)):
        raise QueryError("PARSE_ERROR")
    try:
        if room.campus == "shapingba":
            if str(data.get("roomID", "")).strip().upper() != room.room_number:
                raise QueryError("ROOM_MISMATCH")
            unit = number(data.get("price", display.get("电价")))
            if unit <= 0:
                raise ValueError()
            return Reading(number(data.get("cashBalance",display.get("现金余额"))),
                           subsidy_balance_yuan=number(data.get("subsidiesBalance",display.get("补贴余额"))), unit_price_yuan=unit)
        for key in ("room", "roomname", "roomID", "fjmc", "accountname"):
            if data.get(key) and str(data[key]).strip().upper() != room.room_number:
                raise QueryError("ROOM_MISMATCH")
        # The upstream API can provide direct fields or labelled entries.
        values = {}
        def visit(item):
            if isinstance(item, dict):
                label = item.get("name") or item.get("label") or item.get("title") or item.get("text")
                val = item.get("value", item.get("data"))
                if label and val is not None and not isinstance(val,(list,dict)):
                    values[str(label).strip().lower()] = val
                for key, val in item.items():
                    if not isinstance(val,(dict,list)) and val not in (None,""):
                        values[str(key).strip().lower()] = val
                    else:
                        visit(val)
            elif isinstance(item,list):
                for child in item: visit(child)
        visit(data); visit(display)
        def pick(names, required=False):
            for name in names:
                if name in values: return number(unescape(str(values[name])))
            if required: raise ValueError()
            return None
        cash = pick(("balance","money","surplusmoney","remainingamount","剩余金额","余额"), True)
        meter = pick(("meterreading","reading","electricreading","usedegree","quantity","电表读数","电量读数"))
        subsidy = pick(("subsidy","electricallowance","surplusdegree","电剩余补助（度）","电剩余补助"))
        if price <= 0 or (meter is not None and meter < 0) or (subsidy is not None and subsidy < 0):
            raise ValueError()
        return Reading(cash, subsidy_kwh=subsidy, meter_reading_kwh=meter, unit_price_yuan=price)
    except QueryError:
        raise
    except (ValueError,TypeError,AttributeError):
        raise QueryError("PARSE_ERROR") from None


class ElectricityClient:
    def __init__(self, token_path, *, session="", scheme="https", timeout=40, price="0.54", transport=None):
        if scheme not in {"http", "https"}: raise ValueError("Invalid scheme")
        self.token_path, self.scheme = token_path, scheme
        self.session = session
        self.timeout, self.price = max(1,min(float(timeout),90)), number(price)
        if self.price <= 0: raise ValueError("Invalid price")
        self.transport = transport

    def configured(self):
        try: load_token(self.token_path, self.session); return True
        except QueryError: return False

    async def fetch(self, room, save_response):
        token = load_token(self.token_path, self.session)
        authorization = "bearer " + token
        headers = {"synjones-auth":authorization,"Authorization":"Basic Y2hhcmdlOmNoYXJnZV9zZWNyZXQ=", "User-Agent":"AstrBot-CQU-Electricity/0.1.7"}
        cookies = httpx.Cookies()
        cookies.set("synjones-auth",authorization,domain=HOST,path="/charge")
        seq = 0
        try:
            async with asyncio.timeout(self.timeout):
                async with httpx.AsyncClient(headers=headers,cookies=cookies,timeout=min(15,self.timeout),follow_redirects=False,transport=self.transport) as http:
                    async def request(stage, form):
                        nonlocal seq
                        async with http.stream("POST",f"{self.scheme}://{HOST}/charge/feeitem/getThirdData",data=form) as response:
                            chunks, size = [], 0
                            async for chunk in response.aiter_bytes():
                                chunks.append(chunk); size += len(chunk)
                                if size > 2 * 1024 * 1024:
                                    seq += 1
                                    save_response(seq,stage,response.status_code,response.headers.get("content-type",""),redact_body(b"".join(chunks),token))
                                    raise QueryError("RESPONSE_TOO_LARGE")
                            body = b"".join(chunks)
                            seq += 1
                            save_response(seq,stage,response.status_code,response.headers.get("content-type",""),redact_body(body,token))
                            if response.status_code in (401,403) or 300 <= response.status_code < 400:
                                raise QueryError("AUTH_EXPIRED")
                            if response.status_code >= 400: raise QueryError("HTTP_ERROR")
                            try: result = json.loads(body)
                            except (ValueError,UnicodeError):
                                if (re.search(rb'(?i)(login|password)',body) or "统一认证".encode() in body): raise QueryError("AUTH_EXPIRED")
                                raise QueryError("INVALID_RESPONSE") from None
                            if not isinstance(result,dict): raise QueryError("INVALID_RESPONSE")
                            if str(result.get("code")) in ("401","403"): raise QueryError("AUTH_EXPIRED")
                            if str(result.get("code")) != "200": raise QueryError("INVALID_RESPONSE")
                            return result.get("map") or {}
                    base = {"feeitemid":{"huxi":"448","shapingba":"449"}[room.campus]}
                    initial = await request("buildings",{**base,"type":"select","level":0})
                    levels, buildings = initial.get("total") or [], initial.get("data") or []
                    if not isinstance(levels,list) or len(levels)<2 or not isinstance(buildings,list):
                        raise QueryError("INVALID_RESPONSE")
                    area=re.fullmatch(r"菊园([AB])区",room.building)
                    if room.campus=="shapingba":
                        wanted=shapingba_building(room.building)
                        candidates=[b for b in buildings if isinstance(b,dict) and wanted and shapingba_building(b.get("name",""))==wanted]
                    else:
                        candidates = [b for b in buildings if isinstance(b,dict) and (building_name(b.get("name",""))==building_name(room.building) or (area and re.fullmatch(r"菊园"+area[1]+r"[1-3]栋",building_name(b.get("name","")))))]
                    if not candidates: raise QueryError("BUILDING_NOT_FOUND")
                    if room.campus!="shapingba" and not area and len(candidates)!=1: raise QueryError("AMBIGUOUS_ROOM")
                    bkey, rkey = levels[0].get("code") or "building", levels[-1].get("code") or "room"
                    matched=[]
                    for building in candidates:
                        result = await request("rooms",{**base,"type":"select","level":1,bkey:building["value"]})
                        choices = [r for r in result.get("data",[]) if isinstance(r,dict) and room.room_number in {str(r.get("name","")).strip().upper(),str(r.get("value","")).strip().upper()}]
                        matched.extend((building,r) for r in choices)
                    if not matched: raise QueryError("ROOM_NOT_FOUND")
                    if len(matched)!=1: raise QueryError("AMBIGUOUS_ROOM")
                    building,matched_room=matched[0]
                    final = await request("reading",{**base,"type":"IEC","level":len(levels),bkey:building["value"],rkey:matched_room["value"]})
                    if not final.get("showData"): raise QueryError("INVALID_RESPONSE")
                    resolved=building_name(building["name"])
                    # Preserve an existing equivalent binding key and its history.
                    if room.campus=="huxi" and resolved==building_name(room.building):
                        resolved=room.building
                    return replace(parse_reading(room,final,self.price),resolved_building=resolved)
        except QueryError:
            raise
        except (TimeoutError,httpx.TimeoutException): raise QueryError("TIMEOUT") from None
        except httpx.HTTPError: raise QueryError("NETWORK") from None
        except (ValueError,TypeError,KeyError,AttributeError): raise QueryError("INVALID_RESPONSE") from None
