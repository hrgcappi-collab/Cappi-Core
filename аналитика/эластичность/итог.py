import sys
import os
"""Сводка: эластичность по группам, вверх/вниз, и что это значит в деньгах."""
import json, math, sys, collections, statistics
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from мнк import внутри, мнк
import os
S = sys.argv[1] if len(sys.argv) > 1 else "/tmp/эластичность"  # куда тяга.py сложила выгрузку
d = json.load(open(f"{S}/панель2.json"))
панель, имена, кат = d["панель"], d["имена"], d["категории"]
ВСЕ = sorted({w for x in панель.values() for w in x})
ИНД = {w: i for i, w in enumerate(ВСЕ)}


def ступени(недели):
    w = sorted(недели); блоки = []
    for x in w:
        ц = недели[x]["мода"]
        if ц <= 0:
            continue
        if блоки and abs(ц / блоки[-1]["цена"] - 1) < 0.005:
            блоки[-1]["недели"].append(x)
        else:
            блоки.append({"цена": ц, "недели": [x]})
    база = {}
    for i, б in enumerate(блоки):
        if len(б["недели"]) >= 3:
            ур = б["цена"]
        else:
            сос = [блоки[j] for j in (i - 1, i + 1)
                   if 0 <= j < len(блоки) and len(блоки[j]["недели"]) >= 3]
            if not сос:
                continue
            ур = max(с["цена"] for с in сос)
        for x in б["недели"]:
            база[x] = ур
    return база


точки = []
for к, недели in панель.items():
    if len(недели) < 20 or not list(недели.values())[0].get("чистое"):
        continue
    if sum(v["шт"] for v in недели.values()) / len(недели) < 2:
        continue
    б = ступени(недели)
    for w, v in недели.items():
        if w in б and v["шт"] > 0 and v["мода"] > 0:
            точки.append({"блюдо": к, "неделя": w, "y": math.log(v["шт"]),
                          "ступень": math.log(б[w]),
                          "акция": math.log(v["мода"] / б[w]),
                          "выручка": v["выручка"], "кат": кат.get(к, "—")})


def прогон(выбор, поля):
    y = [т["y"] for т in выбор]
    X = [[т[п] for п in поля] for т in выбор]
    бл = [т["блюдо"] for т in выбор]; нед = [т["неделя"] for т in выбор]
    yd, Xd = внутри(y, X, [бл, нед])
    return мнк(yd, Xd, бл, None, len(set(бл)) + len(set(нед)) - 1)


print("Эластичность по группам меню (постоянная цена / акция)")
счёт = collections.Counter(т["кат"] for т in точки)
for c, n in счёт.most_common():
    под = [т for т in точки if т["кат"] == c]
    блюд = len({т["блюдо"] for т in под})
    if n < 300 or блюд < 8:
        continue
    try:
        r = прогон(под, ["ступень", "акция"])
    except Exception as e:
        print(f"  {c[:26]:<26} не сошлось"); continue
    b1, s1, b2, s2 = r["beta"][0], r["se"][0], r["beta"][1], r["se"][1]
    доля = sum(т["выручка"] for т in под)
    print(f"  {c[:26]:<26} блюд {блюд:>3}  выручка {доля/1e6:>4.2f} млн  "
          f"цена {b1:+5.2f}±{s1:.2f}   акция {b2:+5.2f}±{s2:.2f}")

# Вверх и вниз — по событиям, а не по панели.
соб = json.load(open(f"{S}/события2.json"))
панель_шт = {к: {ИНД[w]: v["шт"] for w, v in x.items()} for к, x in панель.items()}
ГОДЕН = {к for к, x in панель.items()
         if len(x) >= 20 and sum(v["шт"] for v in x.values()) / len(x) >= 2}
менялась = collections.defaultdict(set)
for с in соб["постоянные"] + соб["временные"]:
    for j in (с["i"] - 1, с["i"], с["i"] + 1):
        менялась[с["код"]].add(j)


def средн(к, a, b):
    шт = [v for i, v in панель_шт[к].items() if a <= i <= b]
    return sum(шт) / len(шт) if len(шт) >= max(2, (b - a + 1) // 2) else None


def дельта(к, i):
    a, b = средн(к, i - 5, i - 2), средн(к, i, i + 3)
    return math.log(b / a) if a and b else None


def фон(i):
    зн = [дельта(к, i) for к in ГОДЕН
          if not any(j in менялась[к] for j in range(i - 5, i + 4))]
    зн = [x for x in зн if x is not None]
    return statistics.median(зн) if len(зн) >= 15 else None


def событийно(события, подпись):
    т = []
    for с in события:
        x, f = дельта(с["код"], с["i"]), фон(с["i"])
        if x is None or f is None or abs(с["шаг"]) < 0.004:
            continue
        т.append((с["шаг"], x - f, с["код"]))
    if len(т) < 12:
        print(f"  {подпись:<30} мало событий ({len(т)})"); return
    r = мнк([p[1] for p in т], [[p[0]] for p in т], [p[2] for p in т])
    b, se = r["beta"][0], r["se"][0]
    print(f"  {подпись:<30} событий {len(т):>4}  ε {b:+5.2f} ± {se:.2f}")


print("\nВверх и вниз — отдельно")
событийно([с for с in соб["постоянные"] if с["шаг"] > 0], "повышения")
событийно([с for с in соб["постоянные"] if с["шаг"] < 0], "снижения")

# Деньги
себес = json.load(open(f"{S}/себес.json"))
выр = sum(r.get("DishDiscountSumInt") or 0 for r in себес)
сс = sum(r.get("ProductCostBase.ProductCost") or 0 for r in себес)
ф = сс / выр
print(f"\nФудкост по сети за июнь–сентябрь: {ф*100:.1f}%  "
      f"(выручка {выр/1e6:.2f} млн, себестоимость {сс/1e6:.2f} млн)")
print(f"Эластичность безубыточности повышения: {-1/(1-ф):+.2f} — "
      f"при менее отзывчивом спросе повышение добавляет валовую прибыль")

print("\nЧто будет с деньгами при повышении (месяц, по сегодняшней выручке)")
месяц = выр / (сс and 1) / 3.5      # июнь–сентябрь ≈ 3.5 месяца
print(f"  база: выручка {месяц/1e6:.2f} млн/мес, валовая прибыль "
      f"{месяц*(1-ф)/1e6:.2f} млн/мес")
print(f"  {'шаг':<6}" + "".join(f"{('ε=%.1f' % e):>14}" for e in (0, -0.6, -1.0, -1.4)))
for r_ in (0.03, 0.05, 0.08, 0.10):
    строка = f"  {r_*100:>3.0f}% "
    for e in (0, -0.6, -1.0, -1.4):
        k = (1 + r_) ** e
        выручка = месяц * (1 + r_) * k
        прибыль = месяц * ((1 + r_) - ф) * k
        строка += f"{(прибыль - месяц*(1-ф))/1000:>+9.0f} тыс"
    print(строка)
