#!/usr/bin/env python3
"""Акционные товары и спецпредложения: что это, и как оно продаётся.

Два разных списка, и путать их не стоит.

**Акционные** бот определяет сам по сайту: у позиции заполнено `price_cross` —
перечёркнутая старая цена, ту самую, что рисуется рядом с бейджем скидки.
Список меняется без спроса, вместе с витриной, и это правильно: акция живёт
на сайте, а не в настройках бота.

**Спецпредложение** — ручной список артикулов. Бывает, что позицию нужно
держать на виду без всякой скидки: новинка, сезонное блюдо, то, что
продвигают в зале. Такое сайт не пометит, и знать об этом может только
человек.

Продажи по обоим спискам считаются одинаково — из OLAP по коду блюда.
"""
import json
import os
from datetime import timedelta

import cappi

ФАЙЛ = os.path.expanduser("~/.cappi/special.json")


class НетТакогоАртикула(Exception):
    def __init__(self, код):
        self.код = код
        super().__init__(код)


# ------------------------------------------------------------------ списки
def акционные():
    """Позиции со скидкой на сайте: код, цена, старая цена, размер скидки."""
    меню = {p["id"]: p for p in cappi.cloud_menu()}
    out = []
    for guid, v in cappi.site_prices().items():
        if not v.get("cross"):
            continue
        p = меню.get(guid) or {}
        было, стало = v["cross"], v["price"]
        out.append({"guid": guid, "код": p.get("code"), "название": v["name"],
                    "цена": стало, "было": было, "выгода": было - стало,
                    "процент": (было - стало) / было * 100 if было else 0,
                    "категория": v.get("category")})
    return sorted(out, key=lambda x: -x["выгода"])


def спецпредложения():
    try:
        return json.load(open(ФАЙЛ))
    except Exception:
        return {}


def добавить_спец(код, комментарий=None):
    """Артикул в ручной список. Название подтягиваем сами — по коду его не
    видно, а список без названий читать невозможно.

    Ищем шире, чем меню доставки: в нём 317 позиций из 1477, и всё, что
    продаётся только в зале, туда не попадает. «Креветка в кунжуті КЦ»
    (03395) существует в Syrve, но на сайте её нет — а держать такую на
    виду как раз и хотят.
    """
    меню = {p.get("code"): p for p in cappi.cloud_menu()}
    p = меню.get(str(код))
    if not p:
        with cappi.Syrve() as s:
            свои = [x for x in s.products() if str(x.get("num")) == str(код)
                    and not x.get("deleted")]
        if свои:
            p = {"name": свои[0]["name"], "id": свои[0]["id"]}
    if not p:
        # Обычный KeyError печатается в кавычках — в чате это выглядит как
        # обрывок кода. Своё исключение с чистым текстом.
        raise НетТакогоАртикула(str(код))
    d = спецпредложения()
    d[str(код)] = {"название": p["name"], "guid": p["id"],
                   "комментарий": комментарий}
    json.dump(d, open(ФАЙЛ, "w"), ensure_ascii=False, indent=1)
    return d[str(код)]


def убрать_спец(код):
    d = спецпредложения()
    if str(код) not in d:
        raise KeyError(код)
    ушло = d.pop(str(код))
    json.dump(d, open(ФАЙЛ, "w"), ensure_ascii=False, indent=1)
    return ушло


# ------------------------------------------------------------------ продажи
def продажи(s, day, коды=None):
    """Сколько продано и на сколько — по коду блюда, за учётный день.

    Без фильтра вернёт всё меню: так проще посчитать долю акционных в
    общей выручке, ради которой всё и затевается.
    """
    body = {
        "reportType": "SALES", "buildSummary": False,
        "groupByRowFields": ["DishCode", "DishName"],
        "aggregateFields": ["DishDiscountSumInt", "DishAmountInt", "UniqOrderId"],
        "filters": {"OpenDate.Typed": {
            "filterType": "DateRange", "periodType": "CUSTOM",
            "from": day.isoformat(),
            "to": (day + timedelta(days=1)).isoformat()}},
    }
    r = cappi._post(f"{s.host}/resto/api/v2/reports/olap?key={s.key}", body, timeout=150)
    out = {}
    for row in r.get("data", []):
        код = str(row.get("DishCode") or "").strip()
        if not код or (коды is not None and код not in коды):
            continue
        цель = out.setdefault(код, {"название": row.get("DishName") or "?",
                                    "сумма": 0, "штук": 0, "чеков": 0})
        цель["сумма"] += row.get("DishDiscountSumInt", 0) or 0
        цель["штук"] += row.get("DishAmountInt", 0) or 0
        цель["чеков"] += row.get("UniqOrderId", 0) or 0
    return out


def сводка(s, day, позиции):
    """Свод по списку позиций: продажи, доля в выручке, кто не продавался.

    Позиция без продаж — самое полезное здесь. Акция, которую никто не
    берёт, стоит денег и не работает, но в общей сумме её не разглядеть.
    """
    коды = {str(p["код"]) for p in позиции if p.get("код")}
    факт = продажи(s, day, коды)
    всё = продажи(s, day)
    выручка_всего = sum(v["сумма"] for v in всё.values())
    строки = []
    for p in позиции:
        f = факт.get(str(p.get("код"))) or {"сумма": 0, "штук": 0, "чеков": 0}
        строки.append({**p, **f})
    сумма = sum(x["сумма"] for x in строки)
    return {
        "позиции": sorted(строки, key=lambda x: -x["сумма"]),
        "сумма": сумма,
        "штук": sum(x["штук"] for x in строки),
        "доля": сумма / выручка_всего * 100 if выручка_всего else 0,
        "не_продавались": [x for x in строки if not x["штук"]],
        "выручка_всего": выручка_всего,
    }
