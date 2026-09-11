#!/usr/bin/env python3
"""Телеграм-бот Cappi: смена цен в Syrve и контроль, что цена доехала до сайта и Glovo.

Запуск:  python3 bot.py
Конфиг:  ~/.cappi/api.env  (TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_IDS)
"""
import html, json, os, re, sys, threading, time, traceback, urllib.parse, urllib.request
from difflib import SequenceMatcher
from datetime import date, datetime, timedelta

import access
import bugs
import cappi
import jamshut
import kpi
import cost
import nomenclature
import promo
import stoplist
import report
import webhook

HERE = os.path.dirname(os.path.abspath(__file__))
START = time.time()

CFG = cappi.cfg()
TOKEN = CFG.get("TELEGRAM_BOT_TOKEN", "")
access.bootstrap()          # перенос старого списка в роли, если ещё не было
API = f"https://api.telegram.org/bot{TOKEN}"

STATE_DIR = os.path.expanduser("~/.cappi")
PENDING = os.path.join(STATE_DIR, "pending.json")   # отложенные проверки цен
AUDIT = os.path.join(STATE_DIR, "changes.log")      # журнал изменений цен
UNKNOWN = os.path.join(STATE_DIR, "unknown.log")    # что бот не понял — на ревизию
ALERTS = os.path.join(STATE_DIR, "alerts_seen.json")  # о чём уже сообщали
DENIED = os.path.join(STATE_DIR, "denied.log")      # кто стучался без доступа

REPORT_AT = "22:00"         # когда присылать итоги дня
CHECK_AFTER_MIN = 30        # через сколько проверять, доехала ли цена
MAX_CHANGE_PCT = 50         # скачок больше этого бот не проводит

# Цену по умолчанию меняем следующей датой: приказ вступает в силу, когда
# закроется ночная кассовая смена, около трёх ночи. Так цена не прыгает
# посреди рабочего дня и не задевает уже открытые смены и принятые заказы.
DEFAULT_TOMORROW = True
СЕГОДНЯ_СЛОВА = ("сегодня", "сейчас", "today", "now", "срочно")

_confirm = {}               # токен → подготовленное изменение
_ссылки = {}                # короткий ключ → длинные идентификаторы
_await = {}                 # чат → чего ждём от следующего сообщения
_lock = threading.Lock()

# Меню трёхуровневое: главный экран → модуль → действия. Плоский список из
# семи кнопок читался как свалка; здесь каждый блок живёт отдельно, и добавить
# в него кнопку можно, не трогая остальные.
МЕНЮ = {
    "главное": [["💰 Цены", "🛒 Продажи"],
                ["📈 Показатели", "🧑‍🍳 Персонал"],
                ["🏭 Производство", "🤖 Джамшут"],
                ["⚙️ Админка"]],
    "производство": [["🧮 Себестоимость блюда"],
                     ["📋 Просчёт по составу"],
                     ["➕ Новое блюдо"],
                     ["◀️ Назад"]],
    "цены": [["🔍 Найти позицию"],
             ["📊 Сайт и Glovo", "⏳ На проверке"],
             ["◀️ Назад"]],
    # Продажи — про то, что происходит с заказами прямо сейчас: что не
    # продаётся, что готовится, что отвалилось. Стоп-лист жил в «Ценах»,
    # но цена и наличие — разные вещи, и путать их не стоит.
    "продажи": [["🏷 Акционные", "⭐ Спецпредложение"],
                ["🛑 Стоп-лист"],
                ["🚚 В работе", "❌ Отмены"],
                ["🗑 Списания"],
                ["◀️ Назад"]],
    "показатели": [["📈 Сейчас", "📅 За вчера"],
                   ["📅 Выбрать день"],
                   ["🎯 План", "⏱ Время работы"],
                   ["🚧 Зоны", "😠 Жалобы"],
                   ["◀️ Назад"]],
    # Персонал — про людей на смене. Отдельно от показателей: там про
    # деньги, здесь про тех, кто их зарабатывает, и вопросы разные.
    "персонал": [["👥 Смена сегодня", "👥 Смена вчера"],
                 ["📅 Смена за день", "⚠️ Правки явок"],
                 ["🏆 KPI кухни", "💵 Процент кухни"],
                 ["🕵️ Тайный гость"],
                 ["◀️ Назад"]],
    # Джамшут — подчинённый бот. Core им управляет, он о Core не знает.
    "джамшут": [["🚦 Зоны сейчас", "☔ Непогода"],
                ["🚧 Закрыть зону", "✅ Открыть зону"],
                ["📜 История", "👤 Кто закрывал"],
                ["🩺 Здоровье"],
                ["◀️ Назад"]],
    "админка": [["🔌 Проверка связи", "📜 Журнал цен"],
                ["🐞 Сбои"],
                ["🗣 Непонятые", "🔐 Отказы"],
                ["👥 Доступ"],
                ["🤖 Состояние бота"],
                ["◀️ Назад"]],
}

_menu = {}          # чат → в каком модуле он сейчас


# Какой пункт главного меню какого права требует. Показывать кнопку,
# которая ответит «нельзя», — хуже, чем не показывать её вовсе.
ТРЕБУЕТ = {"💰 Цены": "площадки", "🛒 Продажи": "площадки",
           "📈 Показатели": "показатели", "🧑‍🍳 Персонал": "показатели",
           "🤖 Джамшут": "показатели", "⚙️ Админка": "админка",
           "🏭 Производство": "показатели"}


def keyboard(chat):
    ряды = []
    for row in МЕНЮ[_menu.get(chat, "главное")]:
        видимые = [b for b in row
                   if b not in ТРЕБУЕТ or access.можно(chat, ТРЕБУЕТ[b])]
        if видимые:
            ряды.append([{"text": b} for b in видимые])
    return {"keyboard": ряды, "resize_keyboard": True}


# ------------------------------------------------------------------ Telegram
def tg(method, **params):
    data = urllib.parse.urlencode(
        {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
         for k, v in params.items() if v is not None}).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=70) as r:
        return json.loads(r.read())


def ссылка(*части):
    """Короткий ключ вместо длинных идентификаторов в callback_data.

    Telegram отводит на неё 64 байта, а пара GUID — это 76: кнопка со
    стоп-листом молча превращала весь экран в «HTTP 400 Bad Request».
    Кладём значения в память и передаём номер.
    """
    ключ = str(len(_ссылки) % 100000)
    _ссылки[ключ] = части
    return ключ


def развернуть(ключ, сколько=2):
    части = _ссылки.get(ключ)
    if not части:
        return (None,) * сколько
    return части


def say(chat, text, inline=None, keys=True):
    markup = None
    if inline:
        markup = {"inline_keyboard": inline}
    elif keys:
        markup = keyboard(chat)
    return tg("sendMessage", chat_id=chat, text=text,
              parse_mode="HTML", reply_markup=markup)


# -------------------------------------------------------------------- служебное
def audit(line):
    with open(AUDIT, "a") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}\t{line}\n")


def load_pending():
    try:
        return json.load(open(PENDING))
    except Exception:
        return []


_очередь = threading.Lock()      # pending.json пишут и watcher, и обработчик


def save_pending(items):
    _записать_json(PENDING, items)


# Меню украинское, ищут вперемешку. Часть слов не сводится заменой букв:
# «сок» и «сік» — разные слова. Держим словарь основ: ключ — русская основа,
# значение — украинская, которую и ищем в названии.
RU_UA = {
    "сок": "сик", "напит": "напий", "куриц": "курк", "курин": "куряч",
    "сыр": "сир", "сливочн": "вершков", "говядин": "яловичин",
    "тунец": "тунець", "угор": "вугор", "овощ": "овоч", "гриб": "гриб",
    "остр": "гостр", "копчен": "копчен", "запечен": "запечен",
    "кофе": "кава", "мороженое": "морозив", "картофел": "картопл",
    "яйц": "яйц", "хлеб": "хлиб", "лапш": "локшин", "клубни": "полуни",
    "яблок": "яблук", "огурец": "огирок", "перец": "перець",
    "креветк": "креветк", "лосос": "лосос", "напиток": "напий",
}


