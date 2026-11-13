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
import json, os, sys
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
            # Филиал живого заказа — только в conception (см. CLAUDE.md).
            "филиал": _ФИЛИАЛЫ.get((z.get("conception") or {}).get("name")),
        })
    return out


_ФИЛИАЛЫ = {"1 Лазарева": "Лазарева", "2 Левітана": "Левітана", "Борщик": "Лазарева"}


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


_ПРОБЕЛ = " "     # неразрывный: «442 ₴» не должно рваться на «442» и «₴»


def _грн_(x):
    """Сумма всегда с копейками через запятую — это деньги, и сверяют их
    до копейки с выпиской. Знак ₴ не отрывается от числа."""
    текст = f"{round(x or 0, 2):,.2f}".replace(",", _ПРОБЕЛ).replace(".", ",")
    return текст + _ПРОБЕЛ + "₴"


def _слово(n, одно, два, пять):
    n = abs(n) % 100
    if 11 <= n <= 19:
        return пять
    return одно if n % 10 == 1 else два if 2 <= n % 10 <= 4 else пять


def _без_mono(з):
    """Чем ещё платили в заказе, кроме Mono, — для «закрыт как …»."""
    прочее = [(n, s) for n, s in з["платежи"] if n != ТИП_MONO and s]
    return ", ".join(f"{n} {_грн_(s)}" for n, s in прочее)


def карточка(r, э=lambda s: s, с_датой=False):
    """Одно расхождение — цитатой в две строки: что за заказ и что не так."""
    о, з = r["оплата"], r["заказ"]
    t = _когда(r)
    время = f"{t:%d.%m %H:%M}" if с_датой else f"{t:%H:%M}"
    шапка = " · ".join(x for x in (f"№{з['номер']}" if з else "Без заказа", время,
                                   (з or {}).get("филиал")) if x)
    ст = r["статус"]
    if ст == "нет_денег":
        суть = f"закрыт по Mono на {_грн_(з['mono'])}, денег в Mono нет"
    elif ст == "нет_заказа":
        суть = f"в Mono оплачено {_грн_(о['сумма'])}, заказа в Syrve нет"
    elif ст == "другой_тип":
        суть = f"в Mono оплачено {_грн_(о['сумма'])}, а закрыт как {_без_mono(з) or 'не Mono'}"
    elif ст == "сумма":
        суть = f"в Mono оплачено {_грн_(о['сумма'])}, в Syrve по Mono {_грн_(з['mono'])}"
    elif ст == "удалён":
        причина = f" ({з['причина']})" if з.get("причина") else ""
        суть = f"отменён{причина}, а {_грн_(о['сумма'])} в Mono пришли — проверить возврат"
    else:
        суть = ПОЯСНЕНИЕ.get(ст, "")
    return f"<blockquote>{э(шапка)}\n{э(суть)}</blockquote>"


def сводка(заголовок, строки, э=lambda s: s, с_датой=False, карточек=None):
    """Сообщение целиком: шапка, деньги, итог, карточки расхождений. HTML.

    `карточек` — сколько последних расхождений показать; итог считается
    по всем. Нужно месяцу: длинное сообщение режется по строкам, и разрез
    посреди цитаты ломает разметку."""
    деньги = [r["оплата"] for r in строки if r["оплата"]]
    сошлось = [r for r in строки if r["статус"] in ("ок", "предоплата")]
    проблемы = sorted((r for r in строки if r["статус"] in ПРОБЛЕМЫ), key=_когда)
    предоплат = sum(r["статус"] == "предоплата" for r in сошлось)
    n = len(деньги)
    out = [э(заголовок), "",
           f"<blockquote>{_грн_(sum(о['сумма'] for о in деньги))} · {n} "
           f"{_слово(n, 'оплата', 'оплаты', 'оплат')} · комиссия "
           f"{_грн_(sum(о['комиссия'] for о in деньги))}</blockquote>", ""]
    if not проблемы:
        out.append("✅ Всё сошлось" + (f" · предоплат {предоплат}" if предоплат else ""))
        return "\n".join(out)
    out.append(f"✅ Сошлось {len(сошлось)} из {len(строки)}"
               + (f" · предоплат {предоплат}" if предоплат else ""))
    out += ["", f"❌ {_слово(len(проблемы), 'Расхождение', 'Расхождения', 'Расхождений')} "
                f"{len(проблемы)}"]
    скрыто = len(проблемы) - карточек if карточек and len(проблемы) > карточек else 0
    out += [карточка(r, э, с_датой) for r in проблемы[скрыто:]]
    if скрыто:
        out.append(f"<i>…и ещё {скрыто} раньше — в таблице для Excel</i>")
    return "\n".join(out)


