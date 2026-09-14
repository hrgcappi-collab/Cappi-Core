import sys
import os
"""Как отклик растёт со временем: одна и та же смена цены на разных сроках."""
import json, math, sys, collections, statistics
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from мнк import мнк
import os
S = sys.argv[1] if len(sys.argv) > 1 else "/tmp/эластичность"  # куда тяга.py сложила выгрузку
d = json.load(open(f"{S}/панель2.json"))
панель = d["панель"]
соб = json.load(open(f"{S}/события2.json"))
ВСЕ = sorted({w for x in панель.values() for w in x})
ИНД = {w: i for i, w in enumerate(ВСЕ)}
РЯД = {к: {ИНД[w]: v["шт"] for w, v in x.items()} for к, x in панель.items()}
ГОДЕН = {к for к, x in панель.items()
         if len(x) >= 20 and sum(v["шт"] for v in x.values()) / len(x) >= 2}

менялась = collections.defaultdict(set)
for с in соб["постоянные"] + соб["временные"]:
    for j in range(с["i"] - 1, с["i"] + 2):
        менялась[с["код"]].add(j)


def средн(к, a, b):
    шт = [v for i, v in РЯД[к].items() if a <= i <= b]
    нужно = max(2, (b - a + 1) // 2)
    return sum(шт) / len(шт) if len(шт) >= нужно else None


def дельта(к, i, до_от, до_до, после_от, после_до):
    a = средн(к, i + до_от, i + до_до)
    b = средн(к, i + после_от, i + после_до)
    return math.log(b / a) if a and b else None


def фон(i, окна, чистые=True):
    зн = []
    for к in ГОДЕН:
        if any(j in менялась[к] for j in range(i + окна[0], i + окна[3] + 1)):
            continue
        x = дельта(к, i, *окна)
        if x is not None:
            зн.append(x)
    return statistics.median(зн) if len(зн) >= 15 else None


def оценка(события, окна, подпись):
    точки = []
    for с in события:
        x = дельта(с["код"], с["i"], *окна)
        f = фон(с["i"], окна)
        if x is None or f is None or abs(с["шаг"]) < 0.004:
            continue
        точки.append((с["шаг"], x - f, с["код"]))
    if len(точки) < 12:
        print(f"{подпись:<34} мало событий ({len(точки)})")
        return
    r = мнк([p[1] for p in точки], [[p[0]] for p in точки], [p[2] for p in точки])
    b, se = r["beta"][0], r["se"][0]
    print(f"{подпись:<34} событий {len(точки):>4}   ε {b:+6.2f} ± {se:.2f}"
          f"   95% [{b-1.96*se:+.2f}; {b+1.96*se:+.2f}]")


пост = соб["постоянные"]
print("Постоянные смены цены — отклик по горизонтам")
for имя, окна in [("первые 2 недели", (-5, -2, 0, 1)),
                  ("недели 1–4", (-5, -2, 0, 3)),
                  ("недели 3–8", (-7, -2, 2, 7)),
                  ("недели 5–12", (-7, -2, 4, 11)),
                  ("8 до против 8 после", (-9, -2, 0, 7))]:
    оценка(пост, окна, имя)

print("\nТо же, только шаги крупнее 6%")
крупные = [с for с in пост if abs(с["шаг"]) >= 0.06]
for имя, окна in [("недели 1–4", (-5, -2, 0, 3)),
                  ("недели 5–12", (-7, -2, 4, 11))]:
    оценка(крупные, окна, имя)