def load_aliases():
    """Словарь написаний из aliases.json. Пересобирается `python3 aliases.py`,
    правится руками — потому и читается с диска, а не зашит в код."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "aliases.json")) as f:
            return json.load(f)
    except Exception:
        return {}


ALIASES = load_aliases()


def _close(q, name, cutoff=0.75):
    """Похоже ли слово из названия на запрос. Ловит то, чего не берёт словарь:
    «моти» против «мочі» — обе транслитерации японского, обе в ходу; плюс
    обычные опечатки."""
    return any(SequenceMatcher(None, q, w).ratio() >= cutoff
               for w in name.split() if abs(len(w) - len(q)) <= 3)


def _needles(word):
    """Как ещё может выглядеть это слово в названии.

    Кроме словаря — основа слова: «тунец» должен находить «з тунцем»,
    а окончания в украинском и русском расходятся почти всегда."""
    out = {word} | {ua for ru, ua in RU_UA.items() if ru in word}
    # Основу берём только у слов. У чисел она вредна: от артикула 03395
    # остаётся «033», а это «0,33 л» в названии каждого напитка.
    if len(word) >= 5 and not word.isdigit():
        out.add(word[:len(word) - 2])
    return out


def find(query):
    """Ищем по артикулу или куску названия. Цена — из Cloud API, карточке не верим."""
    q = cappi.norm_full(query)
    menu = cappi.cloud_prices()

    цифры = re.fullmatch(r"\d{3,}", q)

    def matches(code, p):
        if q == str(code).lower():
            return True
        if цифры:
            # Ищут артикул — значит по названию искать нечего.
            return False
        name = cappi.norm_full(p["name"])
        alias = " ".join(ALIASES.get(code, []))
        # Каждое слово запроса должно найтись, иначе «сливочная креветка»
        # вернёт все креветки подряд.
        return all(any(n in name or n in alias for n in _needles(w))
                   for w in q.split())

    hits = [(c, p) for c, p in menu.items() if matches(c, p)]
    if re.fullmatch(r"\d{3,}", q):
        # Похоже на артикул: либо он есть, либо его нет. Приблизительный
        # поиск по цифрам выдал бы десяток случайных позиций — как было с
        # несуществующим 03395, нашедшим двенадцать напитков.
        return sorted(hits, key=lambda x: x[1]["name"])
    if not hits and len(q) >= 3:
        # Точного совпадения нет — пробуем приблизительно, но только тогда,
        # иначе точный запрос утонет в похожих.
        hits = [(c, p) for c, p in menu.items()
                if _close(q, cappi.norm_full(p["name"]))]
    return sorted(hits, key=lambda x: x[1]["name"])


def where_shown(guid, name):
    """Какую цену показывают гостю. Сайт — точно по guid, Glovo — по названию."""
    site = glovo = None
    try:
        row = cappi.site_prices().get(guid)
        site = row["price"] if row else None
    except Exception:
        pass
    try:
        glovo = cappi.glovo_prices().get(cappi.norm(name))
    except Exception:
        pass
    return site, glovo


def fmt(p):
    """Цена без хвоста .0 — 46, а не 46.0."""
    return f"{p:g}"


# ------------------------------------------------------------------- экраны
def помощь(chat):
    """Подсказка под конкретного человека.

    Общий список всех команд читать невозможно, а половина из них для
    оператора всё равно закрыта: показывать их — обещать то, чего не дадим.
    Поэтому строим от роли и от вопроса «что я хочу сделать», а не от
    алфавита команд.
    """
    роль = access.роль(chat) or "—"
    т = [f"<b>Cappi Core</b> · твоя роль: <b>{роль}</b>", "",
         "Всё основное — кнопками снизу. Ниже то, что быстрее написать."]

    if access.можно(chat, "показатели"):
        т += ["", "<b>Посмотреть цифры</b>",
              "  <code>итоги за вчера</code> — выручка, заказы, отмены",
              "  <code>за 9.09</code> — любой прошедший день",
              "  <code>отмены вчера</code> · <code>списания вчера</code>",
              f"  Итоги дня приходят сами в {REPORT_AT}."]

    if access.можно(chat, "цены"):
        т += ["", "<b>Поменять цену</b>",
              "  <code>окрошка</code> — найти позицию, дальше кнопками",
              "  <code>/set 03275 46</code> — одна позиция, ночью",
              "  <code>/set 03275 46 сейчас</code> — прямо сейчас",
              "  Списком — просто пришли прейскурант, по строке на позицию:",
              "  <code>Д Соус унагі 20 гр. 36</code>",
              "  <code>Д_Тофу 20 гр 46</code>",
              "  Бот покажет «было → стало» и проведёт всё одним приказом."]
        т += ["", "<b>Когда цена доедет</b>",
              "  По умолчанию — <b>ночью</b>, около 3:00, когда закроется",
              "  кассовая смена: так цена не прыгает посреди дня и не",
              f"  задевает принятые заказы. Через {CHECK_AFTER_MIN} минут после",
              "  выгрузки бот сам сверит сайт и Glovo и напишет результат.",
              f"  Скачок больше {MAX_CHANGE_PCT}% переспросит один раз —"
              f"  это защита от опечатки, а не запрет."]

    if access.можно(chat, "показатели"):
        т += ["", "<b>Люди и премии</b>",
              "  «Персонал» → KPI кухни, процент кухни, смены",
              "  <code>Лазарева 5/4</code> — тайный гость: всего 5, прошло 4"]

    if access.можно(chat, "админка"):
        т += ["", "<b>Служебное</b>",
              "  «Админка» → связь, журнал цен, сбои, доступ",
              "  <code>/people шеф_id 123456789</code> — кому слать запрос",
              "  правок часов 2-го числа"]

    т += ["", "<i>Не понял — значит записал в журнал сбоев. "
              "Пиши как удобно, разберём.</i>"]
    say(chat, "\n".join(т))


def screen_item(chat, code, p):
    """Карточка позиции с кнопками действий."""
    txt = (f"<b>{p['name']}</b>\n"
           f"артикул <code>{code}</code>\n\n"
           f"в Syrve: <b>{fmt(p['price'])} ₴</b>")
    if p["next"] is not None:
        txt += f"\nзапланировано: <b>{fmt(p['next'])} ₴</b> с {(p['next_date'] or '')[:10]}"
    if not p["in_menu"]:
        txt += "\n<i>не показывается в меню</i>"
    кнопки = [[{"text": "✏️ Изменить цену", "callback_data": f"ed:{code}"}],
              [{"text": "👀 Где и почём показывается", "callback_data": f"sh:{code}"}]]
    try:
        в_стопе = [s for s in stoplist.список() if s["productId"] == p["id"]]
    except Exception:
        в_стопе = []
    if в_стопе:
        txt += "\n<b>🛑 в стопе:</b> " + ", ".join(s["точка"] for s in в_стопе)
        кнопки += [[{"text": f"▶️ Снять со стопа · {s['точка']}",
                     "callback_data": f"sr:{ссылка(p['id'], s['terminalGroupId'])}"}]
                   for s in в_стопе]
    else:
        кнопки += [[{"text": f"🛑 В стоп · {имя}",
                     "callback_data": f"sa:{ссылка(p['id'], tid)}"}]
                   for tid, имя in stoplist.терминалы().items()
                   if имя not in ("Отменённый заказ",)]
    say(chat, txt, inline=кнопки)


def cmd_price(chat, query):
    if not query:
        _await[chat] = {"what": "search"}
        return say(chat, "Что искать? Напиши часть названия или артикул.")
    hits = find(query)
    if not hits:
        if re.fullmatch(r"\d{3,}", query.strip()):
            return say(chat, f"Артикула <b>{query.strip()}</b> в меню нет.\n"
                             f"<i>Проверь номер или найди позицию по названию.</i>")
        return say(chat, f"Не нашёл: <b>{query}</b>")
    if len(hits) == 1:
        return screen_item(chat, *hits[0])
    if len(hits) > 20:
        return say(chat, f"Слишком много совпадений ({len(hits)}). Уточни запрос.")
    say(chat, f"Нашёл {len(hits)}:", inline=[
        [{"text": f"{fmt(p['price'])} ₴  ·  {p['name'][:34]}",
          "callback_data": f"it:{code}"}] for code, p in hits])


def расхождения_витрин():
    """Где Syrve, сайт и Glovo показывают разные цены.

    Гость платит ту цену, которую видит на сайте или в Glovo, а не ту,
    что стоит в Syrve. Пока сверку надо было запускать руками,
    «Моршинська без газу 0,5 л» продавалась на Glovo за 99 ₴ при цене 49 в
    Syrve — и узнали об этом случайно.
    """
    syr = {k: v for k, v in cappi.cloud_prices().items() if v["in_menu"]}
    site, glovo = cappi.site_prices(), cappi.glovo_prices()
    расхождения = []
    for code, v in sorted(syr.items(), key=lambda x: x[1]["name"]):
        row = site.get(v["id"])                       # сайт — точно по guid
        сайт = row["price"] if row else None
        гл = glovo.get(cappi.norm(v["name"]))         # Glovo — только по названию
        if сайт is None and гл is None:
            continue
        if ((сайт is not None and сайт != v["price"])
                or (гл is not None and abs(гл - v["price"]) > 0.01)):
            расхождения.append({"код": code, "название": v["name"],
                                "guid": v["id"], "syrve": v["price"],
                                "сайт": сайт, "glovo": гл, "запланировано": None})
    _отметить_запланированные(расхождения)
    return расхождения, {"syrve": len(syr), "сайт": len(site), "glovo": len(glovo)}


def _отметить_запланированные(расхождения):
    """Проставляем цену, которая встанет ночью, если приказ уже есть.

    Без этого бот ругался на расхождение, которое сам же и закрывает через
    несколько часов: цена на Glovo уже новая, в Syrve ещё старая, приказ
    стоит на сегодняшнюю ночь — и всё равно тревога.
    """
    if not расхождения:
        return
    завтра = (date.today() + timedelta(days=1)).isoformat()
    try:
        with cappi.Syrve() as s:
            for r in расхождения:
                план, _ = s.price_of(r["guid"], завтра)
                if план is not None and abs(план - r["syrve"]) > 0.005:
                    r["запланировано"] = план
    except Exception as e:
        bugs.записать("апи", f"не смог прочитать запланированные цены: {e}",
                      где="сверка")


def _строка_расхождения(r):
    хвост = (f"\n     <i>ночью встанет {fmt(r['запланировано'])} — приказ уже есть</i>"
             if r.get("запланировано") else "")
    return (f"<code>{r['код']}</code> {r['название'][:34]}\n"
            f"     Syrve <b>{fmt(r['syrve'])}</b> · "
            f"сайт {fmt(r['сайт']) if r['сайт'] is not None else '—'} · "
            f"Glovo {fmt(r['glovo']) if r['glovo'] is not None else '—'}{хвост}")


def cmd_check(chat):
    say(chat, "Сверяю цены: Syrve ↔ сайт ↔ Glovo. Это займёт минуту…")
    плохие, сколько = расхождения_витрин()
    head = (f"Syrve {сколько['syrve']} · сайт {сколько['сайт']} · "
            f"Glovo {сколько['glovo']}\n"
            f"<i>сайт сверяется по артикулу, Glovo — по названию</i>\n\n")
    say(chat, head + ("❌ <b>Расхождения</b>\n\n"
                      + "\n".join(_строка_расхождения(r) for r in плохие[:25])
                      if плохие else "✅ Расхождений нет"))


def cmd_pending(chat):
    items = [i for i in load_pending() if i["chat"] == chat]
    if not items:
        return say(chat, "Ничего не стоит на проверке.")
    say(chat, "\n".join(
        f"<code>{i['code']}</code> {i['name'][:28]} → {fmt(i['price'])} ₴\n"
        f"     {'приказ принят?' if i.get('stage') == 'planned' else 'сайт и Glovo'}"
        f" — {datetime.fromisoformat(i['due']):%d.%m %H:%M}" for i in items))


def cmd_report(chat, day=None, live=True):
    say(chat, "Считаю показатели…")
    say(chat, report.render(report.collect(day), live=live))


def cmd_plan(chat, текст):
    """Показывает план или принимает его таблицей — в том виде, как ведут."""
    p = report.load_plan()
    args = текст.split()

    if not текст.strip():
        today = date.today()
        сумма, по_точкам, откуда = report.plan_for(today)
        строки = [f"<b>План на {today:%d.%m}</b> "
                  + (f"— <b>{report.money(сумма)} ₴</b> <i>({откуда})</i>"
                     if сумма else "<i>не задан</i>")]
        for точка, v in sorted(по_точкам.items(), key=lambda x: -x[1]):
            строки.append(f"    {report.ТОЧКИ.get(точка, точка):<10} {report.money(v):>9} ₴")
        if p.get("weekly"):
            строки += ["", "<b>Недельный план</b>"]
            for точка, дни in p["weekly"].items():
                за_неделю = sum(дни.values())
                строки.append(f"  {report.ТОЧКИ.get(точка, точка)} — "
                              f"{report.money(за_неделю)} ₴/нед")
                строки.append("    " + "  ".join(
                    f"{d} {report.money(дни.get(d, 0))}" for d in report.ДНИ))
        строки += ["", "Поставить — пришли таблицу как есть:",
                   "<code>Лазарева\nПн  58420\nВт  58420\n…</code>",
                   "", "Или разово: <code>/plan 45000</code> — на сегодня."]
        return say(chat, "\n".join(строки))

    # Одно число — точечный план на сегодня.
    if len(args) == 1:
        try:
            сумма = float(re.sub(r"[^\d.,]", "", args[0]).replace(",", "."))
        except ValueError:
            return say(chat, f"Не понял сумму: <b>{args[0]}</b>")
        p.setdefault("days", {})[date.today().isoformat()] = сумма
        report.save_plan(p)
        return say(chat, f"План на {date.today():%d.%m}: "
                         f"<b>{report.money(сумма)} ₴</b>\n"
                         f"<i>Он перебивает недельный только на сегодня.</i>")

    план, ошибки = report.parse_plan(текст)
    недели = план.pop("__недели__", None)
    if недели:
        p.setdefault("month_weeks", {})[date.today().strftime("%Y-%m")] = недели
    заполненные = {т: д for т, д in план.items() if д}
    if недели and not заполненные:
        report.save_plan(p)
        итого = sum(недели.values())
        строки = [f"✅ <b>Накопительный план на {date.today():%B %Y}</b>", ""]
        for n, (_, нач, кон) in enumerate(report.недели_месяца(date.today()), 1):
            сумма = недели.get(n)
            if сумма:
                дней = (кон - нач).days + 1
                строки.append(f"  неделя {n}: {нач:%d.%m}–{кон:%d.%m} "
                              f"({дней} дн) — {report.money(сумма)} ₴")
        строки += ["", f"Итого <b>{report.money(итого)} ₴</b> за месяц"]
        return say(chat, "\n".join(строки))
    if not заполненные:
        return say(chat, "Не нашёл в тексте дней недели с суммами.\n"
                         "Ожидаю строку с названием точки, а под ней "
                         "<code>Пн  58420</code> и так далее.")
    # Дописываем, а не заменяем: план часто присылают по одной точке, и
    # затирать этим вторую значит молча обнулить ей план.
    было = p.setdefault("weekly", {})
    for точка, дни in заполненные.items():
        было.setdefault(точка, {}).update(дни)
    report.save_plan(p)
    строки = ["✅ <b>Недельный план принят</b>", ""]
    for точка, дни in заполненные.items():
        строки.append(f"<b>{report.ТОЧКИ.get(точка, точка)}</b> — "
                      f"{report.money(sum(дни.values()))} ₴/нед")
        строки.append("    " + "  ".join(
            f"{d} {report.money(дни.get(d, 0))}" for d in report.ДНИ))
    итого = sum(sum(д.values()) for д in заполненные.values())
    строки += ["", f"Итого <b>{report.money(итого)} ₴</b> в неделю"]
    if ошибки:
        строки += ["", "⚠️ <b>Суммы не сходятся с итогами в таблице:</b>"]
        строки += [f"    {o}" for o in ошибки]
        строки.append("<i>План записал по дням — проверь исходную таблицу.</i>")
    say(chat, "\n".join(строки))


def cmd_healthcheck(chat):
    say(chat, "Проверяю все подключения…")
    import subprocess
    r = subprocess.run([sys.executable, os.path.join(HERE, "healthcheck.py")],
                       capture_output=True, text=True, timeout=300)
    say(chat, "<pre>" + (r.stdout or r.stderr)[-3500:] + "</pre>")


def cmd_audit(chat, n=15):
    """Журнал смен цен — кто, что и когда менял."""
    try:
        строки = open(AUDIT).read().strip().splitlines()[-n:]
    except FileNotFoundError:
        строки = []
    if not строки:
        return say(chat, "Журнал пуст — цены через бота ещё не меняли.")
    out = ["<b>Последние изменения цен</b>", ""]
    for s in строки:
        ч = s.split("\t")
        out.append(f"<code>{ч[0][5:16]}</code> {ч[1]} · {ч[3] if len(ч)>3 else ''}"
                   f"\n     {ч[2][:34] if len(ч)>2 else ''}"
                   f"  {ч[5] if len(ч)>5 else ''}")
    say(chat, "\n".join(out))


def cmd_access(chat):
    """Список людей с ролями и кнопками управления."""
    if not access.можно(chat, "доступ"):
        return say(chat, "Управлять доступом может только админ.")
    люди = access.все()
    строки = [f"<b>Доступ к боту</b> — {len(люди)} чел.", ""]
    кнопки = []
    for uid, v in sorted(люди.items(), key=lambda x: x[1]["роль"]):
        сам = "  ← ты" if int(uid) == chat else ""
        имя = f" {v['имя']}" if v.get("имя") else ""
        строки.append(f"  <code>{uid}</code>{имя} — <b>{v['роль']}</b>{сам}")
        кнопки.append([{"text": f"{v['роль'][:4]}· {uid}{имя}",
                        "callback_data": f"ac:{uid}"}])
    строки += ["", "<b>Роли</b>"]
    строки += [f"  <b>{r}</b> — {access.ОПИСАНИЕ[r]}" for r in access.РОЛИ]
    строки += ["", "Нажми на человека, чтобы сменить роль или убрать.",
               "Добавить: <code>/access 123456789 смотрящий</code>",
               "<i>Свой id человек увидит, написав боту.</i>"]
    say(chat, "\n".join(строки), inline=кнопки or None)


def экран_человека(chat, uid):
    v = access.все().get(str(uid))
    if not v:
        return say(chat, "Такого уже нет в списке.")
    кнопки = [[{"text": f"→ {r}", "callback_data": f"ar:{uid}:{r}"}]
              for r in access.РОЛИ if r != v["роль"]]
    кнопки.append([{"text": "🚫 Убрать доступ", "callback_data": f"ax:{uid}"}])
    say(chat, f"<code>{uid}</code>" + (f" {v['имя']}" if v.get("имя") else "")
              + f"\nсейчас: <b>{v['роль']}</b>"
                f"\nдобавлен: {v.get('когда', '—')[:16]}"
                f" ({v.get('кто_добавил', '—')})",
        inline=кнопки)


def cmd_access_add(chat, args):
    if not access.можно(chat, "доступ"):
        return say(chat, "Управлять доступом может только админ.")
    if not args:
        return say(chat, "Формат: <code>/access 123456789 смотрящий</code>\n"
                         "Роли: " + ", ".join(access.РОЛИ))
    uid = re.sub(r"\D", "", args[0])
    if not uid:
        return say(chat, f"Не похоже на id: <b>{args[0]}</b>")
    роль = args[1].lower() if len(args) > 1 else "смотрящий"
    if роль not in access.РОЛИ:
        return say(chat, f"Неизвестная роль: <b>{роль}</b>\n"
                         "Есть: " + ", ".join(access.РОЛИ))
    имя = " ".join(args[2:]) or None
    access.добавить(uid, роль, кто=chat, имя=имя)
    say(chat, f"✅ <code>{uid}</code> — <b>{роль}</b>\n"
              f"<i>{access.ОПИСАНИЕ[роль]}</i>")
    try:
        say(int(uid), f"Тебе выдали доступ к боту Cappi Core.\n"
                      f"Роль: <b>{роль}</b> — {access.ОПИСАНИЕ[роль]}\n\n"
                      f"Напиши /start.")
    except Exception:
        pass


def cmd_botstate(chat):
    очередь = load_pending()
    строки = [
        "<b>Состояние бота</b>", "",
        f"запущен: <code>{datetime.fromtimestamp(START):%d.%m %H:%M}</code>",
        f"в очереди проверок: <b>{len(очередь)}</b>",
        f"словарь написаний: <b>{sum(len(v) for v in ALIASES.values())}</b>",
        f"итоги дня в <b>{REPORT_AT}</b>",
        f"проверка цен на сайте и в Glovo через <b>{CHECK_AFTER_MIN} мин</b>",
        f"порог опечатки: <b>{MAX_CHANGE_PCT}%</b>",
        "", f"папка: <code>{HERE}</code>",
    ]
    say(chat, "\n".join(строки))


# --------------------------------------------------------------- смена цены
def дата_из_текста(строка):
    """Дата из аргумента команды или None, если это не дата.

    Раньше `date.fromisoformat` вызывался прямо на пользовательском вводе:
    «/report абв» или «/report 2026-13-45» роняли обработчик, и человек
    получал «Сломалось» вместо «это не дата».
    """
    строка = (строка or "").strip()
    for формат in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%d.%m"):
        try:
            д = datetime.strptime(строка, формат).date()
            return д.replace(year=date.today().year) if формат == "%d.%m" else д
        except ValueError:
            continue
    return None


def _default_date():
    """Завтра — чтобы цена сменилась ночью, а не в рабочий день."""
    return date.today() + timedelta(days=1) if DEFAULT_TOMORROW else date.today()


def prepare(chat, code, new_price, when, user, несмотря=False):
    """Готовим изменение и показываем карточку подтверждения.

    Ещё ничего не меняем. `несмотря` — человек увидел предупреждение о
    крупном скачке и подтвердил, что цена верная.
    """
    if not access.можно(chat, "цены"):
        return say(chat, f"Менять цены может оператор или админ, "
                         f"а у тебя роль «{access.роль(chat) or 'без роли'}». "
                         f"Посмотреть цену могу.")
    hits = [h for h in find(code) if str(h[0]).lower() == str(code).lower()]
    if not hits:
        return say(chat, f"Нет позиции с артикулом <code>{code}</code>.")
    _, p = hits[0]
    old = p["price"]
    скачок = abs(new_price - old) / max(old, 1) * 100
    if скачок > MAX_CHANGE_PCT and not несмотря:
        # Порог защищает от опечатки, а не запрещает крупные правки. Раньше
        # здесь был тупик и совет «делай в Syrve руками» — а руками как раз
        # и не надо: приказ, сделанный мимо бота, никто потом не проверит на
        # сайте и в Glovo. Спрашиваем второй раз и проводим.
        куда = "дороже" if new_price > old else "дешевле"
        тк = f"J{int(time.time())}{chat % 1000}"
        with _lock:
            _confirm[тк] = {"скачок": True, "code": code, "price": new_price,
                            "date": when.isoformat(), "chat": chat, "user": user}
        return say(chat,
                   f"⚠️ <b>{p['name']}</b>\n"
                   f"<b>{fmt(old)} → {fmt(new_price)} ₴</b> — "
                   f"на {скачок:.0f}% {куда}\n\n"
                   f"Это больше порога в {MAX_CHANGE_PCT}%, который стоит "
                   f"против опечатки. Если цена верная — проведу.",
                   inline=[[{"text": "⚠️ Да, цена верная",
                             "callback_data": f"gv:{тк}"},
                            {"text": "✖️ Отмена", "callback_data": f"no:{тк}"}]])
    with cappi.Syrve() as s:
        match = [x for x in s.products() if str(x.get("num")) == str(code)]
        if not match:
            return say(chat, f"Артикул <code>{code}</code> есть в меню, "
                             f"но нет в номенклатуре.")
        pid = match[0]["id"]
        cur, dep = s.price_of(pid, when.isoformat())
    if dep is None:
        return say(chat, f"У позиции нет действующей цены на {when:%d.%m} — "
                         f"приказ вслепую делать не буду.")

    tok = f"{int(time.time())}{chat % 1000}"
    with _lock:
        _confirm[tok] = {"pid": pid, "dep": dep, "price": new_price,
                         "date": when.isoformat(), "code": code, "name": p["name"],
                         "old": cur, "chat": chat, "user": user}
    сегодня = when == date.today()
    other = date.today() if сегодня is False else date.today() + timedelta(days=1)
    когда = ("<b>сейчас</b>, посреди дня — задену открытые кассовые смены"
             if сегодня else
             f"ночью с {date.today():%d.%m} на {when:%d.%m}, около 3:00")
    say(chat,
        f"<b>{p['name']}</b>\nартикул <code>{code}</code>\n\n"
        f"<b>{fmt(cur)} ₴  →  {fmt(new_price)} ₴</b>\n"
        f"{когда}",
        inline=[
            [{"text": "✅ Провести", "callback_data": f"go:{tok}"},
             {"text": "✖️ Отмена", "callback_data": f"no:{tok}"}],
            [{"text": ("⚡️ Поменять сейчас" if not сегодня
                       else f"🌙 Лучше ночью, на {other:%d.%m}"),
              "callback_data": f"dt:{tok}"}],
        ])


def do_change(c):
    with cappi.Syrve() as s:
        doc = s.set_price(c["pid"], c["dep"], c["price"], c["date"])
    audit(f'{c["user"]}\t{c["code"]}\t{c["name"]}\t{c["old"]} -> {c["price"]}\t'
          f'с {c["date"]}\tприказ №{doc["documentNumber"]}')
    сегодня = c["date"] == date.today().isoformat()
    # Приказ на завтра проверять сегодня бессмысленно — цена ещё
    # не должна была измениться. Сначала убеждаемся, что Syrve его принял и
    # показывает как запланированный, а сайт и Glovo смотрим в тот день.
    with _очередь:
        items = load_pending()
        items.append({**{k: c[k] for k in ("code", "name", "price", "old",
                                           "chat", "date")},
                      "guid": c["pid"],
                      "doc": doc["documentNumber"],
                      "stage": "showcase" if сегодня else "planned",
                      "due": (datetime.now() + timedelta(
                          minutes=CHECK_AFTER_MIN if сегодня else 5)).isoformat()})
        save_pending(items)
    return doc



# ------------------------------------------------------------- цены списком
# Прейскурант присылают так, как он лежит в таблице: строка = позиция, в
# конце цена. Отвечать на такое «Не понял» — заставлять человека делать
# тридцать раз /set вручную, ради чего бот и не нужен.
СТРОКА_ЦЕНЫ = re.compile(
    r"^\s*(?P<имя>.*?[^\d\s.,])[\s.]+(?P<цена>\d+(?:[.,]\d{1,2})?)\s*(?:грн|₴|uah)?\s*$",
    re.I)


def разобрать_список(текст):
    """Строки «название … цена». Цена — последнее число: в названиях свои
    числа («20 гр», «2 шт», «0,33 л»), и брать первое нельзя."""
    строки, мусор = [], []
    for сырое in текст.splitlines():
        s = сырое.strip().strip("•*-–—|\t ")
        if not s or s.lower().startswith(("итого", "всего", "цена", "назва")):
            continue
        m = СТРОКА_ЦЕНЫ.match(s)
        if not m:
            мусор.append(s)
            continue
        имя = m.group("имя").strip(" .,–—-")
        цена = float(m.group("цена").replace(",", "."))
        if len(имя) < 3 or not 0 < цена < 100000:
            мусор.append(s)
            continue
        строки.append({"имя": имя, "цена": цена})
    return строки, мусор


def похоже_на_список(текст):
    """Список — это когда так выглядит большинство строк, а не одна.

    Порог в три строки нарочный: «Пепероні 96» — это поиск позиции, а не
    прейскурант, и уводить одиночный запрос в массовую смену цен нельзя.
    """
    строки, мусор = разобрать_список(текст)
    return len(строки) >= 3 and len(строки) >= len(мусор)


def cmd_price_list(chat, текст, who):
    if not access.можно(chat, "цены"):
        return say(chat, f"Менять цены может оператор или админ, "
                         f"а у тебя роль «{access.роль(chat) or 'без роли'}». "
                         f"Показать, что в списке, могу — менять нет.")
    строки, мусор = разобрать_список(текст)
    say(chat, f"Разбираю {склонение(len(строки), 'строку', 'строки', 'строк')} "
              f"— смотрю текущие цены…")

    когда = _default_date()
    нашёл, спорные, нет_позиции, без_цены, скачки, совпали = [], [], [], [], [], []
    with cappi.Syrve() as s:
        товары = [p for p in s.products() if not p.get("deleted")]
        индекс = {}
        for p in товары:
            индекс.setdefault(cappi.norm_full(p["name"]), []).append(p)
        for r in строки:
            варианты = индекс.get(cappi.norm_full(r["имя"]), [])
            if not варианты:
                нет_позиции.append(r)
                continue
            if len(варианты) > 1:
                # Два товара с одним названием — какой из них имели в виду,
                # знает только человек. Молча выбрать первый значит с шансом
                # 50% поменять цену не тому.
                спорные.append({**r, "сколько": len(варианты)})
                continue
            p = варианты[0]
            цена, отдел = s.price_of(p["id"], когда.isoformat())
            строка = {**r, "pid": p["id"], "dep": отдел, "было": цена,
                      "название": p["name"], "код": p.get("num"),
                      "price": r["цена"]}
            if отдел is None:
                без_цены.append(строка)
            elif цена is not None and abs(цена - r["цена"]) < 0.005:
                совпали.append(строка)
            elif (цена and abs(r["цена"] - цена) / max(цена, 1) * 100
                  > MAX_CHANGE_PCT):
                скачки.append(строка)
            else:
                нашёл.append(строка)

    if not нашёл and not скачки:
        причины = ", ".join(filter(None, [
            f"{len(совпали)} уже с такой ценой" if совпали else "",
            f"{len(нет_позиции)} не нашёл" if нет_позиции else "",
            f"{len(спорные)} с одинаковыми названиями" if спорные else "",
            f"{len(без_цены)} без действующей цены" if без_цены else ""]))
        return say(chat, f"Менять нечего: {причины}." if причины else
                   "Менять нечего — в списке нет ни одной цены, "
                   "отличной от текущей.")

    tok = f"L{int(time.time())}{chat % 1000}"
    with _lock:
        _confirm[tok] = {"строки": нашёл, "скачки": скачки,
                         "date": когда.isoformat(), "chat": chat, "user": who}

    текст_ = [f"<b>Прейскурант: {len(нашёл)} позиций</b>", ""]
    for r in нашёл[:20]:
        текст_.append(f"• {r['название']} — <b>{fmt(r['было'])} → "
                      f"{fmt(r['цена'])} ₴</b>")
    if len(нашёл) > 20:
        текст_.append(f"<i>…и ещё {len(нашёл) - 20}</i>")

    def хвост(заголовок, список, как=lambda r: r["имя"]):
        if список:
            текст_.append("")
            текст_.append(f"<i>{заголовок}:</i> " +
                          ", ".join(как(r) for r in список[:8]) +
                          (f" <i>и ещё {len(список) - 8}</i>"
                           if len(список) > 8 else ""))

    хвост("Уже с такой ценой", совпали)
    хвост("Не нашёл в номенклатуре", нет_позиции)
    хвост("Несколько товаров с таким названием", спорные)
    хвост("Нет действующей цены", без_цены)
    if скачки:
        текст_ += ["", f"⚠️ <b>Не беру — скачок больше {MAX_CHANGE_PCT}%:</b>"]
        текст_ += [f"• {r['название']}: {fmt(r['было'])} → {fmt(r['цена'])} ₴"
                   for r in скачки[:8]]
        текст_.append("<i>Порог против опечатки. Если цены верные — "
                      "жми «взять и скачки» внизу.</i>")

    сегодня = когда == date.today()
    текст_ += ["", ("Проведу <b>сейчас</b>, посреди дня — задену открытые смены."
                    if сегодня else
                    f"Проведу ночью на {когда:%d.%m}, около 3:00.")]
    другая = date.today() + timedelta(days=1) if сегодня else date.today()
    кнопки = [[{"text": f"✅ Провести {len(нашёл)}", "callback_data": f"gl:{tok}"},
               {"text": "✖️ Отмена", "callback_data": f"no:{tok}"}]]
    if скачки:
        кнопки.append([{"text": f"⚠️ Взять и скачки ({len(скачки)})",
                        "callback_data": f"gj:{tok}"}])
    if нашёл:
        кнопки.append([{"text": ("⚡️ Поменять сейчас" if not сегодня
                                 else f"🌙 Лучше ночью, на {другая:%d.%m}"),
                        "callback_data": f"ld:{tok}"}])
    say(chat, "\n".join(текст_), inline=кнопки)


def провести_список(chat, c, who):
    строки, когда = c["строки"], c["date"]
    if not строки:
        return say(chat, "Список пуст.")
    try:
        with cappi.Syrve() as s:
            doc = s.set_prices(строки, когда)
    except cappi.PriceOrderExists as e:
        имена = ", ".join(r["название"] for r, _ in e.позиции[:5]) or "позиции"
        return say(chat,
                   f"⚠️ Не провёл <b>ничего</b>: на "
                   f"{date.fromisoformat(e.date):%d.%m} уже есть приказ "
                   f"<b>№{e.number}</b> — там {имена}"
                   f"{' и другие' if len(e.позиции) > 5 else ''}.\n\n"
                   f"Второй приказ на ту же дату Syrve не примет, а провести "
                   f"половину списка хуже, чем не проводить: часть цен уедет "
                   f"на сайт, часть нет.\n\n"
                   f"Поставь список на другую дату или закрой тот приказ.")
    except Exception as e:
        return say(chat, f"❌ Не получилось: {e}")

    for r in строки:
        audit(f'{who}\t{r.get("код")}\t{r["название"]}\t{r["было"]} -> '
              f'{r["цена"]}\tс {когда}\tприказ №{doc["documentNumber"]} (списком)')
    сегодня = когда == date.today().isoformat()
    with _очередь:
        items = load_pending()
        for r in строки:
            items.append({"code": r.get("код"), "name": r["название"],
                          "price": r["цена"], "old": r["было"], "chat": chat,
                          "date": когда, "guid": r["pid"],
                          "doc": doc["documentNumber"],
                          "stage": "showcase" if сегодня else "planned",
                          "due": (datetime.now() + timedelta(
                              minutes=CHECK_AFTER_MIN if сегодня else 5)
                                  ).isoformat()})
        save_pending(items)
    say(chat, f"✅ Приказ <b>№{doc['documentNumber']}</b> проведён — "
              f"<b>{склонение(len(строки), 'позиция', 'позиции', 'позиций')}</b> с "
              f"{date.fromisoformat(когда):%d.%m}.\n\n"
              + ("<i>Цены уже в Syrve. Выгрузка идёт раз в 20 минут — "
                 "слежу и напишу, как только встанут на сайте.</i>" if сегодня else
                 "<i>Сайт и Glovo проверю сам и напишу, если где-то "
                 "не совпадёт.</i>"))


# ------------------------------------------------------- фоновая проверка
def _check_planned(it):
    """Приказ на будущее: он ещё не сработал, но Syrve уже должен знать цену
    на ту дату. Это и есть доказательство, что цена сменится.

    Спрашиваем у Syrve ТП, а не у Cloud API. Cloud отдаёт `nextPrice`
    только после выгрузки, и для приказа на завтра там честный `None` —
    из-за этого бот писал «Syrve не показывает запланированную цену» по
    совершенно исправному приказу №0093 и пугал на ровном месте.
    """
    nxt = None
    try:
        with cappi.Syrve() as s:
            nxt, _ = s.price_of(it["guid"], it["date"])
    except Exception as e:
        bugs.записать("апи", f"проверка приказа {it.get('doc')}: {e}",
                      где="_check_planned")
    ok = nxt is not None and abs(nxt - it["price"]) < 0.01
    when = it["date"]
    when_h = datetime.fromisoformat(it["date"]).strftime("%d.%m")
    if ok:
        say(it["chat"],
            f"✅ Приказ принят и стоит в очереди\n\n<b>{it['name']}</b>\n"
            f"приказ №{it['doc']}, {fmt(it['old'])} → {fmt(it['price'])} ₴\n"
            f"сменится ночью, {when_h} около 3:00\n\n"
            f"<i>Проверю сайт и Glovo утром {when_h}.</i>")
    else:
        say(it["chat"],
            f"⚠️ <b>Цена на {when_h} не встала</b>\n\n<b>{it['name']}</b>\n"
            f"приказ №{it['doc']} создан, но на {when_h} Syrve отдаёт "
            f"{f'{fmt(nxt)} ₴' if nxt is not None else 'пусто'}, "
            f"а должен {fmt(it['price'])} ₴.\n\n"
            f"<i>Стоит открыть приказ в Syrve и проверить, что он "
            f"проведён.</i>")
    # В любом случае смотрим цены в день, когда они должны смениться.
    return {**it, "stage": "showcase",
            "due": datetime.fromisoformat(it["date"]).replace(hour=10).isoformat()}


# Сколько ждём «сейчас»: выгрузка идёт раз в 20 минут, Glovo подтягивается
# позже. Ждать молча целый час нельзя — человек поменял цену среди дня
# именно потому, что она нужна сейчас.
БЫСТРАЯ_ПРОВЕРКА_МИН = 2
ЖДЁМ_ВЫГРУЗКУ_МИН = 75


def _check_showcase(it):
    """Цена уже должна была смениться — сверяем сайт и Glovo.

    Для смены «прямо сейчас» проверяем часто и коротко: как только цена
    появилась — говорим. Одна проверка через полчаса отвечала на вопрос
    «доехало?» ровно один раз и почти всегда не вовремя.
    """
    site, glovo = where_shown(it.get("guid", ""), it["name"])
    p = it["price"]
    ok_site = site is not None and abs(site - p) < 0.01
    ok_glovo = glovo is not None and abs(glovo - p) < 0.01
    сейчас = it["date"] == date.today().isoformat()
    ждём = it.get("ждём_с") or datetime.now().isoformat()
    прошло = (datetime.now() - datetime.fromisoformat(ждём)).total_seconds() / 60
    # Сайт — главное: Glovo тянет с задержкой и своим расписанием, из-за
    # него можно ждать час и в итоге сказать то же самое.
    доехало = ok_site or (site is None and ok_glovo)
    if сейчас and not доехало and прошло < ЖДЁМ_ВЫГРУЗКУ_МИН:
        if not it.get("ждём_с"):
            say(it["chat"],
                f"⏳ <b>{it['name']}</b> — приказ проведён, жду выгрузку.\n"
                f"<i>Она идёт раз в 20 минут. Проверяю каждые "
                f"{БЫСТРАЯ_ПРОВЕРКА_МИН} минуты и напишу, как только цена "
                f"встанет на сайте.</i>")
        return {**it, "ждём_с": ждём,
                "due": (datetime.now()
                        + timedelta(minutes=БЫСТРАЯ_ПРОВЕРКА_МИН)).isoformat()}
    if сейчас and доехало and it.get("ждём_с"):
        say(it["chat"],
            f"✅ <b>Цена на сайте</b> — через {прошло:.0f} мин после приказа\n"
            f"{it['name']}: <b>{fmt(it['old'])} → {fmt(p)} ₴</b>"
            + ("" if ok_glovo else "\n\n<i>Glovo подтянется позже, "
                                   "у него своё расписание.</i>"))
        return None
    mark = lambda ok, v: ("✅" if ok else "❌") + f" {fmt(v) if v is not None else 'нет'}"
    say(it["chat"],
        ("✅ Цена доехала везде" if ok_site and ok_glovo
         else "⚠️ Цена доехала не везде")
        + f"\n\n<b>{it['name']}</b>\n"
          f"приказ №{it['doc']}, {fmt(it['old'])} → {fmt(p)} ₴\n\n"
          f"сайт:  {mark(ok_site, site)}\n"
          f"Glovo: {mark(ok_glovo, glovo)}"
        + ("" if ok_site and ok_glovo else
           "\n\n<i>Выгрузка идёт раз в 20 минут, Glovo подтягивается позже — "
           "до часа. Проверь ещё раз кнопкой «Сайт и Glovo».</i>"))
    return None


def _день_отправлен(день):
    """Пережил ли рестарт факт отправки. В памяти он терялся, и контейнер,
    перезапущенный после 22:00, слал итоги дня второй раз."""
    return _виденное("отчёт") == {день.isoformat()}


def _пометить_день(день):
    _запомнить("отчёт", [день.isoformat()])


def _send_daily(sent):
    """Итоги дня в REPORT_AT. sent помнит дату, чтобы не отправить дважды."""
    now = datetime.now()
    if now.strftime("%H:%M") < REPORT_AT or _день_отправлен(now.date()):
        return
    # Строим ДО отметки: сбой Syrve в 22:00 иначе молча убивал сводку дня —
    # день помечен отправленным, а ничего не ушло.
    try:
        текст = report.render(report.collect(), live=False)
    except Exception:
        traceback.print_exc()
        return
    дошло = False
    for uid in access.подписчики_отчёта():
        try:
            say(uid, текст)
            дошло = True
        except Exception:
            traceback.print_exc()
    if дошло:
        _пометить_день(now.date())
        sent["day"] = now.date()


def _зона_изменилась(e):
    """Событие о зоне — сообщаем сразу, не дожидаясь отчёта.

    Пишем словами, а не полями: «зоны: 17, 18, 19» и «без филиала» — это
    дамп ответа API, а читает его человек в час пик. Номера зон
    разворачиваем в названия, пустые поля просто не показываем.
    """
    закрытие = e.get("event") == "zone.close"
    зоны = e.get("zone_ids") or []

    # Названия вместо номеров; повторы убираем — «Фіолетова, Рожева,
    # Фіолетова» получается, когда под одним именем несколько зон.
    район = (e.get("district")
             or next(((jamshut.зона_имя(z) or {}).get("district") for z in зоны
                      if (jamshut.зона_имя(z) or {}).get("district")), None)
             or "")

    # «Фіолетова зона Котовського» при шапке «Закрыт Котовського» — район
    # написан дважды. Оставляем только то, чем зоны отличаются: цвет.
    имена = []
    for z in зоны:
        имя = ((jamshut.зона_имя(z) or {}).get("name") or f"зона {z}")
        for лишнее in (" зона", район, район.capitalize(), район.title()):
            if лишнее:
                имя = имя.replace(лишнее, "")
        имя = " ".join(имя.split()) or f"зона {z}"
        if имя not in имена:
            имена.append(имя)

    минут = e.get("duration_min")
    шапка = ("🚧 <b>Закрыт" if закрытие else "✅ <b>Открыт")
    шапка += f" {район.capitalize()}</b>" if район else "</b>"
    if закрытие and минут:
        шапка += f" — на {минут} мин"

    строки = [шапка]
    if имена:
        строки.append(", ".join(имена))

    авто = e.get("auto") or e.get("source") == "bot"
    кто = "по таймеру" if авто else (e.get("actor") or "—")
    if закрытие:
        хвост = ""
        try:
            до = datetime.fromisoformat(e["at"]) + timedelta(minutes=int(минут or 0))
            хвост = f" · до {до:%H:%M}"
        except Exception:
            pass
        строки.append(f"Закрыл: {кто}{хвост}")
    else:
        было = f"Простоял {минут} мин · " if минут else ""
        строки.append(f"{было}открыл: {кто}")

    ctx = e.get("context") or {}
    if ctx:
        штат = ctx.get("staff") or {}
        строки.append("")
        части = []
        if ctx.get("kitchen") is not None:
            части.append(f"кухня {ctx['kitchen']}")
        if ctx.get("onway") is not None:
            части.append(f"в пути {ctx['onway']}")
        строки.append(f"Заказов в работе {ctx.get('in_work', '?')}"
                      + (f" — {', '.join(части)}" if части else ""))
        if штат:
            смена = []
            if штат.get("cooks") is not None:
                смена.append(f"{штат['cooks']} поваров")
            занято, всего = штат.get("couriers_busy"), штат.get("couriers")
            if всего is not None:
                смена.append(f"{занято} из {всего} курьеров" if занято is not None
                             else f"{всего} курьеров")
            if штат.get("admins") is not None:
                смена.append(f"{штат['admins']} админа")
            if смена:
                строки.append("Смена: " + ", ".join(смена))
    else:
        # Пустой контекст — это «Syrve не ответил», а не «всё спокойно».
        строки += ["", "<i>обстановка неизвестна — Syrve не ответил</i>"]

    for uid in access.подписчики_отчёта():
        try:
            say(uid, "\n".join(строки))
        except Exception:
            pass


def _виденное(раздел):
    try:
        return set(json.load(open(ALERTS)).get(раздел, []))
    except Exception:
        return set()


def _записать_json(путь, данные):
    """Пишем через временный файл и переименование.

    Обычный open('w') при обрыве (деплой, OOM) оставляет обрезанный файл;
    читатели глотают ошибку и считают состояние пустым — а это значит
    повторную рассылку всех сегодняшних алертов и потерю очереди проверок.
    """
    врем = путь + ".tmp"
    with open(врем, "w") as f:
        json.dump(данные, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(врем, путь)


def _запомнить(раздел, ключи):
    try:
        d = json.load(open(ALERTS))
    except Exception:
        d = {}
    # Держим только сегодняшнее: вчерашние ключи ни с чем не сравниваются,
    # а файл иначе растёт без конца.
    d[раздел] = sorted(ключи)
    _записать_json(ALERTS, d)


МАРКЕТОЛОГ = 770891676     # кому адресован запрос по тайному гостю
ШЕФ = None                 # telegram-id шефа; ставится командой /people


def _просить_корректировки(последний):
    """2-го числа — запрос шефу: кто стоял на кухне не своей ролью.

    В явках роль записана та, под которой человек отметился. Если
    администратор отстоял смену на кухне, часы уйдут не туда, и процент
    разделится неправильно. Знает об этом только шеф.
    """
    if time.time() - последний[0] < 3600:
        return
    последний[0] = time.time()
    if date.today().day != kpi.ЗАПРОС_КОРРЕКТИРОВОК_ДЕНЬ:
        return
    месяц, конец = kpi.прошлый_месяц()
    if kpi.состояние_запроса("правки:" + месяц).get("отправлен"):
        return
    имя_месяца = МЕСЯЦЫ[конец.month - 1]
    шеф = kpi.люди().get("шеф_id")
    if not шеф:
        # Молчать в этом месте — худшее, что можно сделать: проценты
        # разделятся по неисправленным часам, и никто не узнает, что запрос
        # вообще не уходил. Поэтому говорим администраторам.
        for admin in (int(u) for u, v in access.все().items()
                      if v.get("роль") == "админ"):
            try:
                say(admin, f"⚠️ <b>Некому отправить запрос правок за "
                           f"{имя_месяца}</b>\n"
                           f"Шеф не привязан к боту, поэтому часы кухни "
                           f"останутся такими, как в явках — со всеми "
                           f"ошибками ролей.\n\n"
                           f"Привязать: <code>/people шеф_id 123456789</code>\n"
                           f"<i>id шефа — пусть напишет боту, бот покажет его "
                           f"в ответе.</i>")
            except Exception:
                pass
        kpi.состояние_запроса("правки:" + месяц,
                              {"отправлен": date.today().isoformat(),
                               "некому": True})
        return
    try:
        say(int(шеф),
            f"🍳 <b>Часы кухни за {имя_месяца}</b>\n\n"
            f"Процент делится по отработанным часам. Если кто-то стоял на "
            f"кухне, а в явке записан другой ролью — поправь:\n\n"
            f"<code>/fix Фамилия +12</code>\n"
            f"<i>плюс часы, которые надо добавить</i>\n\n"
            f"Посмотреть, как сейчас делится: кнопка «💵 Процент кухни».\n"
            f"Если всё верно — ничего делать не надо.")
        kpi.состояние_запроса("правки:" + месяц,
                              {"отправлен": date.today().isoformat()})
    except Exception:
        traceback.print_exc()


def _подчистить_журналы(когда):
    """Раз в сутки убираем из журнала сбоев старьё.

    Функция чистки была написана и не вызывалась ниоткуда: файл рос
    вечно, а вместе с ним — время открытия экрана сбоев.
    """
    if time.time() - когда[0] < 86400:
        return
    когда[0] = time.time()
    try:
        осталось = bugs.подчистить()
        print(f"журнал сбоев подчищен: {осталось} записей")
    except Exception as e:
        print(f"не смог подчистить журнал сбоев: {e}")


def _просить_тайники(последний):
    """3-го числа — запрос маркетологу, 5-го и дальше — напоминание.

    Проверяем раз в час, а не раз в минуту: событие суточное, и частить
    незачем. Само письмо уходит один раз в день — в состоянии записана
    дата последней отправки.
    """
    if time.time() - последний[0] < 3600:
        return
    последний[0] = time.time()
    try:
        действие, месяц, с = kpi.нужен_запрос()
    except Exception:
        return
    if not действие:
        return

    имя_месяца = МЕСЯЦЫ[int(месяц[5:]) - 1]
    просьба = (f"Сколько тайных гостей было и сколько прошло за "
               f"<b>{имя_месяца}</b>?\n\n"
               f"Жми кнопку ниже — там точка и две цифры, ничего писать "
               f"не нужно.\n"
               f"<i>Или ответь сообщением: <code>Лазарева 5/4</code> — "
               f"из пяти прошли четыре.</i>")
    кнопка = [[{"text": f"🕵️ Внести за {имя_месяца.lower()}",
                "callback_data": f"тм:{месяц}"}]]

    if действие == "запрос":
        try:
            say(МАРКЕТОЛОГ, f"🕵️ <b>Тайный гость за {имя_месяца}</b>\n\n"
                            f"{просьба}\n\nСрок — до {kpi.ДЕДЛАЙН_ДЕНЬ}-го числа.",
                inline=кнопка)
        except Exception:
            traceback.print_exc()
        kpi.состояние_запроса(месяц, {"отправлен": date.today().isoformat(),
                                      "последний": date.today().isoformat(),
                                      "напоминаний": 0})
        return

    # Дедлайн прошёл. Пишем обоим: маркетологу — что просрочено, владельцу —
    # что данных нет и KPI посчитать нельзя.
    н = с.get("напоминаний", 0) + 1
    try:
        say(МАРКЕТОЛОГ, f"⚠️ <b>Просрочено: тайный гость за {имя_месяца}</b>\n"
                        f"Срок был до {kpi.ДЕДЛАЙН_ДЕНЬ}-го.\n\n{просьба}",
            inline=кнопка)
    except Exception:
        pass
    for admin in (int(u) for u, v in access.все().items() if v.get("роль") == "админ"):
        try:
            say(admin, f"⚠️ <b>Нет данных по тайному гостю за {имя_месяца}</b>\n"
                       f"Запрошено {с.get('отправлен')}, "
                       f"напоминание {н}-е.\n"
                       f"<i>Без них KPI за {имя_месяца} посчитать нельзя.</i>")
        except Exception:
            pass
    kpi.состояние_запроса(месяц, {**с, "последний": date.today().isoformat(),
                                  "напоминаний": н})


def _watch_витрины(последний):
    """Раз в сутки сверяем цены на сайте и в Glovo с Syrve.

    Сверка была только кнопкой. Кнопку жмут, когда что-то заподозрили, —
    а цена расходится молча, и заметить это можно лишь случайно.
    Говорим только о новом: одно и то же расхождение каждый день
    превращается в шум, который перестают читать.
    """
    if time.time() - последний[0] < 86400:
        return
    последний[0] = time.time()
    try:
        плохие, _ = расхождения_витрин()
    except Exception as e:
        bugs.записать("апи", f"сверка витрин не прошла: {e}", где="сторож")
        return
    было = _виденное("площадки")
    # Про то, что уже поставлено в приказ, молчим: это не расхождение, а
    # несколько часов ожидания. Скажем завтра, если не выровняется.
    ждут = [r for r in плохие if r.get("запланировано")]
    новые = [r for r in плохие if not r.get("запланировано")
             and f"{r['код']}|{r['syrve']}|{r['сайт']}|{r['glovo']}" not in было]
    if not новые:
        return
    _запомнить("площадки", [f"{r['код']}|{r['syrve']}|{r['сайт']}|{r['glovo']}"
                           for r in плохие])
    хвост_план = (f"\n\n<i>Ещё {len(ждут)} выровняются ночью — там приказ "
                  f"уже стоит.</i>" if ждут else "")
    текст = ("💸 <b>Цена для гостя не совпадает с Syrve</b>\n"
             "<i>Гость платит то, что видит на сайте или в Glovo.</i>\n\n"
             + "\n".join(_строка_расхождения(r) for r in новые[:10]))
    if len(новые) > 10:
        текст += f"\n\n<i>…и ещё {len(новые) - 10}</i>"
    текст += хвост_план
    for кому in access.подписчики_отчёта():
        try:
            say(кому, текст)
        except Exception:
            pass


def _watch_losses(последний):
    """Списания блюд и правки явок — то, что происходит тихо.

    Обе вещи законны по отдельности и обе стоят денег. Списание видно
    только в отчёте назавтра, правка явки — вообще нигде. Раз в десять
    минут сверяемся с тем, о чём уже сообщали, и говорим только о новом.
    """
    if time.time() - последний[0] < 600:
        return
    последний[0] = time.time()
    сегодня = date.today()
    try:
        with cappi.Syrve() as s:
            списания = report.deletions(s, сегодня)
            явки = report.attendance(s, сегодня)
    except Exception:
        return

    было = _виденное(f"списания:{сегодня}")
    новые = [x for x in списания
             if f"{x['чек']}|{x['блюдо']}|{x['причина']}" not in было]
    if новые:
        сумма = sum(x["сумма"] for x in новые)
        строки = [f"🗑 <b>Списано за счёт компании: {len(новые)}</b>"
                  + (f" на {report.money(сумма)} ₴" if сумма else "")]
        for x in новые[:12]:
            строки.append(f"    {x['блюдо'][:30]} — {report.money(x['сумма'])} ₴"
                          f"\n      чек {x['чек']} · {x['причина'][:24]} · {x['кто'][:20]}")
        for uid in access.подписчики_отчёта():
            try:
                say(uid, "\n".join(строки))
            except Exception:
                pass
    _запомнить(f"списания:{сегодня}",
               [f"{x['чек']}|{x['блюдо']}|{x['причина']}" for x in списания])

    было = _виденное(f"правки:{сегодня}")
    правки = явки.get("правки") or []
    новые = [p for p in правки
             if f"{p['кто']}|{p['смена']}|{p['правил']}" not in было]
    if новые:
        строки = [f"⚠️ <b>Правки явок задним числом: {len(новые)}</b>"]
        for p in новые[:10]:
            строки.append(f"    {p['кто'][:24]} · {p['роль']} · смена {p['смена']}"
                          f"\n      правка через {p['через']:.0f} мин, {p['правил']}")
        for uid in access.подписчики_отчёта():
            try:
                say(uid, "\n".join(строки))
            except Exception:
                pass
    _запомнить(f"правки:{сегодня}",
               [f"{p['кто']}|{p['смена']}|{p['правил']}" for p in правки])


def _watch_stoplist(последний):
    """Что появилось в стопе и что ушло с прошлой проверки.

    API отдаёт только «как сейчас», поэтому сравниваем со слепком: сам
    момент попадания в стоп иначе проходит незамеченным, а он и есть то,
    о чём стоит сказать сразу — позиция перестала продаваться.
    """
    if time.time() - последний[0] < 300:
        return
    последний[0] = time.time()
    try:
        изм = stoplist.изменения()
    except Exception:
        return
    if not (изм["новые"] or изм["ушли"]):
        return
    строки = []
    if изм["новые"]:
        потери = sum(п["цена"] or 0 for п in изм["новые"])
        строки.append(f"🛑 <b>В стоп ушло: {len(изм['новые'])}</b>"
                      + (f" · {report.money(потери)} ₴ по прайсу" if потери else ""))
        for п in изм["новые"]:
            строки.append(f"    {п['название'][:36]} — "
                          f"{report.money(п['цена'] or 0)} ₴  <i>{п['точка']}</i>")
    if изм["ушли"]:
        строки.append(f"✅ <b>Вернулось в продажу: {len(изм['ушли'])}</b>")
        for п in изм["ушли"]:
            строки.append(f"    {п['название'][:36]}  <i>{п['точка']}</i>")
    строки.append(f"\n<i>всего в стопе сейчас: {len(изм['всего'])}</i>")
    for uid in access.подписчики_отчёта():
        try:
            say(uid, "\n".join(строки))
        except Exception:
            pass


def _pull_zones(последний):
    """Раз в пять минут забираем у Джамшута новые события о зонах.

    Опрос, а не только вебхук: пуш требует публичного адреса, которого у
    бота пока нет. Хранилище и отпечаток общие, поэтому если позже включим
    и вебхук, история не задвоится.
    """
    if time.time() - последний[0] < 300:
        return
    последний[0] = time.time()
    try:
        было = {e["_fp"] for e in webhook.events(date.today())}
        webhook.pull()
        for e in webhook.events(date.today()):
            if e["_fp"] not in было:
                _зона_изменилась(e)
    except Exception:
        traceback.print_exc()


def watcher():
    """Догоняет проверки, опрашивает Джамшута и стоп-лист, шлёт итоги дня."""
    sent = {}
    последний_опрос = [0.0]
    последний_стоп = [0.0]
    последний_потери = [0.0]
    последний_тайник = [0.0]
    последняя_сверка = [0.0]
    последняя_негода = [0.0]
    последняя_чистка = [0.0]
    последний_правки = [0.0]      # свой таймер: общий с тайниками не срабатывал
    while True:
        try:
            _send_daily(sent)
            _подчистить_журналы(последняя_чистка)
            _просить_тайники(последний_тайник)
            _просить_корректировки(последний_правки)
            _pull_zones(последний_опрос)
            _watch_витрины(последняя_сверка)
            _watch_негода(последняя_негода)
            _watch_stoplist(последний_стоп)
            _watch_losses(последний_потери)
            with _очередь:
                keep = []
                for it in load_pending():
                    if datetime.now() < datetime.fromisoformat(it["due"]):
                        keep.append(it)
                        continue
                    try:
                        nxt = (_check_planned(it) if it.get("stage") == "planned"
                               else _check_showcase(it))
                    except Exception as e:
                        # Сбой по одному элементу не должен ронять цикл: иначе
                        # уже обработанные шлются повторно, а вечно падающий
                        # элемент блокирует очередь навсегда.
                        traceback.print_exc()
                        bugs.сбой(f"проверка {it.get('code')}", e)
                        nxt = {**it, "due": (datetime.now()
                                             + timedelta(minutes=15)).isoformat()}
                    if nxt:
                        keep.append(nxt)
                save_pending(keep)
        except Exception:
            traceback.print_exc()
        time.sleep(60)


# ------------------------------------------------------------------- кнопки
def _номер(значение):
    """Число из аргумента кнопки или None.

    Кнопки живут в чате вечно: нажатую вчера жмут сегодня, старый экран
    открывают после обновления бота. Аргумент в ней может быть каким
    угодно, и падать на этом — значит писать «Сломалось» вместо «кнопка
    устарела».
    """
    try:
        return int(str(значение).strip())
    except (TypeError, ValueError):
        return None


def on_button(q):
    chat = q["message"]["chat"]["id"]
    if not access.есть_доступ(q["from"]["id"]):
        return
    act, _, arg = q["data"].partition(":")
    tg("answerCallbackQuery", callback_query_id=q["id"])
    who = q["from"].get("username") or str(q["from"]["id"])

    if act == "ag":                                   # выдать доступ по id
        if not access.можно(chat, "доступ"):
            return say(chat, "Выдавать доступ может только админ. "
                             "Попроси того, кто тебя сюда добавил.")
        uid, _, роль = arg.partition(":")
        if роль not in access.РОЛИ:
            return say(chat, "Кнопка устарела — пришли id заново.")
        try:
            access.добавить(uid, роль, кто=chat)
        except ValueError as e:
            return say(chat, f"Не выйдет: {e}")
        say(chat, f"✅ <code>{uid}</code> — <b>{роль}</b>\n"
                  f"<i>{access.ОПИСАНИЕ[роль]}</i>")
        try:
            say(int(uid), f"Тебе выдали доступ к боту Cappi Core.\n"
                          f"Роль: <b>{роль}</b> — {access.ОПИСАНИЕ[роль]}\n\n"
                          f"Напиши /start.")
        except Exception:
            pass
        return

    if act == "zc":                                   # выбрали зону → на сколько
        зид = _номер(arg)
        if зид is None:
            return say(chat, "Кнопка устарела — открой список зон заново.")
        зона = (jamshut.справочник_зон().get(зид) or {}).get("name", arg)
        return say(chat, f"На сколько закрыть <b>{зона}</b>?", inline=[
            [{"text": f"{м} мин", "callback_data": f"zd:{arg}:{м}"} for м in (15, 30, 45)],
            [{"text": f"{м} мин", "callback_data": f"zd:{arg}:{м}"} for м in (60, 90, 120)],
        ])

    if act == "zd":                                   # подтверждение закрытия
        зид, _, минут = arg.partition(":")
        if _номер(зид) is None or _номер(минут) is None:
            return say(chat, "Кнопка устарела — открой список зон заново.")
        z = jamshut.справочник_зон().get(_номер(зид)) or {}
        return say(chat,
            f"🚧 Закрыть <b>{z.get('name', зид)}</b>\n"
            f"район: {z.get('district', '—')}\n"
            f"на <b>{минут} мин</b>\n\n"
            f"<i>Доставка в эту зону прекратится сразу.</i>",
            inline=[[{"text": "✅ Закрыть", "callback_data": f"zy:{зид}:{минут}"},
                     {"text": "✖️ Отмена", "callback_data": "no:—"}]])

    if act in ("zy", "zo"):                           # выполняем
        if not access.можно(chat, "зоны"):
            return say(chat, "Закрывать и открывать зоны может оператор или "
                         "админ — доставка это чувствует сразу.")
        if act == "zy" and not jamshut.можно_управлять():
            return say(chat, "Нет ключа на запись — закрывать зоны бот не "
                             "может.\n<i>Нужен JAMSHUT_WRITE_TOKEN в конфиге.</i>")
        зид, _, минут = arg.partition(":")
        if _номер(зид) is None or (act == "zy" and _номер(минут) is None):
            return say(chat, "Кнопка устарела — открой список зон заново.")
        z = jamshut.справочник_зон().get(_номер(зид)) or {}
        имя = z.get("name", зид)
        try:
            if act == "zy":
                jamshut.закрыть_зону([_номер(зид)], _номер(минут), who, z.get("district"))
                say(chat, f"🚧 <b>{имя}</b> закрыта на {минут} мин.")
                audit(f"{who}\tзона\tзакрыта\t{имя}\t{минут} мин")
            else:
                return say(chat, f"Открыть <b>{имя}</b>?",
                    inline=[[{"text": "✅ Открыть", "callback_data": f"zk:{зид}"},
                             {"text": "✖️ Отмена", "callback_data": "no:—"}]])
        except jamshut.НетУправления as e:
            say(chat, f"⚠️ {e}")
        except Exception as e:
            say(chat, f"❌ Не получилось: {str(e)[:140]}")
        return

    if act == "zk":                                   # подтверждённое открытие
        if not access.можно(chat, "зоны"):
            return
        if not jamshut.можно_управлять():
            return say(chat, "Нет ключа на запись — открывать зоны бот не "
                             "может.\n<i>Нужен JAMSHUT_WRITE_TOKEN в конфиге.</i>")
        зид = _номер(arg)
        if зид is None:
            return say(chat, "Кнопка устарела — открой список зон заново.")
        z = jamshut.справочник_зон().get(зид) or {}
        имя = z.get("name", arg)
        try:
            jamshut.открыть_зону([зид], who)
            say(chat, f"✅ <b>{имя}</b> открыта.")
            audit(f"{who}\tзона\tоткрыта\t{имя}")
        except Exception as e:
            say(chat, f"❌ Не получилось: {str(e)[:140]}")
        return

    if act in ("sr", "sa"):                           # снять / поставить в стоп
        if not access.можно(chat, "цены"):
            return say(chat, "Управлять стоп-листом может оператор или админ.")
        pid, tid = развернуть(arg)
        if not pid:
            return say(chat, "Кнопка устарела — открой стоп-лист заново.")
        снять = act == "sr"
        имя = next((p["name"] for c_, p in find(pid) if p["id"] == pid), pid[:8])
        точка = stoplist.терминалы().get(tid, tid[:8])
        return say(chat,
            f"{'Снять со стопа' if снять else 'Поставить в стоп'}:\n"
            f"<b>{имя}</b>\nточка: {точка}\n\n"
            + ("<i>Позиция снова начнёт продаваться.</i>" if снять
               else "<i>Продажа прекратится сразу.</i>"),
            inline=[[{"text": "✅ Да", "callback_data":
                      f"{'sy' if снять else 'sn'}:{ссылка(pid, tid)}"},
                     {"text": "✖️ Отмена", "callback_data": "no:—"}]])

    if act in ("sy", "sn"):                           # подтверждено
        if not access.можно(chat, "цены"):
            return
        pid, tid = развернуть(arg)
        if not pid:
            return say(chat, "Кнопка устарела — открой стоп-лист заново.")
        try:
            if act == "sy":
                stoplist.снять(pid, tid)
                say(chat, "✅ Снято со стопа — позиция снова продаётся.")
            else:
                stoplist.поставить(pid, tid)
                say(chat, "🛑 Поставлено в стоп — продажа прекращена.")
            audit(f'{who}\tстоп\t{pid}\t{"снят" if act == "sy" else "поставлен"}\t{tid}')
        except Exception as e:
            say(chat, f"❌ Не получилось: {e}")
        return

    if act == "sx":                                   # убрать из спецпредложения
        if not access.можно(chat, "цены"):
            return say(chat, "Только оператор или админ.")
        try:
            ушло = promo.убрать_спец(arg)
            say(chat, f"🗑 Убрано: <b>{ушло['название']}</b>")
        except KeyError:
            say(chat, "Такого в списке уже нет.")
        return

    if act == "bg":                                   # карточка сбоя
        return cmd_bug_карточка(chat, arg)

    if act == "bd":                                   # сбои за N дней
        return cmd_bugs(chat, дней=_номер(arg) or 14)

    if act == "bf":                                   # сбои по типу
        return cmd_bugs(chat, тип=arg)

    if act == "bs":                                   # пометить сбой
        if not access.можно(chat, "админка"):
            return say(chat, "Помечать сбои может только админ.")
        ид, _, статус = arg.partition(":")
        if статус not in bugs.СТАТУСЫ:
            return say(chat, "Кнопка устарела — открой журнал заново.")
        bugs.пометить(ид, статус, кто=chat)
        say(chat, f"Помечено: <b>{статус}</b>." +
                  ("\n<i>Если повторится — снова появится в списке.</i>"
                   if статус in ("починен", "не баг") else ""))
        return cmd_bugs(chat)

    if act == "нг":                                   # непогода за другой день
        return cmd_негода(chat, date.today() - timedelta(days=1))

    if act == "нгв":                                  # включить/выключить непогоду
        if not access.можно(chat, "зоны"):
            return say(chat, "Включать и выключать непогоду может оператор "
                             "или админ — это деньги гостя.")
        try:
            if arg == "on":
                jamshut.погода_включить(None, who)
                say(chat, "☔ Непогода включена.")
            else:
                jamshut.погода_выключить(who)
                say(chat, "☀️ Непогода выключена.")
            audit(f"{who}\tнепогода\t{arg}")
        except jamshut.НетУправления as e:
            say(chat, f"⚠️ {e}")
        except Exception as e:
            say(chat, f"❌ Не получилось: {str(e)[:140]}")
        return

    if act == "нг":                                   # выбрали группу
        ждём = _await.get(chat)
        группа = next((g for g in _группы() if g["id"].startswith(arg)), None)
        if not ждём or not группа:
            return say(chat, "Кнопка устарела — начни заново.")
        д = ждём.get("блюдо", {})
        д["группа"] = группа
        _await[chat] = {"what": "блюдо_цена", "блюдо": д}
        return say(chat, f"Группа: <b>{группа['name']}</b>\n\nЦена продажи?")

    if act == "сс":                                   # себестоимость блюда
        товар = nomenclature.одна(arg, типы=("DISH", "PREPARED"))
        if not товар:
            return say(chat, "Кнопка устарела — начни заново.")
        return _показать_сс(chat, товар)

    if act == "сз":                                   # выбрали ингредиент
        ждём = _await.pop(chat, None)
        if not ждём or "текст" not in ждём:
            return say(chat, "Кнопка устарела — пришли состав заново.")
        товар = nomenclature.одна(arg, типы=("GOODS", "PREPARED"))
        if not товар:
            return say(chat, "Не нашёл эту позицию.")
        # Запоминаем выбор: в следующий раз «рис» уже не спросим.
        nomenclature.запомнить(ждём["имя"], товар.get("num"))
        if ждём["what"] == "техкарта_выбор":
            return записать_техкарту(chat, ждём["текст"],
                                     {"товар": ждём["товар"]})
        return показать_состав(chat, ждём["текст"])

    if act == "тм":                                   # тайник: выбрали месяц
        return cmd_secret_точка(chat, arg)

    if act == "тт":                                   # тайник: выбрали точку
        месяц, _, точка = arg.partition(":")
        return cmd_secret_ждём(chat, месяц, точка)

    if act == "вр":                                   # время работы: период
        return cmd_время(chat, arg)

    if act == "pc":                                   # процент кухни за месяц
        return cmd_процент(chat, arg)

    if act == "kr":                                   # выбрали роль → месяц
        return cmd_kpi_месяц(chat, arg)

    if act == "km":                                   # роль и месяц → расчёт
        роль, _, месяц = arg.partition(":")
        return cmd_kpi_показать(chat, роль, месяц)

    if act == "sd":                                   # смена за выбранный день
        d = дата_из_текста(arg)
        if d is None:
            return say(chat, "Кнопка устарела — выбери день заново.")
        return cmd_shift(chat, d)

    if act == "sp":
        return cmd_pick_shift_day(chat, _номер(arg) or 0)

    if act == "rd":                                   # отчёт за выбранный день
        d = дата_из_текста(arg)
        if d is None:
            return say(chat, "Кнопка устарела — выбери день заново.")
        return cmd_report(chat, d, live=(d == date.today()))

    if act == "rp":                                   # листаем календарь назад
        return cmd_pick_day(chat, _номер(arg) or 0)

    if act == "ac":                                   # карточка человека
        return экран_человека(chat, arg)

    if act in ("ar", "ax"):                           # смена роли или удаление
        if not access.можно(chat, "доступ"):
            return say(chat, "Менять роли может только админ.")
        uid, _, новая = arg.partition(":")
        try:
            if act == "ax":
                access.убрать(uid)
                say(chat, f"🚫 Доступ у <code>{uid}</code> убран.")
            else:
                access.сменить_роль(uid, новая)
                say(chat, f"✅ <code>{uid}</code> теперь <b>{новая}</b>")
                try:
                    say(int(uid), f"Твоя роль в боте изменена: <b>{новая}</b>\n"
                                  f"<i>{access.ОПИСАНИЕ[новая]}</i>")
                except Exception:
                    pass
        except Exception as e:
            say(chat, f"❌ {e}")
        return cmd_access(chat)

    if act == "it":                                   # выбрали позицию из списка
        hits = [h for h in find(arg) if str(h[0]) == arg]
        return screen_item(chat, *hits[0]) if hits else say(chat, "Позиция пропала из меню.")

    if act == "ed":                                   # «изменить цену»
        hits = [h for h in find(arg) if str(h[0]) == arg]
        if not hits:
            return say(chat, "Позиция пропала из меню.")
        _, p = hits[0]
        _await[chat] = {"what": "price", "code": arg}
        return say(chat, f"<b>{p['name']}</b>\nсейчас <b>{fmt(p['price'])} ₴</b>\n\n"
                         f"Напиши новую цену числом.")

    if act == "sh":                                   # «где показывается»
        hits = [h for h in find(arg) if str(h[0]) == arg]
        if not hits:
            return say(chat, "Позиция пропала из меню.")
        _, p = hits[0]
        say(chat, "Смотрю витрины…")
        s, g = where_shown(p["id"], p["name"])
        m = lambda v: fmt(v) if v is not None else "не нашёл"
        return say(chat, f"<b>{p['name']}</b>\n\n"
                         f"Syrve:  <b>{fmt(p['price'])} ₴</b>\n"
                         f"сайт:   {m(s)}\nGlovo:  {m(g)}")

    with _lock:
        c = _confirm.pop(arg, None)
    if not c:
        return say(chat, "Кнопка устарела — открой экран заново.\n"
                         "<i>Бот помнит подтверждения недолго, чтобы вчерашнее "
                         "нажатие не поменяло сегодняшнюю цену.</i>")

    if act == "no":
        return say(chat, "Отменено, ничего не менял.")

    if act == "dt":                                   # перенести дату
        cur = date.fromisoformat(c["date"])
        new = date.today() + timedelta(days=1) if cur == date.today() else date.today()
        return prepare(chat, c["code"], c["price"], new, who)

    if act == "нб":                                   # создать карточку блюда
        if not access.можно(chat, "цены"):
            return say(chat, "Заводить блюда может оператор или админ.")
        return создать_блюдо(chat, c, who)

    if act == "тк":                                   # перейти к техкарте
        return техкарта_для(chat, c)

    if act == "gv":                                   # скачок подтверждён
        д = дата_из_текста(c["date"])
        if д is None:
            return say(chat, "Кнопка устарела — начни заново.")
        return prepare(chat, c["code"], c["price"], д, who, несмотря=True)

    if act == "gl":                                   # провести список
        return провести_список(chat, c, who)

    if act == "gj":                                   # список: взять и скачки
        # Порог в 50% защищает от опечатки, а не запрещает крупные правки.
        # Показываем ровно то, что добавляем, и просим подтвердить второй раз.
        добавились = c.get("скачки", [])
        if not добавились:
            return say(chat, "Крупных правок в этом списке нет.")
        c["строки"] = c["строки"] + добавились
        c["скачки"] = []
        with _lock:
            _confirm[arg] = c
        добавка = "\n".join(
            f"• {r['название']}: <b>{fmt(r['было'])} → {fmt(r['цена'])} ₴</b> "
            f"(+{(r['цена'] - r['было']) / max(r['было'], 1) * 100:.0f}%)"
            for r in добавились[:8])
        return say(chat, f"Добавляю крупные правки:\n{добавка}\n\n"
                         f"Итого <b>{len(c['строки'])} позиций</b>. Проводим?",
                   inline=[[{"text": f"✅ Провести {len(c['строки'])}",
                             "callback_data": f"gl:{arg}"},
                            {"text": "✖️ Отмена", "callback_data": f"no:{arg}"}]])

    if act == "ld":                                   # список: другая дата
        cur = date.fromisoformat(c["date"])
        нов = date.today() + timedelta(days=1) if cur == date.today() else date.today()
        with cappi.Syrve() as s:
            for r in c["строки"]:
                r["было"], r["dep"] = s.price_of(r["pid"], нов.isoformat())
        c["строки"] = [r for r in c["строки"] if r["dep"]]
        c["date"] = нов.isoformat()
        with _lock:
            _confirm[arg] = c
        return say(chat, f"Ок, тогда на <b>{нов:%d.%m}</b> — "
                         f"{len(c['строки'])} позиций.",
                   inline=[[{"text": f"✅ Провести {len(c['строки'])}",
                             "callback_data": f"gl:{arg}"},
                            {"text": "✖️ Отмена", "callback_data": f"no:{arg}"}]])

    if act == "go":
        try:
            doc = do_change(c)
            сейчас = c["date"] == date.today().isoformat()
            say(chat, f"✅ Приказ <b>№{doc['documentNumber']}</b> проведён\n"
                      f"{c['name']}: <b>{fmt(c['old'])} → {fmt(c['price'])} ₴</b> "
                      f"с {date.fromisoformat(c['date']):%d.%m}\n\n"
                      + ("<i>Цена уже в Syrve. Выгрузка на сайт идёт раз в "
                         "20 минут — слежу и напишу, как только встанет.</i>"
                         if сейчас else
                         f"<i>Проверю сайт и Glovo утром "
                         f"{date.fromisoformat(c['date']):%d.%m}.</i>"))
        except cappi.PriceOrderExists as e:
            other = date.fromisoformat(c["date"]) + timedelta(days=1)
            say(chat,
                f"⚠️ Не меняю: на {date.fromisoformat(e.date):%d.%m} по этой позиции "
                f"уже есть приказ <b>№{e.number}</b>.\n\n"
                f"Syrve не принимает второй приказ на ту же дату, а править "
                f"существующий бесполезно — в Syrve цена изменится, а до сайта "
                f"и Glovo не дойдёт.\n\n"
                f"Поставь на другую дату: <code>/set {c['code']} {fmt(c['price'])} "
                f"{other:%d.%m}</code> — или поправь приказ №{e.number} "
                f"в Syrve руками.")
        except Exception as e:
            say(chat, f"❌ Не получилось: {e}")


# ------------------------------------------------------------------ сообщения
# Текстовые команды. Смысл не в том, чтобы угадать все формулировки — это
# невозможно, — а в том, чтобы покрыть ходовые, а остальное записать в
# unknown.log и раз в пару дней разобрать. Порядок важен: первое совпадение
# выигрывает, поэтому узкие правила стоят выше широких.
ФРАЗЫ = [
    # ── показатели. Порядок важен: узкое выше широкого, иначе широкое
    # правило съедает уточнение. «итоги за вчера» должны дать ВЧЕРА, а не
    # сегодняшний незакрытый день.
    (r"^(за\s+)?позавчера", lambda chat, m, txt: cmd_report(
        chat, date.today() - timedelta(days=2), live=False)),
    # Уточнённые экраны со словом «вчера» — раньше их перехватывал отчёт.
    (r"^(причин\w*\s+отмен|отмен\w*)\s+(за\s+)?вчера",
     lambda chat, m, txt: cmd_cancels(chat, date.today() - timedelta(days=1))),
    (r"^(списан|удален)\w*\s+(за\s+)?вчера",
     lambda chat, m, txt: cmd_deletions(chat, date.today() - timedelta(days=1))),
    (r"(^|\s)(за\s+)?вчера\b", lambda chat, m, txt: cmd_report(
        chat, date.today() - timedelta(days=1), live=False)),
    (r"^(выручк|показател|как дела|что по деньгам|итог|сводк|результат)",
     lambda chat, m, txt: cmd_report(chat, None, live=True)),
    (r"^(выбер|выбрать день|календар|за день|какой день)",
     lambda chat, m, txt: cmd_pick_day(chat)),
    (r"^(за )?(\d+) дн", lambda chat, m, txt: cmd_report(
        chat, date.today() - timedelta(days=int(m.group(2))), live=False)),
    # Дата — только с ведущим нулём или явным «за»/годом, иначе «0.5» и
    # «1.5» (объём напитка) уходили в отчёт вместо поиска позиции.
    (r"^(за\s+)(\d{1,2})\.(\d{1,2})(\.(\d{4}))?$|^(\d{2})\.(\d{2})(\.\d{4})?$",
     lambda chat, m, txt: _отчёт_за_дату(chat, m)),
    (r"^(план|сколько нужно|сколько надо)", lambda chat, m, txt: cmd_plan(chat, "")),
    (r"(в работе|сейчас готов|активные заказ|что готовится)",
     lambda chat, m, txt: cmd_live(chat)),
    (r"(причин\w*\s+отмен|^отмен|сколько отмен)",
     lambda chat, m, txt: cmd_cancels(chat)),
    (r"^(списан|удален|что списал)", lambda chat, m, txt: cmd_deletions(chat)),
    (r"^(акци|скидк|что по акци)", lambda chat, m, txt: cmd_promo(chat)),
    (r"^(спец|спецпредлож)", lambda chat, m, txt: cmd_special(chat)),

    # цены
    (r"^(план|сколько нужно|сколько надо)", lambda chat, m, txt: cmd_plan(chat, "")),
    (r"(в работе|сейчас готов|активные заказ|что готовится)",
     lambda chat, m, txt: cmd_live(chat)),
    # «Причина отмены» — спрашивали именно так, а правило начиналось с
    # «отмен» и мимо проходило.
    (r"(причин\w*\s+отмен|^отмен|сколько отмен)",
     lambda chat, m, txt: cmd_cancels(chat)),
    (r"^(списан|удален|что списал)", lambda chat, m, txt: cmd_deletions(chat)),
    (r"^(акци|скидк|что по акци)", lambda chat, m, txt: cmd_promo(chat)),
    (r"^(спец|спецпредлож)", lambda chat, m, txt: cmd_special(chat)),

    # цены
    (r"^(стоп|что в стопе|стоп.?лист)", lambda chat, m, txt: cmd_stoplist(chat)),
    (r"^(сверк|проверь цен|расхожден|сравни цен)", lambda chat, m, txt: cmd_check(chat)),
    (r"^(помен|измен|постав|обнов)\w*\s+цен\w*\s+(?:на\s+)?(.+?)\s+(?:на|=|до)\s+(\d+)",
     lambda chat, m, txt: команда_цены(chat, m.group(2), m.group(3))),
    (r"^(скольк[оа] стоит|цена|почём|почем|сколько за)\s+(.+)",
     lambda chat, m, txt: cmd_price(chat, m.group(2))),
    (r"^(найди|поиск|покажи)\s+(.+)", lambda chat, m, txt: cmd_price(chat, m.group(2))),
    (r"^(на проверке|что проверя|очеред)", lambda chat, m, txt: cmd_pending(chat)),

    # админское
    (r"^(связь|проверь подключ|healthcheck|что работает)",
     lambda chat, m, txt: cmd_healthcheck(chat)),
    (r"^(журнал|истори|кто мен)", lambda chat, m, txt: cmd_audit(chat)),
    (r"^(доступ|прав|кто может)", lambda chat, m, txt: cmd_access(chat)),
    (r"^(помощ|что умеешь|команды|help|что это|что ты|как польз)",
     lambda chat, m, txt: помощь(chat)),

    # то, чего ещё нет — честно говорим, а не молчим
    (r"^(жалоб|негатив|отзыв)", lambda chat, m, txt: cmd_complaints(chat)),
    (r"^(джамшут|dzhamshut)", lambda chat, m, txt: cmd_jam_state(chat)),
    (r"^(зон|закрыт)", lambda chat, m, txt: cmd_zones(chat)),
]


def команда_цены(chat, что, цена):
    """«поменяй цену садочок на 46» — находим позицию и предлагаем подтвердить."""
    hits = find(что)
    if not hits:
        return say(chat, f"Не нашёл: <b>{что}</b>")
    if len(hits) > 1:
        return say(chat, f"Нашёл {len(hits)} — уточни, какую менять:", inline=[
            [{"text": f"{fmt(p['price'])} ₴ · {p['name'][:32]}",
              "callback_data": f"ed:{code}"}] for code, p in hits[:12]])
    return prepare(chat, hits[0][0], float(цена), _default_date(), "текст")


ДНИ_НЕДЕЛИ = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def cmd_pick_day(chat, сдвиг=0):
    """Выбор дня для отчёта. Кнопками, потому что дату руками набирают с
    опечатками, а формат каждый раз вспоминают заново."""
    кнопки, ряд = [], []
    for i in range(сдвиг, сдвиг + 14):
        d = date.today() - timedelta(days=i)
        подпись = ("сегодня" if i == 0 else "вчера" if i == 1
                   else f"{d:%d.%m} {ДНИ_НЕДЕЛИ[d.weekday()]}")
        ряд.append({"text": подпись, "callback_data": f"rd:{d.isoformat()}"})
        if len(ряд) == 3:
            кнопки.append(ряд); ряд = []
    if ряд:
        кнопки.append(ряд)
    кнопки.append([{"text": "← ещё раньше", "callback_data": f"rp:{сдвиг + 14}"}])
    say(chat, "За какой день?", inline=кнопки)


def cmd_complaints(chat, day=None):
    day = day or date.today()
    ж = report.complaints(day)
    if ж is None:
        return say(chat, "Loopa не настроена — нет LOOPA_TOKEN.")
    if not ж["жалоб"]:
        return say(chat, f"За {day:%d.%m} жалоб нет."
                         + (f" Отзывов всего {ж['отзывов']}." if ж["отзывов"] else ""))
    строки = [f"😠 <b>Жалоб за {day:%d.%m}: {ж['жалоб']}</b>"
              + (f" из {ж['отзывов']} отзывов" if ж["отзывов"] else ""), ""]
    if ж["срочность"]:
        строки.append("по срочности: " + ", ".join(
            f"{s} {n}" for s, n in sorted(ж["срочность"].items(), key=lambda x: -x[1])))
    if ж["по_поводам"]:
        строки += ["", "<b>Поводы</b>"]
        строки += [f"    {report.ПОВОДЫ.get(p, p)} — {n}"
                   for p, n in sorted(ж["по_поводам"].items(), key=lambda x: -x[1])]
    if ж["по_точкам"]:
        строки += ["", "<b>По точкам</b>"]
        строки += [f"    {т} — {n}"
                   for т, n in sorted(ж["по_точкам"].items(), key=lambda x: -x[1])]
    say(chat, "\n".join(строки))


def cmd_zones(chat, day=None):
    строки = []
    try:
        st = webhook.state()
        if st and st.get("count"):
            строки += [f"🔴 <b>Сейчас закрыто зон: {st['count']}</b>",
                       "    " + ", ".join(str(z) for z in st.get("closed", [])), ""]
        elif st:
            строки += ["🟢 Сейчас все зоны открыты", ""]
    except Exception as e:
        строки += [f"<i>Состояние зон недоступно: {str(e)[:80]}</i>", ""]

    з = webhook.zones_summary(day or date.today())
    if not з["закрытий"]:
        return say(chat, "\n".join(строки + ["Сегодня зоны не закрывали."]))
    строки += [f"🚧 <b>Закрытий зон сегодня: {з['закрытий']}</b> · "
               f"{з['минут'] / 60:.1f} ч суммарно", ""]
    for район, r in sorted(з["по_районам"].items(), key=lambda x: -x[1]["минут"]):
        строки.append(f"  {район} — {r['раз']} раз, {r['минут']:.0f} мин")
    if з["ещё_закрыты"]:
        строки += ["", f"<i>{з['ещё_закрыты']} ещё закрыты — "
                       f"время посчитано по плану, а не по факту.</i>"]
    say(chat, "\n".join(строки))


def cmd_live(chat):
    """Только живые заказы — без остального отчёта, когда спрашивают про них."""
    ж = report.live_orders()
    строки = [f"🚚 <b>В работе: {ж['в_работе']}</b> из {ж['всего']} за сегодня", ""]
    for st, n in sorted(ж["статусы"].items(), key=lambda x: -x[1]):
        строки.append(f"    {report.статус(st)} — {n}"
                      + ("  ←" if st in report.В_РАБОТЕ else ""))
    say(chat, "\n".join(строки))


def cmd_cancels(chat, day=None):
    day = day or date.today()
    with cappi.Syrve() as s:
        от = report.cancels(s, day)
        уд = report.removals(s, day)
    когда = "сегодня" if day == date.today() else f"{day:%d.%m}"
    строки = [f"❌ <b>Отмен {когда}: {sum(от.values())}</b>"]
    строки += [f"    {п} — {n}" for п, n in sorted(от.items(), key=lambda x: -x[1])]
    if уд:
        строки += ["", f"🗑 <b>Удалено блюд: {sum(v['штук'] for v in уд.values()):.0f}</b>"]
        for п, v in sorted(уд.items(), key=lambda x: -x[1]["штук"]):
            деньги = f", {report.money(v['сумма'])} ₴" if v["сумма"] else ""
            строки.append(f"    {п} — {v['штук']:.0f}{деньги}")
    say(chat, "\n".join(строки))


def _отказ(m, текст):
    """Кто стучался без доступа.

    Бот отвечает «нет доступа» и забывал об этом. Между «человек ошибся
    ботом» и «кто-то методично подбирается к смене цен» разница только в
    том, повторяется ли попытка, — а без записи этого не увидеть.
    Первую попытку от нового человека показываем админам сразу.
    """
    f = m.get("from") or {}
    uid = f.get("id")
    кто = "@" + f["username"] if f.get("username") else (f.get("first_name") or "—")
    новый = True
    try:
        новый = not any(f"\t{uid}\t" in s for s in open(DENIED))
    except FileNotFoundError:
        pass
    with open(DENIED, "a") as файл:
        файл.write(f"{datetime.now():%Y-%m-%d %H:%M}\t{uid}\t{кто}\t{текст[:120]}\n")
    if новый:
        for admin in (int(u) for u, v in access.все().items()
                      if v.get("роль") == "админ"):
            try:
                say(admin, f"🔐 <b>Попытка входа без доступа</b>\n"
                           f"{кто} · <code>{uid}</code>\n"
                           f"написал: <i>{текст[:80]}</i>\n\n"
                           f"Выдать доступ — пришли <code>{uid}</code>.")
            except Exception:
                pass


def cmd_denied(chat, n=30):
    """Журнал отказов — кто пробовал войти."""
    if not access.можно(chat, "админка"):
        return say(chat, "Журнал отказов — админский экран.")
    try:
        строки = open(DENIED).read().strip().splitlines()
    except FileNotFoundError:
        строки = []
    if not строки:
        return say(chat, "🔐 Попыток входа без доступа не было.")
    from collections import Counter
    люди = Counter(s.split("\t")[1] for s in строки if "\t" in s)
    out = [f"🔐 <b>Попытки входа без доступа: {len(строки)}</b>",
           f"разных людей: {len(люди)}", ""]
    for s in строки[-n:]:
        ч = s.split("\t")
        if len(ч) >= 4:
            out.append(f"  <code>{ч[0][5:]}</code> {ч[2]} · <code>{ч[1]}</code>"
                       f"\n      <i>{ч[3][:60]}</i>")
    повторные = [(u, c) for u, c in люди.most_common() if c > 2]
    if повторные:
        out += ["", "<b>Стучались настойчиво</b>"]
        out += [f"  <code>{u}</code> — {c} раз" for u, c in повторные]
    say(chat, "\n".join(out))


def не_понял(chat, текст, кто):
    """Записываем непонятое — это материал для ревизии, а не мусор."""
    with open(UNKNOWN, "a") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M}\t{кто}\t{текст[:200]}\n")
    bugs.записать("непонял", текст[:200], где="свободный текст", кто=кто)


def cmd_unknown(chat, n=25):
    """Что бот не понял — экран для ревизии раз в пару дней."""
    try:
        строки = open(UNKNOWN).read().strip().splitlines()
    except FileNotFoundError:
        строки = []
    if not строки:
        return say(chat, "Непонятых запросов нет — всё, что писали, бот разобрал.")
    from collections import Counter
    тексты = Counter(s.split("\t")[-1].strip().lower() for s in строки)
    out = [f"<b>Непонятые запросы</b> — всего {len(строки)}", ""]
    for текст, раз in тексты.most_common(n):
        out.append(f"  <code>{текст[:60]}</code>" + (f"  ×{раз}" if раз > 1 else ""))
    out += ["", "<i>Разберём на ревизии: что из этого стоит добавить командой.</i>"]
    say(chat, "\n".join(out))


def открыть(chat, модуль, текст):
    _menu[chat] = модуль
    say(chat, текст)


ДНИ_НЕДЕЛИ = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


# ------------------------------------------------------------------ продажи
# ------------------------------------------------------------------ Джамшут
def cmd_jam_state(chat):
    if not jamshut.настроен():
        return say(chat, "Джамшут не настроен — нет JAMSHUT_URL или токена.")
    try:
        st = jamshut.состояние()
    except Exception as e:
        return say(chat, f"Джамшут не отвечает: {str(e)[:120]}")
    если = st.get("count", 0)
    строки = [("🔴 <b>Сейчас закрыто зон: %d</b>" % если) if если
              else "🟢 <b>Все зоны открыты</b>"]
    if если:
        try:
            зоны = jamshut.справочник_зон()
        except Exception:
            зоны = {}
        for z in st.get("closed", []):
            имя = (зоны.get(z) or {}).get("name", f"зона {z}")
            район = (зоны.get(z) or {}).get("district", "")
            строки.append(f"    {имя}" + (f" · {район}" if район else ""))
    строки.append(f"<i>по данным Джамшута на {(st.get('at') or '')[11:16]}</i>")
    say(chat, "\n".join(строки))


def cmd_jam_close(chat):
    """Выбор зоны для закрытия. Кнопками: номера зон никто не помнит."""
    if not access.можно(chat, "зоны"):
        return say(chat, "Управлять зонами может оператор или админ.")
    try:
        зоны = jamshut.справочник_зон()
        закрыты = set(jamshut.состояние().get("closed") or [])
    except Exception as e:
        return say(chat, f"Джамшут не отвечает: {str(e)[:120]}")
    свободные = [(i, z) for i, z in sorted(зоны.items()) if i not in закрыты]
    if not свободные:
        return say(chat, "Все зоны уже закрыты.")
    say(chat, "Какую зону закрыть?", inline=[
        [{"text": f"{z['name']} · {z.get('district', '')}",
          "callback_data": f"zc:{i}"}] for i, z in свободные])


def cmd_jam_open(chat):
    """Открыть обратно — только из тех, что закрыты."""
    if not access.можно(chat, "зоны"):
        return say(chat, "Управлять зонами может оператор или админ.")
    try:
        зоны = jamshut.справочник_зон()
        закрыты = list(jamshut.состояние().get("closed") or [])
    except Exception as e:
        return say(chat, f"Джамшут не отвечает: {str(e)[:120]}")
    if not закрыты:
        return say(chat, "🟢 Сейчас все зоны открыты — открывать нечего.")
    say(chat, "Какую зону открыть?", inline=[
        [{"text": (зоны.get(i) or {}).get("name", f"зона {i}"),
          "callback_data": f"zo:{i}"}] for i in закрыты])


def cmd_jam_history(chat, дней=7):
    say(chat, f"Смотрю историю за {дней} дней…")
    try:
        события, пропуски = jamshut.история(дней)
    except Exception as e:
        return say(chat, f"Джамшут не отвечает: {str(e)[:120]}")
    if not события and пропуски:
        return say(chat, f"Не смог прочитать историю: "
                         f"{len(пропуски)} из кусков не ответили.\n"
                         f"<i>Это не значит, что событий не было.</i>")
    if not события:
        return say(chat, f"За {дней} дней событий нет.\n"
                         f"<i>История у Джамшута ведётся с 10.09.2026.</i>")
    строки = [f"📜 <b>События зон за {дней} дней: {len(события)}</b>", ""]
    for e in sorted(события, key=lambda x: x.get("at", ""), reverse=True)[:20]:
        знак = "🚧" if e.get("event") == "zone.close" else "✅"
        когда = (e.get("at") or "")[5:16].replace("T", " ")
        кто = "авто" if (e.get("auto") or e.get("source") == "bot") else (e.get("actor") or "—")
        район = e.get("district") or e.get("branch") or "—"
        хвост = f" · {e['duration_min']} мин" if e.get("duration_min") else ""
        строки.append(f"  {знак} {когда} · {район} · {кто}{хвост}")
    if пропуски:
        # Дырка в данных должна быть видна: иначе «мало закрытий» прочитают
        # как хорошую новость.
        куски = ", ".join(f"{с:%d.%m}–{по:%d.%m}" for с, по, _ in пропуски)
        строки += ["", f"⚠️ <i>Не ответили куски: {куски}. "
                       f"Здесь показано не всё.</i>"]
    say(chat, "\n".join(строки))


def cmd_jam_who(chat, дней=7):
    say(chat, "Считаю…")
    try:
        с = jamshut.сводка(дней)
    except Exception as e:
        return say(chat, f"Джамшут не отвечает: {str(e)[:120]}")
    if not с["закрытий"]:
        return say(chat, f"За {дней} дней зоны не закрывали.")
    строки = [f"👤 <b>Закрытий за {дней} дней: {с['закрытий']}</b>",
              f"суммарно {с['минут'] / 60:.1f} ч"
              + (f" · автоматических {с['авто']}" if с["авто"] else ""), "",
              "<b>Кто закрывал</b>"]
    for кто, n in sorted(с["по_людям"].items(), key=lambda x: -x[1]):
        строки.append(f"  {кто} — {n}")
    строки += ["", "<b>Районы</b>"]
    for р, n in sorted(с["по_районам"].items(), key=lambda x: -x[1]):
        строки.append(f"  {р} — {n}")
    if с.get("пропуски"):
        строки += ["", f"⚠️ <i>{len(с['пропуски'])} кусков истории не "
                       f"ответили — цифры неполные.</i>"]
    say(chat, "\n".join(строки))


def cmd_jam_health(chat):
    строки = ["🩺 <b>Джамшут</b>", ""]
    try:
        h = jamshut.здоровье()
        строки.append(f"сервис: <b>{h.get('service', '?')}</b> · "
                      + ("жив ✅" if h.get("ok") else "не отвечает ❌"))
    except Exception as e:
        строки.append(f"не отвечает ❌ — {str(e)[:90]}")
    адрес, _ = jamshut._база()
    строки += [f"адрес: <code>{адрес}</code>", "",
               "<b>Что доступно</b>",
               "  ✅ состояние зон, история событий",
               "  ❌ управление — ручек нет, только чтение", "",
               "<i>Джамшут не имеет доступа к Core: ни адреса, ни токена. "
               "Связь односторонняя.</i>"]
    say(chat, "\n".join(строки))


def cmd_stoplist(chat):
    """Что сейчас в стопе, с кнопкой снять по каждой позиции."""
    try:
        позиции = stoplist.список()
    except Exception as e:
        return say(chat, f"Стоп-лист недоступен: {str(e)[:120]}")
    if not позиции:
        return say(chat, "🟢 Стоп-лист пуст — всё меню продаётся.")
    потери = sum(п["цена"] or 0 for п in позиции)
    строки = [f"🛑 <b>В стопе: {len(позиции)} позиций</b>",
              f"<i>на {report.money(потери)} ₴ по прайсу</i>", ""]
    кнопки = []
    for п in позиции[:20]:
        строки.append(f"  {п['название'][:36]} — {report.money(п['цена'] or 0)} ₴"
                      f"  <i>{п['точка']}</i>")
        if access.можно(chat, "цены"):
            кнопки.append([{"text": f"▶️ снять · {п['название'][:24]} · {п['точка']}",
                            "callback_data":
                                f"sr:{ссылка(п['productId'], п['terminalGroupId'])}"}])
    if len(позиции) > 20:
        строки.append(f"  <i>…и ещё {len(позиции) - 20}</i>")
    say(chat, "\n".join(строки), inline=кнопки or None)


def cmd_deletions(chat, day=None):
    """Списания за счёт компании. Плановый маркетинг сюда не идёт."""
    day = day or date.today()
    with cappi.Syrve() as s:
        сп = report.deletions(s, day)
        всего = report.deletions(s, day, всё=True)
    отсеяно = len(всего) - len(сп)
    хвост = (f"\n<i>плановый маркетинг не считаю потерей: {отсеяно} шт</i>"
             if отсеяно else "")
    if not сп:
        return say(chat, f"За {day:%d.%m} списаний за счёт компании нет." + хвост)
    сумма = sum(x["сумма"] for x in сп)
    строки = [f"🗑 <b>Списано за счёт компании, {day:%d.%m}: {len(сп)} позиций</b>",
              f"на <b>{report.money(сумма)} ₴</b> по прайсу" + хвост, ""]
    по_причинам = {}
    for x in сп:
        r = по_причинам.setdefault(x["причина"], {"раз": 0, "сумма": 0})
        r["раз"] += 1
        r["сумма"] += x["сумма"]
    for причина, r in sorted(по_причинам.items(), key=lambda x: -x[1]["сумма"]):
        строки.append(f"  {причина[:34]} — {r['раз']} шт, {report.money(r['сумма'])} ₴")
    строки += ["", "<b>Поштучно</b>"]
    for x in sorted(сп, key=lambda x: -x["сумма"])[:15]:
        строки.append(f"  {x['блюдо'][:30]} — {report.money(x['сумма'])} ₴"
                      f"\n     чек {x['чек']} · {x['зал']} · {x['кто'][:20]}")
    say(chat, "\n".join(строки))


def _сводка_позиций(chat, day, позиции, заголовок, пусто):
    if not позиции:
        return say(chat, пусто)
    say(chat, "Считаю продажи…")
    with cappi.Syrve() as s:
        св = promo.сводка(s, day, позиции)
    строки = [f"{заголовок} · {day:%d.%m}", "",
              f"продано на <b>{report.money(св['сумма'])} ₴</b> · {св['штук']:.0f} шт",
              f"доля в выручке дня: <b>{св['доля']:.1f}%</b>", ""]
    for x in [x for x in св["позиции"] if x["штук"]][:15]:
        скидка = f"  <i>−{x['процент']:.0f}%</i>" if x.get("процент") else ""
        строки.append(f"  {x['название'][:30]} — {x['штук']:.0f} шт, "
                      f"{report.money(x['сумма'])} ₴{скидка}")
    if св["не_продавались"]:
        строки += ["", f"<b>Ни одной продажи: {len(св['не_продавались'])} из "
                       f"{len(св['позиции'])}</b>"]
        for x in св["не_продавались"][:15]:
            хвост = (f"  <i>скидка {report.money(x['выгода'])} ₴</i>"
                     if x.get("выгода") else "")
            строки.append(f"  {x['код'] or '—'} {x['название'][:30]}{хвост}")
    say(chat, "\n".join(строки))


def cmd_promo(chat, day=None):
    """Акционные — те, у кого на сайте перечёркнута старая цена."""
    try:
        позиции = promo.акционные()
    except Exception as e:
        return say(chat, f"Не смог прочитать сайт: {str(e)[:100]}")
    _сводка_позиций(chat, day or date.today(), позиции,
                    "🏷 <b>Акционные товары</b>",
                    "Сейчас на сайте нет позиций со скидкой.")


def cmd_special(chat, day=None):
    """Спецпредложение — ручной список артикулов."""
    d = promo.спецпредложения()
    if not d:
        if access.можно(chat, "цены"):
            _await[chat] = {"what": "special"}
            return say(chat, "Спецпредложение пустое.\n\n"
                             "Пришли артикул — добавлю. Например <code>03275</code>.\n"
                             "<i>Можно с пометкой: 03275 новинка сентября</i>")
        return say(chat, "Спецпредложение пустое.")
    позиции = [{"код": к, "название": v["название"], "guid": v.get("guid")}
               for к, v in d.items()]
    _сводка_позиций(chat, day or date.today(), позиции,
                    "⭐ <b>Спецпредложение</b>", "")
    if access.можно(chat, "цены"):
        say(chat, "Убрать позицию:", inline=[
            [{"text": f"🗑 {к} · {v['название'][:26]}", "callback_data": f"sx:{к}"}]
            for к, v in d.items()])


def cmd_special_add(chat, args):
    if not access.можно(chat, "цены"):
        return say(chat, "Менять спецпредложение может оператор или админ.")
    if not args:
        return say(chat, "Формат: <code>/special 03275</code>\n"
                         "С пометкой: <code>/special 03275 новинка сентября</code>")
    код = re.sub(r"\D", "", args[0])
    try:
        v = promo.добавить_спец(код, " ".join(args[1:]) or None)
    except promo.НетТакогоАртикула:
        _await[chat] = {"what": "special"}
        соседи = sorted(c for c, _ in find(код[:3]) if c)[:6] if len(код) >= 3 else []
        подсказка = ("\n<i>Рядом есть: " + ", ".join(соседи) + "</i>") if соседи else ""
        return say(chat, f"❌ Артикула <b>{код}</b> в меню нет.{подсказка}\n\n"
                         f"Пришли другой или найди позицию по названию "
                         f"через «🔍 Найти позицию» — там будет её артикул.")
    say(chat, f"⭐ Добавлено в спецпредложение:\n<b>{v['название']}</b>"
              + (f"\n<i>{v['комментарий']}</i>" if v.get("комментарий") else ""))


# ----------------------------------------------------------------- персонал
def cmd_shift(chat, day=None):
    """Кто на смене и сколько зарабатывает час работы."""
    day = day or date.today()
    with cappi.Syrve() as s:
        см = report.attendance(s, day)
        продажи = report.sales(s, day)
    if not см["часов"]:
        return say(chat, f"За {day:%d.%m} закрытых смен нет.\n"
                         f"<i>Пока смена открыта, часы не посчитать.</i>")
    выручка = продажи["всего"]["сумма"]
    строки = [f"👥 <b>Смена {day:%d.%m}</b>", "",
              f"людей: <b>{см['людей']}</b> · часов: <b>{см['часов']:.0f}</b>",
              f"выручка на человеко-час: <b>{report.money(выручка / см['часов'])} ₴</b>",
              ""]
    for роль, r in sorted(см["по_ролям"].items(), key=lambda x: -x[1]["часов"]):
        доля = r["часов"] / см["часов"] * 100
        строки.append(f"  {роль} — {r['людей']} чел, {r['часов']:.0f} ч ({доля:.0f}%)")
    if см["правки"]:
        строки += ["", f"⚠️ <b>Правки задним числом: {len(см['правки'])}</b>"]
        for p in см["правки"][:8]:
            строки.append(f"    {p['кто'][:24]} · {p['смена']} · "
                          f"через {p['через']:.0f} мин ({p['правил']})")
    say(chat, "\n".join(строки))


МЕСЯЦЫ = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль",
          "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]


def _знак(факт, норма, меньше_лучше=True):
    ок = факт <= норма if меньше_лучше else факт >= норма
    return "✅" if ок else "❌"


def cmd_процент(chat, месяц=None):
    """Процент кухни: 2% от выручки, делённые по часам."""
    if not месяц:
        кнопки, ряд = [], []
        d = date.today().replace(day=1)
        for _ in range(6):
            ряд.append({"text": f"{МЕСЯЦЫ[d.month - 1][:3]} {d.year % 100}",
                        "callback_data": f"pc:{d:%Y-%m}"})
            if len(ряд) == 3:
                кнопки.append(ряд); ряд = []
            d = (d - timedelta(days=1)).replace(day=1)
        if ряд:
            кнопки.append(ряд)
        return say(chat, "Процент кухни за какой месяц?", inline=кнопки)

    day = дата_из_текста(f"{месяц}-15")
    if day is None:
        return say(chat, "Кнопка устарела — выбери месяц заново.")
    say(chat, "Считаю…")
    with cappi.Syrve() as s:
        r = kpi.процент_кухни(s, day)
    строки = [f"💵 <b>Процент кухни · {МЕСЯЦЫ[day.month - 1]} {day.year}</b>", ""]
    for филиал, v in r["филиалы"].items():
        строки += [f"<b>{филиал}</b>",
                   f"    выручка {report.money(v['выручка'])} ₴ · "
                   f"пул {kpi.ПРОЦЕНТ_КУХНИ:g}% = <b>{report.money(v['пул'])} ₴</b>",
                   f"    {v['часов']:.0f} ч · {report.money(v['за_час'])} ₴ за час"]
        for имя, x in v["люди"].items():
            правка = f"  <i>{x['правка']:+.0f} ч</i>" if x["правка"] else ""
            строки.append(f"      {имя.split('(')[0].strip()[:24]:<24} "
                          f"{x['итого']:>5.0f} ч  <b>{report.money(x['сумма'])}</b> ₴{правка}")
        строки.append("")
    б = r.get("без_филиала")
    if б:
        строки += [f"<b>Филиал не определён</b> — {б['часов']:.0f} ч, "
                   f"{report.money(б['пул'])} ₴"]
        for имя, x in б["люди"].items():
            строки.append(f"      {имя.split('(')[0].strip()[:24]:<24} "
                          f"{x['итого']:>5.0f} ч  <b>{report.money(x['сумма'])}</b> ₴")
        строки.append("    <i>в имени сотрудника нет филиала — "
                      "считаю от общей выручки по доле часов</i>")
    if (day.year, day.month) == (date.today().year, date.today().month):
        строки += ["", "⚠️ <i>Месяц не закончен — суммы предварительные.</i>"]
    строки += ["", "<i>Каждый филиал делит свою выручку: повар Лазаревой не "
                   "получает долю с Левітани. В пуле повара, су-шефы и "
                   "шеф-повар; администратор — в дни, когда админов на смене "
                   f"было {min(kpi.АДМИН_НА_КУХНЕ.values())} и больше. "
                   "Правки часов: /fix Фамилия +12</i>"]
    say(chat, "\n".join(строки))


def cmd_fix(chat, args):
    """Корректировка часов: /fix Иванов +12"""
    if not access.можно(chat, "показатели"):
        return say(chat, "Правки часов меняют выплаты, поэтому нужен "
                         "доступ к показателям. У тебя его нет.")
    if len(args) < 2:
        текущие = kpi.корректировки()
        строки = ["<b>Правки часов за этот месяц</b>", ""]
        строки += ([f"  {и} — {ч:+g} ч" for и, ч in текущие.items()]
                   if текущие else ["  <i>правок нет</i>"])
        строки += ["", "Поставить: <code>/fix Гуляева +12</code>",
                   "Убрать: <code>/fix Гуляева 0</code>",
                   "<i>Плюс — если человек стоял на кухне, а в явке "
                   "записан другой ролью.</i>"]
        return say(chat, "\n".join(строки))
    фамилия = " ".join(args[:-1])
    try:
        часов = float(args[-1].replace(",", ".").lstrip("+"))
    except ValueError:
        return say(chat, f"Не понял часы: <b>{args[-1]}</b>")
    д = kpi.корректировки()
    # Ищем полное имя, чтобы правка легла на того же человека, что и явки.
    with cappi.Syrve() as s:
        часы = kpi.часы_кухни(s, date.today())
    полное = next((и for и in часы if cappi.norm_full(фамилия) in cappi.norm_full(и)),
                  фамилия)
    if часов == 0:
        д.pop(полное, None)
        kpi.корректировки(значение=д)
        return say(chat, f"Правка для <b>{полное}</b> убрана.")
    д[полное] = часов
    kpi.корректировки(значение=д)
    say(chat, f"✅ <b>{полное.split('(')[0].strip()}</b> {часов:+g} ч "
              f"за {МЕСЯЦЫ[date.today().month - 1]}")


def cmd_kpi(chat):
    """Шаг первый: чей KPI смотрим."""
    say(chat, "Чей KPI?", inline=[
        [{"text": "👨‍🍳 Шеф-повар", "callback_data": "kr:шеф"}],
        [{"text": "🔪 Су-шеф Лазарева", "callback_data": "kr:Лазарева"}],
        [{"text": "🔪 Су-шеф Левітана", "callback_data": "kr:Левітана"}],
    ])


def cmd_kpi_месяц(chat, роль):
    """Шаг второй: за какой месяц."""
    кнопки, ряд = [], []
    d = date.today().replace(day=1)
    for _ in range(6):
        ряд.append({"text": f"{МЕСЯЦЫ[d.month - 1][:3]} {d.year % 100}",
                    "callback_data": f"km:{роль}:{d:%Y-%m}"})
        if len(ряд) == 3:
            кнопки.append(ряд); ряд = []
        d = (d - timedelta(days=1)).replace(day=1)
    if ряд:
        кнопки.append(ряд)
    имя = "шефа" if роль == "шеф" else f"су-шефа {роль}"
    say(chat, f"KPI {имя} за какой месяц?", inline=кнопки)


def cmd_kpi_показать(chat, роль, месяц):
    """Шаг третий: сам расчёт.

    Показываем KPI одной роли, а не всех сразу: у шефа и су-шефа разные
    критерии и разные нормы, а общий экран заставляет искать в нём своё.
    """
    day = дата_из_текста(f"{месяц}-15")
    if day is None:
        return say(chat, "Кнопка устарела — выбери месяц заново.")
    say(chat, "Считаю…")
    with cappi.Syrve() as s:
        r = kpi.посчитать(s, day)
        фио = kpi.кто(s, роль)
    н = kpi.НОРМА
    т = r.get("тайники") or {}
    заголовок = f"🏆 <b>KPI · {МЕСЯЦЫ[day.month - 1]} {day.year}</b>"

    if роль == "шеф":
        строки = [заголовок, f"<b>Шеф-повар</b> · {фио or '—'}", "",
                  f"{_знак(r['время_среднее'], н['время_шеф'])} среднее время выдачи "
                  f"<b>{r['время_среднее']:.1f}</b> мин  <i>норма ≤{н['время_шеф']}</i>",
                  f"{_знак(r['негатив_%'], н['негатив'])} негатив по кухне "
                  f"<b>{r['негатив_%']:.2f}%</b>  <i>норма ≤{н['негатив']:g}</i>"
                  f"\n    {r['негатив']} отзывов на {r['заказов']} заказов",
                  f"{_знак(abs(r['переучёт_%']), н['переучёт'])} переучёт по сети "
                  f"<b>{r['переучёт_%']:.2f}%</b>  <i>норма ±{н['переучёт']:g}</i>"
                  f"\n    {report.money(r['переучёт'])} ₴",
                  f"{_знак(r['списание_%'], н['списание'])} списание по сети "
                  f"<b>{r['списание_%']:.2f}%</b>  <i>норма ≤{н['списание']:g}</i>"
                  f"\n    {report.money(r['списание'])} ₴"]
        всего = sum(v.get("всего", 0) for v in т.values())
        пройдено = sum(v.get("пройдено", 0) for v in т.values())
        if всего:
            проц = пройдено / всего * 100
            строки.append(f"{_знак(проц, н['тайник'], False)} тайный гость "
                          f"<b>{проц:.0f}%</b>  <i>норма ≥{н['тайник']:g}</i>"
                          f"\n    {пройдено} из {всего} по сети")
        else:
            строки.append("⏳ тайный гость — <b>данных нет</b>\n"
                          "    <i>вносится: /secret Лазарева 3 4</i>")
        строки += ["", f"<i>выручка кухни за месяц {report.money(r['выручка'])} ₴</i>"]
    else:
        v = r["точки"].get(роль)
        if not v:
            return say(chat, f"Нет данных по филиалу {роль} за этот месяц.")
        норма_вр = v.get("норма_времени") or н["время_шеф"]
        строки = [заголовок, f"<b>Су-шеф {роль}</b> · {фио or '—'}", "",
                  f"{_знак(v['время'], норма_вр)} время выдачи "
                  f"<b>{v['время']:.1f}</b> мин  <i>норма ≤{норма_вр:g}</i>",
                  f"{_знак(v['списание_%'], н['списание'])} списание "
                  f"<b>{v['списание_%']:.2f}%</b>  <i>норма ≤{н['списание']:g}</i>"
                  f"\n    {report.money(v['списание'])} ₴",
                  f"{_знак(abs(v['переучёт_%']), н['переучёт'])} переучёт "
                  f"<b>{v['переучёт_%']:.2f}%</b>  <i>норма ±{н['переучёт']:g}</i>"
                  f"\n    {report.money(v['переучёт'])} ₴"]
        мои = т.get(роль)
        if мои and мои.get("всего") and мои.get("пройдено") is not None:
            проц = мои["пройдено"] / мои["всего"] * 100
            строки.append(f"{_знак(проц, н['тайник'], False)} тайный гость "
                          f"<b>{проц:.0f}%</b>  <i>норма ≥{н['тайник']:g}</i>"
                          f"\n    {мои['пройдено']} из {мои['всего']}")
        else:
            строки.append("⏳ тайный гость — <b>данных нет</b>\n"
                          f"    <i>вносится: /secret {роль} 3 4</i>")
        строки += ["", f"<i>выручка кухни {report.money(v['выручка'])} ₴, "
                       f"заказов {v['заказов']}</i>"]

    # ── к выплате
    в = kpi.выплата(r, роль)
    строки += ["", f"💰 <b>К выплате: {report.money(в['итого'])} ₴</b> "
                   f"из {report.money(в['максимум'])}"]
    нет_данных = []
    for x in в["строки"]:
        знак = "✅" if x["выполнен"] else ("⏳" if x["факт"] is None else "❌")
        строки.append(f"    {знак} {x['критерий']:<9} {report.money(x['сумма']):>5} ₴")
        if x.get("нет_данных"):
            нет_данных.append(x["критерий"])
    if нет_данных:
        # Не зачли не потому, что человек плохо сработал, а потому что цифры
        # не дошли. Это разные вещи, и человек должен видеть какая.
        строки.append(f"    <i>⏳ {', '.join(нет_данных)} — данных нет, "
                      f"не зачтено. Это не оценка работы: проверь источник "
                      f"и посчитай заново.</i>")

    незакрыт = (day.year, day.month) == (date.today().year, date.today().month)
    if незакрыт:
        строки += ["", "⚠️ <i>Месяц не закончен — цифры предварительные.</i>"]
    строки += ["", "<i>Списание: счета 02.08+03.01+03.06. "
                   "Переучёт: 02.04+02.09. Выручка кухни — только блюда. "
                   "Выполнил — получил, частичных начислений нет.</i>"]
    say(chat, "\n".join(строки))


def разобрать_тайники(текст):
    """«На лазарева 5/4, Левитана 4/3» → сколько всего и сколько прошло.

    Порядок именно такой: сначала сколько было, потом сколько пройдено.
    Так их считает маркетолог, и переучивать человека под формат бота —
    верный способ получать цифры с ошибками.
    """
    из = {}
    for строка in re.split(r"[\n,;]+", текст):
        m = re.search(r"([А-Яа-яЁёІіЇїЄє]{4,})\D{0,12}?(\d+)\s*[/из \\-]+\s*(\d+)",
                      строка.strip())
        if not m:
            continue
        имя = cappi.norm_full(m.group(1))
        филиал = ("Лазарева" if "лазарев" in имя
                  else "Левітана" if "левитан" in имя else None)
        if not филиал:
            continue
        всего, пройдено = int(m.group(2)), int(m.group(3))
        из[филиал] = {"всего": всего, "пройдено": пройдено}
    return из


def _отчёт_за_дату(chat, m):
    """Отчёт за дату из текста. Несуществующее число — внятный ответ,
    а не «day is out of range» в лицо."""
    д, мес, год = (m.group(2), m.group(3), m.group(5)) if m.group(2) \
        else (m.group(6), m.group(7), (m.group(8) or "").lstrip("."))
    try:
        цель = date(int(год) if год else date.today().year, int(мес), int(д))
    except ValueError:
        return say(chat, f"Такой даты нет: <b>{д}.{мес}</b>")
    if цель > date.today():
        return say(chat, f"{цель:%d.%m} ещё не наступило.")
    cmd_report(chat, цель, live=(цель == date.today()))


def сохранить_тайники(chat, разобрано, месяц=None):
    """Записывает результат тайного гостя и показывает, что получилось.

    Была вызвана из двух мест и нигде не определена — бот падал ровно на
    том пути, ради которого всё делалось: маркетолог пишет «Лазарева 5/4».
    """
    d = kpi.тайники(месяц) or {}
    d.update(разобрано)
    kpi.тайники(месяц, d)
    строки = ["🕵️ <b>Записал</b>", ""]
    for точка, v in разобрано.items():
        проц = v["пройдено"] / v["всего"] * 100 if v.get("всего") else 0
        строки.append(f"  {точка} — {v['пройдено']} из {v['всего']} = <b>{проц:.0f}%</b>")
    всего = sum(v.get("всего", 0) for v in d.values())
    пройдено = sum(v.get("пройдено", 0) for v in d.values())
    if всего:
        строки += ["", f"по сети: <b>{пройдено / всего * 100:.0f}%</b> "
                       f"({пройдено} из {всего})"]
    say(chat, "\n".join(строки))


# Тайный гость вносится в три нажатия: месяц → точка → две цифры.
#
# Раньше это была команда «/secret Лазарева 3 4», и в подсказке порядок
# был «пройдено, всего», а в свободном тексте — «всего, пройдено». Два
# порядка для одной цифры означают, что рано или поздно её внесут
# наизнанку, а от неё зависит премия. Плюс точку писали руками, и
# «Левітана» с «Левитана» разъехались в две разные записи.
ТОЧКИ_ТАЙНИКА = ["Лазарева", "Левітана"]


def месяцы_назад(сколько=4):
    """Последние месяцы, начиная с текущего."""
    out, d = [], date.today().replace(day=1)
    for _ in range(сколько):
        out.append(d)
        d = (d - timedelta(days=1)).replace(day=1)
    return out


def cmd_secret(chat, args=None):
    """Экран тайного гостя: что внесено и кнопка внести."""
    if not access.можно(chat, "показатели"):
        return say(chat, "От этих цифр зависит премия, поэтому вносить их "
                         "может тот, у кого есть доступ к показателям.")
    if args:
        # Старый формат «/secret Лазарева 5 4» продолжаем понимать: люди
        # уже привыкли, и отучать их ради красоты незачем.
        return _secret_командой(chat, args)

    строки = ["🕵️ <b>Тайный гость</b>", ""]
    for d in месяцы_назад(3):
        месяц = d.strftime("%Y-%m")
        внесено = kpi.тайники(d) or {}
        подпись = f"{МЕСЯЦЫ[d.month - 1]} {d.year}"
        if внесено:
            всего = sum(v.get("всего", 0) for v in внесено.values())
            пройдено = sum(v.get("пройдено", 0) for v in внесено.values())
            доля = f" — <b>{пройдено / всего * 100:.0f}%</b>" if всего else ""
            строки.append(f"<b>{подпись}</b>{доля}")
            for т in ТОЧКИ_ТАЙНИКА:
                v = внесено.get(т)
                строки.append(f"    {т} — " + (
                    f"{v.get('пройдено')} из {v.get('всего')}" if v
                    else "<i>нет</i>"))
        else:
            строки.append(f"<b>{подпись}</b> — <i>не вносили</i>")
        строки.append("")
    строки.append("<i>Считается доля пройденных: от неё зависит "
                  "премия шефа и су-шефов.</i>")
    say(chat, "\n".join(строки), inline=[
        [{"text": f"Внести за {МЕСЯЦЫ[d.month - 1].lower()}",
          "callback_data": f"тм:{d:%Y-%m}"}] for d in месяцы_назад(3)])


def cmd_secret_точка(chat, месяц):
    """Шаг второй: какая точка."""
    д = дата_из_текста(f"{месяц}-15")
    if д is None:
        return say(chat, "Кнопка устарела — открой «Тайный гость» заново.")
    say(chat, f"Тайный гость за <b>{МЕСЯЦЫ[д.month - 1]} {д.year}</b>.\n"
              f"Какая точка?",
        inline=[[{"text": т, "callback_data": f"тт:{месяц}:{т}"}]
                for т in ТОЧКИ_ТАЙНИКА])


def cmd_secret_ждём(chat, месяц, точка):
    """Шаг третий: ждём две цифры текстом."""
    д = дата_из_текста(f"{месяц}-15")
    if д is None or точка not in ТОЧКИ_ТАЙНИКА:
        return say(chat, "Кнопка устарела — открой «Тайный гость» заново.")
    _await[chat] = {"what": "тайник", "месяц": месяц, "точка": точка}
    было = (kpi.тайники(д) or {}).get(точка)
    хвост = (f"\nСейчас записано: <b>{было['пройдено']} из {было['всего']}</b> "
             f"— перезапишу." if было else "")
    say(chat, f"<b>{точка}</b> · {МЕСЯЦЫ[д.month - 1]} {д.year}\n\n"
              f"Напиши два числа: <b>сколько было тайников</b> и "
              f"<b>сколько пройдено</b>.\n"
              f"<i>Например: <code>5 4</code> — из пяти прошли четыре.</i>"
              f"{хвост}")


def принять_тайник(chat, текст, ждём):
    """Две цифры от человека. Порядок один и тот же везде: всего, пройдено."""
    числа = [int(x) for x in re.findall(r"\d+", текст)][:2]
    if len(числа) < 2:
        _await[chat] = ждём
        return say(chat, "Нужно два числа: сколько было и сколько пройдено.\n"
                         "<i>Например: <code>5 4</code></i>")
    всего, пройдено = числа
    if всего <= 0:
        _await[chat] = ждём
        return say(chat, "Тайников не может быть ноль — проверь первое число.")
    if пройдено > всего:
        # Молча записать 7 из 5 значит выдать премию за несуществующее.
        _await[chat] = ждём
        return say(chat, f"Пройдено ({пройдено}) больше, чем было ({всего}).\n"
                         f"<i>Первым пишется общее число тайников, вторым — "
                         f"сколько из них пройдено.</i>")
    д = дата_из_текста(f"{ждём['месяц']}-15")
    точка = ждём["точка"]
    d = kpi.тайники(д) or {}
    d[точка] = {"всего": всего, "пройдено": пройдено}
    kpi.тайники(д, d)
    доля = пройдено / всего * 100
    осталось = [т for т in ТОЧКИ_ТАЙНИКА if т not in d]
    кнопки = [[{"text": f"Внести {т}", "callback_data": f"тт:{ждём['месяц']}:{т}"}]
              for т in осталось]
    say(chat, f"✅ <b>{точка}</b> · {МЕСЯЦЫ[д.month - 1]} {д.year}\n"
              f"{пройдено} из {всего} — <b>{доля:.0f}%</b>"
              + (f"\n\n<i>Осталось внести: {', '.join(осталось)}</i>"
                 if осталось else
                 "\n\n<i>Обе точки внесены — KPI за месяц можно считать.</i>"),
        inline=кнопки or None)


def _secret_командой(chat, args):
    """Старый формат: /secret Лазарева 5 4 — всего, потом пройдено."""
    точка = kpi.ТОЧКИ.get(args[0].capitalize())
    if not точка:
        return say(chat, f"Не знаю точку «{args[0]}».\n"
                         f"<i>Есть: {', '.join(ТОЧКИ_ТАЙНИКА)}. "
                         f"Проще нажать «🕵️ Тайный гость».</i>")
    try:
        всего, пройдено = int(args[1]), int(args[2])
    except (ValueError, IndexError):
        return say(chat, "Нужно два числа: <code>/secret Лазарева 5 4</code>\n"
                         "<i>сначала сколько было, потом сколько пройдено</i>")
    return принять_тайник(chat, f"{всего} {пройдено}",
                          {"месяц": date.today().strftime("%Y-%m"), "точка": точка})


def cmd_shift_edits(chat, дней=7):
    """Явки, поправленные заметно позже конца смены.

    Правка сама по себе законна — забыли отметить, поправили. Но правка
    через несколько часов меняет оплачиваемые часы задним числом.
    """
    say(chat, f"Смотрю явки за {дней} дней…")
    найдено = []
    with cappi.Syrve() as s:
        for i in range(дней):
            d = date.today() - timedelta(days=i)
            try:
                найдено += [(d, p) for p in report.attendance(s, d)["правки"]]
            except Exception:
                continue
    if not найдено:
        return say(chat, f"За {дней} дней правок задним числом нет.")
    строки = [f"⚠️ <b>Правок задним числом: {len(найдено)}</b> за {дней} дней", ""]
    for d, p in sorted(найдено, key=lambda x: -x[1]["через"])[:20]:
        строки.append(f"  {d:%d.%m} · {p['кто'][:22]} · {p['смена']}\n"
                      f"      через {p['через']:.0f} мин, {p['правил']}")
    say(chat, "\n".join(строки))


def cmd_pick_shift_day(chat, сдвиг=0):
    кнопки, ряд = [], []
    for i in range(сдвиг, сдвиг + 12):
        d = date.today() - timedelta(days=i)
        подпись = ("сегодня" if i == 0 else "вчера" if i == 1
                   else f"{d:%d.%m} {ДНИ_НЕДЕЛИ[d.weekday()]}")
        ряд.append({"text": подпись, "callback_data": f"sd:{d.isoformat()}"})
        if len(ряд) == 3:
            кнопки.append(ряд); ряд = []
    if ряд:
        кнопки.append(ряд)
    кнопки.append([{"text": "← ещё раньше", "callback_data": f"sp:{сдвиг + 12}"}])
    say(chat, "Смена за какой день?", inline=кнопки)


# ------------------------------------------------------------ время работы
# Один вопрос «почему долго везём» разбит на три ответа: кухня, админ,
# курьер. Пока показывали общее время доставки, разговор всегда упирался в
# «это не мы» — теперь у каждого куска свой хозяин и своя цель.
ПОДПИСИ_ВРЕМЕНИ = [
    ("кухня", "Кухня", "готовят"),
    ("админ", "Админ", "собирают и отдают"),
    ("курьер", "Курьер", "в пути"),
]


def склонение(n, один, два, много):
    """«43 заказов» — мелочь, из которой складывается ощущение, что писал
    робот. Пишем по-русски."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        сл = один
    elif n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        сл = два
    else:
        сл = много
    return f"{n} {сл}"