def описать(с, по, строки, э=lambda s: s):
    """Сводка за день или период — HTML для Telegram."""
    один = по - с == timedelta(days=1)
    период = f"{с:%d.%m}" if один else f"{с:%d.%m}–{по - timedelta(days=1):%d.%m}"
    return сводка(f"💳 Онлайн-оплаты · {период}", строки, э, с_датой=not один)


def _когда(r):
    return (r["оплата"] or {}).get("время") or r["заказ"]["создан"]


def проблема(r):
    """Одна строка расхождения словами."""
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
    return " ".join(кусок)


def ключ(r):
    """Чем расхождение отличается от других — чтобы не сообщать дважды."""
    о, з = r["оплата"], r["заказ"]
    return f"{r['статус']}|{(о or {}).get('инвойс', '')}|{(з or {}).get('id', '')}"


# ----------------------------------------------------------- тревоги за день
# Свежее не трогаем: заказ, оплаченный пять минут назад, может ещё не
# доехать до Syrve, а закрытый «по Mono» — ещё ждать денег.
ТРЕВОГА_ЧЕРЕЗ_МИН = 60
# Деньги без заказа днём — ошибка, а ночью — предзаказ: сайт отдаст его
# в Syrve только к открытию (~10:15). Ночные оплаты оставляем утренней
# сводке, иначе каждую ночь будет ложная тревога.
РАБОЧИЕ_ЧАСЫ = (10, 22)


def тревоги(день=None, сейчас=None):
    """Расхождения за сегодня, о которых уже можно говорить."""
    сейчас = сейчас or datetime.now()
    день = день or сейчас.date()
    порог = сейчас - timedelta(minutes=ТРЕВОГА_ЧЕРЕЗ_МИН)
    out = []
    for r in сверить(день):
        if r["статус"] not in ПРОБЛЕМЫ or _когда(r) > порог:
            continue
        if (r["статус"] == "нет_заказа"
                and not РАБОЧИЕ_ЧАСЫ[0] <= r["оплата"]["время"].hour < РАБОЧИЕ_ЧАСЫ[1]):
            continue
        out.append(r)
    return sorted(out, key=_когда)


# ------------------------------------------------------------------- история
# Cloud забывает заказы через неделю, поэтому каждый день сохраняем, пока
# его ещё можно сверить. Месячный отчёт собирается из этих файлов.
ИСТОРИЯ = os.path.expanduser("~/.cappi/моно")


def _в_json(x):
    if isinstance(x, datetime):
        return x.isoformat()
    raise TypeError(type(x).__name__)


def _из_json(r):
    for часть, поля in (("оплата", ("время",)), ("заказ", ("создан", "доставка"))):
        if r.get(часть):
            for п in поля:
                if r[часть].get(п):
                    r[часть][п] = datetime.fromisoformat(r[часть][п])
            if часть == "заказ":
                r[часть]["платежи"] = [tuple(p) for p in r[часть]["платежи"]]
    return r


def _файл(день):
    return os.path.join(ИСТОРИЯ, f"{день.isoformat()}.json")


def сохранить_день(день, строки):
    """Атомарно: обрыв посреди записи не должен оставить пустой файл."""
    os.makedirs(ИСТОРИЯ, exist_ok=True)
    путь = _файл(день)
    данные = json.dumps({"день": день.isoformat(), "сверено": datetime.now().isoformat(),
                         "строки": строки}, ensure_ascii=False, default=_в_json)
    with open(путь + ".tmp", "w", encoding="utf-8") as f:
        f.write(данные)
        f.flush()
        os.fsync(f.fileno())
    os.replace(путь + ".tmp", путь)


def день_из_истории(день):
    try:
        with open(_файл(день), encoding="utf-8") as f:
            return [_из_json(r) for r in json.load(f)["строки"]]
    except (FileNotFoundError, ValueError, KeyError):
        return None


def _без_филиала(д):
    """День сохранён до того, как в заказе появился филиал (13.11.2026).
    Пока Cloud его помнит — пересверяем, чтобы карточки были полными."""
    строки = день_из_истории(д) or []
    return any(r["заказ"] and "филиал" not in r["заказ"] for r in строки)


def день(д):
    """Сверка за день для экрана: из истории или живая.

    Сегодня и вчера — живьём: день ещё идёт или его ещё не сохранили
    (сохраняем утром). Раньше — из истории: там то же самое, только
    мгновенно, а через неделю это единственный источник — Cloud забудет
    заказы, и живая сверка станет невозможной.
    """
    сохранено = день_из_истории(д)
    if сохранено is not None and д < date.today() - timedelta(days=1):
        return сохранено
    try:
        return сверить(д)
    except СлишкомСтаро:
        if сохранено is not None:
            return сохранено
        raise СлишкомСтаро(
            f"За {д:%d.%m} сверки нет: Syrve Cloud помнит заказы около "
            f"{ХРАНИТ_ДНЕЙ} дней, а в истории бота этот день не сохранён "
            f"(история ведётся с 12.11.2026).") from None


