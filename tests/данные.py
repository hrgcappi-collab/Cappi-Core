"""A5 · AC-05: цифры на экранах против Syrve Office.

Контрольные даты задаются аргументами: python3 tests/данные.py 2026-09-11 2026-09-12
Сверяем то, что видит человек, с тем, что отдаёт Syrve по правилам из
CLAUDE.md — и печатаем расхождения, а не «всё ок».
"""
import sys, re; sys.path.insert(0,'.')
import cappi, report, kpi
from datetime import date, timedelta

ДАТЫ=[date.fromisoformat(x) for x in (sys.argv[1:] or ["2026-09-11","2026-09-12"])]

def олап(s, поля, агрегаты, день, фильтры=None, тип="SALES"):
    поле="OpenDate.Typed" if тип=="SALES" else "DateTime.DateTyped"
    body={"reportType":тип,"buildSummary":False,"groupByRowFields":list(поля),
          "aggregateFields":list(агрегаты),
          "filters":{поле:{"filterType":"DateRange","periodType":"CUSTOM",
                           "from":день.isoformat(),"to":(день+timedelta(days=1)).isoformat()},
                     **report.НАШ_ОТДЕЛ(), **(фильтры or {})}}
    return cappi.олап(cappi._post(f"{s.host}/resto/api/v2/reports/olap?key={s.key}", body, timeout=60))

провал=0
for день in ДАТЫ:
    print(f"\n═══ {день:%d.%m.%Y}")
    d=report.collect(день)
    with cappi.Syrve() as s:
        # эталон: правила из CLAUDE.md напрямую
        зак=олап(s,[], ["UniqOrderId"], день, {**report.НЕ_УДАЛЁННЫЕ, **report.ЕДА})
        эт_заказов=(зак[0].get("UniqOrderId") if зак else 0) or 0
        бл=олап(s,[], ["DishAmountInt"], день,
                {**report.НЕ_УДАЛЁННЫЕ, "DishType":{"filterType":"IncludeValues","values":["DISH"]}})
        эт_блюд=(бл[0].get("DishAmountInt") if бл else 0) or 0
        эт_выручка=report.выручка_пиу(s, день, день)["всего"]
        # время: кухня в секундах, пречек и путь в минутах
        вр=олап(s,["RestaurantSection"],
                ["Cooking.KitchenTime.Avg","OrderTime.AveragePrechequeTime","Delivery.WayDurationAvg"],
                день, report.ТОЛЬКО_БЛЮДА())
        эт_время={}
        for r in вр:
            т=report.ТОЧКИ.get(r.get("RestaurantSection"))
            if т: эт_время[т]=(r.get("Cooking.KitchenTime.Avg") or 0)/60 + (r.get("OrderTime.AveragePrechequeTime") or 0)
        см=report.attendance(s, день)
    сверка=[("заказов", d["чеков"], эт_заказов, 0),
            ("блюд", d["продажи"]["блюда"]["штук"], эт_блюд, 0),
            ("выручка ПиУ", d["выручка"]["всего"], эт_выручка, 0)]
    for т,v in (d.get("времена") or {}).items():
        сверка.append((f"время {т}", v["доставка"], эт_время.get(т,0), 1))
    сверка.append(("людей на смене", см["людей"], len({ч["имя"] for ч in см["люди"]}), 0))
    for имя, экран, эталон, допуск in сверка:
        рас = abs(экран-эталон)
        ок = рас <= (эталон*допуск/100 if допуск else 0.01)
        провал += 0 if ок else 1
        print(f"  {'✅' if ок else '❌'} {имя:<18} экран {экран:>12,.2f} · Syrve {эталон:>12,.2f}"
              + ("" if ок else f"  расхождение {рас:,.2f}"))
print(f"\nрасхождений: {провал}")
sys.exit(1 if провал else 0)
