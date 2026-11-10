"""Exercise the actual calendar HTML in Chromium; no database or network use."""
import argparse
import asyncio
import calendar
import copy
import importlib.util
import json
import sys
from pathlib import Path


def fixture(values, *, year=2026, month=10):
    days = [dict(day=f"{year}-{month:02d}-{i:02d}", value=value, status="缺少记录",
                 balance="10" if value is not None else None, balance_captured_at=None,
                 future=False, today=False, start=False, balance_increase=False)
            for i, value in enumerate(values, 1)]
    return dict(schema_version=1, scope="month", title_label="月度用电日历",
                room=dict(campus_label="测试", building="样例楼栋", account="TEST"),
                captured_at=None, start_day=f"{year}-{month:02d}-01", end_day=f"{year}-{month:02d}-28",
                range_label=f"{year}年{month}月", basis_label="自然月统计",
                balance=None, balance_net_change=None, inferred=False,
                total_kwh=str(sum(float(x) for x in values if x is not None)) if any(x is not None for x in values) else None,
                recorded_days=sum(x is not None for x in values), days=days,
                months=[dict(year=year, month=month, first_weekday=calendar.weekday(year,month,1),
                             days_in_month=calendar.monthrange(year,month)[1])])


async def check(root, output, models):
    spec = importlib.util.spec_from_file_location("calendar_check", root/"__init__.py", submodule_search_locations=[str(root)])
    package = importlib.util.module_from_spec(spec)
    sys.modules["calendar_check"] = package; spec.loader.exec_module(package)
    from calendar_check.dashboard import DashboardRenderer, calendar_html
    from calendar_check.calendar_model import calendar_pages
    renderer = DashboardRenderer(output)
    cases = {
        "low_usage": fixture(["1.78", "2.61", "3.47", "4.01", "5.06", None]),
        "ties_zero": fixture(["0", "0", None, "3", "5", "5"]),
        "equal": fixture(["3", "3", "3"]),
        "all_zero": fixture(["0", "0"]),
        "empty": fixture([None, None]),
        "single": fixture([None, "3", None]),
        "display_precision": fixture(["3.00001", "3.00002", None]),
        "tag_collision": fixture(["1", "5", "3"]),
    }
    cases["tag_collision"]["days"][1].update(start=True, balance_increase=True)
    # A cross-month cycle must use each visible month's own range.
    cross = copy.deepcopy(cases["low_usage"])
    september = fixture(["50", "55", "60"], month=9)
    cross.update(scope="cycle", page_index=2, page_count=2,
                 months=september["months"]+cross["months"],
                 days=september["days"]+cross["days"])
    cases["cross_month"] = cross
    for account, model in models.items():
        for index, page in enumerate(calendar_pages(model), 1):
            cases[account+"-"+str(index)] = page
    reports = []
    try:
        for name, model in cases.items():
            html = calendar_html(model)
            image = await renderer._render(html, ready="__calendarReady")
            if name in {k+"-1" for k in models}:
                (output/(name+".html")).write_text(html, encoding="utf-8")
            for width in (800, 400):
                context = await renderer.browser.new_context(viewport={"width":width,"height":1200})
                try:
                    await context.route("**/*", lambda route: route.abort())
                    page = await context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    await page.set_content(html)
                    await page.wait_for_function("window.__calendarReady===true")
                    await page.evaluate("document.fonts.ready")
                    result = await page.evaluate("""() => {
                      const d=window.__calendarModel;
                      const tiles=[...document.querySelectorAll('.day')];
                      const cardResults=[...document.querySelectorAll('.month-card')].map((card,i)=>{
                        const m=d.months[i], prefix=m.year+'-'+String(m.month).padStart(2,'0')+'-';
                        const values=d.days.filter(x=>x.day.startsWith(prefix)&&!x.future&&x.value!==null).map(x=>Number(Number(x.value).toFixed(2)));
                        const low=Math.min(...values), high=Math.max(...values), varied=values.length>0&&high>low;
                        const nodes=[...card.querySelectorAll('.day')];
                        const valid=nodes.filter(n=>n.hasAttribute('data-kwh'));
                        const hi=nodes.filter(n=>n.classList.contains('usage-high'));
                        const lo=nodes.filter(n=>n.classList.contains('usage-low'));
                        return {
                          min:values.length?low:null,max:values.length?high:null,
                          highDays:hi.map(n=>n.dataset.day),lowDays:lo.map(n=>n.dataset.day),
                          levels:[...new Set(valid.map(n=>[...n.classList].find(c=>/^level-/.test(c))))],
                          correct:values.length===valid.length&&nodes.every(n=>{
                            const x=d.days.find(x=>x.day===n.dataset.day);
                            const valid=x&&x.value!==null&&!x.future;
                            if(!valid)return !n.hasAttribute('data-kwh')&&!n.querySelector('.tag-high,.tag-low');
                            const v=Number(Number(x.value).toFixed(2));
                            const balance=n.querySelector('[data-balance]');
                            return n.dataset.kwh===x.value&&
                              (!!n.querySelector('.tag-high')===(varied&&v===high))&&
                              (!!n.querySelector('.tag-low')===(varied&&v===low))&&
                              (x.balance===null ? !balance : balance.dataset.balance===x.balance);
                          }),
                          legend:card.querySelector('.legend').textContent
                        };
                      });
                      const clipped=[...document.querySelectorAll('.day .tag')].filter(tag=>{
                        const a=tag.getBoundingClientRect(),b=tag.closest('.day').getBoundingClientRect();
                        return a.left<b.left||a.right>b.right+1||a.bottom>b.bottom+1;
                      }).length;
                      return {cards:cardResults,clipped,overflow:document.documentElement.scrollWidth>innerWidth};
                    }""")
                    assert not errors and not result["overflow"] and not result["clipped"], (name, width, errors, result)
                    assert all(card["correct"] for card in result["cards"]), (name, result)
                    if name == "low_usage":
                        assert len(result["cards"][0]["levels"]) == 4, result
                    if name == "A7511-1":
                        assert len(result["cards"][0]["levels"]) >= 3, result
                    if width == 800:
                        reports.append(dict(name=name, image=image, checks=result))
                finally:
                    await context.close()
    finally:
        await renderer.close()
    print(json.dumps(dict(passed=len(cases)*2, reports=reports),ensure_ascii=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--models", type=Path)
    args = parser.parse_args()
    models = json.loads(args.models.read_text(encoding="utf-8")) if args.models else {}
    asyncio.run(check(args.root, args.output, models))
