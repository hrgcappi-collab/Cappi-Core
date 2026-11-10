"""Цифры месяца для закрытия: ПиУ, заказы, времена, скидки, ABC. Только чтение.

    python3 закрытие/цифры.py 2026-10 [--папка ПУТЬ]

Папка по умолчанию — `~/Downloads/Закрытие-ГГГГ-ММ/`.
Год к году считается по тому же месяцу прошлого года. Период — полуинтервал
[первое, первое следующего). Подробности и ловушки — в закрытие/README.md.
"""
import sys, os, csv
from datetime import date
from calendar import monthrange

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cappi, report


def месяц(строка):
    """'2026-10' → (date(2026,10,1), date(2026,10,31))."""
    год, мес = (int(ч) for ч in строка.split("-"))
    return date(год, мес, 1), date(год, мес, monthrange(год, мес)[1])


def полуинтервал(первый, последний):
    """Для OLAP: [from, to), где to — первое число следующего месяца."""
    след = (последний.replace(day=1).replace(year=последний.year + 1, month=1)
            if последний.month == 12
            else последний.replace(day=1, month=последний.month + 1))
    return первый.isoformat(), след.isoformat()


def олап(s, тип, поля, агрегаты, с, по, фильтры=None, свой_отдел=True, таймаут=240):
    поле = "OpenDate.Typed" if тип == "SALES" else "DateTime.DateTyped"
    отбор = {поле: {"filterType": "DateRange", "periodType": "CUSTOM",
                    "from": с, "to": по}}
    # В проводках нет поля с id подразделения — фильтровать нечем, см. README.
    if тип == "SALES" and свой_отдел:
        отбор.update(report.НАШ_ОТДЕЛ())
    отбор.update(фильтры or {})
    body = {"reportType": тип, "buildSummary": False,
            "groupByRowFields": list(поля), "aggregateFields": list(агрегаты),
            "filters": отбор}
    return cappi.олап(cappi._post(
        f"{s.host}/resto/api/v2/reports/olap?key={s.key}", body, timeout=таймаут))


def продажи(s, поля, агрегаты, с, по, фильтры=None):
    ф = {**report.НЕ_УДАЛЁННЫЕ, **report.ЕДА, **(фильтры or {})}
    return олап(s, "SALES", поля, агрегаты, с, по, ф)


def проводки(s, поля, с, по):
    return олап(s, "TRANSACTIONS", поля, ["Sum.Incoming", "Sum.Outgoing"], с, по)


def сумма_проводки(x):
    """Расход минус приход: так статья ПиУ получает свой знак."""
    return (x.get("Sum.Outgoing") or 0) - (x.get("Sum.Incoming") or 0)


def шапка(номер, текст):
    print("\n" + "=" * 78)
    print(f"{номер} · {текст}")
    print("=" * 78)


