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
import urllib.parse
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date, datetime, timedelta

import cappi
import webhook

PLAN_FILE = os.path.expanduser("~/.cappi/plan.json")

# Точки продаж. В Syrve это поле RestaurantSection; в плане их пишут коротко,
# поэтому держим соответствие явно, а не угадываем по вхождению подстроки.
ТОЧКИ = {"Зал Лазарева": "Лазарева", "Зал Левитана": "Левитана"}

# Махачкалинскую в плане не выделяют — её выручка идёт в Лазареву.
СЛИВАТЬ = {"Зал Махачкалинская": "Зал Лазарева"}

ДНИ = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

# Категории жалоб Loopa отдаёт кодами — читают их люди.
ПОВОДЫ = {
    "kitchen": "кухня", "delivery": "доставка", "packing": "упаковка",
    "courier": "курьер", "call_center": "кол-центр", "service": "сервис",
    "tech": "техника", "glovo": "Glovo", "guest": "гость",
    "other": "прочее", "podiaka-pozytyv": "благодарность",
}

# Статусы доставки, которые означают «заказ ещё в работе».
В_РАБОТЕ = ("Unconfirmed", "WaitCooking", "ReadyForCooking", "CookingStarted",
            "CookingCompleted", "Waiting", "OnWay")

# Syrve отдаёт статусы по-английски, читают их люди — переводим.
СТАТУСЫ = {
    "Unconfirmed": "не подтверждён",
    "WaitCooking": "ждёт кухню",
    "ReadyForCooking": "готов к приготовлению",
    "CookingStarted": "готовится",
    "CookingCompleted": "приготовлен",
    "Waiting": "ждёт курьера",
    "OnWay": "в пути",
    "Delivered": "доставлен",
    "Closed": "закрыт",
    "Cancelled": "отменён",
}


