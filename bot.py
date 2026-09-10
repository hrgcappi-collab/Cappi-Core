#!/usr/bin/env python3
"""Телеграм-бот Cappi: смена цен в Syrve и контроль, что цена доехала до витрин.

Запуск:  python3 bot.py
Конфиг:  ~/.cappi/api.env  (TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_IDS)
"""
import json, os, re, sys, threading, time, traceback, urllib.parse, urllib.request
from difflib import SequenceMatcher
from datetime import date, datetime, timedelta

import access
import cappi
import jamshut
import kpi
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
PENDING = os.path.join(STATE_DIR, "pending.json")   # отложенные проверки витрин
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
                ["🤖 Джамшут"],
                ["⚙️ Админка"]],
    "цены": [["🔍 Найти позицию"],
             ["📊 Сверка витрин", "⏳ На проверке"],
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
                   ["🎯 План", "🚧 Зоны"],
                   ["😠 Жалобы"],
                   ["◀️ Назад"]],
    # Персонал — про людей на смене. Отдельно от показателей: там про
    # деньги, здесь про тех, кто их зарабатывает, и вопросы разные.
    "персонал": [["👥 Смена сегодня", "👥 Смена вчера"],
                 ["📅 Смена за день", "⚠️ Правки явок"],
                 ["🏆 KPI кухни", "🕵️ Тайный гость"],
                 ["◀️ Назад"]],
    # Джамшут — подчинённый бот. Core им управляет, он о Core не знает.
    "джамшут": [["🚦 Зоны сейчас"],
                ["🚧 Закрыть зону", "✅ Открыть зону"],
                ["📜 История", "👤 Кто закрывал"],
                ["🩺 Здоровье"],
                ["◀️ Назад"]],
    "админка": [["🔌 Проверка связи", "📜 Журнал цен"],
                ["🗣 Непонятые", "🔐 Отказы"],
                ["👥 Доступ"],
                ["🤖 Состояние бота"],
                ["◀️ Назад"]],
}

_menu = {}          # чат → в каком модуле он сейчас


# Какой пункт главного меню какого права требует. Показывать кнопку,
# которая ответит «нельзя», — хуже, чем не показывать её вовсе.
ТРЕБУЕТ = {"💰 Цены": "витрины", "🛒 Продажи": "витрины",
           "📈 Показатели": "показатели", "🧑‍🍳 Персонал": "показатели",
           "🤖 Джамшут": "показатели", "⚙️ Админка": "админка"}


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


def save_pending(items):
    json.dump(items, open(PENDING, "w"), ensure_ascii=False, indent=1)


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
    """Что показывают витрины. Сайт — точно по guid, Glovo — по названию."""
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
HELP = f"""<b>Бот цен Cappi</b>

Кнопки снизу — основное. Командами тоже можно:

<code>/price окрошка</code> — найти позицию
<code>/set 03275 46</code> — сменить цену (вступит в силу ночью)
<code>/set 03275 46 сейчас</code> — поменять прямо сейчас
<code>/report</code> — показатели прямо сейчас
<code>/report 2026-09-09</code> — за прошедший день
<code>/plan 45000</code> — план выручки на сегодня
<code>/plan месяц 1350000</code> — план на месяц
<code>/check</code> — сверка витрин
<code>/pending</code> — что стоит на проверке

По умолчанию цена меняется <b>ночью</b>: приказ ставится на следующую дату
и срабатывает, когда закроется кассовая смена, около 3:00. Так цена не
прыгает посреди дня и не задевает уже принятые заказы.

Как это работает: бот создаёт в Syrve приказ об изменении прейскуранта.
Выгрузка на сайт идёт автоматически раз в 20 минут, Glovo подтягивается
позже. Через {CHECK_AFTER_MIN} минут бот сам проверит витрины и напишет,
доехала ли цена.

Скачок больше {MAX_CHANGE_PCT}% бот не проводит — защита от опечатки.

Итоги дня приходят сами в {REPORT_AT}."""


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


