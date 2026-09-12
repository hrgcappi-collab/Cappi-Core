#!/usr/bin/env python3
"""Выгрузка отчётов из Syrve в плоские CSV: одна строка — один день.

Шесть отчётов, каждый в свой файл. Никаких сводных заголовков и
объединённых ячеек: файл должен открываться чем угодно и грузиться в
любую таблицу без ручной чистки.

Данные берутся помесячно: спросить два с половиной года одним запросом
Syrve не даст, а по дням — это тысяча запросов вместо тридцати.

Источник заказа считается по заказам, а не по строкам: одна и та же
позиция может дать несколько строк отчёта, и складывать по ним заказы
нельзя. Glovo определяется по объединению двух меток — позиция
«G Доставка» и глововский тип оплаты: по отдельности ни одна не
покрывает все заказы.
"""
import csv
import os
import sys
from datetime import date, datetime, timedelta

import cappi
import report

КУДА = os.path.join(os.path.dirname(os.path.abspath(__file__)), "выгрузка")

# Только блюда: товары со склада (напитки в банках, сигареты, посуда) в
# эти отчёты не идут.
ТОЛЬКО_БЛЮДО = {"DishType": {"filterType": "IncludeValues", "values": ["DISH"]}}

# Посёлок (концепция «3 Поселок», она же Зал Махачкалинская) не
# учитываем: точка работала с января 2024 по 16.03.2025 и закрылась, и в
# отчётах её данные только мешают сравнивать периоды. В боте она
# сливается с Лазаревой — здесь отбрасывается целиком.
ТОЧКИ = {"Зал Лазарева": "Лазарева", "Зал Левитана": "Левітана"}


def точка(название):
    return ТОЧКИ.get(название)
КОД_ДОСТАВКИ_ГЛОВО = "00984"
ОПЛАТА_ГЛОВО = ["Глово_Каппи (безнал)", "Глово-готівка"]


def месяцы(с, по):
    """Границы месяцев в периоде: [(начало, конец_исключительно), …]."""
    т = date(с.year, с.month, 1)
    out = []
    while т <= по:
        след = date(т.year + (т.month == 12), т.month % 12 + 1, 1)
        out.append((max(т, с), min(след, по + timedelta(days=1))))
        т = след
    return out


def олап(s, поля, агрегаты, с, по, фильтры=None, тип="SALES"):
    поле_даты = "OpenDate.Typed" if тип == "SALES" else "DateTime.DateTyped"
    body = {"reportType": тип, "buildSummary": False,
            "groupByRowFields": list(поля), "aggregateFields": list(агрегаты),
            "filters": {поле_даты: {"filterType": "DateRange",
                                    "periodType": "CUSTOM",
                                    "from": с.isoformat(), "to": по.isoformat()},
                        **report.НАШ_ОТДЕЛ(), **(фильтры or {})}}
    return cappi.олап(cappi._post(f"{s.host}/resto/api/v2/reports/olap?key={s.key}",
                       body, timeout=300))


def заказы_глово(s, с, по):
    """Номера заказов Glovo: по позиции доставки и по типу оплаты."""
    номера = set()
    for фильтр in ({"DishCode": {"filterType": "IncludeValues",
                                 "values": [КОД_ДОСТАВКИ_ГЛОВО]}},
                   {"PayTypes": {"filterType": "IncludeValues",
                                 "values": ОПЛАТА_ГЛОВО}}):
        for r in олап(s, ["OrderNum"], ["UniqOrderId"], с, по, фильтр):
            if r.get("OrderNum") is not None:
                номера.add(r["OrderNum"])
    return номера


def источник(тип_заказа, глово):
    if глово:
        return "Glovo"
    if тип_заказа and "амовив" in тип_заказа:
        return "самовывоз"
    return "доставка"


def писать(имя, поля, строки):
    os.makedirs(КУДА, exist_ok=True)
    путь = os.path.join(КУДА, имя)
    with open(путь, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=поля, delimiter=";")
        w.writeheader()
        for с in строки:
            w.writerow(с)
    print(f"  {имя}: {len(строки)} строк")
    return путь


