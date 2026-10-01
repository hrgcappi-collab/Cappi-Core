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
        # Эталон — то же множество, что в отчёте: блюда и товары. Банка колы
        # создаёт заказ, значит и в числитель она обязана попадать.
        бл=олап(s,[], ["DishAmountInt"], день,
                {**report.НЕ_УДАЛЁННЫЕ, **report.ЕДА})
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
            ("блюд", d["продажи"]["блюда_и_товары"]["штук"], эт_блюд, 0),
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

# Полнота словаря концепций. Концепция, которой нет в КОНЦЕПЦИИ, молча
# выпадает из разбивки по филиалам, оставаясь в общем итоге: сумма строк
# перестаёт сходиться с числом над ними, и выглядит это как пропажа денег,
# а не как незаполненный словарь. Спрашиваем широкий период — концепция
# может месяцами молчать и ожить, как «Борщик».
print("\nполнота словаря концепций (за год):")
from datetime import timedelta as _td
_по = date.today()
_с = _по - _td(days=365)
with cappi.Syrve() as s:
    _r = cappi._post(
        f"{s.host}/resto/api/v2/reports/olap?key={s.key}",
        {"reportType": "TRANSACTIONS", "buildSummary": False,
         "groupByRowFields": ["Conception"],
         "aggregateFields": ["Sum.Incoming", "Sum.Outgoing"],
         "filters": {"DateTime.DateTyped": {
             "filterType": "DateRange", "periodType": "CUSTOM",
             "from": _с.isoformat(), "to": (_по + _td(days=1)).isoformat()},
             "Account.Name": {"filterType": "IncludeValues",
                              "values": list(report.СЧЕТА_ВЫРУЧКИ)}}},
        timeout=90)
_чужие = []
for _x in cappi.олап(_r):
    _имя = _x.get("Conception")
    _сум = (_x.get("Sum.Outgoing") or 0) - (_x.get("Sum.Incoming") or 0)
    if abs(_сум) < 1:
        continue
    if _имя not in report.КОНЦЕПЦИИ:
        _чужие.append((_имя, _сум))
for _имя, _сум in _чужие:
    print(f"  ❌ «{_имя}» нет в КОНЦЕПЦИИ — {_сум:,.0f} ₴ выпадает из разбивки")
if not _чужие:
    print("  ✅ все концепции с выручкой разобраны")
провал += len(_чужие)

# Полнота имён филиалов в Loopa. У Лазаревой их там два, и пока второе
# («Болтенка») не было известно, недельный отчёт показывал по ней 10 отзывов
# вместо 348. Новое имя должно ронять тест, а не тихо уносить половину жалоб
# в строку, которой никто не смотрит.
print("\nполнота имён филиалов в Loopa (за месяц):")
try:
    _о = report._loopa(**{"from": (_по - _td(days=30)).isoformat(),
                          "to": _по.isoformat(), "group_by": "branch"}) or {}
    _незнакомые = []
    for _g in _о.get("groups", []):
        _имя = (_g.get("key") or "").strip()
        _кол = _g.get("count") or 0
        if not _кол or not _имя:
            continue
        if report.филиал_loopa(_имя) or cappi.norm(_имя) in report.LOOPA_НЕ_ФИЛИАЛ:
            continue
        _незнакомые.append((_имя, _кол))
    for _имя, _кол in _незнакомые:
        print(f"  ❌ «{_имя}» не сопоставлено ни с одним филиалом — {_кол} отзывов")
    if not _незнакомые:
        print("  ✅ все имена с отзывами разобраны")
    провал += len(_незнакомые)
except Exception as _e:
    print(f"  ⏭ Loopa не ответила: {str(_e)[:70]}")

sys.exit(1 if провал else 0)
