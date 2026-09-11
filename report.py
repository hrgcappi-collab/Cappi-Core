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
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import cappi
import webhook

PLAN_FILE = os.path.expanduser("~/.cappi/plan.json")

# Точки продаж. В Syrve это поле RestaurantSection; в плане их пишут коротко,
# поэтому держим соответствие явно, а не угадываем по вхождению подстроки.
# Одно написание на весь бот. В Syrve точка записана русской «Левитана», в
# жизни и в KPI — украинской «Левітана»; в одном отчёте встречались оба, и
# выглядело это как две разные точки.
ТОЧКИ = {"Зал Лазарева": "Лазарева", "Зал Левитана": "Левітана"}

# Махачкалинскую в плане не выделяют — её выручка идёт в Лазареву.
СЛИВАТЬ = {"Зал Махачкалинская": "Зал Лазарева"}

ДНИ = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

# Категории жалоб Loopa отдаёт кодами — читают их люди.
def повод_по_русски(код):
    """Loopa отдаёт коды, иногда составные: «delivery,admin». Показывать их
    как есть — заставлять человека переводить в уме каждое утро."""
    куски = [ч.strip() for ч in str(код).split(",") if ч.strip()]
    return ", ".join(ПОВОДЫ.get(ч, ч) for ч in куски) or str(код)


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
            # Сравниваем нормализованно: в плане пишут «Левитана» русской
            # «и», у нас точка называется «Левітана». Простое вхождение
            # подстроки на этом разваливалось, и план уходил в никуда.
            цель = cappi.norm(строка)
            найдено = next((полн for полн, кор in ТОЧКИ.items()
                            if cappi.norm(кор) in цель), None)
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


# Удалённые блюда и заказы из подсчётов исключаются — так же, как в сводной
# таблице Syrve, по которой сверяются. Без этого фильтра за 09.09 выходило
# 458 блюд вместо 406: в счёт попадало всё, что успели пробить и удалить.
# На выручку не влияет — у удалённого она и так ноль.
# Подразделение. В базе их два, но продаёт только «Cappi Одесса»: у
# «Cappi Днепр» ноль заказов за всю историю. Фильтр стоит не ради цифр —
# они и так сходятся, — а чтобы чужое подразделение не могло однажды
# просочиться в выручку, KPI и проценты кухни.
def НАШ_ОТДЕЛ():
    return {"Department.Id": {"filterType": "IncludeValues",
                              "values": [cappi.отдел()]}}


НЕ_УДАЛЁННЫЕ = {
    "DeletedWithWriteoff": {"filterType": "IncludeValues", "values": ["NOT_DELETED"]},
    "OrderDeleted": {"filterType": "IncludeValues", "values": ["NOT_DELETED"]},
}

# Заказом считается чек с едой или товаром. Служебные строки — доставка,
# «Замовлення з додатку» — образуют свои чеки, и без этого фильтра их
# набегало 208 против 111 в Syrve.
ЕДА = {"DishType": {"filterType": "IncludeValues", "values": ["DISH", "GOODS"]}}


# ------------------------------------------------------------------- OLAP
def _olap(s, day, group, aggregate, extra_filters=None, колонки=None):
    body = {
        "reportType": "SALES",
        "buildSummary": False,
        "groupByRowFields": group,
        "groupByColFields": колонки or [],
        "aggregateFields": aggregate,
        "filters": {
            # Конец периода — следующий день: Syrve не принимает from == to.
            "OpenDate.Typed": {"filterType": "DateRange", "periodType": "CUSTOM",
                               "from": day.isoformat(),
                               "to": (day + timedelta(days=1)).isoformat()},
            **НАШ_ОТДЕЛ(),
            **(extra_filters or {}),
        },
    }
    r = cappi._post(f"{s.host}/resto/api/v2/reports/olap?key={s.key}", body, timeout=150)
    return r.get("data", [])


def orders_count(s, day):
    """Сколько чеков за день на самом деле.

    Складывать чеки по типам товара нельзя: заказ с блюдом и напитком
    попадёт в оба типа и посчитается дважды — за 09.09 так получалось 330
    вместо 208. Спрашиваем без разбивки, тогда Syrve считает уникальные.
    """
    rows = _olap(s, day, [], ["UniqOrderId"], {**НАШ_ОТДЕЛ(), **НЕ_УДАЛЁННЫЕ, **ЕДА})
    return sum(r.get("UniqOrderId", 0) or 0 for r in rows)


