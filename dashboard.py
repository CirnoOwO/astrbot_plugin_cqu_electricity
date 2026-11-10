from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path

from .dashboard_model import BJ, make_model
from .calendar_model import calendar_pages


def dashboard_input(repo, identity, *, now=None, window_minutes=15):
    """Read only this caller's bound room; no query or database writes."""
    binding = repo.binding(identity)
    if not binding:
        return None
    room = {"id": binding["room_id"], "campus": binding["campus"],
            "building": binding["building"], "room_number": binding["room_number"]}
    return dashboard_room_input(repo, room, now=now, window_minutes=window_minutes)


def dashboard_room_input(repo, room, *, now=None, window_minutes=15):
    """Internal adapter for a known room, shared by rendering and reminders."""
    now = now or datetime.now(BJ)
    rows = repo.completed_runs(room["id"], now=now)
    fields = ("total_balance_yuan", "cash_balance_yuan", "meter_reading_kwh",
              "unit_price_yuan", "subsidy_kwh", "subsidy_balance_yuan")
    captures = []
    for row in rows:
        item = {key: row[key] for key in
                ("id", "started_at", "finished_at", "status", "error_code")}
        item["capture_slot"] = row.get("capture_slot")
        if row["status"] == "success":
            item["reading"] = {key: row[key] for key in fields}
        captures.append(item)
    return {"schema_version": 1,
            "room": {"campus": room["campus"], "building": room["building"],
                     "account": room["room_number"]},
            "as_of": now.isoformat(),
            "schedule": {"time": "14:00", "window_minutes": window_minutes},
            "captures": captures}


def dashboard_html(model):
    return report_html(model, "template.html")


def calendar_html(model):
    return report_html(model, "calendar_template.html")


def report_html(model, filename):
    encoded = json.dumps(model, ensure_ascii=False, separators=(",", ":"),
                         allow_nan=False).replace("<", r"\u003c").replace(">", r"\u003e").replace("&", r"\u0026")
    template = Path(__file__).with_name(filename).read_text(encoding="utf-8-sig")
    if template.count("__REPORT_DATA__") != 1:
        raise ValueError("Invalid electricity dashboard template")
    return template.replace("__REPORT_DATA__", encoded)


class DashboardRenderer:
    """One local browser, isolated pages, and no external render service."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.gate = asyncio.Lock()
        self.playwright = None
        self.browser = None

    async def render(self, data):
        model = make_model(data)
        html = dashboard_html(model)
        async with self.gate:
            async with asyncio.timeout(25):
                return await self._render(html)

    async def render_calendar(self, model):
        outputs = []
        async with self.gate:
            try:
                for page in calendar_pages(model):
                    async with asyncio.timeout(25):
                        outputs.append(await self._render(calendar_html(page), ready="__calendarReady"))
                return outputs
            except BaseException:
                for output in outputs:
                    Path(output).unlink(missing_ok=True)
                raise

    async def _render(self, html, *, ready="__reportReady"):
        from playwright.async_api import async_playwright
        self.directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.directory, 0o700)
        old = sorted(self.directory.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
        for index, path in enumerate(old):
            if index >= 127 or time.time() - path.stat().st_mtime > 86400:
                path.unlink(missing_ok=True)
        if self.browser is None or not self.browser.is_connected():
            if self.playwright is not None:
                await self.playwright.stop()
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(headless=True)
        context = await self.browser.new_context(
            viewport={"width": 800, "height": 1000}, device_scale_factor=2, locale="zh-CN")
        output = self.directory / (uuid.uuid4().hex + ".png")
        try:
            await context.route("**/*", lambda route: route.abort())
            page = await context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.set_content(html, wait_until="load", timeout=10000)
            await page.wait_for_function(f"window.{ready} === true", timeout=10000)
            await page.evaluate("document.fonts.ready")
            invalid = await page.evaluate("""() => document.documentElement.scrollWidth > 800 ||
                [...document.querySelectorAll('svg *')].some(e =>
                [...e.attributes].some(a => /NaN|Infinity/.test(a.value)))""")
            if errors or invalid:
                raise RuntimeError("Invalid dashboard rendering")
            await page.locator('.report').screenshot(path=str(output))
            if os.name != "nt":
                os.chmod(output, 0o600)
            return str(output)
        except BaseException:
            output.unlink(missing_ok=True)
            raise
        finally:
            await context.close()

    async def close(self):
        async with self.gate:
            if self.browser is not None:
                await self.browser.close()
                self.browser = None
            if self.playwright is not None:
                await self.playwright.stop()
                self.playwright = None