def cmd_check(chat):
    say(chat, "Сверяю Syrve ↔ сайт ↔ Glovo, это займёт минуту…")
    syr = {k: v for k, v in cappi.cloud_prices().items() if v["in_menu"]}
    site, glovo = cappi.site_prices(), cappi.glovo_prices()
    bad = []
    for code, v in sorted(syr.items(), key=lambda x: x[1]["name"]):
        row = site.get(v["id"])                       # сайт — точно по guid
        s = row["price"] if row else None
        g = glovo.get(cappi.norm(v["name"]))          # Glovo — только по названию
        if s is None and g is None:
            continue
        if (s is not None and s != v["price"]) or (g is not None and abs(g - v["price"]) > 0.01):
            bad.append(f"<code>{code}</code> {v['name'][:34]}\n"
                       f"     Syrve <b>{fmt(v['price'])}</b> · сайт {fmt(s) if s is not None else '—'}"
                       f" · Glovo {fmt(g) if g is not None else '—'}")
    head = (f"Syrve {len(syr)} · сайт {len(site)} · Glovo {len(glovo)}\n"
            f"<i>сайт сверяется по артикулу, Glovo — по названию</i>\n\n")
    say(chat, head + ("❌ <b>Расхождения</b>\n\n" + "\n".join(bad[:25])
                      if bad else "✅ Расхождений нет"))


def cmd_pending(chat):
    items = [i for i in load_pending() if i["chat"] == chat]
    if not items:
        return say(chat, "Ничего не стоит на проверке.")
    say(chat, "\n".join(
        f"<code>{i['code']}</code> {i['name'][:28]} → {fmt(i['price'])} ₴\n"
        f"     {'приказ принят?' if i.get('stage') == 'planned' else 'витрины'}"
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
        f"проверка витрин через <b>{CHECK_AFTER_MIN} мин</b> после смены цены",
        f"порог опечатки: <b>{MAX_CHANGE_PCT}%</b>",
        "", f"папка: <code>{HERE}</code>",
    ]
    say(chat, "\n".join(строки))


# --------------------------------------------------------------- смена цены
def _default_date():
    """Завтра — чтобы цена сменилась ночью, а не в рабочий день."""
    return date.today() + timedelta(days=1) if DEFAULT_TOMORROW else date.today()


def prepare(chat, code, new_price, when, user):
    if not access.можно(chat, "цены"):
        return say(chat, "Менять цены может оператор или админ. "
                         "У тебя роль «смотрящий».")
    """Готовим изменение и показываем карточку подтверждения. Ещё ничего не меняем."""
    hits = [h for h in find(code) if str(h[0]).lower() == str(code).lower()]
    if not hits:
        return say(chat, f"Нет позиции с артикулом <code>{code}</code>.")
    _, p = hits[0]
    old = p["price"]
    if abs(new_price - old) / max(old, 1) * 100 > MAX_CHANGE_PCT:
        return say(chat, f"⚠️ Скачок больше {MAX_CHANGE_PCT}%: "
                         f"<b>{fmt(old)} → {fmt(new_price)} ₴</b>\n"
                         f"Похоже на опечатку. Такие изменения бот не проводит — "
                         f"если это правда нужно, делай в Syrve руками.")
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
    items = load_pending()
    сегодня = c["date"] == date.today().isoformat()
    # Приказ на завтра проверять по витринам сегодня бессмысленно — цена ещё
    # не должна была измениться. Сначала убеждаемся, что Syrve его принял и
    # показывает как запланированный, а витрины смотрим уже в тот день.
    items.append({**{k: c[k] for k in ("code", "name", "price", "old", "chat", "date")},
                  "guid": c["pid"],
                  "doc": doc["documentNumber"],
                  "stage": "showcase" if сегодня else "planned",
                  "due": (datetime.now() + timedelta(
                      minutes=CHECK_AFTER_MIN if сегодня else 5)).isoformat()})
    save_pending(items)
    return doc


# ------------------------------------------------------- фоновая проверка
def _check_planned(it):
    """Приказ на будущее: он ещё не сработал, но Syrve уже должен показывать
    его как запланированный. Это и есть доказательство, что цена сменится."""
    p = cappi.cloud_prices().get(it["code"], {})
    nxt, when = p.get("next"), (p.get("next_date") or "")[:10]
    ok = nxt is not None and abs(nxt - it["price"]) < 0.01
    when_h = datetime.fromisoformat(it["date"]).strftime("%d.%m")
    if ok:
        say(it["chat"],
            f"✅ Приказ принят и стоит в очереди\n\n<b>{it['name']}</b>\n"
            f"приказ №{it['doc']}, {fmt(it['old'])} → {fmt(it['price'])} ₴\n"
            f"сменится ночью, {when_h} около 3:00\n\n"
            f"<i>Проверю витрины утром {when_h}.</i>")
    else:
        say(it["chat"],
            f"⚠️ Syrve не показывает запланированную цену\n\n<b>{it['name']}</b>\n"
            f"приказ №{it['doc']} создан, но в меню нет отметки о смене на "
            f"{fmt(it['price'])} ₴{f' (стоит {fmt(nxt)} с {when})' if nxt else ''}.\n"
            f"Стоит открыть приказ в Syrve и проверить галочку «Приказ действует».")
    # В любом случае смотрим витрины в день, когда цена должна смениться.
    return {**it, "stage": "showcase",
            "due": datetime.fromisoformat(it["date"]).replace(hour=10).isoformat()}


def _check_showcase(it):
    """Цена уже должна была смениться — сверяем витрины."""
    site, glovo = where_shown(it.get("guid", ""), it["name"])
    p = it["price"]
    ok_site = site is not None and abs(site - p) < 0.01
    ok_glovo = glovo is not None and abs(glovo - p) < 0.01
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
           "до часа. Проверь ещё раз кнопкой «Сверка витрин».</i>"))
    return None


