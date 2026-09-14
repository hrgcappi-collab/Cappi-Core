import sys
import os
"""Кто отзывчивее: дорогие или дешёвые, ходовые или редкие."""
import json, math, sys, collections, statistics
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from мнк import внутри, мнк
import os
S = sys.argv[1] if len(sys.argv) > 1 else "/tmp/эластичность"  # куда тяга.py сложила выгрузку
d = json.load(open(f"{S}/панель2.json")); панель = d["панель"]; имена = d["имена"]


def ступени(недели):
    w = sorted(недели); блоки = []
    for x in w:
        ц = недели[x]["мода"]
        if ц <= 0: continue
        if блоки and abs(ц / блоки[-1]["цена"] - 1) < 0.005:
            блоки[-1]["недели"].append(x)
        else:
            блоки.append({"цена": ц, "недели": [x]})
    база = {}
    for i, б in enumerate(блоки):
        if len(б["недели"]) >= 3:
            ур = б["цена"]
        else:
            сос = [блоки[j] for j in (i-1, i+1)
                   if 0 <= j < len(блоки) and len(блоки[j]["недели"]) >= 3]
            if not сос: continue
            ур = max(с["цена"] for с in сос)
        for x in б["недели"]:
            база[x] = ур
    return база


точки = []
for к, недели in панель.items():
    if len(недели) < 20 or not list(недели.values())[0].get("чистое"): continue
    ср = sum(v["шт"] for v in недели.values()) / len(недели)
    if ср < 2: continue
    б = ступени(недели)
    цена_ср = statistics.median(v["мода"] for v in недели.values())
    for w, v in недели.items():
        if w in б and v["шт"] > 0 and v["мода"] > 0:
            точки.append({"блюдо": к, "неделя": w, "y": math.log(v["шт"]),
                          "ступень": math.log(б[w]), "акция": math.log(v["мода"]/б[w]),
                          "объём": ср, "цена": цена_ср})


def прогон(выбор, подпись):
    y = [т["y"] for т in выбор]; X = [[т["ступень"], т["акция"]] for т in выбор]
    бл = [т["блюдо"] for т in выбор]; нед = [т["неделя"] for т in выбор]
    yd, Xd = внутри(y, X, [бл, нед])
    r = мнк(yd, Xd, бл, None, len(set(бл)) + len(set(нед)) - 1)
    print(f"  {подпись:<34} блюд {len(set(бл)):>3}  "
          f"цена {r['beta'][0]:+5.2f}±{r['se'][0]:.2f}   "
          f"акция {r['beta'][1]:+5.2f}±{r['se'][1]:.2f}")


цены = sorted({т["блюдо"]: т["цена"] for т in точки}.items(), key=lambda x: x[1])
объёмы = sorted({т["блюдо"]: т["объём"] for т in точки}.items(), key=lambda x: -x[1])
медцена = цены[len(цены)//2][1]
топ = {к for к, _ in объёмы[:40]}
print(f"медианная цена позиции {медцена:.0f} ₴, ходовых (топ-40) {len(топ)}")
прогон([т for т in точки if т["цена"] >= медцена], "дороже медианы")
прогон([т for т in точки if т["цена"] < медцена], "дешевле медианы")
прогон([т for т in точки if т["блюдо"] in топ], "ходовые (топ-40 по штукам)")
прогон([т for т in точки if т["блюдо"] not in топ], "остальные")
