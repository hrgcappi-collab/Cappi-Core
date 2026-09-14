import sys
"""Акции идут пачками — считаем пачками, иначе одна и та же просадка
группы приписывается каждой позиции по отдельности."""
import json, math, datetime as dt, collections, statistics, sys
import os
S = sys.argv[1] if len(sys.argv) > 1 else "/tmp/эластичность"  # куда тяга.py сложила выгрузку
сырое = json.load(open(f"{S}/панель.json"))
себес = {r["DishCode"]: r for r in json.load(open(f"{S}/себес.json")) if r.get("DishCode")}
дни = collections.defaultdict(dict); имена, кат = {}, {}
for r in сырое:
    к = r["DishCode"]
    if not к: continue
    д = dt.date.fromisoformat(r["OpenDate.Typed"][:10])
    имена[к] = r["DishName"]; кат[к] = r.get("DishCategory") or "—"
    з = дни[к].setdefault(д, {"шт": 0.0, "меню": 0.0, "факт": 0.0})
    з["шт"] += r["DishAmountInt"] or 0
    з["меню"] += r["DishSumInt"] or 0
    з["факт"] += r["DishDiscountSumInt"] or 0
# Полный дневной ряд: день без продаж — это ноль, а не пропуск.
ВСЕ_ДНИ = sorted({д for x in дни.values() for д in x})
ШТУКИ = {}
for к, x in дни.items():
    пер = [д for д in ВСЕ_ДНИ if min(x) <= д <= max(x)]
    ШТУКИ[к] = {д: x.get(д, {}).get("шт", 0.0) for д in пер}

эп = json.load(open(f"{S}/акции.json"))
для = []
for т in эп:
    a = dt.date.fromisoformat(т["с"])
    для.append({**т, "a": a, "b": a + dt.timedelta(days=т["дней"] - 1)})

# Пачки: эпизоды, чьи окна пересекаются.
для.sort(key=lambda x: x["a"])
кампании = []
for т in для:
    if кампании and т["a"] <= кампании[-1]["b"] + dt.timedelta(days=1):
        к = кампании[-1]
        к["b"] = max(к["b"], т["b"]); к["позиции"].append(т)
    else:
        кампании.append({"a": т["a"], "b": т["b"], "позиции": [т]})
print(f"эпизодов {len(для)} → кампаний {len(кампании)}")

ПРОМО_ДНИ = collections.defaultdict(set)
for т in для:
    д = т["a"]
    while д <= т["b"]:
        ПРОМО_ДНИ[т["код"]].add(д); д += dt.timedelta(days=1)


def маржа_шт(к):
    r = себес.get(к) or {}
    шт = r.get("DishAmountInt") or 0
    if not шт: return None, None
    сс = (r.get("ProductCostBase.ProductCost") or 0) / шт
    цена = (r.get("DishDiscountSumInt") or 0) / шт
    return цена - сс, сс


def ожидание(к, дни_к):
    """Штуки в день, каких ждали бы без акции: те же дни недели вокруг."""
    дн = {д.weekday() for д in дни_к}
    зн = [ШТУКИ[к][д] for с in list(range(-28, -4)) + list(range(5, 29))
          for д in [дни_к[0] + dt.timedelta(days=с)]
          if д in ШТУКИ[к] and д not in ПРОМО_ДНИ[к] and д.weekday() in дн]
    return sum(зн) / len(зн) if len(зн) >= 6 else None


итоги = []
for к in кампании:
    дни_к = [к["a"] + dt.timedelta(days=i) for i in range((к["b"] - к["a"]).days + 1)]
    if len(дни_к) > 21: continue
    в_промо = {т["код"] for т in к["позиции"]}
    промо_д = промо_ожид = 0.0
    for т in к["позиции"]:
        м, сс = маржа_шт(т["код"])
        if м is None: continue
        свои = [дни[т["код"]][д] for д in дни_к if д in дни[т["код"]]]
        шт = (sum(s["шт"] for s in свои) / len([д for д in дни_к if д in ШТУКИ[т["код"]]])
              if свои else 0.0)
        цена = (sum(s["факт"] for s in свои) / max(sum(s["шт"] for s in свои), 1e-9)
                if свои else 0.0)
        ож = ожидание(т["код"], дни_к)
        if ож is None: continue
        промо_д += (цена - сс) * шт
        промо_ожид += (м) * ож
    # Всё остальное меню
    ост_д = ост_ожид = 0.0
    for блюдо in дни:
        if блюдо in в_промо or блюдо not in себес:
            continue
        м, сс = маржа_шт(блюдо)
        if м is None: continue
        свои = [ШТУКИ[блюдо][д] for д in дни_к
                if д in ШТУКИ[блюдо] and д not in ПРОМО_ДНИ[блюдо]]
        if len(свои) < len(дни_к) * 0.8: continue
        ож = ожидание(блюдо, дни_к)
        if ож is None: continue
        ост_д += м * (sum(свои) / len(свои))
        ост_ожид += м * ож
    if промо_ожид <= 0 or ост_ожид <= 0: continue
    итоги.append({"с": к["a"].isoformat(), "дней": len(дни_к),
                  "позиций": len(в_промо),
                  "промо": промо_д - промо_ожид,
                  "остальное": ост_д - ост_ожид,
                  "итого": (промо_д - промо_ожид) + (ост_д - ост_ожид),
                  "фон_ожид": ост_ожид})

n = len(итоги)
print(f"кампаний с полным расчётом: {n}")
for поле in ("промо", "остальное", "итого"):
    зн = [x[поле] for x in итоги]
    print(f"  {поле:<11} медиана {statistics.median(зн):>+8.0f} ₴/день   "
          f"среднее {sum(зн)/n:>+8.0f}   в плюс {sum(1 for v in зн if v>0)}/{n}")
print("\nпо кампаниям:")
for x in sorted(итоги, key=lambda x: x["с"]):
    print(f"   {x['с']} {x['дней']:>2}д  позиций {x['позиций']:>2}   "
          f"промо {x['промо']:>+7.0f}   остальное {x['остальное']:>+8.0f}   "
          f"итого {x['итого']:>+8.0f} ₴/день")