def главное(s, этот, прошлый, папка, подпись, подпись_пр):
    # ── 1 · выручка по правилу ПиУ ──────────────────────────────────────
    шапка(1, f"ВЫРУЧКА ПО ПиУ · {подпись} против {подпись_пр}")
    в = {}
    for ключ, (первый, последний) in (("этот", этот), ("прошлый", прошлый)):
        в[ключ] = report.выручка_пиу(s, первый, последний)
    for ключ, п in (("прошлый", подпись_пр), ("этот", подпись)):
        d = в[ключ]
        точки = " · ".join(f"{к} {v:,.0f}" for к, v in sorted(d["точки"].items()))
        print(f"  {п:<16} {d['всего']:>14,.2f}   {точки}")
    if в["прошлый"]["всего"]:
        рост = (в["этот"]["всего"] / в["прошлый"]["всего"] - 1) * 100
        print(f"  {'год к году':<16} {рост:>13.1f}%")
    доли = в["этот"]["точки"]
    если = sum(доли.values())
    if если:
        print("  доли филиалов: " + " · ".join(
            f"{к} {v / если * 100:.1f}%" for к, v in sorted(доли.items())))

    # ── 2 · ПиУ по статьям, два года рядом ──────────────────────────────
    шапка(2, f"ПиУ ПО СТАТЬЯМ · {подпись} против {подпись_пр}")
    статьи = {}
    for ключ, (первый, последний) in (("этот", этот), ("прошлый", прошлый)):
        с, по = полуинтервал(первый, последний)
        for x in проводки(s, ["Account.Code", "Account.Name"], с, по):
            к = ((x.get("Account.Code") or ""),
                 (x.get("Account.Name") or "?").strip())
            a = статьи.setdefault(к, {"этот": 0.0, "прошлый": 0.0})
            a[ключ] += сумма_проводки(x)
    путь = os.path.join(папка, f"пиу-по-статьям-{подпись}.csv")
    with open(путь, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["код счёта", "статья", f"{подпись_пр}, ₴", f"{подпись}, ₴",
                    "изменение, ₴", "изменение, %"])
        for (код, имя), v in sorted(статьи.items()):
            if abs(v["этот"]) < 1 and abs(v["прошлый"]) < 1:
                continue
            изм = v["этот"] - v["прошлый"]
            проц = (v["этот"] / v["прошлый"] - 1) * 100 if v["прошлый"] else None
            w.writerow([код, имя, f"{v['прошлый']:.2f}", f"{v['этот']:.2f}",
                        f"{изм:.2f}", f"{проц:.1f}" if проц is not None else ""])
    print(f"  {os.path.basename(путь)} — {len(статьи)} статей, два года рядом")
    крупные = sorted(((к, v) for к, v in статьи.items()
                      if abs(v["этот"] - v["прошлый"]) >= 20000),
                     key=lambda kv: -abs(kv[1]["этот"] - kv[1]["прошлый"]))
    print(f"\n  {'статья':<48}{подпись_пр:>13}{подпись:>13}{'измен.':>13}")
    for (код, имя), v in крупные[:25]:
        print(f"  {имя[:46]:<48}{v['прошлый']:>13,.0f}{v['этот']:>13,.0f}"
              f"{v['этот'] - v['прошлый']:>13,.0f}")
    if not крупные:
        print("  — изменений крупнее 20 000 ₴ нет —")

    # ── 3 · негативные остатки ──────────────────────────────────────────
    шапка(3, "КОРЕКЦІЯ НЕГАТИВНИХ ЗАЛИШКІВ")
    с, по = полуинтервал(*этот)
    нашлось = False
    # В проводках склад называется Store, а не Store.Name: имени с точкой
    # в этом отчёте нет, и запрос с ним отвечает HTTP 400.
    for поле in ("Conception", "Store", "Product.Name"):
        строки = [x for x in проводки(s, ["Account.Name", поле], с, по)
                  if "02.07" in (x.get("Account.Name") or "")
                  or "егатив" in (x.get("Account.Name") or "")]
        if not строки:
            continue
        нашлось = True
        print(f"\n  разрез по {поле}:")
        for x in sorted(строки, key=lambda y: сумма_проводки(y)):
            имя = x.get(поле) or "— не указано —"
            print(f"    {str(имя)[:46]:<46} {сумма_проводки(x):>12,.2f}")
        print(f"    {'итого':<46} "
              f"{sum(сумма_проводки(x) for x in строки):>12,.2f}")
    if not нашлось:
        print("  — статьи нет, корректировок за месяц не было —")