def sales(s, day):
    """Выручка, чеки и количество — в разрезе типа товара."""
    rows = _olap(s, day, ["DishType"],
                 ["DishDiscountSumInt", "UniqOrderId", "DishAmountInt"],
                 НЕ_УДАЛЁННЫЕ)
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
        # У «всего» суммы складываются верно, а чеки — нет: см. orders_count.
        "всего": взять(*by.keys()),
        "блюда": взять("DISH"),                 # тип товара «Блюдо»
        "блюда_и_товары": взять("DISH", "GOODS"),
        "по_типам": {t: взять(t) for t in by},
    }


def by_category(s, day):
    """Заказы и блюда по категориям, отдельно Блюдо и Товар, по точкам.

    Разрез такой же, как в сводной таблице Syrve, к которой все привыкли:
    строки — тип товара и категория, колонки — концепции.

    Важно про «Заказов». Сложить его по категориям нельзя: заказ с роллами
    и пиццей попадёт в обе строки. Поэтому итог берём отдельным запросом
    без разбивки, а по строкам он честен только внутри своей строки.

    Кнопки в боте пока нет — не понадобилась. Функция проверена на живых
    данных и лежит готовой: чтобы включить, нужен экран и строка в BUTTONS.
    """
    rows = _olap(s, day, ["DishType", "DishCategory"],
                 ["UniqOrderId", "DishAmountInt"],
                 {**НАШ_ОТДЕЛ(), **НЕ_УДАЛЁННЫЕ, **ЕДА}, колонки=["Conception"])
    out = {}
    for r in rows:
        тип = r.get("DishType") or "—"
        кат = r.get("DishCategory") or "без категории"
        точка = (r.get("Conception") or "—").lstrip("012 ").strip()
        цель = out.setdefault(тип, {}).setdefault(кат, {})
        т = цель.setdefault(точка, {"заказов": 0, "блюд": 0})
        т["заказов"] += r.get("UniqOrderId", 0) or 0
        т["блюд"] += r.get("DishAmountInt", 0) or 0
    return out


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
                "to": (day + timedelta(days=1)).isoformat()},
                **НАШ_ОТДЕЛ()}}
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


# Списания, которые не считаются потерей: плановый маркетинг. Их много и
# они запланированы, поэтому в алертах они только шумели бы, заглушая
# настоящие — «Со списанием» и «за счёт компании», которых пара в день.
НЕ_ПОТЕРЯ = ("Реклама Блогеров",)


def deletions(s, day, всё=False):
    """Удаления блюд СО СПИСАНИЕМ — поштучно, с чеком, причиной иофициантом.

    Сумму берём без скидки (DishSumInt): списанное по акции в сумме со
    скидкой выглядит бесплатным, хотя себестоимость никуда не делась.
    """
    rows = _olap(s, day,
                 ["OrderNum", "DishName", "RemovalType", "RestaurantSection",
                  "OrderWaiter.Name", "HourClose"],
                 ["DishSumInt", "DishAmountInt"],
                 {"DeletedWithWriteoff": {"filterType": "IncludeValues",
                                          "values": ["DELETED_WITH_WRITEOFF"]}})
    out = [{"чек": r.get("OrderNum"), "блюдо": r.get("DishName") or "?",
            "причина": r.get("RemovalType") or "—",
            "зал": r.get("RestaurantSection") or "—",
            "кто": r.get("OrderWaiter.Name") or "—",
            "час": r.get("HourClose") or "",
            "сумма": r.get("DishSumInt", 0) or 0,
            "штук": r.get("DishAmountInt", 0) or 0} for r in rows]
    if всё:
        return out
    return [x for x in out if x["причина"] not in НЕ_ПОТЕРЯ]


def removals(s, day):
    """Удаления блюд по причинам — соседний показатель, часто нужен вместе.

    Считаем блюда, а не чеки. Раньше здесь стояло UniqOrderId, и десять
    блюд, удалённых одним чеком, показывались как «1»: заголовок обещал
    блюда, а число отвечало на другой вопрос.
    """
    rows = _olap(s, day, ["RemovalType"], ["DishAmountInt", "DishSumInt"])
    out = {}
    for r in rows:
        причина = r.get("RemovalType")
        if not причина:
            continue
        цель = out.setdefault(причина, {"штук": 0, "сумма": 0})
        цель["штук"] += r.get("DishAmountInt", 0) or 0
        цель["сумма"] += r.get("DishSumInt", 0) or 0
    return out


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