def статус(код):
    return СТАТУСЫ.get(код, код)


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
        # Дата вида 07.09.2026 — план на конкретный день, он точнее дня недели.
        дата = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", части[0].strip())
        неделя = re.match(r"(\d)\s*недел", имя)
        день = next((d for d in ДНИ if имя.startswith(d)), None)
        if неделя and число is not None:
            # «1 неделя 908000» — накопительный план месяца, он вне точек.
            план.setdefault("__недели__", {})[int(неделя.group(1))] = число
        elif дата and число is not None and точка:
            д, м, г = (int(x) for x in дата.groups())
            план[точка][date(г, м, д).isoformat()] = число
        elif день and число is not None and точка:
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
    """План на день: сумма по точкам плюс разбивка.

    Порядок важен. План на конкретную дату перебивает план по дню недели:
    даты присылают на неделю вперёд с учётом акций и праздников, а день
    недели — это усреднённый шаблон.
    """
    p = load_plan()
    d = day.isoformat()
    план = p.get("weekly", {})
    по_датам = {т: v[d] for т, v in план.items() if d in v}
    if по_датам:
        return sum(по_датам.values()), по_датам, "на дату"
    if план:
        день = ДНИ[day.weekday()]
        по_точкам = {т: v[день] for т, v in план.items() if день in v}
        if по_точкам:
            return sum(по_точкам.values()), по_точкам, f"по дню недели ({день})"
    month = p.get("months", {}).get(day.strftime("%Y-%m"))
    if month:
        в_месяце = (date(day.year + day.month // 12, day.month % 12 + 1, 1)
                    - timedelta(days=1)).day
        return month / в_месяце, {}, f"месячный ÷ {в_месяце}"
    return None, {}, None


def недели_месяца(day):
    """Границы недель месяца: календарные, с понедельника по воскресенье.
    Первая и последняя обычно неполные — так их и планируют."""
    первое = day.replace(day=1) if False else date(day.year, day.month, 1)
    последнее = (date(day.year + day.month // 12, day.month % 12 + 1, 1)
                 - timedelta(days=1))
    границы, начало, d, n = [], первое, первое, 1
    while d <= последнее:
        if d.weekday() == 6 or d == последнее:
            границы.append((n, начало, d))
            n += 1
            начало = d + timedelta(days=1)
        d += timedelta(days=1)
    return границы


def месячный_план(day):
    """Сколько должно быть заработано с начала месяца по сегодня.

    Текущая неделя считается пропорционально прошедшим дням: сравнивать
    факт за три дня с планом на всю неделю бессмысленно, процент выйдет
    втрое ниже правды.
    """
    недели = load_plan().get("month_weeks", {}).get(day.strftime("%Y-%m"))
    if not недели:
        return None, None
    накоплено = 0
    for n, начало, конец in недели_месяца(day):
        сумма = недели.get(str(n)) or недели.get(n)
        if not сумма:
            continue
        if конец <= day:
            накоплено += сумма
        elif начало <= day:
            дней = (конец - начало).days + 1
            прошло = (day - начало).days + 1
            накоплено += сумма * прошло / дней
    return накоплено, sum(float(v) for v in недели.values())


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
    out = {}
    for r in rows:
        имя = r.get("RestaurantSection")
        if not имя:
            continue
        имя = СЛИВАТЬ.get(имя, имя)          # Махачкалинская → Лазарева
        цель = out.setdefault(имя, {"сумма": 0, "чеки": 0})
        цель["сумма"] += r.get("DishDiscountSumInt", 0) or 0
        цель["чеки"] += r.get("UniqOrderId", 0) or 0
    return out


def month_to_date(s, day):
    """Выручка с первого числа по этот день включительно."""
    первое = date(day.year, day.month, 1)
    body = {"reportType": "SALES", "buildSummary": False,
            "groupByRowFields": ["RestaurantSection"],
            "aggregateFields": ["DishDiscountSumInt"],
            "filters": {"OpenDate.Typed": {
                "filterType": "DateRange", "periodType": "CUSTOM",
                "from": первое.isoformat(),
                "to": (day + timedelta(days=1)).isoformat()}}}
    r = cappi._post(f"{s.host}/resto/api/v2/reports/olap?key={s.key}", body, timeout=150)
    return sum(row.get("DishDiscountSumInt", 0) or 0 for row in r.get("data", []))


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


# ------------------------------------------------------------------- явки
# Процент выполнения плана не объясняет, почему не дотянули: мало заказов
# или вывели вдвое больше людей, чем нужно. Выручка на человеко-час
# объясняет.

def attendance(s, day):
    """Кто был на смене, сколько часов, и правили ли записи задним числом."""
    q = urllib.parse.urlencode({"from": day.isoformat(),
                                "to": (day + timedelta(days=1)).isoformat(),
                                "key": s.key})
    xml = cappi._get(f"{s.host}/resto/api/employees/attendance?{q}", timeout=90)
    имена = _справочник(s, "employees", "employee")
    роли = _справочник(s, "employees/roles", "role")

    по_ролям, правки, всего_часов = {}, [], 0.0
    for a in ET.fromstring(xml).findall("attendance"):
        d1, d2 = a.findtext("dateFrom"), a.findtext("dateTo")
        if not (d1 and d2):
            continue                      # смена ещё открыта
        часов = (datetime.fromisoformat(d2)
                 - datetime.fromisoformat(d1)).total_seconds() / 3600
        всего_часов += часов
        роль = роли.get(a.findtext("roleId")) or "—"
        r = по_ролям.setdefault(роль, {"людей": 0, "часов": 0.0})
        r["людей"] += 1
        r["часов"] += часов

        # Запись, поправленная заметно позже конца смены, — повод посмотреть.
        изменена = a.findtext("modified")
        if изменена:
            try:
                разрыв = (datetime.fromisoformat(изменена)
                          - datetime.fromisoformat(d2)).total_seconds() / 60
                if разрыв > 30:
                    правки.append({
                        "кто": имена.get(a.findtext("employeeId")) or "?",
                        "роль": роль, "смена": f"{d1[11:16]}–{d2[11:16]}",
                        "через": разрыв, "правил": a.findtext("userModified") or "?"})
            except Exception:
                pass

    return {"по_ролям": по_ролям, "часов": всего_часов,
            "людей": sum(r["людей"] for r in по_ролям.values()),
            "правки": правки}


def _справочник(s, путь, тег):
    try:
        xml = cappi._get(f"{s.host}/resto/api/{путь}?key={s.key}", timeout=90)
        return {x.findtext("id"): x.findtext("name") for x in ET.fromstring(xml).iter(тег)}
    except Exception:
        return {}


# ------------------------------------------------------------------- жалобы
# Жалоба — это отзыв с негативной тональностью. Loopa размечает тональность
# сама, и брать все отзывы подряд бессмысленно: за десять дней их 258, из
# них негативных 19. Показатель «сколько отзывов» ни о чём не говорит,
# показатель «сколько недовольных» — говорит.

def _loopa(**params):
    c = cappi.cfg()
    if not c.get("LOOPA_TOKEN"):
        return None
    q = "&".join(f"{k}={v}" for k, v in params.items())
    return json.loads(cappi._get(
        f"{c['LOOPA_URL'].rstrip('/')}/metrics/reviews?{q}",
        {"Authorization": f"Bearer {c['LOOPA_TOKEN']}"}, timeout=40))


def complaints(day, разрез="category"):
    """Жалобы за день: сколько и по каким поводам."""
    д = day.isoformat()
    общее = _loopa(**{"from": д, "to": д, "tone": "negative"})
    if общее is None:
        return None
    по = _loopa(**{"from": д, "to": д, "tone": "negative", "group_by": разрез}) or {}
    точки = _loopa(**{"from": д, "to": д, "tone": "negative", "group_by": "branch"}) or {}
    срочность = _loopa(**{"from": д, "to": д, "tone": "negative",
                          "group_by": "urgency"}) or {}
    всего = _loopa(**{"from": д, "to": д}) or {}
    return {
        "жалоб": общее.get("total", 0),
        "отзывов": всего.get("total", 0),
        "по_поводам": {g["key"]: g["count"] for g in по.get("groups", []) if g["key"]},
        "по_точкам": {g["key"]: g["count"] for g in точки.get("groups", []) if g["key"]},
        "срочность": {g["key"]: g["count"] for g in срочность.get("groups", []) if g["key"]},
    }


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
             "отмены": cancels(s, day), "удаления": removals(s, day),
             "месяц_факт": month_to_date(s, day)}
        try:
            d["смена"] = attendance(s, day)
        except Exception as e:
            d["смена"] = {"ошибка": str(e)[:100]}
    try:
        d["жалобы"] = complaints(day)
    except Exception as e:
        d["жалобы"] = {"ошибка": str(e)[:100]}
    try:
        d["живые"] = live_orders(day)
    except Exception as e:
        d["живые"] = {"ошибка": str(e)[:120]}
    d["зоны"] = webhook.zones_summary(day)
    d["план"], d["план_точки"], d["план_откуда"] = plan_for(day)
    d["месяц_план"], d["месяц_всего"] = месячный_план(day)
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

    if d.get("месяц_план"):
        факт, план_нак = d["месяц_факт"], d["месяц_план"]
        pr = факт / план_нак * 100 if план_нак else 0
        знак = "✅" if pr >= 100 else ("🟡" if pr >= 85 else "🔴")
        строки += ["", f"{знак} <b>С начала месяца</b>",
                   f"    {money(факт)} ₴ / {money(план_нак)} ₴  <b>{pr:.0f}%</b>",
                   f"    месяц целиком: {money(d['месяц_всего'])} ₴"]

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

    см = d.get("смена") or {}
    if см.get("часов"):
        на_час = вс["сумма"] / см["часов"] if см["часов"] else 0
        строки += ["", f"👥 Смена: <b>{см['людей']} чел</b> · "
                       f"{см['часов']:.0f} человеко-часов",
                   f"    выручка на час: <b>{money(на_час)} ₴</b>"]
        for роль, r in sorted(см["по_ролям"].items(), key=lambda x: -x[1]["часов"]):
            строки.append(f"    {роль} — {r['людей']} чел, {r['часов']:.0f} ч")
        if см["правки"]:
            строки.append(f"    ⚠️ <b>правок задним числом: {len(см['правки'])}</b>")

    ж = d.get("жалобы") or {}
    if "ошибка" in ж:
        строки += ["", f"⚠️ Жалобы недоступны: {ж['ошибка']}"]
    elif ж:
        доля = f" из {ж['отзывов']} отзывов" if ж.get("отзывов") else ""
        строки += ["", f"😠 Жалоб: <b>{ж['жалоб']}</b>{доля}"]
        крит = ж["срочность"].get("critical", 0) + ж["срочность"].get("high", 0)
        if крит:
            строки.append(f"    <b>срочных: {крит}</b>")
        for повод, n in sorted(ж["по_поводам"].items(), key=lambda x: -x[1]):
            строки.append(f"    {ПОВОДЫ.get(повод, повод)} — {n}")
        if len(ж["по_точкам"]) > 1:
            строки.append("    " + " · ".join(
                f"{т} {n}" for т, n in sorted(ж["по_точкам"].items(), key=lambda x: -x[1])))

    з = d.get("зоны") or {}
    if з.get("закрытий"):
        часы = з["минут"] / 60
        строки += ["", f"🚧 Закрытий зон: <b>{з['закрытий']}</b>"
                       f"  ·  <b>{часы:.1f} ч</b> суммарно"]
        for район, r in sorted(з["по_районам"].items(), key=lambda x: -x[1]["минут"]):
            строки.append(f"    {район} — {r['раз']} раз, {r['минут']:.0f} мин")
        хвост = []
        if з["ещё_закрыты"]:
            хвост.append(f"{з['ещё_закрыты']} ещё закрыты — время по плану, не факт")
        if з["авто"]:
            хвост.append(f"{з['авто']} закрыты автоматически")
        if хвост:
            строки.append(f"    <i>{'; '.join(хвост)}</i>")

    ж = d.get("живые", {})
    if "ошибка" in ж:
        строки += ["", f"⚠️ Живые заказы недоступны: {ж['ошибка']}"]
    elif ж:
        строки += ["", f"🚚 Заказов в работе: <b>{ж['в_работе']}</b> из {ж['всего']}"]
        for st, n in sorted(ж["статусы"].items(), key=lambda x: -x[1]):
            метка = " ←" if st in В_РАБОТЕ else ""
            строки.append(f"    {статус(st)} — {n}{метка}")

    if live:
        # Для прошедшего дня приписка вредна: там уже итог, а фраза
        # «цифры не итоговые» заставляет сомневаться в готовом числе.
        строки += ["", "<i>Учётный день: ночная смена попадёт в этот же день, "
                       "поэтому вечерние цифры ещё не итоговые.</i>"]
    return "\n".join(строки)


if __name__ == "__main__":
    d = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()
    import re
    print(re.sub(r"</?[bi]>", "", render(collect(d), live=(d == date.today()))))