def грн(x):
    """Деньги с копейками. Себестоимость «59.9508 ₴» — это не точность,
    а необработанное число: копейки нужны, тысячные нет."""
    if x is None:
        return "—"
    return f"{x:,.2f}".replace(",", " ").replace(".", ",")


def мин(x):
    """Минуты с одним знаком. «21.3667 мин» — это не точность, это шум:
    решение принимают по десятым, остальное только мешает читать."""
    return f"{x:.1f}".rstrip("0").rstrip(".")


def _против_цели(факт, цель_):
    """Цель — не украшение, поэтому пишем не только «сколько», но и «на
    сколько мимо». Иначе цифру читают, а вывод не делают.

    Имя не _знак: так уже называется функция KPI выше, и второе
    определение молча перекрывало первое — экраны KPI падали, как только
    в тайниках появлялись данные.
    """
    if цель_ is None:
        return ""
    разница = факт - цель_
    if разница <= 0:
        return f"  ✅ <i>цель {мин(цель_)}</i>"
    return f"  ⚠️ <i>цель {мин(цель_)}, +{мин(разница)}</i>"


def _блок_времени(точки, заголовок):
    строки = [f"<b>{заголовок}</b>"]
    for точка, v in sorted(точки.items()):
        цель_доставки = sum(report.цель(k, точка) or 0
                            for k, *_ in ПОДПИСИ_ВРЕМЕНИ)
        строки.append("")
        строки.append(f"<b>{точка}</b>  <i>{склонение(v['заказов'], 'заказ', 'заказа', 'заказов')}</i>")
        for ключ, имя, что in ПОДПИСИ_ВРЕМЕНИ:
            строки.append(f"{имя}: <b>{мин(v[ключ])} мин</b>"
                          f"{_против_цели(v[ключ], report.цель(ключ, точка))}")
        строки.append(f"Доставка без КЦ: <b>{мин(v['доставка'])} мин</b>"
                      f"{_против_цели(v['доставка'], цель_доставки or None)}")
    if not точки:
        строки.append("\nНет данных за этот период.")
    return "\n".join(строки)


