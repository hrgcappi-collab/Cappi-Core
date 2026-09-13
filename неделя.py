#!/usr/bin/env python3
"""Недельная сводка: план/факт, чек, потери, отзывы, времена, фудкост.

Запуск из терминала:
    python3 неделя.py            прошедшая неделя (пн–вс)
    python3 неделя.py 2026-09-07 неделя, в которую попадает дата

Форму задал заказчик 13.09.2026: строки — показатели, столбцы — Лазарева,
Левітана и сеть. Таблицей она и рисуется, в <pre>: три колонки цифр без
выравнивания читать невозможно.

Про времена. Здесь они считаются не из OLAP, а по отметкам самих заказов
из Cloud API: `whenCreated`, `whenConfirmed`, `whenSended`, `whenDelivered`.
Причина — в OLAP нет отметки «кол-центр принял», а без неё строку
«ожидание клиентом без КЦ» можно только выдумать. По отметкам видно, что
кол-центр занимает 3 минуты, а не 16, как выходило при вычитании
служебного времени целиком: остальные тринадцать — это сборка и ожидание
курьера, и клиент ждёт их наравне со всем прочим.

Предзаказы из времён исключены. Заказ, созданный в полдень на семь
вечера, даёт «время доставки» в семь часов и портит среднее так, что оно
перестаёт что-либо значить.
"""
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import cappi
import report

ТОЧКИ = ["Лазарева", "Левітана"]

# Премия администратора за неделю. Правило заказчика от 14.09.2026: если
# недельный план филиала выполнен — по этой ставке каждому, кто на нём
# работал, независимо от числа смен. Не выполнен — никому и ничего:
# премия за результат филиала, а не за присутствие.
КПИ_АДМИНА = 160
РОЛЬ_АДМИНА = "Администратор-кассир"
ПРЕДЗАКАЗ_МИН = 90        # «на время» — если просили больше чем на полтора часа вперёд
РАЗУМНЫЙ_ПРЕДЕЛ = 240     # минут: дольше — это не доставка, а сбой отметок


def границы(день=None):
    """Понедельник–воскресенье недели, в которую попадает день."""
    день = день or date.today()
    начало = день - timedelta(days=день.weekday())
    return начало, начало + timedelta(days=6)


def прошлая():
    начало, конец = границы(date.today() - timedelta(days=7))
    return начало, конец


def _олап(s, с, по, группы, поля, фильтры=None):
    body = {"reportType": "SALES", "buildSummary": False,
            "groupByRowFields": группы, "aggregateFields": поля,
            "filters": {"OpenDate.Typed": {"filterType": "DateRange",
                                           "periodType": "CUSTOM",
                                           "from": с.isoformat(),
                                           "to": (по + timedelta(days=1)).isoformat()},
                        **report.НАШ_ОТДЕЛ(), **(фильтры or {})}}
    return cappi.олап(cappi._post(
        f"{s.host}/resto/api/v2/reports/olap?key={s.key}", body, timeout=120))


def _точка(имя):
    return report.ТОЧКИ.get(report.СЛИВАТЬ.get(имя, имя))