def заказы(s, этот, подпись):
    с, по = полуинтервал(*этот)

    # ── 4 · заказы, блюда и сумма по филиалам ───────────────────────────
    шапка(4, f"ЗАКАЗЫ И БЛЮДА · {подпись} · ПО ФИЛИАЛАМ")
    for пояснение, ф in (("с фильтром еды (так и считаем)",
                          {**report.НЕ_УДАЛЁННЫЕ, **report.ЕДА}),
                         ("без фильтра еды — для сверки, задваивает",
                          dict(report.НЕ_УДАЛЁННЫЕ))):
        строки = олап(s, "SALES", ["Conception"],
                      ["UniqOrderId", "DishAmountInt", "DishDiscountSumInt"],
                      с, по, ф)
        итого = [0, 0.0, 0.0]
        print(f"\n  {пояснение}:")
        for x in sorted(строки, key=lambda y: str(y.get("Conception"))):
            к = x.get("Conception")
            з = x.get("UniqOrderId") or 0
            б = x.get("DishAmountInt") or 0
            д = x.get("DishDiscountSumInt") or 0
            наш = report.КОНЦЕПЦИИ.get(к, "???")
            print(f"    {str(к):<16} {з:>7} зак · {б:>8.0f} блюд · "
                  f"{д:>12,.0f} ₴   → {наш}")
            if наш:
                итого[0] += з; итого[1] += б; итого[2] += д
        чек = итого[2] / итого[0] if итого[0] else 0
        print(f"    {'ИТОГО наше':<16} {итого[0]:>7} зак · {итого[1]:>8.0f} блюд"
              f" · {итого[2]:>12,.0f} ₴ · чек {чек:,.0f} ₴")

    # ── 5 · разрез по типу заказа ───────────────────────────────────────
    шапка(5, f"ТИПЫ ЗАКАЗОВ · {подпись}")
    по_типу = {}
    for x in продажи(s, ["OrderType", "Conception"],
                     ["UniqOrderId", "DishDiscountSumInt"], с, по):
        if not report.КОНЦЕПЦИИ.get(x.get("Conception")):
            continue
        т = (x.get("OrderType") or "?").strip()
        a = по_типу.setdefault(т, [0, 0.0])
        a[0] += x.get("UniqOrderId") or 0
        a[1] += x.get("DishDiscountSumInt") or 0
    вз = sum(a[0] for a in по_типу.values())
    вс = sum(a[1] for a in по_типу.values())
    for т, a in sorted(по_типу.items(), key=lambda kv: -kv[1][0]):
        print(f"  {т[:38]:<38} {a[0]:>6} зак · {a[1]:>12,.0f} ₴ · "
              f"чек {a[1] / a[0] if a[0] else 0:>7,.0f} ₴")
    print(f"  {'ВСЕГО':<38} {вз:>6} зак · {вс:>12,.0f} ₴ · "
          f"чек {вс / вз if вз else 0:>7,.0f} ₴")

    # ── 6 · времена ─────────────────────────────────────────────────────
    шапка(6, f"ВРЕМЕНА · {подпись} · минуты")
    строки = []
    for x in продажи(s, ["Conception"],
                     ["Cooking.KitchenTime.Avg", "OrderTime.AveragePrechequeTime",
                      "Delivery.WayDurationAvg", "UniqOrderId"], с, по):
        к = x.get("Conception")
        if not report.КОНЦЕПЦИИ.get(к):
            continue
        кухня = (x.get("Cooking.KitchenTime.Avg") or 0) / 60   # отдаётся в секундах
        пречек = x.get("OrderTime.AveragePrechequeTime") or 0
        путь = x.get("Delivery.WayDurationAvg") or 0
        строки.append((к, кухня, max(пречек - кухня, 0), путь, пречек + путь,
                       x.get("UniqOrderId") or 0))
    print(f"  {'концепция':<16} {'кухня':>7} {'сборка':>7} {'курьер':>7} "
          f"{'итого':>7} {'заказов':>8}")
    for к, кух, сб, кур, ито, з in строки:
        print(f"  {к:<16} {кух:>7.1f} {сб:>7.1f} {кур:>7.1f} {ито:>7.1f} {з:>8}")
    вес = sum(с_[5] for с_ in строки) or 1
    print(f"  {'СЕТЬ (взвеш.)':<16} " + " ".join(
        f"{sum(с_[i] * с_[5] for с_ in строки) / вес:>7.1f}" for i in (1, 2, 3, 4))
        + f" {вес:>8}")

    # ── 7 · скидки ──────────────────────────────────────────────────────
    шапка(7, f"СКИДКИ ПО ТИПАМ · {подпись}")
    по_скидке = {}
    for x in продажи(s, ["OrderDiscount.Type", "Conception"],
                     ["DiscountSum", "UniqOrderId"], с, по):
        наш = report.КОНЦЕПЦИИ.get(x.get("Conception"))
        if not наш:
            continue
        т = (x.get("OrderDiscount.Type") or "— без скидки").strip()
        a = по_скидке.setdefault(т, {"сумма": 0.0, "зак": 0, "по_ф": {}})
        a["сумма"] += x.get("DiscountSum") or 0
        a["зак"] += x.get("UniqOrderId") or 0
        a["по_ф"][наш] = a["по_ф"].get(наш, 0) + (x.get("DiscountSum") or 0)
    for т, a in sorted(по_скидке.items(), key=lambda kv: -abs(kv[1]["сумма"])):
        ф = " · ".join(f"{к} {v:,.0f}" for к, v in sorted(a["по_ф"].items()))
        print(f"  {т[:34]:<34} {a['сумма']:>11,.0f} ₴ · {a['зак']:>6} зак   {ф}")
    print(f"  {'ИТОГО скидок':<34} "
          f"{sum(a['сумма'] for a in по_скидке.values()):>11,.0f} ₴")