def _send_daily(sent):
    """Итоги дня в REPORT_AT. sent помнит дату, чтобы не отправить дважды."""
    now = datetime.now()
    if now.strftime("%H:%M") < REPORT_AT or sent.get("day") == now.date():
        return
    sent["day"] = now.date()
    текст = report.render(report.collect(), live=False)
    for uid in access.подписчики_отчёта():
        try:
            say(uid, текст)
        except Exception:
            traceback.print_exc()


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


def _запомнить(раздел, ключи):
    try:
        d = json.load(open(ALERTS))
    except Exception:
        d = {}
    # Держим только сегодняшнее: вчерашние ключи ни с чем не сравниваются,
    # а файл иначе растёт без конца.
    d[раздел] = sorted(ключи)
    json.dump(d, open(ALERTS, "w"), ensure_ascii=False)


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
    while True:
        try:
            _send_daily(sent)
            _pull_zones(последний_опрос)
            _watch_stoplist(последний_стоп)
            _watch_losses(последний_потери)
            keep = []
            for it in load_pending():
                if datetime.now() < datetime.fromisoformat(it["due"]):
                    keep.append(it)
                    continue
                nxt = (_check_planned(it) if it.get("stage") == "planned"
                       else _check_showcase(it))
                if nxt:
                    keep.append(nxt)
            save_pending(keep)
        except Exception:
            traceback.print_exc()
        time.sleep(60)


