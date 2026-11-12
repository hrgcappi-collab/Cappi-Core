#!/usr/bin/env python3
"""Сверка онлайн-оплат monobank с заказами Syrve.

Онлайн-оплата сайта и приложения идёт через эквайринг mono (plata), а в
Syrve такой заказ приходит с типом оплаты «Банковская карта (Mono)».
Других способов попасть в этот тип нет: за 10–13.11.2026 все 69 живых
заказов с ним нашли пару в выписке, и все 69 оплат выписки — свой заказ.
Поэтому любое расхождение — это ошибка, а не шум:

  * деньги в mono есть, заказа с Mono нет — оплатили онлайн, а закрыли
    другим типом (чаще всего «Готівка»), или заказ потерялся;
  * заказ закрыт по Mono, денег нет — закрыли «картой», а денег на счёте
    нет;
  * заказ удалён, а деньги пришли — гостю должны вернуть.

Связать оплату с заказом по ключу нельзя: в `reference` mono лежит UUID
сайта, в Syrve его нет ни в одном поле (ни id, ни externalData). Пара
ищется по сумме Mono-части заказа и времени: заказ создаётся в ту же
минуту, когда гость платит (на 11.11 — 28 из 28 с разницей 0–1 мин).

Предоплата «сегодня за завтра»: оплата и создание заказа случаются в один
момент, а доставка — позже. Поэтому заказы берём по дате доставки с
запасом вперёд (`ВПЕРЁД_ДНЕЙ`), а сверяем день по дате оплаты.

Выручку по-прежнему считаем по ПиУ (см. CLAUDE.md). Здесь — только
проверка, что онлайн-деньги и заказы сходятся по каждому чеку.
"""
import json, sys
from datetime import date, datetime, timedelta, timezone

import cappi

MONO_URL = "https://api.monobank.ua"
ТИП_MONO = "Банковская карта (Mono)"
ТИП_БОНУСЫ = "Оплата бонусами"

# Сколько минут между оплатой и созданием заказа считать «тем же
# заказом». На живых данных — 0–1 минута; запас на медленный сайт.
ОКНО_МИН = 30
# Насколько вперёд может быть доставка оплаченного заказа.
ВПЕРЁД_ДНЕЙ = 3
# Предзаказ сайт отдаёт в Syrve не сразу, а к открытию точки: оплата
# 05.11 в 09:40, заказ №86675 создан в 10:15. Поэтому заказ может
# появиться позже оплаты — до суток.
ПОЗЖЕ_ЧАСОВ = 24
# Cloud хранит доставки около недели: 12.11 он уже не отдал ни одного
# заказа за 04.11, хотя оплат в тот день было 26. Старше — не сверяем,
# иначе каждая оплата выглядит как «заказа нет».
ХРАНИТ_ДНЕЙ = 7
# mono отдаёт выписку не больше чем за 31 сутки за запрос.
_MONO_МАКС_ДНЕЙ = 31


# ------------------------------------------------------------------- mono
def _местное(iso):
    """Время mono приходит в UTC («…Z»). Переводим в местное время
    процесса — в образе это TZ=Europe/Kyiv, так что летнее время
    учитывается без tzdata."""
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return t.astimezone().replace(tzinfo=None)


def _в_unix(d):
    """Полночь местного дня — в unix-время."""
    return int(datetime(d.year, d.month, d.day).astimezone().timestamp())


