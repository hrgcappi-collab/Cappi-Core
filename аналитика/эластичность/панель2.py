import sys
"""Дневной ряд цен по блюду → недельная панель с разделением цены на две части:
   базовый уровень (медианный фильтр ±28 дней) и временное отклонение (акция).
"""
import json, datetime as dt, collections, statistics, sys
import os
S = sys.argv[1] if len(sys.argv) > 1 else "/tmp/эластичность"  # куда тяга.py сложила выгрузку
сырое = json.load(open(f"{S}/панель.json"))

дни = collections.defaultdict(dict)
имена, кат = {}, {}
for r in сырое:
    к = r["DishCode"]
    if not к:
        continue
    д = dt.date.fromisoformat(r["OpenDate.Typed"][:10])
    имена[к] = r["DishName"]; кат[к] = r.get("DishCategory") or "—"
    з = дни[к].setdefault(д, {"шт": 0.0, "меню": 0.0, "факт": 0.0})
    з["шт"] += r["DishAmountInt"] or 0
    з["меню"] += r["DishSumInt"] or 0
    з["факт"] += r["DishDiscountSumInt"] or 0

ВСЕ_ДНИ = sorted({д for x in дни.values() for д in x})
НАЧ, КОН = ВСЕ_ДНИ[0], ВСЕ_ДНИ[-1]
КАЛЕНДАРЬ = [НАЧ + dt.timedelta(days=i) for i in range((КОН - НАЧ).days + 1)]
ОКНО = 28


def пн(д):
    return д - dt.timedelta(days=д.weekday())


панель = {}
for к, по_дням in дни.items():
    цена = {д: з["меню"] / з["шт"] for д, з in по_дням.items() if з["шт"] > 0}
    if len(цена) < 40:
        continue
    # «Чистая» цена — ступенчатая: несколько круглых уровней и ничего между.
    уровни = {round(c, 2) for c in цена.values()}
    круглых = sum(1 for c in цена.values() if abs(c - round(c)) < 0.01) / len(цена)
    чистое = len(уровни) <= 12 and круглых > 0.95
    # Цена на каждый день: где не продавали — тянем последнюю известную.
    ряд, последняя = {}, None
    первый = min(цена)
    for д in КАЛЕНДАРЬ:
        if д in цена:
            последняя = цена[д]
        if д >= первый and последняя is not None:
            ряд[д] = последняя
    # База — медиана по окну ±28 дней. Медиана переживает короткую акцию
    # (несколько дней из пятидесяти семи) и следует за постоянной сменой.
    дни_ряда = sorted(ряд)
    индекс = {д: i for i, д in enumerate(дни_ряда)}
    значения = [ряд[д] for д in дни_ряда]
    база = {}
    for i, д in enumerate(дни_ряда):
        a, b = max(0, i - ОКНО), min(len(значения), i + ОКНО + 1)
        база[д] = statistics.median(значения[a:b])

    по_нед = collections.defaultdict(lambda: {"шт": 0.0, "меню": 0.0, "факт": 0.0,
                                              "цены": [], "базы": [], "дней": 0})
    for д in дни_ряда:
        n = по_нед[пн(д)]
        n["цены"].append(ряд[д]); n["базы"].append(база[д]); n["дней"] += 1
        з = по_дням.get(д)
        if з:
            n["шт"] += з["шт"]; n["меню"] += з["меню"]; n["факт"] += з["факт"]
    недели = {}
    for w, n in по_нед.items():
        if n["дней"] < 5 or n["шт"] <= 0:
            continue
        счёт = collections.Counter(round(c, 2) for c in n["цены"])
        недели[w.isoformat()] = {
            "шт": n["шт"], "чистое": чистое,
            "мода": счёт.most_common(1)[0][0],
            "цена": sum(n["цены"]) / len(n["цены"]),      # по дням, не по штукам
            "база": sum(n["базы"]) / len(n["базы"]),
            "реал": n["факт"] / n["шт"],
            "выручка": n["факт"], "дней": n["дней"],
        }
    if недели:
        панель[к] = недели

json.dump({"панель": панель, "имена": имена, "категории": кат},
          open(f"{S}/панель2.json", "w"), ensure_ascii=False)

нед = sorted({w for d in панель.values() for w in d})
print("блюд:", len(панель), "недель:", len(нед), нед[0], "…", нед[-1])
точек = sum(len(d) for d in панель.values())
акц = sum(1 for d in панель.values() for v in d.values() if v["цена"] < v["база"] * 0.97)
рост = sum(1 for d in панель.values() for v in d.values() if v["цена"] > v["база"] * 1.03)
print(f"точек блюдо×неделя: {точек}   недель ниже базы на 3%+: {акц}   выше: {рост}")
