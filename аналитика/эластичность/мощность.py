import sys
"""Честная мощность замера: делим меню пополам вслепую и смотрим, какая
разница возникает сама по себе. Это и есть шум, с которым придётся спорить.
"""
import json, math, random, statistics, collections, sys
import os
S = sys.argv[1] if len(sys.argv) > 1 else "/tmp/эластичность"  # куда тяга.py сложила выгрузку
d = json.load(open(f"{S}/панель2.json"))
панель = d["панель"]
соб = json.load(open(f"{S}/события2.json"))
ВСЕ = sorted({w for x in панель.values() for w in x})
ИНД = {w: i for i, w in enumerate(ВСЕ)}
ШТ = {к: {ИНД[w]: v["шт"] for w, v in x.items()} for к, x in панель.items()}
ГОДЕН = sorted(к for к, x in панель.items()
               if len(x) >= 24 and sum(v["шт"] for v in x.values()) / len(x) >= 2)
random.seed(7)


def сумма(коды, a, b):
    s = 0.0
    for к in коды:
        s += sum(v for i, v in ШТ[к].items() if a <= i <= b)
    return s


def разница(A, B, i):
    a1, a2 = сумма(A, i - 5, i - 2), сумма(A, i, i + 3)
    b1, b2 = сумма(B, i - 5, i - 2), сумма(B, i, i + 3)
    if min(a1, a2, b1, b2) <= 0:
        return None
    return math.log(a2 / a1) - math.log(b2 / b1)


print(f"позиций в пуле: {len(ГОДЕН)}")
print("\nслучайное разбиение, шум разницы объёмов за 4 недели:")
for n_т, n_к in ((120, 40), (100, 60), (60, 60), (40, 40)):
    if n_т + n_к > len(ГОДЕН):
        continue
    зн = []
    for _ in range(300):
        смесь = random.sample(ГОДЕН, n_т + n_к)
        A, B = смесь[:n_т], смесь[n_т:]
        i = random.randint(6, len(ВСЕ) - 5)
        x = разница(A, B, i)
        if x is not None:
            зн.append(x)
    σ = statistics.pstdev(зн)
    print(f"   {n_т:>3} против {n_к:>3}   σ = {σ*100:4.1f}%   "
          + "   ".join(f"шаг {ш*100:>2.0f}% → ε ±{1.96*σ/ш:.2f}"
                       for ш in (0.05, 0.08, 0.12)))