def досохранить(вчера=None):
    """Сверяет и сохраняет все дни, которые Cloud ещё помнит, а в истории
    их нет. Возвращает {день: строки} для сохранённых сейчас."""
    вчера = вчера or date.today() - timedelta(days=1)
    новые = {}
    for i in range(ХРАНИТ_ДНЕЙ - 2, -1, -1):
        д = вчера - timedelta(days=i)
        if not os.path.exists(_файл(д)) or _без_филиала(д):
            новые[д] = сверить(д)
            сохранить_день(д, новые[д])
    return новые


def месяц(год, мес):
    """Строки за месяц из истории и дни, которых в ней нет."""
    д = date(год, мес, 1)
    строки, пропущены = [], []
    while д.month == мес and д < date.today():
        дня = день_из_истории(д)
        if дня is None:
            пропущены.append(д)
        else:
            строки += дня
        д += timedelta(days=1)
    return строки, пропущены


def в_csv(строки):
    """Таблица для Excel: точка с запятой и BOM, иначе Excel ломает кириллицу."""
    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Дата", "Время оплаты", "Статус", "Сумма оплаты", "Комиссия",
                "Карта", "Инвойс mono", "Заказ №", "Оплаты в Syrve", "Сумма заказа",
                "Доставка", "Отмена"])
    for r in sorted(строки, key=_когда):
        о, з = r["оплата"] or {}, r["заказ"] or {}
        t = _когда(r)
        w.writerow([
            f"{t:%d.%m.%Y}", f"{о['время']:%H:%M}" if о else "",
            ПОЯСНЕНИЕ.get(r["статус"], "сошлось"),
            f"{о['сумма']:.2f}".replace(".", ",") if о else "",
            f"{о['комиссия']:.2f}".replace(".", ",") if о else "",
            о.get("карта", ""), о.get("инвойс", ""),
            з.get("номер", ""),
            ", ".join(f"{n} {s:g}" for n, s in з.get("платежи") or []),
            f"{з['сумма']:.2f}".replace(".", ",") if з else "",
            f"{з['доставка']:%d.%m %H:%M}" if з.get("доставка") else "",
            з.get("причина") or "",
        ])
    return ("﻿" + buf.getvalue()).encode("utf-8")


# ------------------------------------------------------------------- xlsx
# Настоящий Excel, а не CSV: на телефоне CSV открывается текстом, а в
# Excel — узкими колонками без выделения. Библиотек нет (бот живёт на
# стандартной), поэтому файл собран руками: xlsx — это zip с XML внутри.
# Строки — inline, без таблицы общих строк: так проще и для 300 строк
# размер не важен.

_КОЛОНКИ = [  # (заголовок, ширина, тип)
    ("Дата", 11, "т"), ("Время оплаты", 9, "т"), ("Филиал", 11, "т"),
    ("Статус", 38, "т"), ("Сумма оплаты", 13, "₴"), ("Комиссия", 11, "₴"),
    ("Заказ №", 9, "ч"), ("Оплаты в Syrve", 44, "т"), ("Сумма заказа", 13, "₴"),
    ("Доставка", 12, "т"), ("Отмена", 22, "т"), ("Карта", 18, "т"),
    ("Инвойс mono", 23, "т"),
]

