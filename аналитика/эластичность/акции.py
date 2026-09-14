import sys
import os
"""Акции по дням: сколько штук добавляет снижение цены на несколько дней.

Считаем эпизодами. Эпизод — подряд идущие дни, когда цена в меню ниже
своего обычного уровня. Ожидание без акции берём по тем же дням недели
до и после эпизода, а общий фон дня — по блюдам, которых цена не
касалась: иначе в эластичность попадёт просто хороший вечер.
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
    if not к:
        continue
    д = dt.date.fromisoformat(r["OpenDate.Typed"][:10])
    имена[к] = r["DishName"]; кат[к] = r.get("DishCategory") or "—"
    з = дни[к].setdefault(д, {"шт": 0.0, "меню": 0.0})
    з["шт"] += r["DishAmountInt"] or 0
    з["меню"] += r["DishSumInt"] or 0

ВСЕ_ДНИ = sorted({д for x in дни.values() for д in x})
НАЧ, КОН = ВСЕ_ДНИ[0], ВСЕ_ДНИ[-1]
КАЛ = [НАЧ + dt.timedelta(days=i) for i in range((КОН - НАЧ).days + 1)]

цена, база, штуки = {}, {}, {}
for к, по_дням in дни.items():
    ц = {д: з["меню"] / з["шт"] for д, з in по_дням.items() if з["шт"] > 0}
    if len(ц) < 60:
        continue
    уровни = {round(c, 2) for c in ц.values()}
    круглых = sum(1 for c in ц.values() if abs(c - round(c)) < 0.01) / len(ц)
    if len(уровни) > 12 or круглых < 0.95:
        continue                       # цена размазана — эпизод не выделить
    ряд, посл = {}, None
    первый = min(ц)
    for д in КАЛ:
        посл = ц.get(д, посл)
        if д >= первый and посл is not None:
            ряд[д] = посл
    дн = sorted(ряд)
    зн = [ряд[д] for д in дн]
    цена[к] = ряд
    база[к] = {д: statistics.median(зн[max(0, i - 28):i + 29])
               for i, д in enumerate(дн)}
    штуки[к] = {д: по_дням.get(д, {}).get("шт", 0.0) for д in дн}

print("блюд со ступенчатой ценой и историей:", len(цена))

# Эпизоды снижения
эпизоды = []
for к in цена:
    дн = sorted(цена[к])
    текущий = None
    for д in дн:
        низко = цена[к][д] < база[к][д] * 0.97
        if низко and текущий is None:
            текущий = [д, д]
        elif низко:
            текущий[1] = д
        elif текущий:
            эпизоды.append((к, текущий[0], текущий[1]))
            текущий = None
    if текущий:
        эпизоды.append((к, текущий[0], текущий[1]))
эпизоды = [(к, a, b) for к, a, b in эпизоды if (b - a).days <= 21]
print("эпизодов снижения цены:", len(эпизоды))

# Кто в эпизоде — тех в фон не берём.
в_акции = collections.defaultdict(set)
for к, a, b in эпизоды:
    д = a - dt.timedelta(days=3)
    while д <= b + dt.timedelta(days=3):
        в_акции[к].add(д); д += dt.timedelta(days=1)


def норма(к, a, b):
    """Ожидаемые штуки в день: те же дни недели вокруг эпизода, без акции."""
    дни_недели = {(a + dt.timedelta(days=i)).weekday()
                  for i in range((b - a).days + 1)}
    зн = []
    for сдвиг in list(range(-28, -3)) + list(range(4, 29)):
        д = a + dt.timedelta(days=сдвиг)
        if д not in штуки[к] or д in в_акции[к]:
            continue
        if д.weekday() not in дни_недели:
            continue
        if цена[к][д] < база[к][д] * 0.99:
            continue
        зн.append(штуки[к][д])
    return (sum(зн) / len(зн), len(зн)) if len(зн) >= 6 else (None, 0)


def фон_дней(a, b, исключить):
    """Во сколько раз эти дни вообще лучше/хуже обычных — по чужим блюдам."""
    внутри, снаружи = [], []
    дни_недели = {(a + dt.timedelta(days=i)).weekday()
                  for i in range((b - a).days + 1)}
    for к in цена:
        if к == исключить:
            continue
        сумма_в = [штуки[к][д] for д in штуки[к]
                   if a <= д <= b and д not in в_акции[к]]
        if not сумма_в:
            continue
        вне = [штуки[к][д] for сдвиг in list(range(-28, -3)) + list(range(4, 29))
               for д in [a + dt.timedelta(days=сдвиг)]
               if д in штуки[к] and д not in в_акции[к]
               and д.weekday() in дни_недели]
        if len(вне) < 6:
            continue
        ср_вне = sum(вне) / len(вне)
        if ср_вне <= 0:
            continue
        внутри.append(sum(сумма_в) / len(сумма_в)); снаружи.append(ср_вне)
    if len(внутри) < 20:
        return None
    return sum(внутри) / sum(снаружи)


точки = []
for к, a, b in эпизоды:
    ожидание, сколько = норма(к, a, b)
    if not ожидание or ожидание < 0.7:
        continue
    факт = [штуки[к][д] for д in штуки[к] if a <= д <= b]
    if not факт:
        continue
    ф = фон_дней(a, b, к)
    if not ф:
        continue
    q = sum(факт) / len(факт)
    if q <= 0:
        continue
    цены_эп = [цена[к][д] for д in цена[к] if a <= д <= b]
    баз_эп = [база[к][д] for д in база[к] if a <= д <= b]
    шаг = math.log(sum(цены_эп) / len(цены_эп) / (sum(баз_эп) / len(баз_эп)))
    отклик = math.log(q / (ожидание * ф))
    точки.append({"шаг": шаг, "отклик": отклик, "код": к, "имя": имена[к],
                  "дней": (b - a).days + 1, "с": a.isoformat(),
                  "было": ожидание, "стало": q, "кат": кат[к]})

print("эпизодов с чистым замером:", len(точки))
r = мнк([т["отклик"] for т in точки], [[т["шаг"]] for т in точки],
        [т["код"] for т in точки])
b, se = r["beta"][0], r["se"][0]
print(f"\nэластичность по акциям: {b:+.2f} ± {se:.2f}   "
      f"95% [{b-1.96*se:+.2f}; {b+1.96*se:+.2f}]")
глубокие = [т for т in точки if т["шаг"] < -0.12]
if len(глубокие) >= 10:
    r2 = мнк([т["отклик"] for т in глубокие], [[т["шаг"]] for т in глубокие],
             [т["код"] for т in глубокие])
    b2, s2 = r2["beta"][0], r2["se"][0]
    print(f"только глубокие (12%+): {len(глубокие)} эпизодов, "
          f"{b2:+.2f} ± {s2:.2f}")
    медиана = statistics.median(т["отклик"] for т in глубокие)
    мс = statistics.median(т["шаг"] for т in глубокие)
    print(f"   медиана: цена {мс*100:+.0f}% → штуки {(math.exp(медиана)-1)*100:+.0f}%")

print("\nсамые заметные эпизоды:")
for т in sorted(точки, key=lambda т: т["отклик"] - т["шаг"], reverse=True)[:8]:
    print(f"   {т['имя'][:26]:<26} {т['с']} {т['дней']}д  цена {т['шаг']*100:+5.0f}%  "
          f"штук/день {т['было']:.1f}→{т['стало']:.1f}  отклик {(math.exp(т['отклик'])-1)*100:+.0f}%")
json.dump(точки, open(f"{S}/акции.json", "w"), ensure_ascii=False)
