#!/usr/bin/env python3
"""База клієнтів по блоках середнього чека — вивантаження у TSV.

    python3 аналитика/клієнти.py [днів]        # за замовчуванням 90

Кладе файли у `выгрузка/` — вона в .gitignore, бо це телефони гостей.
Джерело — OLAP Syrve по `Delivery.CustomerPhone`. Фільтр «тільки їжа»
обов'язковий: без нього UniqOrderId задвоює кожну доставку службовим
рядком (див. CLAUDE.md).
"""
import os
import re
import sys
import datetime as dt

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import cappi
import report

БЛОКИ = [("до 400", 0, 400, "до400"), ("400–499", 400, 500, "400-499"),
         ("500–699", 500, 700, "500-699"), ("700–999", 700, 1000, "700-999"),
         ("1000+", 1000, 10 ** 9, "1000плюс")]
# Порог, за яким номер швидше за все не гостьовий: маленький чек і багато
# замовлень — так виглядає персонал, а не людина, що замовляє додому.
СЛУЖБОВИЙ_ЧЕК, СЛУЖБОВИХ_ЗАМОВЛЕНЬ = 250, 10
КУДИ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "выгрузка")


def зібрати(днів=90):
    по = dt.date.today()
    с = по - dt.timedelta(days=днів)
    with cappi.Syrve() as s:
        body = {"reportType": "SALES", "buildSummary": False,
                "groupByRowFields": ["Delivery.CustomerPhone"],
                "aggregateFields": ["UniqOrderId", "DishDiscountSumInt",
                                    "DishAmountInt"],
                "filters": {"OpenDate.Typed": {"filterType": "DateRange",
                                               "periodType": "CUSTOM",
                                               "from": с.isoformat(),
                                               "to": (по + dt.timedelta(1)).isoformat()},
                            **report.НАШ_ОТДЕЛ(), **report.НЕ_УДАЛЁННЫЕ,
                            **report.ЕДА}}
        рядки = cappi.олап(cappi._post(
            f"{s.host}/resto/api/v2/reports/olap?key={s.key}", body, timeout=180))
    клієнти = []
    for r in рядки:
        тел = re.sub(r"\D", "", str(r.get("Delivery.CustomerPhone") or ""))
        n = int(r.get("UniqOrderId") or 0)
        сума = r.get("DishDiscountSumInt") or 0
        if len(тел) < 9 or n <= 0:
            continue
        чек = сума / n
        блок = next(имя for имя, a, b, _ in БЛОКИ if a <= чек < b)
        клієнти.append({
            "тел": "+" + тел, "замовлень": n, "сума": сума, "чек": чек,
            "блок": блок,
            "мітка": "службовий?" if (чек < СЛУЖБОВИЙ_ЧЕК
                                      and n >= СЛУЖБОВИХ_ЗАМОВЛЕНЬ) else "",
        })
    клієнти.sort(key=lambda k: -k["сума"])
    return клієнти, с, по


def записати(шлях, клієнти):
    os.makedirs(os.path.dirname(шлях), exist_ok=True)
    with open(шлях, "w") as f:
        f.write("Телефон\tЗамовлень\tСума, ₴\tСередній чек, ₴\tБлок\tПримітка\n")
        for k in клієнти:
            f.write(f"{k['тел']}\t{k['замовлень']}\t{k['сума']:.0f}\t"
                    f"{k['чек']:.0f}\t{k['блок']}\t{k['мітка']}\n")
    return len(клієнти)


def main():
    днів = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    клієнти, с, по = зібрати(днів)
    гроші = sum(k["сума"] for k in клієнти)
    замовлень = sum(k["замовлень"] for k in клієнти)
    print(f"період {с} … {по} ({днів} днів)")
    print(f"клієнтів {len(клієнти)} · замовлень {замовлень} · "
          f"сума {гроші:,.0f} ₴".replace(",", " "))
    основа = os.path.join(КУДИ, f"клієнти-{днів}днів")
    записати(f"{основа}-усі.tsv", клієнти)
    print(f"\n{'блок':<10}{'клієнтів':>10}{'замовлень':>11}{'сума':>14}"
          f"{'сер. чек':>11}{'грошей':>10}   файл")
    for имя, a, b, файл in БЛОКИ:
        гр = [k for k in клієнти if k["блок"] == имя]
        if not гр:
            continue
        з = sum(k["замовлень"] for k in гр)
        с_ = sum(k["сума"] for k in гр)
        записати(f"{основа}-{файл}.tsv", гр)
        print(f"{имя:<10}{len(гр):>10}{з:>11}{с_:>14,.0f}{с_ / з:>11,.0f}"
              f"{с_ / гроші * 100:>9.1f}%   клієнти-{днів}днів-{файл}.tsv"
              .replace(",", " "))
    службові = [k for k in клієнти if k["мітка"]]
    if службові:
        print(f"\nпозначено «службовий?»: {len(службові)} номерів "
              f"(чек < {СЛУЖБОВИЙ_ЧЕК} ₴ і {СЛУЖБОВИХ_ЗАМОВЛЕНЬ}+ замовлень)")
    разових = sum(1 for k in клієнти if k["замовлень"] == 1)
    print(f"разових клієнтів: {разових} · повторних: {len(клієнти) - разових}")


if __name__ == "__main__":
    main()