def cmd_время(chat, период="день"):
    """Времена за период плюс разбивка — по дням, неделям или месяцам."""
    сегодня = date.today()
    with cappi.Syrve() as s:
        if период == "день":
            с = по = сегодня - timedelta(days=1)
            заголовок = f"⏱ Время работы · вчера, {с:%d.%m}"
            точки = report.времена(s, с, по)
            ряды = report.времена_по_дням(s, сегодня - timedelta(days=7), сегодня)
            хвост = _тренд({d: v for d, v in ряды.items()},
                           lambda d: f"{date.fromisoformat(d):%d.%m}")
        elif период == "неделя":
            недели = report.времена_по_неделям(s, недель=6)
            ключ = list(недели)[-1]
            заголовок = (f"⏱ Время работы · эта неделя, "
                         f"с {недели[ключ]['с']:%d.%m}")
            точки = недели[ключ]["точки"]
            хвост = _тренд({k: v["точки"] for k, v in недели.items()},
                           lambda k: f"{date.fromisoformat(k):%d.%m}")
        else:
            месяцы = report.времена_по_месяцам(s, месяцев=6)
            ключ = list(месяцы)[-1]
            заголовок = f"⏱ Время работы · этот месяц"
            точки = месяцы[ключ]["точки"]
            хвост = _тренд({k: v["точки"] for k, v in месяцы.items()},
                           lambda k: МЕСЯЦЫ_КОРОТКО[int(k[-2:]) - 1])

    кнопки = [[{"text": t, "callback_data": f"вр:{p}"}
               for t, p in (("День", "день"), ("Неделя", "неделя"),
                            ("Месяц", "месяц")) if p != период]]
    say(chat, _блок_времени(точки, заголовок) + хвост, inline=кнопки)


