import sys
import os
"""Акция добавляет спрос или перетягивает его с соседей по меню?

Для каждого эпизода смотрим, что в это время происходило с остальными
блюдами той же группы — и со всем меню разом.
"""
import json, math, datetime as dt, collections, statistics, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from мнк import мнк
import os
S = sys.argv[1] if len(sys.argv) > 1 else "/tmp/эластичность"  # куда тяга.py сложила выгрузку
сырое = json.load(open(f"{S}/панель.json"))
дни = collections.defaultdict(dict)
имена, кат = {}, {}
for r in сырое:
    к = r["DishCode"]
    if not к: continue
    д = dt.date.fromisoformat(r["OpenDate.Typed"][:10])
    имена[к] = r["DishName"]; кат[к] = r.get("DishCategory") or "—"
    з = дни[к].setdefault(д, {"шт": 0.0, "меню": 0.0})
    з["шт"] += r["DishAmountInt"] or 0; з["меню"] += r["DishSumInt"] or 0

эп = json.load(open(f"{S}/акции.json"))
ВСЕ_ДНИ = sorted({д for x in дни.values() for д in x})
ШТУК = collections.defaultdict(float)       # (категория, день) → штук
ВСЕГО = collections.defaultdict(float)
for к, x in дни.items():
    for д, з in x.items():
        ШТУК[(кат[к], д)] += з["шт"]; ВСЕГО[д] += з["шт"]

# Блюда в акции — чтобы вычесть их из группы.
в_акции = collections.defaultdict(set)
for т in эп:
    a = dt.date.fromisoformat(т["с"])
    for i in range(т["дней"]):
        в_акции[т["код"]].add(a + dt.timedelta(days=i))


def окно(ряд, дни_эп, дни_недели):
    внутри = [ряд[д] for д in дни_эп if д in ряд]
    вне = [ряд[д] for сдвиг in list(range(-28, -3)) + list(range(4, 29))
           for д in [дни_эп[0] + dt.timedelta(days=сдвиг)]
           if д in ряд and д.weekday() in дни_недели]
    if not внутри or len(вне) < 6:
        return None
    a, b = sum(внутри) / len(внутри), sum(вне) / len(вне)
    if a <= 0 or b <= 0:
        return None
    return math.log((sum(внутри) / len(внутри)) / (sum(вне) / len(вне)))


пары = []
for т in эп:
    a = dt.date.fromisoformat(т["с"])
    дни_эп = [a + dt.timedelta(days=i) for i in range(т["дней"])]
    дн = {д.weekday() for д in дни_эп}
    группа = кат[т["код"]]
    ряд_группы = {}
    for д in ВСЕ_ДНИ:
        сумма = ШТУК[(группа, д)]
        свой = дни[т["код"]].get(д, {}).get("шт", 0.0)
        # вычитаем само акционное блюдо и всех, кто в акции в этот день
        чужие_акции = sum(дни[к].get(д, {}).get("шт", 0.0) for к in в_акции
                          if к != т["код"] and кат.get(к) == группа and д in в_акции[к])
        ряд_группы[д] = сумма - свой - чужие_акции
    г = окно(ряд_группы, дни_эп, дн)
    в = окно(ВСЕГО, дни_эп, дн)
    if г is None or в is None:
        continue
    пары.append({"шаг": т["шаг"], "своё": т["отклик"], "группа": г - в,
                 "всё": в, "код": т["код"], "имя": т["имя"], "дней": т["дней"]})

print("эпизодов с замером по группе:", len(пары))
for имя, поле in (("само блюдо", "своё"), ("остальная группа", "группа")):
    r = мнк([p[поле] for p in пары], [[p["шаг"]] for p in пары],
            [p["код"] for p in пары])
    b, se = r["beta"][0], r["se"][0]
    print(f"  {имя:<20} ε {b:+6.2f} ± {se:.2f}   95% [{b-1.96*se:+.2f}; {b+1.96*se:+.2f}]")

глуб = [p for p in пары if p["шаг"] < -0.12]
мш = statistics.median(p["шаг"] for p in глуб)
мс = statistics.median(p["своё"] for p in глуб)
мг = statistics.median(p["группа"] for p in глуб)
print(f"\nмедианная акция {мш*100:+.0f}%:")
print(f"   само блюдо        {(math.exp(мс)-1)*100:+6.0f}% штук")
print(f"   остальная группа  {(math.exp(мг)-1)*100:+6.0f}% штук (сверх общего фона дня)")
