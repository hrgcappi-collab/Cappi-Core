#!/usr/bin/env python3
"""Читающая ручка в Syrve для любой сессии, у которой нет ключей.

    python3 айка.py что [аргументы...]        → JSON в stdout

Зачем это есть. Ключи живут в двух местах — на макбуке и на сервере, — и
копировать их куда-то ещё нельзя: пароль торговой точки умеет создавать
приказы о ценах. Облачная сессия, у которой нет ни того ни другого,
ходит сюда по ssh с ключом, которому разрешена ровно одна команда: этот
файл. Ключи остаются на сервере, наружу уходит только ответ.

**Здесь нет и не должно быть ни одной операции записи.** Ни приказов, ни
стоп-листа, ни техкарт, ни настроек. Если понадобится что-то менять —
это делает человек через бота, с карточкой подтверждения. Добавлять сюда
запись значит обойти все проверки, ради которых карточка существует.
"""
import datetime as dt
import json
import sys

sys.path.insert(0, __import__("os").path.dirname(__file__) or ".")
import cappi
import report

МАКС_ДНЕЙ = 92


def _день(s, умолчание=None):
    if not s:
        return умолчание
    return dt.date.fromisoformat(s)


def _период(args, по_умолчанию=7):
    """«с по» или «за N дней назад». Конец включительно."""
    сегодня = dt.date.today()
    if len(args) >= 2 and "-" in args[0]:
        с, по = _день(args[0]), _день(args[1])
    elif args and args[0].isdigit():
        по = сегодня
        с = по - dt.timedelta(days=int(args[0]))
    else:
        по = сегодня
        с = по - dt.timedelta(days=по_умолчанию)
    if (по - с).days > МАКС_ДНЕЙ:
        raise SystemExit(json.dumps(
            {"ошибка": f"период больше {МАКС_ДНЕЙ} дней"}, ensure_ascii=False))
    return с, по


def продажи(args):
    """продажи [с по | N] [--разрез DishName,...] [--считать DishAmountInt,...]"""
    разрез = ["DishName"]
    считать = ["DishAmountInt", "DishDiscountSumInt"]
    отбор = ""
    чистые = []
    i = 0
    while i < len(args):
        if args[i] == "--разрез":
            разрез = args[i + 1].split(","); i += 2
        elif args[i] == "--считать":
            считать = args[i + 1].split(","); i += 2
        elif args[i] == "--отбор":
            отбор = args[i + 1]; i += 2
        else:
            чистые.append(args[i]); i += 1
    с, по = _период(чистые)
    with cappi.Syrve() as s:
        r = report.свободный_запрос(s, разрез, считать, с, по, отбор=отбор)
    return {"период": [str(с), str(по)], "разрез": r["разрез"],
            "считать": r["считать"], "строк": len(r["строки"]), "строки": r["строки"]}


def цены(args):
    """цены — действующий прейскурант нашего подразделения"""
    день = _день(args[0] if args else None, dt.date.today())
    with cappi.Syrve() as s:
        сырые = s.prices(день.isoformat())
        товары = {p["id"]: p for p in s.products()}
    out = []
    for p in сырые:
        if p.get("departmentId") != cappi.отдел():
            continue
        живые = [x for x in (p.get("prices") or []) if x.get("included")]
        if not живые:
            continue
        т = товары.get(p["productId"]) or {}
        out.append({"код": т.get("num"), "название": т.get("name"),
                    "id": p["productId"], "цена": живые[-1]["price"],
                    "тип": т.get("type")})
    return {"дата": str(день), "подразделение": cappi.отдел_имя(),
            "позиций": len(out), "позиции": out}


def себестоимость(args):
    """себестоимость [с по | N] — фактическая по продажам, с фудкостом"""
    с, по = _период(args, 28)
    with cappi.Syrve() as s:
        body = {"reportType": "SALES", "buildSummary": False,
                "groupByRowFields": ["DishCode", "DishName", "DishCategory"],
                "aggregateFields": ["DishAmountInt", "DishDiscountSumInt",
                                    "DishSumInt", "ProductCostBase.ProductCost"],
                "filters": {"OpenDate.Typed": {"filterType": "DateRange",
                                               "periodType": "CUSTOM",
                                               "from": с.isoformat(),
                                               "to": (по + dt.timedelta(1)).isoformat()},
                            **report.НАШ_ОТДЕЛ(), **report.НЕ_УДАЛЁННЫЕ,
                            **report.ЕДА}}
        строки = cappi.олап(cappi._post(
            f"{s.host}/resto/api/v2/reports/olap?key={s.key}", body, timeout=180))
    out = []
    for r in строки:
        шт = r.get("DishAmountInt") or 0
        if not шт:
            continue
        сс = (r.get("ProductCostBase.ProductCost") or 0) / шт
        реал = (r.get("DishDiscountSumInt") or 0) / шт
        меню = (r.get("DishSumInt") or 0) / шт
        out.append({"код": r.get("DishCode"), "название": r.get("DishName"),
                    "категория": r.get("DishCategory"), "продано": шт,
                    "цена_меню": round(меню, 2), "цена_факт": round(реал, 2),
                    "себестоимость": round(сс, 2),
                    "фудкост_%": round(сс / реал * 100, 1) if реал else None})
    return {"период": [str(с), str(по)], "позиций": len(out), "позиции": out}