МЕСЯЦЫ_КОРОТКО = ["янв", "фев", "мар", "апр", "май", "июн",
                  "июл", "авг", "сен", "окт", "ноя", "дек"]


def _тренд(ряды, подпись):
    """Динамика доставки по периодам. Одно число без вчерашнего — просто
    число; рядом с прошлым оно уже становится «лучше» или «хуже»."""
    if len(ряды) < 2:
        return ""
    out = ["", "", "<b>Доставка без КЦ, динамика</b>"]
    for ключ, точки in list(ряды.items())[-8:]:
        куски = "  ".join(f"{т[:3]} <b>{мин(v['доставка'])}</b>"
                          for т, v in sorted(точки.items()))
        out.append(f"{подпись(ключ)}:  {куски}")
    return "\n".join(out)




# ------------------------------------------------------------------ негода
def cmd_негода(chat, день=None):
    """Компенсация за непогоду: берут ли её и со всех ли своих доставок.

    Позиция включается на стороне Джамшута, а деньги приходят в Syrve —
    значит проверить, что одно соответствует другому, можно только здесь.
    """
    день = день or date.today()
    say(chat, "Смотрю заказы…")
    with cappi.Syrve() as s:
        д = report.негода_сейчас(s, день)
    взяли, пропущены = д["за_день"], д["пропущены"]
    if not взяли:
        строки = [f"☔ <b>Непогода · {день:%d.%m}</b>", "",
                  "Компенсацию сегодня не брали ни разу.",
                  "<i>Либо погода хорошая, либо режим не включали.</i>"]
        return say(chat, "\n".join(строки), inline=_кнопки_негоды())
    сумма = sum(з["сумма"] for з in взяли)
    окно = f"{взяли[0]['время'][11:16]}–{взяли[-1]['время'][11:16]}"
    строки = [f"☔ <b>Непогода · {день:%d.%m}</b>", "",
              f"Компенсацию взяли с {склонение(len(взяли), 'заказа', 'заказов', 'заказов')}"
              f" на <b>{report.money(сумма)} ₴</b>",
              f"окно: <b>{окно}</b>",
              f"<i>Считаются только свои курьерские: самовывоз ничего не "
              f"везёт, заказы Glovo везёт Glovo.</i>"]
    if д["бесплатные"]:
        строки += ["", f"⚠️ <b>Прошли с нулевой суммой: "
                       f"{len(д['бесплатные'])}</b>",
                   "<i>Позиция в чеке есть, денег не взяла — проверь её цену.</i>"]
    if пропущены:
        строки += ["", f"⚠️ <b>Мимо кассы: "
                       f"{склонение(len(пропущены), 'заказ', 'заказа', 'заказов')}</b>"]
        строки += [f"    {з['время'][11:16]} · №{з['номер']}" for з in пропущены[:10]]
        строки.append("<i>Это заказы внутри окна непогоды, в которых "
                      "компенсации нет. Бывает законно — своя зона, "
                      "постоянный гость, — но стоит посмотреть.</i>")
    else:
        строки += ["", "✅ Внутри окна компенсацию взяли со всех."]
    say(chat, "\n".join(строки), inline=_кнопки_негоды())