# Стили по номеру: 0 обычный, 1 заголовок, 2 деньги, 3 текст-расхождение,
# 4 деньги-расхождение, 5 итог текст, 6 итог деньги, 7 число, 8 число-расхождение.
_СТИЛИ = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="1"><numFmt numFmtId="164" formatCode="#,##0.00"/></numFmts>
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="4"><fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFE7ECF3"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFFBE0E0"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="2"><border/><border><top style="thin"><color rgb="FF999999"/></top></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="9">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>
<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="0" fontId="0" fillId="3" borderId="0" xfId="0" applyFill="1"/>
<xf numFmtId="164" fontId="0" fillId="3" borderId="0" xfId="0" applyNumberFormat="1" applyFill="1"/>
<xf numFmtId="0" fontId="1" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1"/>
<xf numFmtId="164" fontId="1" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyBorder="1"/>
<xf numFmtId="1" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="1" fontId="0" fillId="3" borderId="0" xfId="0" applyNumberFormat="1" applyFill="1"/>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def _буква(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _ячейка(адрес, значение, стиль):
    from xml.sax.saxutils import escape
    if значение is None or значение == "":
        return f'<c r="{адрес}" s="{стиль}"/>'
    if isinstance(значение, tuple):            # ("f", формула, значение)
        return (f'<c r="{адрес}" s="{стиль}"><f>{escape(значение[1])}</f>'
                f'<v>{значение[2]}</v></c>')
    if isinstance(значение, (int, float)):
        return f'<c r="{адрес}" s="{стиль}"><v>{значение}</v></c>'
    return (f'<c r="{адрес}" s="{стиль}" t="inlineStr"><is><t xml:space="preserve">'
            f'{escape(str(значение))}</t></is></c>')


def в_xlsx(строки, лист="Онлайн-оплаты"):
    """Таблица месяца в .xlsx: шапка закреплена, фильтр, расхождения красным,
    внизу итог формулами — Excel пересчитает, если строки отфильтровать."""
    import io, zipfile
    from xml.sax.saxutils import escape
    ряды = [[(з, 1) for з, _, _ in _КОЛОНКИ]]
    for r in sorted(строки, key=_когда):
        о, з = r["оплата"] or {}, r["заказ"] or {}
        t = _когда(r)
        беда = r["статус"] in ПРОБЛЕМЫ
        стиль = {"т": 3 if беда else 0, "₴": 4 if беда else 2, "ч": 8 if беда else 7}
        значения = [
            f"{t:%d.%m.%Y}", f"{о['время']:%H:%M}" if о else "", з.get("филиал") or "",
            "предоплата" if r["статус"] == "предоплата"
            else ПОЯСНЕНИЕ.get(r["статус"], "сошлось"),
            о.get("сумма") if о else None, о.get("комиссия") if о else None,
            з.get("номер") if з else None,
            ", ".join(f"{n} {_грн_(s).replace(_ПРОБЕЛ, ' ')}" for n, s in з.get("платежи") or []),
            з.get("сумма") if з else None,
            f"{з['доставка']:%d.%m %H:%M}" if з.get("доставка") else "",
            з.get("причина") or "", о.get("карта", ""), о.get("инвойс", ""),
        ]
        ряды.append([(v, стиль[тип]) for v, (_, _, тип) in zip(значения, _КОЛОНКИ)])
    последний = len(ряды)
    деньги = [r["оплата"] for r in строки if r["оплата"]]
    проблем = sum(r["статус"] in ПРОБЛЕМЫ for r in строки)
    итог = [("Итого", 5), ("", 5), ("", 5), (f"Расхождений: {проблем}", 5),
            (("f", f"SUBTOTAL(9,E2:E{последний})", round(sum(о["сумма"] for о in деньги), 2)), 6),
            (("f", f"SUBTOTAL(9,F2:F{последний})", round(sum(о["комиссия"] for о in деньги), 2)), 6)]
    итог += [("", 5)] * (len(_КОЛОНКИ) - len(итог))
    ряды.append(итог)

    xml_рядов = "".join(
        f'<row r="{i}">' + "".join(_ячейка(f"{_буква(j)}{i}", v, s)
                                   for j, (v, s) in enumerate(ряд)) + "</row>"
        for i, ряд in enumerate(ряды, 1))
    конец = _буква(len(_КОЛОНКИ) - 1)
    лист_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
        'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
        '<cols>' + "".join(f'<col min="{i}" max="{i}" width="{ш}" customWidth="1"/>'
                           for i, (_, ш, _) in enumerate(_КОЛОНКИ, 1)) + '</cols>'
        f'<sheetData>{xml_рядов}</sheetData>'
        f'<autoFilter ref="A1:{конец}{последний}"/></worksheet>')
    имя = escape(лист[:31])
    части = {
        "[Content_Types].xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '</Types>',
        "_rels/.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>',
        "xl/workbook.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets><sheet name="{имя}" sheetId="1" r:id="rId1"/></sheets>'
            '<definedNames><definedName name="_xlnm._FilterDatabase" localSheetId="0" hidden="1">'
            f"'{имя}'!$A$1:${конец}${последний}</definedName></definedNames></workbook>",
        "xl/_rels/workbook.xml.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '</Relationships>',
        "xl/styles.xml": _СТИЛИ,
        "xl/worksheets/sheet1.xml": лист_xml,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for путь, текст in части.items():
            z.writestr(путь, текст.encode("utf-8"))
    return buf.getvalue()


def _дата(s):
    return datetime.strptime(s, "%Y-%m-%d").date() if "-" in s \
        else datetime.strptime(s + f".{date.today().year}" if s.count(".") == 1 else s,
                               "%d.%m.%Y").date()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    с = _дата(sys.argv[1]) if len(sys.argv) > 1 else date.today() - timedelta(days=1)
    по = _дата(sys.argv[2]) + timedelta(days=1) if len(sys.argv) > 2 else с + timedelta(days=1)
    try:
        import re
        print(re.sub(r"</?blockquote>", "", описать(с, по, сверить(с, по))).replace(_ПРОБЕЛ, " "))
    except СлишкомСтаро as e:
        print(e)