def наборы(args):
    """наборы — цена каждого набора против суммы цен его частей"""
    день = dt.date.today()
    with cappi.HQ() as hq:
        карты = hq.техкарты(день.isoformat(), (день + dt.timedelta(1)).isoformat())
        товары = hq.products()
    по_id = {p["id"]: p for p in товары}
    with cappi.Syrve() as s:
        прайс = {}
        for p in s.prices(день.isoformat()):
            if p.get("departmentId") != cappi.отдел():
                continue
            живые = [x for x in (p.get("prices") or []) if x.get("included")]
            if живые:
                прайс[p["productId"]] = живые[-1]["price"]
    # Последняя карта на продукт: их бывает несколько с разных дат.
    карта = {}
    for c in карты:
        pid = c.get("assembledProductId")
        if not pid:
            continue
        if pid not in карта or (c.get("dateFrom") or "") > (карта[pid].get("dateFrom") or ""):
            карта[pid] = c
    out = []
    for pid, c in карта.items():
        p = по_id.get(pid)
        цена = прайс.get(pid)
        if not p or p.get("type") != "DISH" or not цена:
            continue
        состав = []
        for i in (c.get("items") or []):
            d = по_id.get(i.get("productId"))
            if d and d.get("type") == "DISH" and прайс.get(d["id"]):
                кол = i.get("amountOut") or i.get("amountIn") or 1
                состав.append({"блюдо": d["name"].strip(), "цена": прайс[d["id"]],
                               "количество": кол if кол and кол < 10 else 1})
        if len(состав) < 2:
            continue
        сумма = sum(x["цена"] * x["количество"] for x in состав)
        out.append({"набор": p["name"].strip(), "цена": цена,
                    "сумма_частей": round(сумма),
                    "скидка_%": round((1 - цена / сумма) * 100, 1) if сумма else 0,
                    "состав": состав})
    out.sort(key=lambda x: x["скидка_%"])
    return {"дата": str(день), "наборов": len(out), "наборы": out}


def заказы(args):
    """заказы [дата] — живые заказы доставки за день из Cloud"""
    день = _день(args[0] if args else None, dt.date.today())
    return report.live_orders(день)


def стоп(args):
    """стоп — что сейчас в стоп-листе"""
    import stoplist
    позиции = stoplist.список()
    return {"позиций": len(позиции), "позиции": позиции}


def витрины(args):
    """витрины — где цена на сайте или в Glovo не совпадает с Syrve"""
    import bot
    плохие, всего = bot.расхождения_витрин()
    return {"проверено": всего, "расхождений": len(плохие), "позиции": плохие}


def здоровье(args):
    """здоровье — отвечают ли источники"""
    import subprocess
    import os
    r = subprocess.run([sys.executable,
                        os.path.join(os.path.dirname(__file__) or ".", "healthcheck.py")],
                       capture_output=True, text=True, timeout=120)
    строки = [s.strip() for s in (r.stdout or "").splitlines() if s.strip()[:1] in ("✅", "❌", "⏭")]
    return {"проверок": len(строки), "строки": строки, "код": r.returncode}


ЧТО = {"продажи": продажи, "цены": цены, "себестоимость": себестоимость,
       "наборы": наборы, "заказы": заказы, "стоп": стоп, "витрины": витрины,
       "здоровье": здоровье}


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "помощь"):
        print(json.dumps({
            "что умеет": {и: (ф.__doc__ or "").strip().splitlines()[0]
                          for и, ф in ЧТО.items()},
            "период": "«с по» датами или «N» — сколько дней назад; "
                      f"не больше {МАКС_ДНЕЙ} дней",
            "записи нет": "эта ручка только читает; менять цены может "
                          "только человек через бота",
        }, ensure_ascii=False, indent=1))
        return 0
    имя = sys.argv[1]
    ф = ЧТО.get(имя)
    if not ф:
        print(json.dumps({"ошибка": f"не знаю «{имя}»",
                          "есть": sorted(ЧТО)}, ensure_ascii=False))
        return 2
    try:
        print(json.dumps(ф(sys.argv[2:]), ensure_ascii=False, default=str))
    except cappi.ВнешнийСбой as e:
        print(json.dumps({"ошибка": "источник не отвечает", "детали": str(e)},
                         ensure_ascii=False))
        return 1
    except Exception as e:
        print(json.dumps({"ошибка": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