def оплаты(с, по):
    """Успешные онлайн-оплаты mono за [с, по) по местным суткам."""
    токен = cappi.cfg().get("MONO_MERCHANT_TOKEN")
    if not токен:
        raise cappi.ВнешнийСбой("monobank", "не задан MONO_MERCHANT_TOKEN")
    out, d = [], с
    while d < по:
        до = min(по, d + timedelta(days=_MONO_МАКС_ДНЕЙ))
        сырое = cappi._get(f"{MONO_URL}/api/merchant/statement"
                           f"?from={_в_unix(d)}&to={_в_unix(до) - 1}",
                           {"X-Token": токен})
        try:
            r = json.loads(сырое)
        except ValueError:
            raise cappi.ВнешнийСбой("monobank", "ответ не JSON") from None
        for x in cappi.форма(cappi.форма(r, dict, "monobank").get("list") or [],
                             list, "monobank"):
            if not isinstance(x, dict) or x.get("status") != "success":
                continue
            out.append({
                "время": _местное(x["date"]),
                "сумма": round((x.get("amount") or 0) / 100, 2),
                "комиссия": round(((x.get("amount") or 0) - (x.get("profit") or 0)) / 100, 2),
                "ref": x.get("reference") or "",
                "инвойс": x.get("invoiceId") or "",
                "карта": x.get("maskedPan") or "",
            })
        d = до
    return sorted(out, key=lambda x: x["время"])


# ---------------------------------------------------------------- Syrve Cloud
def _cloud():
    c = cappi.cfg()
    tok = cappi.форма(cappi._post(f"{c['SYRVE_CLOUD_URL']}/api/1/access_token",
                                  {"apiLogin": c["SYRVE_CLOUD_API_KEY"]}),
                      dict, "Syrve Cloud").get("token")
    if not tok:
        raise cappi.ВнешнийСбой("Syrve Cloud", "не выдал токен")
    return c, {"Authorization": f"Bearer {tok}"}


def _кусок(c, H, от, до):
    """Доставки за [от, до]. На сутки целиком Cloud отвечает
    TOO_MANY_DATA_REQUESTED, поэтому при отказе делим интервал пополам."""
    f = "%Y-%m-%d %H:%M:%S.000"
    try:
        r = cappi._post(f"{c['SYRVE_CLOUD_URL']}/api/1/deliveries/by_delivery_date_and_status",
                        {"organizationIds": [c["SYRVE_ORG_ID"]],
                         "deliveryDateFrom": от.strftime(f),
                         "deliveryDateTo": до.strftime(f)}, H, timeout=120)
    except RuntimeError as e:
        if "TOO_MANY_DATA" in str(e) and до - от > timedelta(hours=1):
            середина = от + (до - от) / 2
            return _кусок(c, H, от, середина) + _кусок(c, H, середина + timedelta(seconds=1), до)
        raise
    return [o for org in cappi.форма(r, dict, "Syrve Cloud").get("ordersByOrganizations") or []
            for o in org.get("orders") or [] if isinstance(o, dict)]


def _время(s):
    return datetime.fromisoformat(s[:19]) if s else None


def заказы(с, по):
    """Доставки Syrve с датой доставки в [с, по)."""
    c, H = _cloud()
    сырые, d = [], datetime(с.year, с.month, с.day)
    конец = datetime(по.year, по.month, по.day)
    while d < конец:
        сырые += _кусок(c, H, d, d + timedelta(hours=12) - timedelta(seconds=1))
        d += timedelta(hours=12)
    out, были = [], set()
    for o in сырые:
        z = o.get("order") or {}
        if o.get("id") in были or not z.get("whenCreated"):
            continue
        были.add(o.get("id"))
        платежи = [(p.get("paymentType", {}).get("name") or "?", p.get("sum") or 0)
                   for p in z.get("payments") or [] if isinstance(p, dict)]
        out.append({
            "id": o.get("id"),
            "номер": z.get("number"),
            "создан": _время(z.get("whenCreated")),
            "доставка": _время(z.get("completeBefore")),
            "сумма": z.get("sum") or 0,
            "mono": round(sum(s for n, s in платежи if n == ТИП_MONO), 2),
            "платежи": платежи,
            # isDeleted в Cloud не про удаление: за 10–13.11 он стоит у 369
            # из 425 закрытых заказов. Отменённый заказ — это статус
            # Cancelled и cancelInfo с причиной.
            "удалён": z.get("status") == "Cancelled",
            "причина": ((z.get("cancelInfo") or {}).get("cause") or {}).get("name"),
            "статус": z.get("status"),
        })
    return out


# --------------------------------------------------------------------- сверка
def _рядом(a, b, минут=ОКНО_МИН):
    return a and b and abs((a - b).total_seconds()) <= минут * 60