# Сотрудники и роли меняются раз в месяц, а запрашивались на каждый день
# отдельно: экран «Правки явок» за неделю тянул их четырнадцать раз и
# работал сорок пять секунд. Держим полчаса.
_КЭШ = {}
_КЭШ_ЖИВЁТ = 1800


def _справочник(s, путь, тег):
    ключ = (s.host, путь)
    свежий = _КЭШ.get(ключ)
    if свежий and time.time() - свежий[0] < _КЭШ_ЖИВЁТ:
        return свежий[1]
    try:
        xml = cappi._get(f"{s.host}/resto/api/{путь}?key={s.key}", timeout=90)
        d = {x.findtext("id"): x.findtext("name") for x in ET.fromstring(xml).iter(тег)}
    except Exception:
        return (свежий or (0, {}))[1]      # лучше устаревшее, чем пустое
    _КЭШ[ключ] = (time.time(), d)
    return d


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
    """Всё, из чего складывается отчёт за день.

    Запросы идут параллельно: они друг от друга не зависят, а по очереди
    складывались в полминуты ожидания на каждый просмотр отчёта. Syrve
    отвечает на них одновременно без возражений.

    Каждый кусок ловит свою ошибку сам: отвалившаяся Loopa не должна
    оставлять человека вообще без выручки и заказов.
    """
    day = day or date.today()
    d = {"день": day}

    def безопасно(имя, функция, запасное=None):
        try:
            return имя, функция()
        except Exception as e:
            return имя, (запасное if запасное is not None
                         else {"ошибка": str(e)[:120]})

    with cappi.Syrve() as s:
        задачи = [
            ("продажи", lambda: sales(s, day)),
            ("точки", lambda: sales_by_point(s, day)),
            ("отмены", lambda: cancels(s, day)),
            ("удаления", lambda: removals(s, day)),
            ("месяц_факт", lambda: month_to_date(s, day)),
            ("чеков", lambda: orders_count(s, day)),
            ("смена", lambda: attendance(s, day)),
            ("акции", lambda: _акции(s, day)),
            ("жалобы", lambda: complaints(day)),
            ("живые", lambda: live_orders(day)),
        ]
        with ThreadPoolExecutor(max_workers=len(задачи)) as пул:
            for имя, значение in пул.map(lambda з: безопасно(з[0], з[1]), задачи):
                d[имя] = значение

    d["зоны"] = webhook.zones_summary(day)
    d["план"], d["план_точки"], d["план_откуда"] = plan_for(day)
    d["месяц_план"], d["месяц_всего"] = месячный_план(day)
    return d


def _акции(s, day):
    import promo
    св = promo.сводка(s, day, promo.акционные())
    return {"сумма": св["сумма"], "доля": св["доля"],
            "позиций": len(св["позиции"]),
            "без_продаж": len(св["не_продавались"])}