def abc(s, этот, папка, подпись):
    # ── 8 · ABC по позициям ─────────────────────────────────────────────
    шапка(8, f"ABC ПО ПОЗИЦИЯМ · {подпись}")
    с, по = полуинтервал(*этот)
    поз = {}
    for x in продажи(s, ["DishCode", "DishName", "DishCategory", "Conception"],
                     ["DishAmountInt", "DishDiscountSumInt",
                      "ProductCostBase.ProductCost"], с, по):
        if not report.КОНЦЕПЦИИ.get(x.get("Conception")):
            continue
        к = (x.get("DishCode") or "", x.get("DishName") or "?")
        a = поз.setdefault(к, {"кат": x.get("DishCategory") or "",
                               "шт": 0.0, "выр": 0.0, "сс": 0.0})
        a["шт"] += x.get("DishAmountInt") or 0
        a["выр"] += x.get("DishDiscountSumInt") or 0
        a["сс"] += x.get("ProductCostBase.ProductCost") or 0
    список = [{"код": к[0], "имя": к[1], **v, "приб": v["выр"] - v["сс"]}
              for к, v in поз.items()]
    список.sort(key=lambda p: -p["выр"])
    итог = sum(p["выр"] for p in список) or 1
    сс = sum(p["сс"] for p in список)
    накоп = 0.0
    классы = {"A": [0, 0.0, 0.0], "B": [0, 0.0, 0.0], "C": [0, 0.0, 0.0]}
    for p in список:
        накоп += p["выр"]
        доля = накоп / итог
        p["класс"] = "A" if доля <= 0.8 else ("B" if доля <= 0.95 else "C")
        k = классы[p["класс"]]
        k[0] += 1; k[1] += p["выр"]; k[2] += p["приб"]
    print(f"  позиций {len(список)} · выручка {итог:,.0f} ₴ · "
          f"себестоимость {сс:,.0f} ₴ · прибыль {итог - сс:,.0f} ₴ · "
          f"фудкост {сс / итог * 100:.1f}%")
    for кл in "ABC":
        n, выр, приб = классы[кл]
        доля = n / len(список) * 100 if список else 0
        print(f"    класс {кл}: {n:>4} позиций ({доля:>4.1f}%) · "
              f"{выр:>12,.0f} ₴ ({выр / итог * 100:>4.1f}%) · "
              f"прибыль {приб:>12,.0f} ₴")
    print(f"\n  {'код':<8}{'позиция':<36}{'шт':>7}{'выручка':>12}"
          f"{'прибыль':>12}{'ФК%':>6}{'клс':>4}")
    for p in список[:20]:
        фк = p["сс"] / p["выр"] * 100 if p["выр"] else 0
        print(f"  {str(p['код']):<8}{p['имя'][:34]:<36}{p['шт']:>7.0f}"
              f"{p['выр']:>12,.0f}{p['приб']:>12,.0f}{фк:>5.0f}%{p['класс']:>4}")
    плохие = [p for p in список if p["класс"] == "C" and p["приб"] <= 0]
    print("\n  — класс C с нулевой или отрицательной прибылью —")
    for p in плохие[:12]:
        print(f"  {str(p['код']):<8}{p['имя'][:34]:<36}{p['шт']:>7.0f}"
              f"{p['выр']:>12,.0f}{p['приб']:>12,.0f}")
    print(f"  всего таких: {len(плохие)}")
    путь = os.path.join(папка, f"abc-{подпись}.csv")
    with open(путь, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["код", "позиция", "категория", "продано, шт", "выручка, ₴",
                    "себестоимость, ₴", "прибыль, ₴", "фудкост, %", "класс ABC"])
        for p in список:
            w.writerow([p["код"], p["имя"], p["кат"], f"{p['шт']:.0f}",
                        f"{p['выр']:.2f}", f"{p['сс']:.2f}", f"{p['приб']:.2f}",
                        f"{p['сс'] / p['выр'] * 100:.1f}" if p["выр"] else "",
                        p["класс"]])
    print(f"  полный ABC: {os.path.basename(путь)} ({len(список)} позиций)")


def main(аргументы):
    if not аргументы or аргументы[0].startswith("-"):
        print(__doc__)
        return 2
    подпись = аргументы[0]
    этот = месяц(подпись)
    подпись_пр = f"{этот[0].year - 1}-{этот[0].month:02d}"
    прошлый = месяц(подпись_пр)
    папка = os.path.expanduser(f"~/Downloads/Закрытие-{подпись}")
    # Непонятный аргумент — отказ, а не папка. Пока второй аргумент молча
    # считался папкой, опечатка в флаге делала в корне репозитория каталог
    # с именем флага и клала отчёт туда (10.11.2026, папка «--блоки»).
    хвост = list(аргументы[1:])
    while хвост:
        флаг = хвост.pop(0)
        if флаг == "--папка" and хвост:
            папка = os.path.expanduser(хвост.pop(0))
        else:
            print(f"не понял аргумент: {флаг}\n")
            print(__doc__)
            return 2
    os.makedirs(папка, exist_ok=True)
    print(f"месяц {подпись} · {этот[0]}…{этот[1]} · год к году с {подпись_пр}")
    print(f"csv → {папка}")
    with cappi.Syrve() as s:
        главное(s, этот, прошлый, папка, подпись, подпись_пр)
        заказы(s, этот, подпись)
        abc(s, этот, папка, подпись)
    print(f"\nготово. csv в {папка}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