# ------------------------------------------------------------------ отчёты
def продажи_и_позиции(s, с, по):
    """Отчёты 1 и 2: заказы, суммы, скидки и позиции по дням и источникам."""
    продажи, позиции = {}, {}
    for м1, м2 in месяцы(с, по):
        глово = заказы_глово(s, м1, м2)
        строки = олап(s, ["OpenDate.Typed", "RestaurantSection", "OrderType",
                          "OrderNum"],
                      ["DishDiscountSumInt", "DishSumInt", "DiscountSum",
                       "DishAmountInt", "OrderItems"],
                      м1, м2, {**report.НЕ_УДАЛЁННЫЕ, **ТОЛЬКО_БЛЮДО})
        for r in строки:
            день = (r.get("OpenDate.Typed") or "")[:10]
            точка = ТОЧКИ.get(r.get("RestaurantSection"))
            if not день or not точка:
                continue
            ист = источник(r.get("OrderType"), r.get("OrderNum") in глово)
            ключ = (день, точка, ист)
            п = продажи.setdefault(ключ, {"заказов": set(), "сумма_со_скидкой": 0.0,
                                          "сумма_без_скидки": 0.0, "сумма_скидки": 0.0})
            п["заказов"].add(r.get("OrderNum"))
            п["сумма_со_скидкой"] += r.get("DishDiscountSumInt") or 0
            п["сумма_без_скидки"] += r.get("DishSumInt") or 0
            п["сумма_скидки"] += r.get("DiscountSum") or 0
            z = позиции.setdefault(ключ, {"заказов": set(), "позиций": 0.0})
            z["заказов"].add(r.get("OrderNum"))
            z["позиций"] += r.get("DishAmountInt") or 0
        print(f"    {м1:%Y-%m}: заказов {len(строки)}")
    п1 = [{"дата": д, "точка": т, "источник": и, "заказов": len(v["заказов"]),
           "сумма_со_скидкой": round(v["сумма_со_скидкой"], 2),
           "сумма_скидки": round(v["сумма_скидки"], 2)}
          for (д, т, и), v in sorted(продажи.items())]
    п2 = [{"дата": д, "точка": т, "источник": и, "заказов": len(v["заказов"]),
           "позиций_всего": round(v["позиций"], 2)}
          for (д, т, и), v in sorted(позиции.items())]
    return п1, п2


def отмены(s, с, по):
    """Отчёт 3: отменённые заказы с причинами."""
    out = {}
    for м1, м2 in месяцы(с, по):
        глово = заказы_глово(s, м1, м2)
        строки = олап(s, ["OpenDate.Typed", "RestaurantSection", "OrderType",
                          "OrderNum", "Delivery.CancelCause"],
                      ["DishDiscountSumInt", "DishSumInt"], м1, м2,
                      {"OrderDeleted": {"filterType": "IncludeValues",
                                        "values": ["DELETED"]}, **ТОЛЬКО_БЛЮДО})
        for r in строки:
            день = (r.get("OpenDate.Typed") or "")[:10]
            точка = ТОЧКИ.get(r.get("RestaurantSection"))
            if not день or not точка:
                continue
            ключ = (день, точка,
                    источник(r.get("OrderType"), r.get("OrderNum") in глово),
                    r.get("Delivery.CancelCause") or "не указана")
            v = out.setdefault(ключ, {"заказов": set(), "сумма": 0.0})
            v["заказов"].add(r.get("OrderNum"))
            # У отменённого заказа сумма со скидкой обнуляется — берём
            # сумму без скидки: это и есть то, что не продали.
            v["сумма"] += (r.get("DishSumInt")
                           or r.get("DishDiscountSumInt") or 0)
    return [{"дата": д, "точка": т, "источник": и,
             "отменено_заказов": len(v["заказов"]),
             "сумма": round(v["сумма"], 2), "причина": п}
            for (д, т, и, п), v in sorted(out.items())]


def гости(s, с, по):
    """Отчёт 4: новые и повторные.

    Новый — тот, чья карточка клиента заведена в этот же день: другого
    признака «первый заказ» в отчёте нет.
    """
    out = {}
    for м1, м2 in месяцы(с, по):
        строки = олап(s, ["OpenDate.Typed", "RestaurantSection", "OrderNum",
                          "Delivery.CustomerCreatedDateTyped",
                          "Delivery.CustomerPhone"],
                      ["DishDiscountSumInt"], м1, м2, {**report.НЕ_УДАЛЁННЫЕ, **ТОЛЬКО_БЛЮДО})
        for r in строки:
            день = (r.get("OpenDate.Typed") or "")[:10]
            точка = ТОЧКИ.get(r.get("RestaurantSection"))
            телефон = r.get("Delivery.CustomerPhone")
            if not день or not точка or not телефон:
                continue
            заведён = (r.get("Delivery.CustomerCreatedDateTyped") or "")[:10]
            новый = заведён == день
            v = out.setdefault((день, точка), {"новых": set(), "повторных": set(),
                                               "зак_новых": set(), "зак_повт": set()})
            (v["новых"] if новый else v["повторных"]).add(телефон)
            (v["зак_новых"] if новый else v["зак_повт"]).add(r.get("OrderNum"))
    return [{"дата": д, "точка": т, "новых_гостей": len(v["новых"]),
             "повторных": len(v["повторных"]),
             "заказов_новых": len(v["зак_новых"]),
             "заказов_повторных": len(v["зак_повт"])}
            for (д, т), v in sorted(out.items())]


def по_блюдам(s, с, по):
    """Отчёт 5: продажи блюд помесячно."""
    out = []
    for м1, м2 in месяцы(с, по):
        строки = олап(s, ["RestaurantSection", "DishName", "DishCategory",
                          "DishCode"],
                      ["DishAmountInt", "DishDiscountSumInt"], м1, м2,
                      report.НЕ_УДАЛЁННЫЕ)
        for r in строки:
            точка = ТОЧКИ.get(r.get("RestaurantSection"))
            if not точка:
                continue
            out.append({"месяц": f"{м1:%Y-%m}", "точка": точка,
                        "блюдо": r.get("DishName"),
                        "артикул": r.get("DishCode"),
                        "категория": r.get("DishCategory") or "",
                        "продано_шт": round(r.get("DishAmountInt") or 0, 3),
                        "выручка": round(r.get("DishDiscountSumInt") or 0, 2)})
        print(f"    {м1:%Y-%m}: позиций {len(строки)}")
    return out


