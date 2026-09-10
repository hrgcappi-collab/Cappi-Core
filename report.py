#!/usr/bin/env python3
"""Показатели дня: выручка против плана, заказы, блюда, отмены, живые заказы.

Запуск из терминала:
    python3 report.py            за сегодня
    python3 report.py 2026-09-09 за конкретный день

Откуда что берётся:
    выручка, заказы, блюда, отмены   OLAP-отчёт Syrve (учётный день)
    заказы в работе                  Syrve Cloud API, живые статусы доставки
    план                             ~/.cappi/plan.json, ставится из бота

Про учётный день. OLAP считает не по календарным суткам, а по учётному дню
заведения: ночная смена с 10-го на 11-е целиком относится к 10-му. Поэтому
отчёт в 22:00 — это ещё не итог дня, а срез: ночные заказы попадут в тот же
день и добавятся после. Так и подписано в тексте, иначе цифры выглядят
занижёнными без объяснения.

Про полуинтервал дат. Syrve отвергает запрос, где начало периода равно
концу, — конец должен быть следующим днём. Отсюда `to = day + 1`.
"""
import json
import os
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta

import cappi

PLAN_FILE = os.path.expanduser("~/.cappi/plan.json")

# Точки продаж. В Syrve это поле RestaurantSection; в плане их пишут коротко,
# поэтому держим соответствие явно, а не угадываем по вхождению подстроки.
ТОЧКИ = {"Зал Лазарева": "Лазарева", "Зал Левитана": "Левитана"}

ДНИ = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

# Статусы доставки, которые означают «заказ ещё в работе».
В_РАБОТЕ = ("Unconfirmed", "WaitCooking", "ReadyForCooking", "CookingStarted",
            "CookingCompleted", "Waiting", "OnWay")


# ------------------------------------------------------------------- план
def load_plan():
    try:
        return json.load(open(PLAN_FILE))
    except Exception:
        return {}


def save_plan(p):
    json.dump(p, open(PLAN_FILE, "w"), ensure_ascii=False, indent=1)


def parse_plan(текст):
    """Разбирает таблицу плана в том виде, в каком её ведут: название точки
    строкой, под ним дни недели с суммами.

        \tЛазарева
        Пн\t58420
        ...
        Неделя\t477050

    Строка «Неделя» — это контрольная сумма из исходной таблицы, её не
    записываем, но сверяем: если не сходится, значит строка потерялась при
    копировании, и лучше сказать об этом сразу.
    """
    план, точка, ошибки = {}, None, []
    for сырая in текст.splitlines():
        строка = сырая.strip()
        if not строка:
            continue
        части = re.split(r"[\t;,]+|\s{2,}", строка)
        имя = части[0].strip().lower()
        число = None
        if len(части) > 1:
            try:
                число = float(re.sub(r"[^\d.,]", "", части[-1]).replace(",", "."))
            except ValueError:
                число = None
        день = next((d for d in ДНИ if имя.startswith(d)), None)
        if день and число is not None and точка:
            план[точка][день] = число
        elif имя.startswith("недел") and число is not None and точка:
            факт = sum(план[точка].values())
            if abs(факт - число) > 1:
                ошибки.append(f"{точка}: дни дают {факт:,.0f}, "
                              f"а в строке «Неделя» {число:,.0f}")
        elif число is None and len(строка) < 40:
            # Строка без числа — это заголовок точки.
            найдено = next((полн for полн, кор in ТОЧКИ.items()
                            if кор.lower() in строка.lower()), None)
            точка = найдено or строка
            план.setdefault(точка, {})
    return план, ошибки