def счёт(n, один, два, много):
    """«2 раз» и «1 ещё закрыты» читаются как машинный перевод. Отчёт
    читают каждый день — пусть он будет написан по-русски."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        сл = один
    elif n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        сл = два
    else:
        сл = много
    return f"{n} {сл}"


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

    товары = p["по_типам"].get("GOODS") or {"чеки": 0, "штук": 0}
    строки += ["",
               f"📦 Заказов: <b>{d.get('чеков') or p['блюда']['чеки']}</b>",
               f"🍱 Блюд: <b>{p['блюда_и_товары']['штук']:,.0f}</b>".replace(",", " "),
               f"    блюда: {p['блюда']['чеки']} в заказах · "
               f"{p['блюда']['штук']:.0f} шт   ·   "
               f"товары: {товары['чеки']} в заказах · {товары['штук']:.0f} шт"]

    if d["отмены"]:
        всего = sum(d["отмены"].values())
        строки += ["", f"❌ Отмен: <b>{всего}</b>"]
        for причина, n in sorted(d["отмены"].items(), key=lambda x: -x[1]):
            строки.append(f"    {причина} — {n}")
    else:
        строки += ["", "❌ Отмен: <b>0</b>"]

    if d["удаления"]:
        всего = sum(v["штук"] for v in d["удаления"].values())
        строки += ["", f"🗑 Удалено блюд: <b>{всего:.0f}</b>"]
        for причина, v in sorted(d["удаления"].items(), key=lambda x: -x[1]["штук"]):
            деньги = f", {money(v['сумма'])} ₴" if v["сумма"] else ""
            строки.append(f"    {причина} — {v['штук']:.0f}{деньги}")

    ак = d.get("акции") or {}
    if ак.get("позиций"):
        строки += ["", f"🏷 Акционные: <b>{money(ак['сумма'])} ₴</b> · "
                       f"{ак['доля']:.0f}% выручки"]
        if ак["без_продаж"]:
            строки.append(f"    <i>не продавались: {ак['без_продаж']} из "
                          f"{ак['позиций']}</i>")

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
            строки.append(f"    {повод_по_русски(повод)} — {n}")
        if len(ж["по_точкам"]) > 1:
            строки.append("    " + " · ".join(
                f"{т} {n}" for т, n in sorted(ж["по_точкам"].items(), key=lambda x: -x[1])))

    з = d.get("зоны") or {}
    if з.get("закрытий"):
        часы = з["минут"] / 60
        строки += ["", f"🚧 Закрытий зон: <b>{з['закрытий']}</b>"
                       f"  ·  <b>{часы:.1f} ч</b> суммарно"]
        for район, r in sorted(з["по_районам"].items(), key=lambda x: -x[1]["минут"]):
            строки.append(f"    {район} — {счёт(r['раз'], 'раз', 'раза', 'раз')}, "
                          f"{r['минут']:.0f} мин")
        хвост = []
        if з["ещё_закрыты"]:
            хвост.append(счёт(з["ещё_закрыты"], "зона ещё закрыта",
                               "зоны ещё закрыты", "зон ещё закрыты")
                         + " — время по плану, не факт")
        if з["авто"]:
            хвост.append(f"{з['авто']} закрыты автоматически")
        if хвост:
            строки.append(f"    <i>{'; '.join(хвост)}</i>")

    ж = d.get("живые", {})
    if "ошибка" in ж:
        строки += ["", f"⚠️ Живые заказы недоступны: {ж['ошибка']}"]
    elif ж and (live or ж["в_работе"]):
        строки += ["", f"🚚 Заказов в работе: <b>{ж['в_работе']}</b> из {ж['всего']}"]
        for st, n in sorted(ж["статусы"].items(), key=lambda x: -x[1]):
            метка = " ←" if st in В_РАБОТЕ else ""
            строки.append(f"    {статус(st)} — {n}{метка}")
    elif ж:
        # Прошедший день: «в работе 0» — не новость, а вот чем кончились
        # заказы, знать полезно.
        отменено = ж["статусы"].get("Cancelled", 0)
        строки += ["", f"🚚 Доставок: <b>{ж['всего']}</b>"
                       + (f" · отменено {отменено}" if отменено else "")]

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


# ------------------------------------------------------------------ негода
# Компенсация за непогоду — отдельная позиция, которую Джамшут включает,
# когда доставка дорожает: дождь, шторм, гололёд.
#
# Она положена не всем. Самовывоз ничего не везёт, а заказы Glovo везёт
# сам Glovo по своим тарифам — там компенсацию не берут. Считать её долю
# от всех заказов подряд бессмысленно: получится «половина заказов мимо»
# там, где всё правильно.
КОД_НЕГОДЫ = "02771"

# Как отличить заказ Glovo. Ни одна метка по отдельности не покрывает все:
# за 1–10.09 из 160 заказов Glovo у 104 есть и позиция «G Доставка», и
# оплата «Глово», у 43 — только позиция (курьеру Glovo платят наличными),
# у 13 — только оплата, включая «Самовивіз ГЛОВО», где доставки нет вовсе.
# Поэтому считаем заказ глововским, если сработала любая из меток.
#
# «GetOrder» в источнике для этого не годится: под ним идут все заказы из
# приложения, обычные наличные в том числе.
КОД_ДОСТАВКИ_ГЛОВО = "00984"          # «G Доставка»
ОПЛАТА_ГЛОВО = ("Глово_Каппи (безнал)", "Глово-готівка")


def это_глово(заказ):
    return (КОД_ДОСТАВКИ_ГЛОВО in заказ["коды"]
            or any("лово" in о for о in заказ["оплаты"])
            or "ГЛОВО" in (заказ.get("тип") or "").upper())


def заказы_с_негодой(s, day=None):
    """Свои курьерские заказы за день: когда приняты и взята ли компенсация.

    По заказам, а не по часам: режим включают и выключают посреди часа, и
    почасовая доля показывает «50%» там, где на самом деле было сто
    процентов до выключения и ноль после.

    Glovo и самовывоз отсеиваются здесь, в Python, а не фильтром OLAP:
    фильтр режет строки, а не заказы, и «заказы без позиции G» через него
    не выразить.
    """
    day = day or date.today()
    body = {
        "reportType": "SALES", "buildSummary": False,
        "groupByRowFields": ["OrderNum", "OpenTime", "DishCode", "PayTypes",
                             "OrderType", "Delivery.ServiceType"],
        "aggregateFields": ["DishDiscountSumInt"],
        "filters": {
            "OpenDate.Typed": {"filterType": "DateRange", "periodType": "CUSTOM",
                               "from": day.isoformat(),
                               "to": (day + timedelta(days=1)).isoformat()},
            **НАШ_ОТДЕЛ(), **НЕ_УДАЛЁННЫЕ,
        },
    }
    r = cappi._post(f"{s.host}/resto/api/v2/reports/olap?key={s.key}",
                    body, timeout=180)
    заказы = {}
    for строка in r.get("data", []):
        номер = строка.get("OrderNum")
        з = заказы.setdefault(номер, {
            "номер": номер, "время": строка.get("OpenTime") or "",
            "негода": False, "сумма": 0, "коды": set(), "оплаты": set(),
            "тип": строка.get("OrderType"),
            "услуга": строка.get("Delivery.ServiceType")})
        з["коды"].add(str(строка.get("DishCode")))
        if строка.get("PayTypes"):
            з["оплаты"].add(строка["PayTypes"])
        if str(строка.get("DishCode")) == КОД_НЕГОДЫ:
            з["негода"] = True
            з["сумма"] += строка.get("DishDiscountSumInt") or 0
    свои = [з for з in заказы.values()
            if з.get("услуга") == "COURIER" and not это_глово(з)]
    return sorted(свои, key=lambda з: з["время"])


def негода_сейчас(s, day=None, окно_минут=60):
    """Похоже ли, что режим непогоды включён, и берут ли компенсацию.

    Включённым считаем, если компенсация была хотя бы в одном заказе за
    последний час: спросить у Джамшута напрямую пока нельзя — ручки нет.
    """
    day = day or date.today()
    заказы = заказы_с_негодой(s, day)
    порог = (datetime.now() - timedelta(minutes=окно_минут)).strftime("%H:%M")
    свежие = [з for з in заказы if з["время"][11:16] >= порог]
    с_негодой = [з for з in свежие if з["негода"]]
    без = [з for з in свежие if not з["негода"]]
    # Пропущенными считаем только те, что пришли между первым и последним
    # компенсированным: до включения и после выключения их брать не за что.
    пропущены = []
    if с_негодой:
        начало, конец = с_негодой[0]["время"], с_негодой[-1]["время"]
        пропущены = [з for з in без if начало < з["время"] < конец]
    return {
        "включена": bool(с_негодой),
        "взяли": с_негодой,
        "пропущены": пропущены,
        "бесплатные": [з for з in с_негодой if з["сумма"] <= 0],
        "всего_свежих": len(свежие),
        "за_день": [з for з in заказы if з["негода"]],
    }


# ------------------------------------------------------------ время работы
# Цепочка «заказ принят → еда готова → курьер доехал» разбита на три куска,
# и у каждого свой хозяин: кухня, админ, курьер. Общее «время доставки»
# ничего не говорит о том, кто именно тормозит, — поэтому считаем по частям.
#
# Админ считается вычитанием: время в пречеке включает дорогу, а работа
# админа — это то, что осталось, когда дорогу вычли.
ЦЕЛИ_ВРЕМЕНИ = {
    "кухня":   {"Лазарева": 25.0, "Левітана": 20.0},
    "админ":   {"Лазарева": 15.0, "Левітана": 15.0},
    "курьер":  {"Лазарева": 18.0, "Левітана": 18.0},
}

# Только блюда и только неудалённые заказы: удалённый заказ никто не готовил,
# а товар со склада не проходит через кухню и занижал бы её время.
def ТОЛЬКО_БЛЮДА():
    """Функция, а не константа: отдел читается из конфига, и трогать его
    на импорте значит ронять бот при старте вместо понятного отказа."""
    return {**НАШ_ОТДЕЛ(), **НЕ_УДАЛЁННЫЕ,
            "DishType": {"filterType": "IncludeValues", "values": ["DISH"]}}


def цель(что, филиал):
    return ЦЕЛИ_ВРЕМЕНИ.get(что, {}).get(филиал)


def _время_olap(s, с, по, поля):
    body = {
        "reportType": "SALES", "buildSummary": False,
        "groupByRowFields": поля,
        "aggregateFields": ["Cooking.KitchenTime.Avg",
                            "OrderTime.AveragePrechequeTime",
                            "Delivery.WayDurationAvg", "UniqOrderId"],
        "filters": {
            "OpenDate.Typed": {"filterType": "DateRange", "periodType": "CUSTOM",
                               "from": с.isoformat(),
                               "to": (по + timedelta(days=1)).isoformat()},
            **ТОЛЬКО_БЛЮДА(),
        },
    }
    r = cappi._post(f"{s.host}/resto/api/v2/reports/olap?key={s.key}",
                    body, timeout=180)
    return r.get("data", [])


def _строка_времени(r):
    кухня = (r.get("Cooking.KitchenTime.Avg") or 0) / 60      # отдаётся в секундах
    пречек = r.get("OrderTime.AveragePrechequeTime") or 0
    путь = r.get("Delivery.WayDurationAvg") or 0
    админ = пречек - путь
    return {"кухня": кухня, "админ": админ, "курьер": путь,
            "доставка": кухня + админ + путь, "пречек": пречек,
            "заказов": r.get("UniqOrderId") or 0}


def времена(s, с, по):
    """Средние времена по филиалам за период.

    Период спрашиваем у Syrve целиком, а не складываем дневные средние:
    среднее средних врёт тем сильнее, чем неровнее загрузка по дням, а
    неровная она всегда — пятница и вторник несопоставимы.
    """
    out = {}
    for r in _время_olap(s, с, по, ["RestaurantSection"]):
        имя = СЛИВАТЬ.get(r.get("RestaurantSection"), r.get("RestaurantSection"))
        точка = ТОЧКИ.get(имя)
        if not точка:
            continue
        было = out.get(точка)
        новое = _строка_времени(r)
        if было is None:
            out[точка] = новое
        else:
            # Махачкалинская вливается в Лазареву: складываем средние с
            # весом по числу заказов, иначе маленькая точка перевесит.
            n1, n2 = было["заказов"], новое["заказов"]
            всего = n1 + n2 or 1
            out[точка] = {k: (было[k] * n1 + новое[k] * n2) / всего
                          for k in ("кухня", "админ", "курьер", "доставка",
                                    "пречек")}
            out[точка]["заказов"] = всего
    return out


def времена_по_дням(s, с, по):
    """Разбивка по дням — чтобы видеть не «в среднем плохо», а какой день."""
    out = {}
    for r in _время_olap(s, с, по, ["OpenDate.Typed", "RestaurantSection"]):
        имя = СЛИВАТЬ.get(r.get("RestaurantSection"), r.get("RestaurantSection"))
        точка = ТОЧКИ.get(имя)
        д = (r.get("OpenDate.Typed") or "")[:10]
        if not точка or not д:
            continue
        out.setdefault(д, {})[точка] = _строка_времени(r)
    return dict(sorted(out.items()))


def времена_по_неделям(s, недель=8):
    """По неделям: каждую неделю спрашиваем отдельно, снова чтобы не
    усреднять средние."""
    сегодня = date.today()
    старт = сегодня - timedelta(days=сегодня.weekday() + 7 * (недель - 1))
    периоды = []
    for i in range(недель):
        н = старт + timedelta(days=7 * i)
        if н > сегодня:
            break
        периоды.append((н, min(н + timedelta(days=6), сегодня)))
    return _времена_периодов(s, периоды, lambda н, к: н.isoformat())


def времена_по_месяцам(s, месяцев=6):
    сегодня = date.today()
    г, м = сегодня.year, сегодня.month
    периоды = []
    for _ in range(месяцев):
        периоды.append((г, м))
        г, м = (г - 1, 12) if м == 1 else (г, м - 1)
    границы = []
    for г, м in reversed(периоды):
        н = date(г, м, 1)
        к = min(date(г + (м == 12), м % 12 + 1, 1) - timedelta(days=1), сегодня)
        границы.append((н, к))
    return _времена_периодов(s, границы, lambda н, к: f"{н.year}-{н.month:02d}")


def _времена_периодов(s, периоды, ключ):
    """Несколько периодов сразу. По очереди шесть месяцев — это шесть
    запросов подряд и пять секунд ожидания на кнопку, которую жмут, чтобы
    быстро посмотреть динамику."""
    if not периоды:
        return {}
    with ThreadPoolExecutor(max_workers=min(len(периоды), 8)) as пул:
        точки = list(пул.map(lambda п: времена(s, п[0], п[1]), периоды))
    return {ключ(н, к): {"с": н, "по": к, "точки": т}
            for (н, к), т in zip(периоды, точки)}
