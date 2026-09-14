import sys
import os
"""Что было с позициями, которым однажды подняли цену на 10% и больше."""
import json, math, collections, statistics, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from мнк import мнк
import os
S = sys.argv[1] if len(sys.argv) > 1 else "/tmp/эластичность"  # куда тяга.py сложила выгрузку
d = json.load(open(f"{S}/панель2.json")); панель = d["панель"]
соб = json.load(open(f"{S}/события2.json"))
ВСЕ = sorted({w for x in панель.values() for w in x}); ИНД = {w: i for i, w in enumerate(ВСЕ)}
ШТ = {к: {ИНД[w]: v["шт"] for w, v in x.items()} for к, x in панель.items()}
ГОДЕН = {к for к, x in панель.items()
         if len(x) >= 20 and sum(v["шт"] for v in x.values()) / len(x) >= 2}
менялась = collections.defaultdict(set)
for с in соб["постоянные"] + соб["временные"]:
    менялась[с["код"]].add(с["i"])


def средн(к, a, b):
    зн = [v for i, v in ШТ[к].items() if a <= i <= b]
    return sum(зн) / len(зн) if len(зн) >= max(2, (b - a + 1) // 2) else None


def дельта(к, i):
    a, b = средн(к, i - 5, i - 2), средн(к, i, i + 3)
    return math.log(b / a) if a and b else None


def фон(i):
    зн = [дельта(к, i) for к in ГОДЕН
          if not any(j in менялась[к] for j in range(i - 5, i + 4))]
    зн = [x for x in зн if x is not None]
    return statistics.median(зн) if len(зн) >= 15 else None


ряд = []
for с in соб["постоянные"]:
    if с["шаг"] < math.log(1.08):
        continue
    x, f = дельта(с["код"], с["i"]), фон(с["i"])
    if x is None or f is None:
        continue
    ряд.append((с, x, f))
ряд.sort(key=lambda t: -t[0]["шаг"])
print(f"позиций с постоянным повышением на 8%+ и чистым замером: {len(ряд)}\n")
print(f"{'позиция':<28}{'неделя':<12}{'цена':>14}{'штук':>9}{'фон':>8}{'ε':>7}")
for с, x, f in ряд:
    шаг = math.exp(с["шаг"]) - 1
    print(f"{с['имя'][:27]:<28}{с['неделя']:<12}"
          f"{с['p0']:>6.0f}→{с['p1']:<6.0f}{(math.exp(x)-1)*100:>+8.0f}%"
          f"{(math.exp(f)-1)*100:>+7.0f}%{(x - f)/с['шаг']:>+7.1f}")
если = [(x - f) / с["шаг"] for с, x, f in ряд]
print(f"\nмедиана эластичности по этим позициям: {statistics.median(если):+.2f}")
r = мнк([x - f for _, x, f in ряд], [[с["шаг"]] for с, _, _ in ряд],
        [с["код"] for с, _, _ in ряд])
print(f"регрессия по ним: {r['beta'][0]:+.2f} ± {r['se'][0]:.2f}")