def plan_for(day):
    """План на день: сумма по точкам плюс разбивка. Точечный план на дату
    перебивает недельный — им пользуются, когда день выбивается из обычного."""
    p = load_plan()
    d = day.isoformat()
    if d in p.get("days", {}):
        сумма = p["days"][d]
        return сумма, {}, "на день"
    недельный = p.get("weekly", {})
    if недельный:
        день = ДНИ[day.weekday()]
        по_точкам = {т: v.get(день, 0) for т, v in недельный.items()}
        return sum(по_точкам.values()), по_точкам, f"недельный, {день}"
    month = p.get("months", {}).get(day.strftime("%Y-%m"))
    if month:
        в_месяце = (date(day.year + day.month // 12, day.month % 12 + 1, 1)
                    - timedelta(days=1)).day
        return month / в_месяце, {}, f"месячный ÷ {в_месяце}"
    return None, {}, None


# ------------------------------------------------------------------- OLAP
def _olap(s, day, group, aggregate, extra_filters=None):
    body = {
        "reportType": "SALES",
        "buildSummary": False,
        "groupByRowFields": group,
        "aggregateFields": aggregate,
        "filters": {
            # Конец периода — следующий день: Syrve не принимает from == to.
            "OpenDate.Typed": {"filterType": "DateRange", "periodType": "CUSTOM",
                               "from": day.isoformat(),
                               "to": (day + timedelta(days=1)).isoformat()},
            **(extra_filters or {}),
        },
    }
    r = cappi._post(f"{s.host}/resto/api/v2/reports/olap?key={s.key}", body, timeout=150)
    return r.get("data", [])


def sales(s, day):
    """Выручка, чеки и количество — в разрезе типа товара."""
    rows = _olap(s, day, ["DishType"],
                 ["DishDiscountSumInt", "UniqOrderId", "DishAmountInt"])
    by = {r.get("DishType"): r for r in rows}

    def взять(*типы):
        сумма = чеки = штук = 0
        for t in типы:
            r = by.get(t) or {}
            сумма += r.get("DishDiscountSumInt", 0) or 0
            чеки += r.get("UniqOrderId", 0) or 0
            штук += r.get("DishAmountInt", 0) or 0
        return {"сумма": сумма, "чеки": чеки, "штук": штук}

    return {
        "всего": взять(*by.keys()),
        "блюда": взять("DISH"),                 # тип товара «Блюдо»
        "блюда_и_товары": взять("DISH", "GOODS"),
        "по_типам": {t: взять(t) for t in by},
    }


def sales_by_point(s, day):
    """Выручка и чеки по точкам — план ведут именно так."""
    rows = _olap(s, day, ["RestaurantSection"],
                 ["DishDiscountSumInt", "UniqOrderId"])
    return {r.get("RestaurantSection"): {"сумма": r.get("DishDiscountSumInt", 0) or 0,
                                         "чеки": r.get("UniqOrderId", 0) or 0}
            for r in rows if r.get("RestaurantSection")}


def cancels(s, day):
    """Отмены доставки по причинам."""
    rows = _olap(s, day, ["Delivery.CancelCause"], ["UniqOrderId"])
    out = {}
    for r in rows:
        причина = r.get("Delivery.CancelCause")
        if причина:                              # пустая причина — это не отмена
            out[причина] = r.get("UniqOrderId", 0)
    return out


def removals(s, day):
    """Удаления блюд по причинам — соседний показатель, часто нужен вместе."""
    rows = _olap(s, day, ["RemovalType"], ["UniqOrderId"])
    return {r["RemovalType"]: r.get("UniqOrderId", 0)
            for r in rows if r.get("RemovalType")}


# --------------------------------------------------------------- живые заказы
def live_orders(day=None):
    """Статусы заказов доставки за день — то, что происходит прямо сейчас."""
    day = day or date.today()
    c = cappi.cfg()
    tok = cappi._post(f"{c['SYRVE_CLOUD_URL']}/api/1/access_token",
                      {"apiLogin": c["SYRVE_CLOUD_API_KEY"]}, timeout=40)["token"]
    r = cappi._post(
        f"{c['SYRVE_CLOUD_URL']}/api/1/deliveries/by_delivery_date_and_status",
        {"organizationIds": [c["SYRVE_ORG_ID"]],
         "deliveryDateFrom": f"{day} 00:00:00.000",
         "deliveryDateTo": f"{day + timedelta(days=1)} 00:00:00.000"},
        {"Authorization": f"Bearer {tok}"}, timeout=90)
    orders = [o for g in r.get("ordersByOrganizations", []) for o in g.get("orders", [])]
    статусы = Counter(o.get("order", {}).get("status") for o in orders)
    return {"всего": len(orders), "статусы": dict(статусы),
            "в_работе": sum(v for k, v in статусы.items() if k in В_РАБОТЕ)}


# ------------------------------------------------------------------- сборка
def collect(day=None):
    day = day or date.today()
    with cappi.Syrve() as s:
        d = {"день": day, "продажи": sales(s, day), "точки": sales_by_point(s, day),
             "отмены": cancels(s, day), "удаления": removals(s, day)}
    try:
        d["живые"] = live_orders(day)
    except Exception as e:
        d["живые"] = {"ошибка": str(e)[:120]}
    d["план"], d["план_точки"], d["план_откуда"] = plan_for(day)
    return d


def money(v):
    return f"{v:,.0f}".replace(",", " ")


def render(d, live=False):
    """Текст отчёта. live=True — срез «прямо сейчас», иначе итог дня."""
    p, вс = d["продажи"], d["продажи"]["всего"]
    строки = [f"<b>{'Сейчас' if live else 'Итоги дня'} · {d['день']:%d.%m}</b>", ""]

    if d["план"]:
        proc = вс["сумма"] / d["план"] * 100 if d["план"] else 0
        знак = "✅" if proc >= 100 else ("🟡" if proc >= 85 else "🔴")
        строки += [f"{знак} <b>Выручка {money(вс['сумма'])} ₴</b>",
                   f"    план {money(d['план'])} ₴ · <b>{proc:.0f}%</b>"
                   f"  <i>({d['план_откуда']})</i>",
                   f"    не хватает {money(max(0, d['план'] - вс['сумма']))} ₴"
                   if proc < 100 else f"    сверх плана {money(вс['сумма'] - d['план'])} ₴"]
    else:
        строки += [f"💰 <b>Выручка {money(вс['сумма'])} ₴</b>",
                   "    <i>план не задан — /plan</i>"]

    if d.get("точки"):
        строки.append("")
        for точка, ф in sorted(d["точки"].items(), key=lambda x: -x[1]["сумма"]):
            кор = ТОЧКИ.get(точка, точка)
            пл = (d.get("план_точки") or {}).get(точка)
            хвост = ""
            if пл:
                pr = ф["сумма"] / пл * 100
                хвост = (f" / {money(пл)}  <b>{pr:.0f}%</b>"
                         + ("  ✅" if pr >= 100 else ("  🟡" if pr >= 85 else "  🔴")))
            строки.append(f"    {кор:<10} {money(ф['сумма']):>9} ₴{хвост}")

    строки += ["",
               f"📦 Заказов: <b>{p['блюда']['чеки']}</b>  <i>(тип товара: Блюдо)</i>",
               f"🍱 Блюд: <b>{p['блюда_и_товары']['штук']:,.0f}</b>"
               .replace(",", " ") + "  <i>(Блюдо + Товар)</i>"]

    if d["отмены"]:
        всего = sum(d["отмены"].values())
        строки += ["", f"❌ Отмен: <b>{всего}</b>"]
        for причина, n in sorted(d["отмены"].items(), key=lambda x: -x[1]):
            строки.append(f"    {причина} — {n}")
    else:
        строки += ["", "❌ Отмен: <b>0</b>"]

    if d["удаления"]:
        строки += ["", "🗑 Удаления блюд:"]
        for причина, n in sorted(d["удаления"].items(), key=lambda x: -x[1]):
            строки.append(f"    {причина} — {n}")

    ж = d.get("живые", {})
    if "ошибка" in ж:
        строки += ["", f"⚠️ Живые заказы недоступны: {ж['ошибка']}"]
    elif ж:
        строки += ["", f"🚚 Заказов в работе: <b>{ж['в_работе']}</b> из {ж['всего']}"]
        for st, n in sorted(ж["статусы"].items(), key=lambda x: -x[1]):
            если_в_работе = " ←" if st in В_РАБОТЕ else ""
            строки.append(f"    {st} — {n}{если_в_работе}")

    строки += ["", "<i>Учётный день: ночная смена попадёт в этот же день, "
                   "поэтому вечерние цифры ещё не итоговые.</i>"]
    return "\n".join(строки)


if __name__ == "__main__":
    d = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()
    import re
    print(re.sub(r"</?[bi]>", "", render(collect(d), live=(d == date.today()))))