# ------------------------------------------------------------------- кнопки
def on_button(q):
    chat = q["message"]["chat"]["id"]
    if not access.есть_доступ(q["from"]["id"]):
        return
    act, _, arg = q["data"].partition(":")
    tg("answerCallbackQuery", callback_query_id=q["id"])
    who = q["from"].get("username") or str(q["from"]["id"])

    if act == "ag":                                   # выдать доступ по id
        if not access.можно(chat, "доступ"):
            return say(chat, "Только админ.")
        uid, _, роль = arg.partition(":")
        access.добавить(uid, роль, кто=chat)
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
        зона = (jamshut.справочник_зон().get(int(arg)) or {}).get("name", arg)
        return say(chat, f"На сколько закрыть <b>{зона}</b>?", inline=[
            [{"text": f"{м} мин", "callback_data": f"zd:{arg}:{м}"} for м in (15, 30, 45)],
            [{"text": f"{м} мин", "callback_data": f"zd:{arg}:{м}"} for м in (60, 90, 120)],
        ])

    if act == "zd":                                   # подтверждение закрытия
        зид, _, минут = arg.partition(":")
        z = jamshut.справочник_зон().get(int(зид)) or {}
        return say(chat,
            f"🚧 Закрыть <b>{z.get('name', зид)}</b>\n"
            f"район: {z.get('district', '—')}\n"
            f"на <b>{минут} мин</b>\n\n"
            f"<i>Доставка в эту зону прекратится сразу.</i>",
            inline=[[{"text": "✅ Закрыть", "callback_data": f"zy:{зид}:{минут}"},
                     {"text": "✖️ Отмена", "callback_data": "no:—"}]])

    if act in ("zy", "zo"):                           # выполняем
        if not access.можно(chat, "зоны"):
            return say(chat, "Только оператор или админ.")
        зид, _, минут = arg.partition(":")
        z = jamshut.справочник_зон().get(int(зид)) or {}
        имя = z.get("name", зид)
        try:
            if act == "zy":
                jamshut.закрыть_зону([int(зид)], int(минут), who, z.get("district"))
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
        z = jamshut.справочник_зон().get(int(arg)) or {}
        имя = z.get("name", arg)
        try:
            jamshut.открыть_зону([int(arg)], who)
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

    if act == "kr":                                   # выбрали роль → месяц
        return cmd_kpi_месяц(chat, arg)

    if act == "km":                                   # роль и месяц → расчёт
        роль, _, месяц = arg.partition(":")
        return cmd_kpi_показать(chat, роль, месяц)

    if act == "sd":                                   # смена за выбранный день
        return cmd_shift(chat, date.fromisoformat(arg))

    if act == "sp":
        return cmd_pick_shift_day(chat, int(arg))

    if act == "rd":                                   # отчёт за выбранный день
        d = date.fromisoformat(arg)
        return cmd_report(chat, d, live=(d == date.today()))

    if act == "rp":                                   # листаем календарь назад
        return cmd_pick_day(chat, int(arg))

    if act == "ac":                                   # карточка человека
        return экран_человека(chat, arg)

    if act in ("ar", "ax"):                           # смена роли или удаление
        if not access.можно(chat, "доступ"):
            return say(chat, "Только админ.")
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
        return say(chat, "Запрос устарел, начни заново.")

    if act == "no":
        return say(chat, "Отменено, ничего не менял.")

    if act == "dt":                                   # перенести дату
        cur = date.fromisoformat(c["date"])
        new = date.today() + timedelta(days=1) if cur == date.today() else date.today()
        return prepare(chat, c["code"], c["price"], new, who)

    if act == "go":
        try:
            doc = do_change(c)
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
            return
            say(chat, f"✅ Приказ <b>№{doc['documentNumber']}</b> проведён\n"
                      f"{c['name']}: <b>{fmt(c['old'])} → {fmt(c['price'])} ₴</b> "
                      f"с {date.fromisoformat(c['date']):%d.%m}\n\n"
                      f"Проверю витрины через {CHECK_AFTER_MIN} минут.")
        except Exception as e:
            say(chat, f"❌ Не получилось: {e}")