def _кнопки_негоды():
    """Управление непогодой — когда Джамшут научится его отдавать."""
    if not jamshut.умеет("/weather/state"):
        return [[{"text": "📅 За вчера", "callback_data": "нг:вчера"}]]
    try:
        состояние = jamshut.погода_состояние()
    except Exception:
        return [[{"text": "📅 За вчера", "callback_data": "нг:вчера"}]]
    включена = состояние.get("on")
    return [[{"text": "☀️ Выключить непогоду" if включена
                      else "☔ Включить непогоду",
              "callback_data": "нгв:off" if включена else "нгв:on"}],
            [{"text": "📅 За вчера", "callback_data": "нг:вчера"}]]


def _watch_негода(последний):
    """Пока непогода включена — следим, что компенсацию берут со всех.

    Позицию включает Джамшут, а деньги считает Syrve, и между ними никто
    не смотрит. Один пропущенный заказ — двадцать гривен, но если правило
    применяется через раз, то это уже не про двадцать гривен.
    """
    if time.time() - последний[0] < 900:
        return
    последний[0] = time.time()
    try:
        with cappi.Syrve() as s:
            д = report.негода_сейчас(s)
    except Exception as e:
        bugs.записать("апи", f"проверка непогоды не прошла: {e}", где="сторож")
        return
    if not д["включена"]:
        return
    было = _виденное(f"негода:{date.today()}")
    новые = [з for з in д["пропущены"] + д["бесплатные"]
             if str(з["номер"]) not in было]
    if not новые:
        return
    _запомнить(f"негода:{date.today()}",
               [str(з["номер"]) for з in д["пропущены"] + д["бесплатные"]])
    пусто = [з for з in новые if з["негода"]]
    мимо = [з for з in новые if not з["негода"]]
    строки = ["☔ <b>Непогода включена, но компенсация берётся не со всех</b>"]
    if мимо:
        строки += ["", f"<b>Без компенсации: {len(мимо)}</b>"]
        строки += [f"    {з['время'][11:16]} · №{з['номер']}" for з in мимо[:8]]
    if пусто:
        строки += ["", f"<b>С нулевой суммой: {len(пусто)}</b>"]
        строки += [f"    {з['время'][11:16]} · №{з['номер']}" for з in пусто[:8]]
    строки += ["", "<i>Считаются только свои курьерские заказы. "
                   "Если это законно — своя зона или постоянный гость — "
                   "просто не обращай внимания.</i>"]
    for кому in access.подписчики_отчёта():
        try:
            say(кому, "\n".join(строки))
        except Exception:
            pass


