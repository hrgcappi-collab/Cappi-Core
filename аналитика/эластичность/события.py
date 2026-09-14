import sys
"""Находим все смены цены в меню и смотрим, какого они размера."""
import json, datetime as dt, collections, sys, math
import os
S = sys.argv[1] if len(sys.argv) > 1 else "/tmp/эластичность"  # куда тяга.py сложила выгрузку
d = json.load(open(f"{S}/недели.json"))
панель = {к: {dt.date.fromisoformat(w): v for w, v in x.items()}
          for к, x in d["панель"].items()}
имена, кат = d["имена"], d["категории"]

события = []
for к, ряд in панель.items():
    недели = sorted(ряд)
    for i in range(1, len(недели)):
        w0, w1 = недели[i - 1], недели[i]
        if (w1 - w0).days != 7:          # пропуск в продажах — не событие
            continue
        p0, p1 = ряд[w0]["цена"], ряд[w1]["цена"]
        if p0 <= 0 or p1 <= 0:
            continue
        шаг = p1 / p0 - 1
        if abs(шаг) < 0.005:
            continue
        события.append({"код": к, "имя": имена.get(к, к), "кат": кат.get(к, ""),
                        "неделя": w1.isoformat(), "p0": p0, "p1": p1,
                        "шаг": шаг})

json.dump(события, open(f"{S}/события.json", "w"), ensure_ascii=False)
print("событий:", len(события))
по_неделям = collections.Counter(с["неделя"] for с in события)
print("\nпо неделям (где больше трёх):")
for w, n in sorted(по_неделям.items()):
    if n >= 3:
        рост = [с["шаг"] for с in события if с["неделя"] == w]
        рост.sort()
        print(f"  {w}  позиций {n:>3}  медиана {рост[len(рост)//2]*100:+5.1f}%  "
              f"размах {min(рост)*100:+.1f}…{max(рост)*100:+.1f}%")
шаги = sorted(abs(с["шаг"]) for с in события)
print("\nразмер шага, все события:")
for q in (0.25, 0.5, 0.75, 0.9, 0.95, 1.0):
    print(f"  {int(q*100):>3}% ≤ {шаги[min(len(шаги)-1,int(q*len(шаги)))]*100:.1f}%")
print("рост / снижение:", sum(1 for с in события if с["шаг"] > 0),
      "/", sum(1 for с in события if с["шаг"] < 0))
