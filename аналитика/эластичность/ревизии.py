import sys
import os
"""Две массовые ревизии прейскуранта — что с ними стало на самом деле."""
import json, math, collections, statistics, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from мнк import мнк
import os
S = sys.argv[1] if len(sys.argv) > 1 else "/tmp/эластичность"  # куда тяга.py сложила выгрузку
d = json.load(open(f"{S}/панель2.json"))
панель, имена = d["панель"], d["имена"]
соб = json.load(open(f"{S}/события2.json"))
ВСЕ = sorted({w for x in панель.values() for w in x})
ИНД = {w: i for i, w in enumerate(ВСЕ)}
ШТ = {к: {ИНД[w]: v["шт"] for w, v in x.items()} for к, x in панель.items()}
ГОДЕН = {к for к, x in панель.items()
         if len(x) >= 20 and sum(v["шт"] for v in x.values()) / len(x) >= 2}
менялась = collections.defaultdict(set)
for с in соб["постоянные"] + соб["временные"]:
    менялась[с["код"]].add(с["i"])


def средн(к, a, b):
    зн = [v for i, v in ШТ[к].items() if a <= i <= b]
    return sum(зн) / len(зн) if len(зн) >= max(2, (b - a + 1) // 2) else None


def дельта(к, i, окно=4):
    a, b = средн(к, i - окно - 1, i - 2), средн(к, i, i + окно - 1)
    return math.log(b / a) if a and b else None


for дата in ("2026-07-27", "2026-08-17", "2026-08-24"):
    i = ИНД.get(дата)
    if i is None:
        continue
    поднятые = [с for с in соб["постоянные"] if с["неделя"] == дата and с["шаг"] > 0]
    коды = {с["код"] for с in поднятые}
    if len(поднятые) < 10:
        continue
    ф = [дельта(к, i) for к in ГОДЕН
         if к not in коды and not any(j in менялась[к] for j in range(i - 5, i + 4))]
    ф = [x for x in ф if x is not None]
    свои = [(с, дельта(с["код"], i)) for с in поднятые]
    свои = [(с, x) for с, x in свои if x is not None]
    if not свои or len(ф) < 15:
        continue
    шаг = statistics.median(с["шаг"] for с, _ in свои)
    подн = statistics.median(x for _, x in свои)
    фон = statistics.median(ф)
    r = мнк([x - фон for _, x in свои], [[с["шаг"]] for с, _ in свои],
            [с["код"] for с, _ in свои])
    b, se = r["beta"][0], r["se"][0]
    print(f"{дата}: поднято {len(поднятые)} позиций, медиана {шаг*100:+.1f}% "
          f"(максимум {max(с['шаг'] for с in поднятые)*100:+.1f}%)")
    print(f"   штуки у поднятых {(math.exp(подн)-1)*100:+5.1f}%   "
          f"фон (нетронутые, {len(ф)} позиций) {(math.exp(фон)-1)*100:+5.1f}%   "
          f"разница {(math.exp(подн-фон)-1)*100:+5.1f}%")
    print(f"   эластичность по этой ревизии: {b:+.2f} ± {se:.2f}\n")