def _момент(v):
    try:
        return datetime.strptime(v[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def этапы(с, по):
    """Средние по этапам заказа, по отметкам времени из Cloud.

    Неделю одним запросом Cloud не отдаёт — отвечает «too many data», —
    поэтому по дню за раз и параллельно.
    """
    c = cappi.cfg()
    tok = cappi.форма(cappi._post(f"{c['SYRVE_CLOUD_URL']}/api/1/access_token",
                                  {"apiLogin": c["SYRVE_CLOUD_API_KEY"]}, timeout=40),
                      dict, "Syrve Cloud").get("token")
    if not tok:
        raise cappi.ВнешнийСбой("Syrve Cloud", "не выдал токен")

    def день(d):
        r = cappi._post(
            f"{c['SYRVE_CLOUD_URL']}/api/1/deliveries/by_delivery_date_and_status",
            {"organizationIds": [c["SYRVE_ORG_ID"]],
             "deliveryDateFrom": f"{d} 00:00:00.000",
             "deliveryDateTo": f"{d + timedelta(days=1)} 00:00:00.000"},
            {"Authorization": f"Bearer {tok}"}, timeout=120)
        return [o.get("order") or {}
                for g in (cappi.форма(r, dict, "Syrve Cloud")
                          .get("ordersByOrganizations") or []) if isinstance(g, dict)
                for o in (g.get("orders") or []) if isinstance(o, dict)]

    дни = [с + timedelta(days=i) for i in range((по - с).days + 1)]
    with ThreadPoolExecutor(max_workers=7) as пул:
        заказы = [o for куча in пул.map(день, дни) for o in куча]

    собрано = {}
    for o in заказы:
        if o.get("status") != "Closed" or not o.get("whenDelivered"):
            continue
        создан = _момент(o.get("whenCreated"))
        доставлен = _момент(o.get("whenDelivered"))
        if not создан or not доставлен:
            continue
        к_сроку = _момент(o.get("completeBefore"))
        if к_сроку and (к_сроку - создан).total_seconds() / 60 > ПРЕДЗАКАЗ_МИН:
            continue                      # предзаказ: ждал не кухню, а свой час
        всего = (доставлен - создан).total_seconds() / 60
        if not 0 < всего < РАЗУМНЫЙ_ПРЕДЕЛ:
            continue
        имя = report.КОНЦЕПЦИИ.get((o.get("conception") or {}).get("name"))
        if not имя:
            continue
        д = собрано.setdefault(имя, {"всего": [], "кц": [], "путь": [], "кухня": []})
        д["всего"].append(всего)
        подтверждён = _момент(o.get("whenConfirmed"))
        if подтверждён:
            д["кц"].append((подтверждён - создан).total_seconds() / 60)
        отправлен = _момент(o.get("whenSended"))
        if отправлен:
            д["путь"].append((доставлен - отправлен).total_seconds() / 60)
        начало = _момент(o.get("cookingStartTime"))
        готово = _момент(o.get("whenCookingCompleted"))
        if начало and готово:
            д["кухня"].append((готово - начало).total_seconds() / 60)

    среднее = lambda з: sum(з) / len(з) if з else 0
    out = {}
    for т, д in собрано.items():
        out[т] = {"заказов": len(д["всего"]), "всего": среднее(д["всего"]),
                  "кц": среднее(д["кц"]), "путь": среднее(д["путь"]),
                  "кухня": среднее(д["кухня"])}
        out[т]["сборка"] = max(out[т]["всего"] - out[т]["кц"]
                               - out[т]["кухня"] - out[т]["путь"], 0)
        out[т]["без_кц"] = out[т]["всего"] - out[т]["кц"]
    return out


def админы(с, по):
    """Кто из администраторов на каком филиале работал и сколько смен.

    Филиал берётся из имени сотрудника: в явках подразделение у всех одно,
    «Cappi Одесса», другого источника нет. У кого филиал не разобрался —
    отдельной группой, выдумывать не станем.
    """
    дни = [с + timedelta(days=i) for i in range((по - с).days + 1)]
    with cappi.Syrve() as s:
        with ThreadPoolExecutor(max_workers=7) as пул:
            явки = list(пул.map(lambda d: report.attendance(s, d), дни))
    out = {}
    for я in явки:
        for ч in я["люди"]:
            if ч["роль"] != РОЛЬ_АДМИНА:
                continue
            ф = ч["филиал"] or "без филиала"
            имя = ч["имя"].split("(")[0].strip()
            з = out.setdefault(ф, {}).setdefault(имя, {"смен": 0, "часов": 0.0})
            з["смен"] += 1
            з["часов"] += ч["часов"]
    return out


def отзывы(с, по, ветка=None):
    """Негатив Loopa: всего, доля и разрез кухня/сервис."""
    import urllib.parse
    доп = {"branch": urllib.parse.quote(ветка)} if ветка else {}
    осн = {"from": с.isoformat(), "to": по.isoformat()}
    всего = report._loopa(**осн, **доп) or {}
    негатив = report._loopa(**осн, **доп, tone="negative") or {}
    по_поводам = report._loopa(**осн, **доп, tone="negative",
                               group_by="category") or {}
    кухня = сервис = 0
    for g in по_поводам.get("groups", []):
        ключи = [k.strip() for k in str(g.get("key", "")).replace(",", " ").split()]
        # У отзыва бывает несколько поводов сразу — «kitchen, packing».
        # Поэтому суммы по разрезам не обязаны сойтись с общим числом.
        if "kitchen" in ключи:
            кухня += g.get("count", 0)
        if {"service", "admin", "call_center"} & set(ключи):
            сервис += g.get("count", 0)
    return {"отзывов": всего.get("total", 0), "негатив": негатив.get("total", 0),
            "кухня": кухня, "сервис": сервис}


def собрать(с=None, по=None):
    с, по = (с, по) if с else прошлая()
    д = {т: {} for т in ТОЧКИ}
    сеть = {}

    with cappi.Syrve() as s:
        в = report.выручка_пиу(s, с, по)
        сеть["факт"] = в["всего"]
        for т in ТОЧКИ:
            д[т]["факт"] = в["точки"].get(т, 0)

        еда = {**report.НЕ_УДАЛЁННЫЕ, **report.ЕДА}
        for r in _олап(s, с, по, ["RestaurantSection"],
                       ["UniqOrderId", "DishAmountInt"], еда):
            т = _точка(r.get("RestaurantSection"))
            if т:
                д[т]["заказы"] = д[т].get("заказы", 0) + (r.get("UniqOrderId") or 0)
                д[т]["блюда"] = д[т].get("блюда", 0) + (r.get("DishAmountInt") or 0)
        # Сеть спрашиваем отдельно: заказ, тронувший обе точки, в сумме по
        # точкам посчитался бы дважды.
        общий = _олап(s, с, по, [], ["UniqOrderId", "DishAmountInt"], еда)
        сеть["заказы"] = sum(r.get("UniqOrderId") or 0 for r in общий)
        сеть["блюда"] = sum(r.get("DishAmountInt") or 0 for r in общий)

        # У отменённого заказа выручка нулевая по определению, поэтому
        # сумма берётся до скидки: это цена того, что не доехало.
        # «Перенос на другую точку» — не потеря: заказ выполнен вторым
        # залом и уже посчитан в его выручке.
        for r in _олап(s, с, по, ["RestaurantSection", "Delivery.CancelCause"],
                       ["UniqOrderId", "DishSumInt"]):
            причина = r.get("Delivery.CancelCause")
            т = _точка(r.get("RestaurantSection"))
            if not причина or not т:
                continue
            ключ = "перенос" if "еренос" in причина else "отказ"
            д[т][ключ] = д[т].get(ключ, 0) + (r.get("UniqOrderId") or 0)
            д[т][ключ + "_грн"] = д[т].get(ключ + "_грн", 0) + (r.get("DishSumInt") or 0)

        for r in _олап(s, с, по, ["RestaurantSection"],
                       ["ProductCostBase.ProductCost", "DishDiscountSumInt"]):
            т = _точка(r.get("RestaurantSection"))
            if т:
                д[т]["сс"] = д[т].get("сс", 0) + (r.get("ProductCostBase.ProductCost") or 0)
                д[т]["прод"] = д[т].get("прод", 0) + (r.get("DishDiscountSumInt") or 0)

    план = report.load_plan().get("weekly", {})
    for т in ТОЧКИ:
        полное = next((k for k, v in report.ТОЧКИ.items() if v == т), т)
        дни = план.get(полное, {})
        д[т]["план"] = sum(
            дни.get((с + timedelta(days=i)).isoformat())
            or дни.get(report.ДНИ[(с + timedelta(days=i)).weekday()]) or 0
            for i in range((по - с).days + 1))

    свои = админы(с, по)
    for т in ТОЧКИ:
        д[т]["админы"] = свои.get(т, {})
    сеть["админы_без_филиала"] = свои.get("без филиала", {})

    эт = этапы(с, по)
    for т in ТОЧКИ:
        д[т].update({"время": эт.get(т) or {}})
        д[т].update(отзывы(с, по, т))
    сеть.update(отзывы(с, по))
    сеть["план"] = sum(д[т]["план"] for т in ТОЧКИ)
    for к in ("отказ", "отказ_грн", "перенос", "перенос_грн", "сс", "прод"):
        сеть[к] = sum(д[т].get(к, 0) for т in ТОЧКИ)
    # Времена по сети — с весом по числу заказов: иначе меньшая точка
    # весит столько же, сколько большая.
    вес = {т: (д[т]["время"] or {}).get("заказов", 0) for т in ТОЧКИ}
    всего_вес = sum(вес.values()) or 1
    сеть["время"] = {k: sum((д[т]["время"] or {}).get(k, 0) * вес[т] for т in ТОЧКИ)
                     / всего_вес
                     for k in ("всего", "кц", "кухня", "сборка", "путь", "без_кц")}
    сеть["время"]["заказов"] = всего_вес
    return {"с": с, "по": по, "точки": д, "сеть": сеть}


# ------------------------------------------------------------------ рисуем
def _ч(v, знаков=0):
    return f"{v:,.{знаков}f}".replace(",", " ").replace(".", ",")


def для_кпи(с, по):
    """Только то, из чего считается премия: план, факт и кто был на смене.

    Отдельный сбор нарочно: полный недельный отчёт тянет OLAP, Loopa и
    Cloud по дням — полминуты ради двух чисел и списка фамилий.
    """
    д = {т: {} for т in ТОЧКИ}
    with cappi.Syrve() as s:
        в = report.выручка_пиу(s, с, по)
    for т in ТОЧКИ:
        д[т]["факт"] = в["точки"].get(т, 0)
    план = report.load_plan().get("weekly", {})
    for т in ТОЧКИ:
        полное = next((k for k, v in report.ТОЧКИ.items() if v == т), т)
        дни = план.get(полное, {})
        д[т]["план"] = sum(
            дни.get((с + timedelta(days=i)).isoformat())
            or дни.get(report.ДНИ[(с + timedelta(days=i)).weekday()]) or 0
            for i in range((по - с).days + 1))
    свои = админы(с, по)
    for т in ТОЧКИ:
        д[т]["админы"] = свои.get(т, {})
    return {"с": с, "по": по, "точки": д,
            "сеть": {"админы_без_филиала": свои.get("без филиала", {})}}


def кпи(d):
    """Премия администраторов за неделю, строками.

    Отдельной функцией, потому что её спрашивают и саму по себе: в
    понедельник по ней начисляют, и лезть за ней в середину большого
    отчёта неудобно.
    """
    т, сеть = d["точки"], d["сеть"]
    строки = [f"👤 <b>KPI АДМИНИСТРАТОРОВ</b> · {d['с']:%d.%m}–{d['по']:%d.%m}",
              f"<i>план недели выполнен — {КПИ_АДМИНА} ₴ каждому, кто был "
              f"на филиале</i>"]
    к_начислению = 0
    for имя_точки in ТОЧКИ:
        x = т[имя_точки]
        план, факт = x.get("план") or 0, x.get("факт") or 0
        выполнен = bool(план) and факт >= план
        доля = f"{факт / план * 100:.1f}".replace(".", ",") if план else "—"
        строки.append("")
        строки.append(f"<b>{имя_точки}</b> · {'✅' if выполнен else '❌'} {доля}%"
                      + ("" if выполнен or not план else
                         f" · не хватило {_ч(план - факт)} ₴"))
        люди = x.get("админы") or {}
        if not люди:
            строки.append("    <i>никто не отмечался администратором</i>")
        for кто, з in sorted(люди.items(), key=lambda i: -i[1]["смен"]):
            сумма = КПИ_АДМИНА if выполнен else 0
            к_начислению += сумма
            строки.append(f"    {кто} · "
                          + report.счёт(з["смен"], "смена", "смены", "смен")
                          + f" · <b>{сумма} ₴</b>")
    строки += ["", f"К начислению · <b>{к_начислению} ₴</b>"]
    ничьи = сеть.get("админы_без_филиала") or {}
    if ничьи:
        # Без филиала премию не считаем: неизвестно, чей план он выполнял.
        строки.append(f"<i>Без филиала в явках: {', '.join(ничьи)} — премия "
                      f"не начислена, филиал не определён.</i>")
    return строки


def нарисовать(d):
    """Таблица в <pre>: строки — показатели, столбцы — точки и сеть."""
    т, сеть = d["точки"], d["сеть"]
    колонки = [т["Лазарева"], т["Левітана"], сеть]
    в = lambda x: x.get("время") or {}

    def ряд(подпись, как):
        значения = []
        for x in колонки:
            try:
                значения.append(как(x))
            except (ZeroDivisionError, KeyError, TypeError):
                значения.append("—")
        return f"{подпись:<13}" + "".join(f"{з:>8}" for з in значения)

    строки = [
        ряд("", lambda x: ""),
        ряд("План %", lambda x: f"{x['факт'] / x['план'] * 100:.0f}%"),
        ряд("Факт, тыс", lambda x: _ч(x["факт"] / 1000)),
        ряд("План, тыс", lambda x: _ч(x["план"] / 1000)),
        ряд("Ср. чек", lambda x: _ч(x["факт"] / x["заказы"])),
        ряд("Длина чека", lambda x: _ч(x["блюда"] / x["заказы"], 1)),
        ряд("Заказов", lambda x: _ч(x["заказы"])),
        ряд("Блюд", lambda x: _ч(x["блюда"])),
        ряд("Потери, шт", lambda x: _ч(x.get("отказ", 0))),
        ряд("Потери, тыс", lambda x: _ч(x.get("отказ_грн", 0) / 1000)),
        ряд("Негатив %", lambda x: f"{x['негатив'] / x['отзывов'] * 100:.1f}%"
                                   .replace(".", ",")),
        ряд("Негатив, шт", lambda x: str(x["негатив"])),
        ряд("· кухня", lambda x: str(x["кухня"])),
        ряд("· сервис", lambda x: str(x["сервис"])),
        ряд("Кухня, мин", lambda x: _ч(в(x)["кухня"])),
        ряд("Сборка", lambda x: _ч(в(x)["сборка"])),
        ряд("Путь", lambda x: _ч(в(x)["путь"])),
        ряд("Всего ждёт", lambda x: _ч(в(x)["всего"])),
        ряд("· без КЦ", lambda x: _ч(в(x)["без_кц"])),
        ряд("ФК %", lambda x: f"{x['сс'] / x['прод'] * 100:.1f}%".replace(".", ",")),
    ]
    строки[0] = f"{'':<13}{'Лазар':>8}{'Левіт':>8}{'Сеть':>8}"

    шапка = [report.ЧЕРТА, "📊 <b>CAPPI CORE</b> · НЕДЕЛЯ",
             f"{d['с']:%d.%m} – {d['по']:%d.%m}", report.ЧЕРТА, ""]
    доля = сеть["факт"] / сеть["план"] if сеть.get("план") else None
    if доля is not None:
        шапка += [f"{report.цвет(доля, report.КРАСНОЕ['выручка'], False)} "
                  f"<b>{_ч(сеть['факт'])} ₴</b> · {доля * 100:.0f}% плана", ""]
    хвост = []
    перенос = сеть.get("перенос", 0)
    if перенос:
        хвост += ["", f"<i>Сверх потерь — перенос на другую точку: "
                      f"{report.счёт(перенос, 'заказ', 'заказа', 'заказов')} на "
                      f"{_ч(сеть['перенос_грн'])} ₴. Это не потеря: заказ "
                      f"выполнен вторым залом.</i>"]
    return ("\n".join(шапка) + "<pre>" + "\n".join(строки) + "</pre>"
            + "\n".join(хвост))


if __name__ == "__main__":
    import re
    д = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else None
    с, по = границы(д) if д else прошлая()
    print(re.sub(r"</?(b|i|pre)>", "", нарисовать(собрать(с, по))))
