# Тянем всё, на чём будет считаться эластичность:
#   дневная панель продаж по блюдам, приказы о цене, номенклатура, себестоимость.
import sys, json, datetime as dt
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
import cappi, report

import os
S = sys.argv[1] if len(sys.argv) > 1 else "/tmp/эластичность"  # куда тяга.py сложила выгрузку
НАЧАЛО, КОНЕЦ = dt.date(2026, 1, 1), dt.date(2026, 9, 15)


def олап(s, c, по, разрез, счёт):
    body = {"reportType": "SALES", "buildSummary": False,
            "groupByRowFields": разрез, "aggregateFields": счёт,
            "filters": {"OpenDate.Typed": {"filterType": "DateRange",
                                           "periodType": "CUSTOM",
                                           "from": c.isoformat(), "to": по.isoformat()},
                        **report.НАШ_ОТДЕЛ(), **report.НЕ_УДАЛЁННЫЕ, **report.ЕДА}}
    return cappi.олап(cappi._post(
        f"{s.host}/resto/api/v2/reports/olap?key={s.key}", body, timeout=180))


with cappi.Syrve() as s:
    панель = []
    д = НАЧАЛО
    while д < КОНЕЦ:
        след = (д.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        до = min(след, КОНЕЦ)
        куски = олап(s, д, до, ["OpenDate.Typed", "DishCode", "DishName", "DishCategory"],
                     ["DishAmountInt", "DishDiscountSumInt", "DishSumInt"])
        панель += куски
        print(f"  {д}..{до}  строк {len(куски)}", flush=True)
        д = след
    json.dump(панель, open(f"{S}/панель.json", "w"), ensure_ascii=False)

    себес = олап(s, dt.date(2026, 6, 1), КОНЕЦ, ["DishCode", "DishName"],
                 ["DishAmountInt", "DishDiscountSumInt",
                  "ProductCostBase.ProductCost"])
    json.dump(себес, open(f"{S}/себес.json", "w"), ensure_ascii=False)
    print("себестоимость:", len(себес), flush=True)

    приказы = s.orders("2025-10-01", "2026-09-20")
    json.dump(приказы, open(f"{S}/приказы.json", "w"), ensure_ascii=False)
    print("приказов:", len(приказы), flush=True)

    товары = [{"id": p.get("id"), "код": p.get("num"), "имя": p.get("name"),
               "группа": p.get("parent"), "тип": p.get("type")}
              for p in s.products()]
    json.dump(товары, open(f"{S}/товары.json", "w"), ensure_ascii=False)
    print("товаров:", len(товары), flush=True)

    цены = s.prices(КОНЕЦ.isoformat())
    json.dump(цены, open(f"{S}/цены.json", "w"), ensure_ascii=False)
    print("цен:", len(цены), flush=True)