# ------------------------------------------------------------------ сообщения
# Текстовые команды. Смысл не в том, чтобы угадать все формулировки — это
# невозможно, — а в том, чтобы покрыть ходовые, а остальное записать в
# unknown.log и раз в пару дней разобрать. Порядок важен: первое совпадение
# выигрывает, поэтому узкие правила стоят выше широких.
ФРАЗЫ = [
    # показатели
    (r"^(выручк|показател|как дела|что по деньгам|итог|сводк|результат)",
     lambda chat, m, txt: cmd_report(chat, None, live=True)),
    (r"(за )?вчера", lambda chat, m, txt: cmd_report(
        chat, date.today() - timedelta(days=1), live=False)),
    (r"^(за )?(позавчера)", lambda chat, m, txt: cmd_report(
        chat, date.today() - timedelta(days=2), live=False)),
    (r"^(выбер|выбрать день|календар|за день|какой день)",
     lambda chat, m, txt: cmd_pick_day(chat)),
    (r"^(за )?(\d+) дн", lambda chat, m, txt: cmd_report(
        chat, date.today() - timedelta(days=int(m.group(2))), live=False)),
    (r"^(за )?(\d{1,2})\.(\d{1,2})(\.(\d{4}))?$",
     lambda chat, m, txt: cmd_report(
         chat, date(int(m.group(5) or date.today().year),
                    int(m.group(3)), int(m.group(2))), live=False)),
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
     lambda chat, m, txt: say(chat, HELP)),

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


def cmd_cancels(chat):
    with cappi.Syrve() as s:
        от = report.cancels(s, date.today())
        уд = report.removals(s, date.today())
    строки = [f"❌ <b>Отмен сегодня: {sum(от.values())}</b>"]
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
        return say(chat, "Только админ.")
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
        события = jamshut.история(дней)
    except Exception as e:
        return say(chat, f"Джамшут не отвечает: {str(e)[:120]}")
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
    day = date.fromisoformat(месяц + "-15")
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
        if мои and мои.get("всего"):
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
    for x in в["строки"]:
        знак = "✅" if x["выполнен"] else ("⏳" if x["факт"] is None else "❌")
        строки.append(f"    {знак} {x['критерий']:<9} {report.money(x['сумма']):>5} ₴")

    незакрыт = (day.year, day.month) == (date.today().year, date.today().month)
    if незакрыт:
        строки += ["", "⚠️ <i>Месяц не закончен — цифры предварительные.</i>"]
    строки += ["", "<i>Списание: счета 02.08+03.01+03.06. "
                   "Переучёт: 02.04+02.09. Выручка кухни — только блюда. "
                   "Выполнил — получил, частичных начислений нет.</i>"]
    say(chat, "\n".join(строки))


def cmd_secret(chat, args):
    """Внести результат тайного гостя: /secret Лазарева 3 4"""
    if not access.можно(chat, "показатели"):
        return say(chat, "Нет доступа.")
    if len(args) < 3:
        текущее = kpi.тайники() or {}
        строки = ["🕵️ <b>Тайный гость</b>", ""]
        if текущее:
            for т, v in текущее.items():
                строки.append(f"  {т} — {v.get('пройдено')} из {v.get('всего')}")
        else:
            строки.append("  <i>за этот месяц ещё не вносили</i>")
        строки += ["", "Внести: <code>/secret Лазарева 3 4</code>",
                   "<i>филиал, пройдено, всего</i>"]
        return say(chat, "\n".join(строки))
    точка = args[0].capitalize()
    try:
        пройдено, всего = int(args[1]), int(args[2])
    except ValueError:
        return say(chat, "Числа не разобрал. Формат: <code>/secret Лазарева 3 4</code>")
    d = kpi.тайники() or {}
    d[точка] = {"пройдено": пройдено, "всего": всего}
    kpi.тайники(значение=d)
    say(chat, f"🕵️ {точка}: <b>{пройдено} из {всего}</b> "
              f"за {МЕСЯЦЫ[date.today().month - 1]}")


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