def _позже(з, о):
    """Заказ создан не раньше оплаты (с запасом) и не позже чем через сутки."""
    сдвиг = (з["создан"] - о["время"]).total_seconds()
    return -ОКНО_МИН * 60 <= сдвиг <= ПОЗЖЕ_ЧАСОВ * 3600


class СлишкомСтаро(RuntimeError):
    """Syrve Cloud уже не отдаёт заказы за эти даты."""


def _подобрать(оплата, кандидаты, занято, условие, по_времени=None):
    по_времени = по_времени or (lambda з: _рядом(з["создан"], оплата["время"]))
    лучшие = [з for з in кандидаты
              if з["id"] not in занято and условие(з) and по_времени(з)]
    if not лучшие:
        return None
    з = min(лучшие, key=lambda з: abs((з["создан"] - оплата["время"]).total_seconds()))
    занято.add(з["id"])
    return з


def сверить(с, по=None):
    """Сверка за [с, по) по местным суткам. По умолчанию — один день.

    Возвращает список строк {статус, оплата, заказ, текст}; статусы:
      ок, предоплата — всё сошлось;
      удалён       — заказ удалён, а деньги пришли: проверить возврат;
      другой_тип   — деньги пришли, заказ закрыт не по Mono;
      сумма        — заказ по Mono рядом по времени, но сумма другая;
      нет_заказа   — деньги пришли, заказа не нашли;
      нет_денег    — заказ по Mono, денег в mono нет.
    """
    по = по or с + timedelta(days=1)
    if с < date.today() - timedelta(days=ХРАНИТ_ДНЕЙ - 1):
        raise СлишкомСтаро(
            f"Syrve Cloud хранит доставки около {ХРАНИТ_ДНЕЙ} дней — "
            f"сверить можно с {date.today() - timedelta(days=ХРАНИТ_ДНЕЙ - 1):%d.%m}")
    # Запас по краям: оплата в 23:59 и заказ в 00:01 — одна пара.
    пл = оплаты(с - timedelta(days=1), по + timedelta(days=1))
    зк = заказы(с - timedelta(days=1), по + timedelta(days=ВПЕРЁД_ДНЕЙ))
    в_периоде = lambda t: t and datetime(с.year, с.month, с.day) <= t < datetime(по.year, по.month, по.day)

    занято, пары = set(), {}
    живые_mono = [з for з in зк if з["mono"] and not з["удалён"]]
    удалённые_mono = [з for з in зк if з["mono"] and з["удалён"]]
    # У части заказов Cloud отдаёт двойника с тем же номером и одной
    # строкой «Готівкова оплата 0». Денег в нём нет — в пары не берём.
    прочие = [з for з in зк if not з["mono"] and not з["удалён"]
              and any(s for _, s in з["платежи"])]
    та_же_сумма = lambda з, о: abs(з["mono"] - о["сумма"]) < 0.01
    # Проходы от строгого к мягкому: сначала точные пары, потом всё прочее,
    # иначе мягкое правило заберёт заказ, которому есть точная пара.
    проходы = [
        ("ок", живые_mono, та_же_сумма, None),
        ("ок", живые_mono, та_же_сумма, _позже),           # предзаказ к открытию
        ("удалён", удалённые_mono, та_же_сумма, _позже),
        ("другой_тип", прочие,
         lambda з, о: abs(з["сумма"] - о["сумма"]) < 0.01
         or abs(з["сумма"] - sum(s for n, s in з["платежи"] if n == ТИП_БОНУСЫ) - о["сумма"]) < 0.01,
         None),
        ("сумма", живые_mono, lambda з, о: _рядом(з["создан"], о["время"], 5), None),
    ]
    for статус, кандидаты, условие, время in проходы:
        for о in пл:
            if id(о) in пары:
                continue
            з = _подобрать(о, кандидаты, занято, lambda з: условие(з, о),
                           время and (lambda з: время(з, о)))
            if з:
                пары[id(о)] = (статус, з)

    строки = []
    for о in пл:
        if not в_периоде(о["время"]):
            continue
        статус, з = пары.get(id(о), ("нет_заказа", None))
        if статус == "ок" and з["доставка"] and з["доставка"].date() > о["время"].date():
            статус = "предоплата"
        строки.append({"статус": статус, "оплата": о, "заказ": з})
    for з in живые_mono:
        if з["id"] not in занято and в_периоде(з["создан"]):
            строки.append({"статус": "нет_денег", "оплата": None, "заказ": з})
    return строки


