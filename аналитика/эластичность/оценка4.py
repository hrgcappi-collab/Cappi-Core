import sys
import os
"""Панель, где «база» — это ступень цены, а не сглаженная кривая.

Ступень: уровень, на котором цена простояла три недели и больше. Всё,
что короче, — временное отклонение (акция), и база для такой недели
берётся от соседней ступени.
"""
import json, math, sys, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from мнк import внутри, мнк
import os
S = sys.argv[1] if len(sys.argv) > 1 else "/tmp/эластичность"  # куда тяга.py сложила выгрузку
d = json.load(open(f"{S}/панель2.json"))
панель = d["панель"]
ВСЕ = sorted({w for x in панель.values() for w in x})
ИНД = {w: i for i, w in enumerate(ВСЕ)}


def ступени(недели):
    w = sorted(недели)
    блоки = []
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
            соседи = [блоки[j] for j in (i - 1, i + 1) if 0 <= j < len(блоки)
                      and len(блоки[j]["недели"]) >= 3]
            if not соседи:
                continue
            ур = max(с["цена"] for с in соседи)   # акция — это снижение
        for x in б["недели"]:
            база[x] = ур
    return база


точки = []
for к, недели in панель.items():
    if len(недели) < 20 or not list(недели.values())[0].get("чистое"):
        continue
    if sum(v["шт"] for v in недели.values()) / len(недели) < 2:
        continue
    база = ступени(недели)
    for w, v in недели.items():
        if w not in база or v["шт"] <= 0 or v["мода"] <= 0:
            continue
        точки.append({"блюдо": к, "неделя": w, "t": ИНД[w],
                      "y": math.log(v["шт"]),
                      "ступень": math.log(база[w]),
                      "акция": math.log(v["мода"] / база[w])})

акций = sum(1 for т in точки if т["акция"] < -0.02)
print(f"наблюдений {len(точки)}, блюд {len({т['блюдо'] for т in точки})}, "
      f"недель по акционной цене {акций}")


def прогон(поля, подпись, тренд=False, отбор=None):
    выбор = [dict(т) for т in точки if not отбор or отбор(т)]
    if тренд:
        по = collections.defaultdict(list)
        for т in выбор:
            по[т["блюдо"]].append(т)
        for гр in по.values():
            if len(гр) < 6:
                continue
            mt = sum(т["t"] for т in гр) / len(гр)
            зн = sum((т["t"] - mt) ** 2 for т in гр) or 1.0
            for п in ["y"] + поля:
                mv = sum(т[п] for т in гр) / len(гр)
                b = sum((т["t"] - mt) * (т[п] - mv) for т in гр) / зн
                for т in гр:
                    т[п] -= mv + b * (т["t"] - mt)
        выбор = [т for т in выбор if len(по[т["блюдо"]]) >= 6]
    y = [т["y"] for т in выбор]
    X = [[т[п] for п in поля] for т in выбор]
    бл = [т["блюдо"] for т in выбор]; нед = [т["неделя"] for т in выбор]
    yd, Xd = внутри(y, X, [бл, нед])
    r = мнк(yd, Xd, бл, None,
            len(set(бл)) * (2 if тренд else 1) + len(set(нед)) - 1)
    print(f"{подпись:<46} n={r['n']:<5} " + "   ".join(
        f"{п} {b:+.2f}±{se:.2f} [{b-1.96*se:+.2f};{b+1.96*se:+.2f}]"
        for п, b, se in zip(поля, r["beta"], r["se"])))
    return r


прогон(["ступень", "акция"], "ступень цены + акция")
прогон(["ступень", "акция"], "  + собственный тренд блюда", тренд=True)
прогон(["ступень"], "только ступень, недели без акций",
       отбор=lambda т: т["акция"] > -0.005)
прогон(["ступень"], "  то же + тренд блюда", тренд=True,
       отбор=lambda т: т["акция"] > -0.005)