def время_доставки(s, с, по):
    """Отчёт 6: среднее и медиана времени доставки по дням.

    Время считается от приёма заказа до закрытия, то есть включая
    кол-центр: это «Время обслуживания» в Syrve. Оно больше, чем
    «доставка без КЦ» в отчёте «Время работы» — за август 92 против 71
    минуты, и разница как раз кол-центр.

    Медианы в OLAP нет — считаем по заказам сами. Медиана важнее
    среднего: один заказ, уехавший на два часа, среднее портит, а
    медиану нет.
    """
    out = {}
    for м1, м2 in месяцы(с, по):
        # OrderLength нельзя агрегировать — она поле группировки, а не
        # сумма. Берём как есть по каждому заказу и считаем сами.
        строки = олап(s, ["OpenDate.Typed", "RestaurantSection", "OrderNum",
                          "OrderTime.OrderLength"], ["UniqOrderId"], м1, м2,
                      {**report.НЕ_УДАЛЁННЫЕ, **ТОЛЬКО_БЛЮДО,
                       "Delivery.ServiceType": {"filterType": "IncludeValues",
                                                "values": ["COURIER"]},
                       # Предзаказ «на время» создают за часы до доставки, и
                       # он превращает среднее в бессмыслицу: 128 минут там,
                       # где реально 92.
                       "OrderType": {"filterType": "ExcludeValues",
                                     "values": ["Доставка на время"]}})
        for r in строки:
            день = (r.get("OpenDate.Typed") or "")[:10]
            точка = ТОЧКИ.get(r.get("RestaurantSection"))
            длит = r.get("OrderTime.OrderLength")
            if not день or not точка or not длит or длит <= 0:
                continue
            out.setdefault((день, точка), []).append(длит)
    строки = []
    for (д, т), значения in sorted(out.items()):
        значения.sort()
        n = len(значения)
        медиана = (значения[n // 2] if n % 2
                   else (значения[n // 2 - 1] + значения[n // 2]) / 2)
        строки.append({"дата": д, "точка": т, "заказов": n,
                       "среднее_время_мин": round(sum(значения) / n, 1),
                       "медиана": round(медиана, 1)})
    return строки


ОТЧЁТЫ = {
    "1": ("01-продажи-по-источнику.csv", date(2024, 1, 1)),
    "2": ("02-позиций-в-заказе.csv", date(2025, 1, 1)),
    "3": ("03-отмены.csv", date(2025, 1, 1)),
    "4": ("04-новые-и-повторные.csv", date(2025, 1, 1)),
    "5": ("05-продажи-по-блюдам.csv", date(2025, 1, 1)),
    "6": ("06-время-доставки.csv", date(2026, 1, 1)),
}


def main():
    какие = sys.argv[1:] or list(ОТЧЁТЫ)
    по = date.today() - timedelta(days=1)
    with cappi.Syrve() as s:
        if "1" in какие or "2" in какие:
            print("1–2: продажи и позиции…")
            п1, п2 = продажи_и_позиции(s, ОТЧЁТЫ["1"][1], по)
            if "1" in какие:
                писать(ОТЧЁТЫ["1"][0],
                       ["дата", "точка", "источник", "заказов",
                        "сумма_со_скидкой", "сумма_скидки"], п1)
            if "2" in какие:
                граница = ОТЧЁТЫ["2"][1].isoformat()
                писать(ОТЧЁТЫ["2"][0],
                       ["дата", "точка", "источник", "заказов", "позиций_всего"],
                       [r for r in п2 if r["дата"] >= граница])
        if "3" in какие:
            print("3: отмены…")
            писать(ОТЧЁТЫ["3"][0],
                   ["дата", "точка", "источник", "отменено_заказов", "сумма",
                    "причина"], отмены(s, ОТЧЁТЫ["3"][1], по))
        if "4" in какие:
            print("4: новые и повторные…")
            писать(ОТЧЁТЫ["4"][0],
                   ["дата", "точка", "новых_гостей", "повторных",
                    "заказов_новых", "заказов_повторных"],
                   гости(s, ОТЧЁТЫ["4"][1], по))
        if "5" in какие:
            print("5: продажи по блюдам…")
            писать(ОТЧЁТЫ["5"][0],
                   ["месяц", "точка", "блюдо", "артикул", "категория",
                    "продано_шт", "выручка"],
                   по_блюдам(s, ОТЧЁТЫ["5"][1], по))
        if "6" in какие:
            print("6: время доставки…")
            писать(ОТЧЁТЫ["6"][0],
                   ["дата", "точка", "заказов", "среднее_время_мин", "медиана"],
                   время_доставки(s, ОТЧЁТЫ["6"][1], по))


if __name__ == "__main__":
    main()