# ---------------------------------------------------------------------- вывод
ЗНАЧКИ = {
    "ок": "✅", "предоплата": "⏳", "удалён": "↩️", "другой_тип": "❌",
    "сумма": "⚠️", "нет_заказа": "❌", "нет_денег": "❌",
}
ПОЯСНЕНИЕ = {
    "предоплата": "оплата сегодня, доставка позже",
    "удалён": "заказ отменён, а деньги пришли — проверить возврат",
    "другой_тип": "оплатили онлайн, а закрыли другим типом",
    "сумма": "сумма в Syrve не совпадает с оплатой",
    "нет_заказа": "деньги в mono есть, заказа в Syrve нет",
    "нет_денег": "закрыто по Mono, а денег в mono нет",
}
ПРОБЛЕМЫ = ("удалён", "другой_тип", "сумма", "нет_заказа", "нет_денег")


def _грн(x):
    return f"{x:,.2f}".replace(",", " ").replace(".", ",") + " ₴"


def описать(с, по, строки, э=lambda s: s):
    """Сводка для человека: итог и только проблемные строки."""
    период = с.strftime("%d.%m") if по - с == timedelta(days=1) \
        else f"{с:%d.%m}–{по - timedelta(days=1):%d.%m}"
    деньги = [r["оплата"] for r in строки if r["оплата"]]
    сошлось = [r for r in строки if r["статус"] in ("ок", "предоплата")]
    проблемы = [r for r in строки if r["статус"] in ПРОБЛЕМЫ]
    out = [f"Сверка mono ↔ Syrve за {период}",
           f"Онлайн-оплат: {len(деньги)} на {_грн(sum(о['сумма'] for о in деньги))}"
           f" (комиссия {_грн(sum(о['комиссия'] for о in деньги))})",
           f"Сошлось: {len(сошлось)}"
           + (f", из них предоплат {sum(r['статус'] == 'предоплата' for r in сошлось)}"
              if any(r['статус'] == 'предоплата' for r in сошлось) else "")]
    if not проблемы:
        out.append("✅ Расхождений нет")
        return "\n".join(out)
    out.append(f"\nРасхождений: {len(проблемы)}")
    for r in sorted(проблемы, key=lambda r: (r["оплата"] or {}).get("время")
                    or r["заказ"]["создан"]):
        о, з = r["оплата"], r["заказ"]
        кусок = [ЗНАЧКИ[r["статус"]]]
        if о:
            кусок.append(f"{о['время']:%d.%m %H:%M} оплата {_грн(о['сумма'])}")
        if з:
            кусок.append(f"заказ №{з['номер']}"
                         + (f" ({', '.join(f'{n} {s:g}' for n, s in з['платежи'])})"
                            if з["платежи"] else "")
                         + (f", отмена: {з['причина']}" if з.get("причина") else ""))
        кусок.append("— " + ПОЯСНЕНИЕ[r["статус"]])
        out.append(э(" ".join(кусок)))
    return "\n".join(out)


def _дата(s):
    return datetime.strptime(s, "%Y-%m-%d").date() if "-" in s \
        else datetime.strptime(s + f".{date.today().year}" if s.count(".") == 1 else s,
                               "%d.%m.%Y").date()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    с = _дата(sys.argv[1]) if len(sys.argv) > 1 else date.today() - timedelta(days=1)
    по = _дата(sys.argv[2]) + timedelta(days=1) if len(sys.argv) > 2 else с + timedelta(days=1)
    try:
        print(описать(с, по, сверить(с, по)))
    except СлишкомСтаро as e:
        print(e)