# ------------------------------------------------------------- журнал сбоев
# Смысл экрана не в том, чтобы показать ошибки, а в том, чтобы их закрывали.
# Поэтому здесь не лента, а список проблем со статусом: пока проблему не
# пометили, она остаётся наверху и мозолит глаза.
def cmd_bugs(chat, дней=14, тип=None):
    if not access.можно(chat, "админка"):
        return say(chat, "Журнал сбоев — админский экран.")
    группы = bugs.группы(дней=дней, тип=тип)
    св = bugs.сводка(дней)
    if not группы:
        return say(chat, f"🐞 <b>Чисто</b>\nЗа {дней} дней ни падений, "
                         f"ни непонятых запросов.\n\n"
                         f"<i>Починенное скрыто — вернётся, если повторится.</i>")
    out = [f"🐞 <b>Журнал сбоев</b> · {дней} дней",
           f"{склонение(св['проблем'], 'проблема', 'проблемы', 'проблем')}, "
           f"{склонение(св['случаев'], 'случай', 'случая', 'случаев')}"
           + (f", из них падений {св['падений']}" if св["падений"] else ""), ""]
    кнопки = []
    for g in группы[:10]:
        когда = datetime.fromisoformat(str(g["последний"]))
        метка = "🆕" if g["статус"] == "новый" else "🔧"
        место = f" · {g['где']}" if g["где"] else ""
        out.append(f"{метка} {bugs.ТИПЫ.get(g['тип'], g['тип'])}{место}")
        out.append(f"   <code>{g['что'][:70]}</code>")
        out.append(f"   <i>{склонение(g['раз'], 'раз', 'раза', 'раз')}, "
                   f"последний {когда:%d.%m %H:%M}</i>")
        out.append("")
        кнопки.append([{"text": f"{g['что'][:22]} · {g['раз']}",
                        "callback_data": f"bg:{g['id']}"}])
    if len(группы) > 10:
        out.append(f"<i>…и ещё {len(группы) - 10}</i>")
    кнопки.append([{"text": "💥 Только падения", "callback_data": "bf:сбой"},
                   {"text": "🗣 Непонятые", "callback_data": "bf:непонял"}])
    кнопки.append([{"text": "Всё за 60 дней", "callback_data": "bd:60"}])
    say(chat, "\n".join(out), inline=кнопки)


def cmd_bug_карточка(chat, ид):
    g = next((x for x in bugs.группы(дней=365, скрывать_починенные=False)
              if x["id"] == ид), None)
    if not g:
        return say(chat, "Такой записи нет — возможно, её вычистили по сроку.")
    out = [f"{bugs.ТИПЫ.get(g['тип'], g['тип'])}",
           f"<b>{g['что'][:200]}</b>", ""]
    if g["где"]:
        out.append(f"где: <code>{g['где']}</code>")
    out.append(f"случаев: <b>{g['раз']}</b>  ·  статус: <b>{g['статус']}</b>")
    out.append(f"впервые: {datetime.fromisoformat(str(g['первый'])):%d.%m %H:%M}  ·  "
               f"последний: {datetime.fromisoformat(str(g['последний'])):%d.%m %H:%M}")
    if g["люди"]:
        out.append(f"у кого: {', '.join(g['люди'][:5])}")
    if len(set(g["примеры"])) > 1:
        out += ["", "<b>Примеры</b>"] + [f"  <code>{p[:70]}</code>"
                                         for p in dict.fromkeys(g["примеры"])]
    if g["детали"]:
        хвост = g["детали"].strip().splitlines()[-12:]
        out += ["", "<b>Что именно упало</b>",
                "<pre>" + html.escape("\n".join(хвост)) + "</pre>"]
    say(chat, "\n".join(out), inline=[
        [{"text": "✅ Починено", "callback_data": f"bs:{ид}:починен"},
         {"text": "🙈 Не баг", "callback_data": f"bs:{ид}:не баг"}],
        [{"text": "🔧 Взял в работу", "callback_data": f"bs:{ид}:в работе"}],
        [{"text": "◀️ К списку", "callback_data": "bd:14"}]])



# ------------------------------------------------------------- производство
# Три вещи, которые до сих пор делались руками в Syrve Office: посчитать
# себестоимость, посчитать её по черновому составу и завести новое блюдо.
#
# Себестоимость Syrve наружу не отдаёт — считаем сами из техкарт и
# складских остатков. Совпадает с их колонкой до копейки.
СОСТАВ_СТРОКА = re.compile(
    r"^\s*(?P<имя>.+?)\s+(?P<кол>\d+(?:[.,]\d+)?)\s*"
    r"(?P<ед>кг|г|гр|грамм|шт|мл|л)?\s*$", re.I)

# Во сколько раз перевести в основную единицу (кг, шт, л).
В_ОСНОВНОЙ = {"г": 0.001, "гр": 0.001, "грамм": 0.001, "мл": 0.001,
              "кг": 1.0, "л": 1.0, "шт": 1.0}


def cmd_производство(chat):
    открыть(chat, "производство",
            "<b>Производство</b>\nСебестоимость, фудкост, новые блюда.")


def cmd_сс(chat):
    """Себестоимость готового блюда по его техкарте."""
    _await[chat] = {"what": "сс"}
    say(chat, "Какое блюдо посчитать?\n"
              "<i>Артикул или часть названия — «00154» или «боніто».</i>")


def показать_сс(chat, запрос):
    кандидаты = nomenclature.найти(запрос, типы=("DISH", "PREPARED"))
    if not кандидаты:
        return say(chat, f"Не нашёл «{запрос[:40]}» в номенклатуре.")
    if len(кандидаты) > 1 and кандидаты[0][1] < 95 and (
            кандидаты[0][1] - кандидаты[1][1] < 15):
        return say(chat, "Уточни, какое именно:", inline=[
            [{"text": f"{p['name'][:34]} · {p.get('num')}",
              "callback_data": f"сс:{p.get('num')}"}] for p, _ in кандидаты[:6]])
    _показать_сс(chat, кандидаты[0][0])


def _показать_сс(chat, товар):
    say(chat, "Считаю…")
    r = cost.блюда(товар["id"])
    if not r:
        return say(chat, f"У «{товар['name']}» нет техкарты — "
                         f"считать не из чего.")
    цена = None
    try:
        цена = (cappi.cloud_prices().get(str(товар.get("num"))) or {}).get("price")
    except Exception:
        pass
    строки = [f"🧮 <b>{товар['name']}</b>",
              f"<i>артикул {товар.get('num')}</i>", ""]
    for с in r["строки"]:
        if с["сумма"] is None:
            строки.append(f"    {с['название'][:30]:<30} "
                          f"{с['кол']:.3f} — <i>нет цены</i>")
        else:
            строки.append(f"    {с['название'][:30]}\n"
                          f"        {с['кол']:.3f} × {грн(с['за_единицу'])} = "
                          f"<b>{грн(с['сумма'])} ₴</b>")
    строки += ["", f"<b>Себестоимость: {грн(r['итого'])} ₴</b>"]
    if цена:
        фк = cost.фудкост(r["итого"], цена)
        строки.append(f"цена <b>{fmt(цена)} ₴</b> · фудкост <b>{фк:.1f}%</b> · "
                      f"маржа <b>{грн(цена - r['итого'])} ₴</b>")
    if r["дыры"]:
        строки += ["", f"⚠️ <i>Без цены: {', '.join(dict.fromkeys(r['дыры']))}. "
                       f"Нет ни остатка на складе, ни своей техкарты — "
                       f"итог занижен.</i>"]
    say(chat, "\n".join(строки))


def cmd_состав(chat):
    _await[chat] = {"what": "состав"}
    say(chat, "Пришли состав — по строке на ингредиент:\n"
              "<code>рис 130 г</code>\n<code>лосось филе 40 г</code>\n"
              "<code>креветка 4 шт</code>\n\n"
              "<i>Названия можно писать по-человечески: «рис», а не "
              "«Рис на суши (готовый) ПФ». Что не пойму — переспрошу.</i>")


def разобрать_состав(текст):
    """Строки «ингредиент количество единица» → позиции номенклатуры."""
    найдено, спорные, мимо = [], [], []
    for сырая in текст.splitlines():
        строка = сырая.strip().strip("•-–—*\t ")
        if not строка:
            continue
        m = СОСТАВ_СТРОКА.match(строка)
        if not m:
            мимо.append(строка)
            continue
        кол = float(m.group("кол").replace(",", "."))
        ед = (m.group("ед") or "").lower()
        кол *= В_ОСНОВНОЙ.get(ед, 1.0)
        имя = m.group("имя").strip()
        кандидаты = nomenclature.найти(имя, типы=("GOODS", "PREPARED"))
        один = nomenclature.одна(имя, типы=("GOODS", "PREPARED"))
        if один:
            найдено.append((один, кол))
        elif кандидаты:
            спорные.append({"имя": имя, "кол": кол,
                            "варианты": [p for p, _ in кандидаты[:5]]})
        else:
            мимо.append(строка)
    return найдено, спорные, мимо


def показать_состав(chat, текст):
    найдено, спорные, мимо = разобрать_состав(текст)
    if not найдено and not спорные:
        return say(chat, "Не разобрал ни строки.\n"
                         "<i>Формат: «рис 130 г» — название, количество, "
                         "единица.</i>")
    if спорные:
        # Спрашиваем по одному: список из пяти вопросов сразу никто не
        # осилит, а ошибка в ингредиенте — это ошибка в себестоимости.
        с = спорные[0]
        _await[chat] = {"what": "состав_выбор", "текст": текст,
                        "имя": с["имя"]}
        return say(chat, f"Что такое «<b>{с['имя']}</b>»?",
                   inline=[[{"text": f"{p['name'][:34]} · {p.get('num')}",
                             "callback_data": f"сз:{p.get('num')}"}]
                           for p in с["варианты"]])
    say(chat, "Считаю…")
    r = cost.по_составу(найдено)
    строки = ["🧮 <b>Просчёт по составу</b>", ""]
    for с in r["строки"]:
        if с["сумма"] is None:
            строки.append(f"    {с['название'][:30]} — <i>нет цены</i>")
        else:
            строки.append(f"    {с['название'][:30]}\n"
                          f"        {с['кол']:.3f} × {грн(с['за_единицу'])} = "
                          f"<b>{грн(с['сумма'])} ₴</b>")
    строки += ["", f"<b>Себестоимость: {грн(r['итого'])} ₴</b>", ""]
    for ц in (r["итого"] * k for k in (3.0, 3.5, 4.0, 4.5)):
        строки.append(f"    при цене {fmt(round(ц / 10) * 10)} ₴ — "
                      f"фудкост {cost.фудкост(r['итого'], ц):.0f}%")
    if мимо:
        строки += ["", f"<i>Не разобрал: {', '.join(мимо[:4])}</i>"]
    if r["дыры"]:
        строки += ["", f"⚠️ <i>Без цены: {', '.join(dict.fromkeys(r['дыры']))}</i>"]
    say(chat, "\n".join(строки))



# --------------------------------------------------------- новое блюдо
# Два шага, как это и делается в жизни: сперва карточка (чтобы блюдо
# появилось в номенклатуре и получило артикул), потом техкарта (чтобы
# оно списывалось со склада).
#
# Второй шаг Syrve через API не отдаёт: `assemblyCharts/save` отвечает
# ASSEMBLY_CHART_IS_NOT_EDITABLE при любом наборе полей, любой дате и с
# id и без, а пользователь api — системный администратор, так что права
# ни при чём. Поэтому состав бот готовит, считает и отдаёт человеку в
# том виде, в каком его останется вбить в Office. Как только Syrve
# ручку откроет, шаг заработает сам: код готов.


def cmd_новое_блюдо(chat):
    if not access.можно(chat, "цены"):
        return say(chat, "Заводить блюда может оператор или админ — "
                         "это запись в номенклатуру.")
    _await[chat] = {"what": "блюдо_имя", "блюдо": {}}
    say(chat, "➕ <b>Новое блюдо</b>\n\nШаг 1 из 2 — карточка.\n\n"
              "Как называется?")