BUTTONS = {
    "🤖 джамшут": lambda chat: открыть(chat, "джамшут",
        "<b>Джамшут</b>\nБот закрытия зон. Core им управляет, он Core не видит."),
    "🚦 зоны сейчас": cmd_jam_state,
    "🚧 закрыть зону": cmd_jam_close,
    "✅ открыть зону": cmd_jam_open,
    "📜 история": cmd_jam_history,
    "👤 кто закрывал": cmd_jam_who,
    "🩺 здоровье": cmd_jam_health,
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
    "📊 сверка витрин": cmd_check,
    "⏳ на проверке": cmd_pending,

    # модуль «Показатели»
    "📈 сейчас": lambda chat: cmd_report(chat, None, live=True),
    "📅 за вчера": lambda chat: cmd_report(chat, date.today() - timedelta(days=1),
                                          live=False),
    "📅 выбрать день": cmd_pick_day,
    "🎯 план": lambda chat: cmd_plan(chat, ""),
    "🚧 зоны": cmd_zones,
    "😠 жалобы": cmd_complaints,

    # модуль «Админка»
    "🔌 проверка связи": cmd_healthcheck,
    "📜 журнал цен": cmd_audit,
    "👥 доступ": cmd_access,
    "🗣 непонятые": cmd_unknown,
    "🔐 отказы": cmd_denied,
    "🤖 состояние бота": cmd_botstate,
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
        if st["what"] == "special":
            return cmd_special_add(chat, text.split())
        if st["what"] == "price":
            try:
                price = float(text.replace(",", ".").strip())
            except ValueError:
                _await[chat] = st
                return say(chat, f"Это не похоже на цену: <b>{text}</b>. Напиши числом.")
            return prepare(chat, st["code"], price, _default_date(), who)

    cmd, *args = text.split()
    cmd = cmd.lower().split("@")[0]
    if cmd in ("/start", "/help"):
        _menu[chat] = "главное"
        say(chat, HELP)
    elif cmd == "/price":
        cmd_price(chat, " ".join(args))
    elif cmd == "/check":
        cmd_check(chat)
    elif cmd == "/pending":
        cmd_pending(chat)
    elif cmd in ("/report", "/итоги"):
        d = date.fromisoformat(args[0]) if args else None
        cmd_report(chat, d, live=not args)
    elif cmd == "/people":
        if not access.можно(chat, "админка"):
            say(chat, "Только админ.")
        elif len(args) < 2:
            текущее = kpi.люди()
            say(chat, "<b>Кто на ролях</b>\n"
                + "\n".join(f"  {р} — {и}" for р, и in текущее.items())
                + "\n\nПоменять: <code>/people Левітана Ткачук Максим</code>"
                  "\n<i>Справочник Syrve отстаёт: там су-шефы записаны "
                  "поваром и учётчиком, поэтому держим отдельно.</i>")
        else:
            д = kpi.люди()
            д[args[0]] = " ".join(args[1:])
            kpi.люди(д)
            say(chat, f"✅ {args[0]} — <b>{' '.join(args[1:])}</b>")
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
            elif re.fullmatch(r"\d{1,2}\.\d{1,2}", a):     # 12.09
                d, m = (int(x) for x in a.split("."))
                when = date(date.today().year, m, d)
        prepare(chat, args[0], price, when, who)
    elif text.startswith("/"):
        say(chat, "Не знаю такой команды. Жми кнопки снизу или /help")
    elif sum(1 for l in text.splitlines()
             if re.match(r"\s*(пн|вт|ср|чт|пт|сб|вс)\b", l.strip().lower())) >= 3:
        # Вставили таблицу плана — понятно и без команды.
        cmd_plan(chat, text)
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
            return say(chat, f"Не понял: <b>{text[:60]}</b>\n"
                             f"<i>Записал — разберём на ревизии. "
                             f"Пока попробуй кнопки или /help.</i>")
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
        {"command": "price", "description": "найти позицию и цену"},
        {"command": "set", "description": "сменить цену"},
        {"command": "check", "description": "сверка витрин"},
        {"command": "pending", "description": "что стоит на проверке"},
        {"command": "help", "description": "как это работает"},
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
                    chat = ((u.get("message") or u.get("callback_query", {}).get("message")
                             or {}).get("chat") or {}).get("id")
                    if chat:
                        say(chat, f"❌ Ошибка: {e}")
        except Exception:
            traceback.print_exc()
            time.sleep(5)


if __name__ == "__main__":
    main()