def блюдо_шаг(chat, текст, ждём):
    """Мастер: имя → группа → цена → выход → создание карточки."""
    д = ждём.get("блюдо", {})
    что = ждём["what"]

    if что == "блюдо_имя":
        д["имя"] = текст.strip()[:100]
        _await[chat] = {"what": "блюдо_группа", "блюдо": д}
        похожие = nomenclature.найти(д["имя"], типы=("DISH",), предел=3)
        подсказка = ""
        if похожие:
            подсказка = ("\n\n<i>Похожее уже есть: "
                         + ", ".join(p["name"][:28] for p, _ in похожие)
                         + ". Если это оно — не заводи второе.</i>")
        return say(chat, f"<b>{д['имя']}</b>\n\nВ какую группу?"
                         f"\n<i>Напиши часть названия группы — «пицца», "
                         f"«роли», «напої».</i>{подсказка}")

    if что == "блюдо_группа":
        варианты = _найти_группы(текст)
        if not варианты:
            _await[chat] = ждём      # не теряем нить: шаг повторяется
            похожие = ", ".join(g["name"] for g in _группы()[:6])
            return say(chat, f"Не нашёл группу «{текст[:30]}».\n"
                             f"<i>Группы называются по-украински — «Піца», "
                             f"«Роли», «Напої». Напиши иначе.</i>")
        if len(варианты) > 1:
            _await[chat] = ждём
            return say(chat, "Какая именно группа?", inline=[
                [{"text": g["name"][:36], "callback_data": f"нг:{g['id'][:36]}"}]
                for g in варианты[:6]])
        группа = варианты[0]
        д["группа"] = группа
        _await[chat] = {"what": "блюдо_цена", "блюдо": д}
        return say(chat, f"Группа: <b>{группа['name']}</b>\n\nЦена продажи?")

    if что == "блюдо_цена":
        try:
            д["цена"] = float(текст.replace(",", ".").strip())
        except ValueError:
            _await[chat] = ждём
            return say(chat, "Это не похоже на цену. Напиши числом.")
        _await[chat] = {"what": "блюдо_выход", "блюдо": д}
        return say(chat, "Выход порции в граммах?\n"
                         "<i>Можно «0» — если вес не нужен.</i>")

    if что == "блюдо_выход":
        try:
            д["выход"] = float(текст.replace(",", ".").strip()) / 1000
        except ValueError:
            _await[chat] = ждём
            return say(chat, "Напиши числом, в граммах.")
        tok = f"N{int(time.time())}{chat % 1000}"
        with _lock:
            _confirm[tok] = {"блюдо": д, "chat": chat}
        return say(chat,
                   f"➕ <b>{д['имя']}</b>\n"
                   f"группа: {д['группа']['name']}\n"
                   f"цена: <b>{fmt(д['цена'])} ₴</b>\n"
                   f"выход: {д['выход'] * 1000:.0f} г\n\n"
                   f"<i>Артикул Syrve присвоит сам. В меню доставки блюдо "
                   f"сразу не попадёт — это отдельная галочка.</i>",
                   inline=[[{"text": "✅ Создать карточку",
                             "callback_data": f"нб:{tok}"},
                            {"text": "✖️ Отмена", "callback_data": f"no:{tok}"}]])


def _найти_группы(запрос):
    """Группы по куску названия.

    Сравниваем нормализованно: группы названы по-украински («Піца»,
    «Роли», «Напої»), а пишут их как придётся. norm_full сводит і→и,
    ї→и, є→е — после этого «пицца» и «піца» не совпадут всё равно, из-за
    удвоенной «ц», поэтому добавлен приблизительный проход.
    """
    з = cappi.norm_full(запрос)
    группы = _группы()
    точные = [g for g in группы if cappi.norm_full(g["name"]) == з]
    if точные:
        return точные[:1]
    вхождение = [g for g in группы if з in cappi.norm_full(g["name"])]
    if вхождение:
        return вхождение
    близкие = [g for g in группы
               if SequenceMatcher(None, з, cappi.norm_full(g["name"])).ratio() > 0.7]
    return близкие


_КЭШ_ГРУПП = {"когда": 0, "что": []}


def _группы():
    if time.time() - _КЭШ_ГРУПП["когда"] > 1800:
        with cappi.Syrve() as s:
            d = json.loads(cappi._get(
                f"{s.host}/resto/api/v2/entities/products/group/list?key={s.key}",
                timeout=60))
        _КЭШ_ГРУПП.update({"когда": time.time(),
                           "что": [g for g in d if not g.get("deleted")]})
    return _КЭШ_ГРУПП["что"]


def создать_блюдо(chat, c, who):
    д = c["блюдо"]
    say(chat, "Создаю карточку…")
    донор = _донор_группы(д["группа"])
    карточка = {
        "name": д["имя"], "description": "",
        "parent": д["группа"]["id"], "type": "DISH",
        "defaultSalePrice": д["цена"], "defaultIncludedInMenu": False,
        "unitWeight": д["выход"], "unitCapacity": 0, "deleted": False,
    }
    # Категории и место приготовления копируем с соседа по группе: угадать
    # их нельзя, а без них карточка заводится неправильной.
    for поле in ("category", "accountingCategory", "mainUnit", "placeType",
                 "modifierSchemaId", "taxCategory"):
        if донор and донор.get(поле):
            карточка[поле] = донор[поле]
    try:
        with cappi.Syrve() as s:
            созданное = s.создать_товар(карточка)
    except Exception as e:
        return say(chat, f"❌ Не получилось: {str(e)[:180]}")
    audit(f"{who}\tблюдо\tсоздано\t{созданное.get('num')}\t{д['имя']}")
    nomenclature.всё(обновить=True)
    tok = f"T{int(time.time())}{chat % 1000}"
    with _lock:
        _confirm[tok] = {"товар": созданное, "chat": chat}
    say(chat, f"✅ <b>Карточка создана</b>\n"
              f"{созданное['name']}\n"
              f"артикул <code>{созданное.get('num')}</code> · "
              f"код {созданное.get('code')}\n\n"
              f"Шаг 2 из 2 — техкарта. Пришли состав.",
        inline=[[{"text": "📋 Ввести состав", "callback_data": f"тк:{tok}"}]])


def _донор_группы(группа):
    """Любое живое блюдо той же группы — источник категорий."""
    свои = [p for p in nomenclature.всё()
            if p.get("parent") == группа["id"] and p.get("type") == "DISH"]
    if not свои:
        return None
    with cappi.Syrve() as s:
        полные = json.loads(cappi._get(
            f"{s.host}/resto/api/v2/entities/products/list?key={s.key}",
            timeout=90))
    по_ид = {p["id"]: p for p in полные}
    return по_ид.get(свои[0]["id"])


def техкарта_для(chat, c):
    _await[chat] = {"what": "техкарта", "товар": c["товар"]}
    say(chat, f"Состав для <b>{c['товар']['name']}</b> — "
              f"по строке на ингредиент:\n"
              f"<code>рис 130 г</code>\n<code>лосось филе 40 г</code>\n\n"
              f"<i>Посчитаю себестоимость и соберу техкарту.</i>")


def записать_техкарту(chat, текст, ждём):
    товар = ждём["товар"]
    найдено, спорные, мимо = разобрать_состав(текст)
    if спорные:
        с = спорные[0]
        _await[chat] = {"what": "техкарта_выбор", "товар": товар,
                        "текст": текст, "имя": с["имя"]}
        return say(chat, f"Что такое «<b>{с['имя']}</b>»?",
                   inline=[[{"text": f"{p['name'][:34]} · {p.get('num')}",
                             "callback_data": f"сз:{p.get('num')}"}]
                           for p in с["варианты"]])
    if not найдено:
        return say(chat, "Не разобрал состав. Формат: «рис 130 г».")
    r = cost.по_составу(найдено)
    карта = {
        "assembledProductId": товар["id"],
        "dateFrom": date.today().isoformat(),
        "assembledAmount": 1,
        "productWriteoffStrategy": "ASSEMBLE",
        "productSizeAssemblyStrategy": "COMMON",
        "items": [{"productId": т["id"], "sortWeight": float(i),
                   "amountIn": кол, "amountMiddle": кол, "amountOut": кол}
                  for i, (т, кол) in enumerate(найдено)],
    }
    строки = [f"📋 <b>Техкарта · {товар['name']}</b>", ""]
    for т, кол in найдено:
        строки.append(f"    {т.get('num')}  {т['name'][:32]}  <b>{кол:g}</b>")
    строки += ["", f"<b>Себестоимость: {грн(r['итого'])} ₴</b>"]
    цена = товар.get("defaultSalePrice")
    if цена:
        строки.append(f"при цене {fmt(цена)} ₴ — фудкост "
                      f"<b>{cost.фудкост(r['итого'], цена):.1f}%</b>")
    try:
        with cappi.Syrve() as s:
            s.сохранить_техкарту(карта)
        строки += ["", "✅ <i>Техкарта записана в Syrve.</i>"]
    except cappi.ТехкартаЗакрыта:
        строки += ["", "⚠️ <b>Syrve не принимает техкарты через API.</b>",
                   "<i>Проверено: отказывает при любом составе и любой дате, "
                   "и права здесь ни при чём. Состав выше — в том виде, в "
                   "каком его останется вбить в Office: артикул, название, "
                   "количество в основной единице.</i>"]
    except Exception as e:
        строки += ["", f"❌ <i>{str(e)[:160]}</i>"]
    if r["дыры"]:
        строки += ["", f"⚠️ <i>Без цены: {', '.join(dict.fromkeys(r['дыры']))}</i>"]
    say(chat, "\n".join(строки))


BUTTONS = {
    "🤖 джамшут": lambda chat: открыть(chat, "джамшут",
        "<b>Джамшут</b>\nБот закрытия зон. Core им управляет, он Core не видит."),
    "🚦 зоны сейчас": cmd_jam_state,
    "🚧 закрыть зону": cmd_jam_close,
    "✅ открыть зону": cmd_jam_open,
    "📜 история": cmd_jam_history,
    "👤 кто закрывал": cmd_jam_who,
    "🩺 здоровье": cmd_jam_health,
    "☔ непогода": cmd_негода,
    "🧑‍🍳 персонал": lambda chat: открыть(chat, "персонал",
        "<b>Персонал</b>\nКто на смене, часы, выручка на человеко-час."),
    "🏷 акционные": cmd_promo,
    "⭐ спецпредложение": cmd_special,
    "🛑 стоп-лист": cmd_stoplist,
    "🚚 в работе": cmd_live,
    "❌ отмены": cmd_cancels,
    "🗑 списания": cmd_deletions,
    "👥 смена сегодня": lambda chat: cmd_shift(chat, date.today()),
    "👥 смена вчера": lambda chat: cmd_shift(chat, date.today() - timedelta(days=1)),
    "📅 смена за день": cmd_pick_shift_day,
    "⚠️ правки явок": cmd_shift_edits,
    "🏆 kpi кухни": cmd_kpi,
    "💵 процент кухни": lambda chat: cmd_процент(chat),
    "🕵️ тайный гость": lambda chat: cmd_secret(chat, []),
    # главное меню — вход в модули
    "💰 цены": lambda chat: открыть(chat, "цены",
        "<b>Цены</b>\nПоиск позиций, смена цены, сверка витрин."),
    "📈 показатели": lambda chat: открыть(chat, "показатели",
        "<b>Показатели</b>\nВыручка против плана, заказы, отмены, живые заказы."),
    "🛒 продажи": lambda chat: открыть(chat, "продажи",
        "<b>Продажи</b>\nСтоп-лист, заказы в работе, отмены."),
    "⚙️ админка": lambda chat: открыть(chat, "админка",
        "<b>Админка</b>\nПодключения, журнал, доступ, состояние."),
    "◀️ назад": lambda chat: открыть(chat, "главное", "Главное меню"),

    # модуль «Цены»
    "🔍 найти позицию": lambda chat: cmd_price(chat, ""),
    "📊 сайт и glovo": cmd_check,
    "⏳ на проверке": cmd_pending,

    # модуль «Показатели»
    "📈 сейчас": lambda chat: cmd_report(chat, None, live=True),
    "📅 за вчера": lambda chat: cmd_report(chat, date.today() - timedelta(days=1),
                                          live=False),
    "📅 выбрать день": cmd_pick_day,
    "🎯 план": lambda chat: cmd_plan(chat, ""),
    "⏱ время работы": cmd_время,
    "🚧 зоны": cmd_zones,
    "😠 жалобы": cmd_complaints,

    # модуль «Админка»
    "🔌 проверка связи": cmd_healthcheck,
    "📜 журнал цен": cmd_audit,
    "👥 доступ": cmd_access,
    "🗣 непонятые": cmd_unknown,
    "🔐 отказы": cmd_denied,
    "🐞 сбои": cmd_bugs,
    "🤖 состояние бота": cmd_botstate,

    # модуль «Производство»
    "🏭 производство": cmd_производство,
    "🧮 себестоимость блюда": cmd_сс,
    "📋 просчёт по составу": cmd_состав,
    "➕ новое блюдо": cmd_новое_блюдо,
}


def _проверить_кнопки():
    """Каждая кнопка меню должна иметь обработчик.

    Меню и обработчики лежат в разных местах, и при добавлении пункта легко
    забыть второе. Бот при этом не падает — он молча отвечает «не понял» на
    собственную кнопку, и выясняется это уже от людей. Лучше не запуститься.
    """
    все = [b for м in МЕНЮ.values() for ряд in м for b in ряд]
    сироты = [b for b in все if b.lower() not in BUTTONS]
    if сироты:
        raise SystemExit("Кнопки без обработчика: " + ", ".join(сироты))


def on_message(m):
    text, chat = m.get("text", ""), (m.get("chat") or {}).get("id")
    if not text or chat is None:
        return
    uid = m["from"]["id"]
    if not access.есть_доступ(uid):
        _отказ(m, text)
        return say(chat, "Нет доступа.\n\nТвой id: <code>%d</code>\n"
                         "<i>Покажи его тому, у кого роль «админ» — "
                         "он выдаст доступ из бота.</i>" % uid, keys=False)
    who = m["from"].get("username") or str(uid)

    if text.lower() in BUTTONS:
        return BUTTONS[text.lower()](chat)

    # ждём ответа на заданный вопрос?
    st = _await.pop(chat, None)
    if st and not text.startswith("/"):
        if st["what"] == "search":
            return cmd_price(chat, text)
        if st["what"] == "сс":
            return показать_сс(chat, text)
        if st["what"] == "состав":
            return показать_состав(chat, text)
        if st["what"] == "техкарта":
            return записать_техкарту(chat, text, st)
        if st["what"].startswith("блюдо_"):
            return блюдо_шаг(chat, text, st)
        if st["what"] == "тайник":
            return принять_тайник(chat, text, st)
        if st["what"] == "special":
            return cmd_special_add(chat, text.split())
        if st["what"] == "price":
            try:
                price = float(text.replace(",", ".").strip())
            except ValueError:
                _await[chat] = st
                return say(chat, f"Это не похоже на цену: <b>{text}</b>. Напиши числом.")
            return prepare(chat, st["code"], price, _default_date(), who)

    части = text.split()
    if not части:
        # Пустое сообщение или одни пробелы — Telegram такое присылает,
        # например когда пересылают картинку с пробелом в подписи.
        return
    cmd, *args = части
    cmd = cmd.lower().split("@")[0]
    if cmd in ("/start", "/help"):
        _menu[chat] = "главное"
        помощь(chat)
    elif cmd == "/price":
        cmd_price(chat, " ".join(args))
    elif cmd == "/check":
        cmd_check(chat)
    elif cmd == "/pending":
        cmd_pending(chat)
    elif cmd in ("/report", "/итоги"):
        d = дата_из_текста(args[0]) if args else None
        if args and d is None:
            return say(chat, f"Не понял дату: <b>{args[0]}</b>\n"
                             f"<i>Например: <code>/report 2026-09-10</code> "
                             f"или <code>/report 10.09</code></i>")
        cmd_report(chat, d, live=not args)
    elif cmd == "/people":
        if not access.можно(chat, "админка"):
            say(chat, "Кто на каких ролях — админский экран.")
        elif len(args) < 2:
            текущее = kpi.люди()
            say(chat, "<b>Кто на ролях</b>\n"
                + "\n".join(f"  {р} — {и}" for р, и in текущее.items())
                + "\n\nПоменять: <code>/people Левітана Ткачук Максим</code>"
                  "\n<code>/people шеф_id 123456789</code> — telegram шефа, "
                  "ему уходит запрос на правку часов 2-го числа."
                  "\n<i>ФИО бот находит в Syrve сам; это на случай, "
                  "если кого-то не завели вовремя.</i>")
        else:
            д = kpi.люди()
            д[args[0]] = " ".join(args[1:])
            kpi.люди(д)
            подсказка = ("\n<i>Теперь ему будет уходить запрос на правку "
                         "часов 2-го числа.</i>" if args[0] == "шеф_id" else "")
            say(chat, f"✅ {args[0]} — <b>{' '.join(args[1:])}</b>{подсказка}")
    elif cmd == "/fix":
        cmd_fix(chat, args)
    elif cmd == "/secret":
        cmd_secret(chat, args)
    elif cmd == "/kpi":
        cmd_kpi(chat)
    elif cmd == "/special":
        cmd_special_add(chat, args)
    elif cmd == "/access":
        cmd_access_add(chat, args)
    elif cmd == "/plan":
        cmd_plan(chat, text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else "")
    elif cmd == "/set":
        if len(args) < 2:
            return say(chat, "Формат: <code>/set &lt;артикул&gt; &lt;цена&gt; [завтра]</code>")
        try:
            price = float(args[1].replace(",", "."))
        except ValueError:
            return say(chat, f"Не понял цену: <b>{args[1]}</b>")
        when = _default_date()
        if len(args) > 2:
            a = args[2].lower()
            if a in СЕГОДНЯ_СЛОВА:
                when = date.today()
            else:
                разобрана = дата_из_текста(a)
                if разобрана is None:
                    return say(chat, f"Не понял дату: <b>{a}</b>\n"
                                     f"<i>Пиши «сегодня» или «12.09».</i>")
                when = разобрана
        prepare(chat, args[0], price, when, who)
    elif text.startswith("/"):
        say(chat, f"Не знаю команду <b>{cmd}</b>.\n"
                  f"<i>Всё основное — кнопками снизу, список команд — /help.</i>")
    elif (разобрано := разобрать_тайники(text)) and access.можно(chat, "показатели"):
        # Марина пишет «Лазарева 5/4» без всякой команды — так и принимаем.
        сохранить_тайники(chat, разобрано)
    elif sum(1 for l in text.splitlines()
             if re.match(r"\s*(пн|вт|ср|чт|пт|сб|вс)\b", l.strip().lower())) >= 3:
        # Вставили таблицу плана — понятно и без команды.
        cmd_plan(chat, text)
    elif похоже_на_список(text):
        # Прейскурант прислали как есть, из таблицы. Раньше на это отвечало
        # «Не понял» — и человек шёл делать тридцать /set руками.
        cmd_price_list(chat, text, who)
    else:
        низ = text.strip().lower()
        if re.fullmatch(r"\d{7,12}", низ) and access.можно(chat, "доступ"):
            # Админ прислал один telegram-id — почти наверняка «выдай ему
            # доступ». Спрашиваем роль, а не отправляем в поиск блюд.
            if низ in access.все():
                return say(chat, f"<code>{низ}</code> уже есть — "
                                 f"роль <b>{access.роль(низ)}</b>.",
                           inline=[[{"text": "Открыть карточку",
                                     "callback_data": f"ac:{низ}"}]])
            return say(chat, f"Выдать доступ <code>{низ}</code>?\nКакая роль:",
                       inline=[[{"text": r, "callback_data": f"ag:{низ}:{r}"}]
                               for r in access.РОЛИ])
        for шаблон, действие in ФРАЗЫ:
            m = re.search(шаблон, низ)
            if m:
                return действие(chat, m, text)
        # Не команда — считаем поиском. Если и поиск пуст, запись пойдёт
        # в unknown.log: значит человек хотел чего-то, чего бот не умеет.
        if not find(text):
            не_понял(chat, text, who)
            подсказка = ("<i>Похоже на прейскурант? Пришли списком, "
                         "по строке на позицию: «Название 46».</i>"
                         if len(text.splitlines()) > 1 else
                         "<i>Для поиска хватит куска названия или артикула.</i>")
            return say(chat, f"Не понял: <b>{text[:60]}</b>\n{подсказка}\n"
                             f"<i>Записал в журнал — разберём.</i>")
        cmd_price(chat, text)



# ---------------------------------------------------------------- главный цикл
def main():
    if not TOKEN:
        raise SystemExit("Нет TELEGRAM_BOT_TOKEN в ~/.cappi/api.env")
    люди = access.все()
    if not люди:
        print("⚠ Никому не выдан доступ. Заполни TELEGRAM_ALLOWED_IDS "
              "в ~/.cappi/api.env — эти id станут админами при первом запуске.")
    else:
        print("доступ: " + ", ".join(f"{u}·{v['роль']}" for u, v in люди.items()))
    порт = webhook.serve()
    if порт:
        webhook.СЛУШАТЕЛИ.append(_зона_изменилась)
        print(f"приёмник событий Джамшута на порту {порт}")
    else:
        print("CORE_WEBHOOK_TOKEN не задан — события зон не принимаются")
    _проверить_кнопки()
    threading.Thread(target=watcher, daemon=True).start()
    tg("setMyCommands", commands=[
        {"command": "report", "description": "показатели: сейчас или за день"},
        {"command": "price", "description": "найти позицию и цену"},
        {"command": "set", "description": "сменить цену одной позиции"},
        {"command": "check", "description": "сверка витрин: Syrve, сайт, Glovo"},
        {"command": "pending", "description": "какие цены ждут проверки"},
        {"command": "kpi", "description": "KPI шефа и су-шефов"},
        {"command": "help", "description": "что умеет бот"},
    ])
    print("бот запущен")
    offset = None
    while True:
        try:
            for u in tg("getUpdates", offset=offset, timeout=50).get("result", []):
                offset = u["update_id"] + 1
                try:
                    if "callback_query" in u:
                        on_button(u["callback_query"])
                    elif "message" in u:
                        on_message(u["message"])
                except Exception as e:
                    traceback.print_exc()
                    что = ((u.get("message") or {}).get("text")
                           or (u.get("callback_query") or {}).get("data") or "?")
                    кто = ((u.get("message") or u.get("callback_query")
                            or {}).get("from") or {}).get("id")
                    ид = bugs.сбой(f"на «{что[:40]}»", e, кто=кто)
                    chat = ((u.get("message") or u.get("callback_query", {}).get("message")
                             or {}).get("chat") or {}).get("id")
                    if chat:
                        # Человек должен понимать, что об этом уже знают, а не
                        # гадать, дошло ли до кого-нибудь.
                        say(chat, f"❌ Сломалось на «{что[:40]}».\n"
                                  f"<i>Записал в журнал ошибок"
                                  f"{f' — {ид}' if ид else ''}, разберём. "
                                  f"Данные не пострадали.</i>")
        except Exception:
            traceback.print_exc()
            time.sleep(5)


if __name__ == "__main__":
    main()
