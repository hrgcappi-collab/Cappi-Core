#!/usr/bin/env python3
"""Телеграм-бот Cappi: смена цен в Syrve и контроль, что цена доехала до витрин.

Запуск:  python3 bot.py
Конфиг:  ~/.cappi/api.env  (TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_IDS)
"""
import json, os, re, threading, time, traceback, urllib.parse, urllib.request
from difflib import SequenceMatcher
from datetime import date, datetime, timedelta

import cappi
import report

CFG = cappi.cfg()
TOKEN = CFG.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED = {int(x) for x in re.findall(r"\d+", CFG.get("TELEGRAM_ALLOWED_IDS", ""))}
API = f"https://api.telegram.org/bot{TOKEN}"

STATE_DIR = os.path.expanduser("~/.cappi")
PENDING = os.path.join(STATE_DIR, "pending.json")   # отложенные проверки витрин
AUDIT = os.path.join(STATE_DIR, "changes.log")      # журнал изменений цен

REPORT_AT = "22:00"         # когда присылать итоги дня
CHECK_AFTER_MIN = 30        # через сколько проверять, доехала ли цена
MAX_CHANGE_PCT = 50         # скачок больше этого бот не проводит

# Цену по умолчанию меняем следующей датой: приказ вступает в силу, когда
# закроется ночная кассовая смена, около трёх ночи. Так цена не прыгает
# посреди рабочего дня и не задевает уже открытые смены и принятые заказы.
DEFAULT_TOMORROW = True
СЕГОДНЯ_СЛОВА = ("сегодня", "сейчас", "today", "now", "срочно")

_confirm = {}               # токен → подготовленное изменение
_await = {}                 # чат → чего ждём от следующего сообщения
_lock = threading.Lock()

# Постоянная клавиатура снизу — основные действия под рукой.
KB_MAIN = {
    "keyboard": [
        [{"text": "🔍 Найти позицию"}, {"text": "📊 Сверка витрин"}],
        [{"text": "📈 Показатели"}, {"text": "⏳ На проверке"}],
        [{"text": "❓ Помощь"}],
    ],
    "resize_keyboard": True,
}


# ------------------------------------------------------------------ Telegram
def tg(method, **params):
    data = urllib.parse.urlencode(
        {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
         for k, v in params.items() if v is not None}).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=70) as r:
        return json.loads(r.read())


def say(chat, text, inline=None, keyboard=True):
    markup = None
    if inline:
        markup = {"inline_keyboard": inline}
    elif keyboard:
        markup = KB_MAIN
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
    if len(word) >= 5:
        out.add(word[:len(word) - 2])
    return out


def find(query):
    """Ищем по артикулу или куску названия. Цена — из Cloud API, карточке не верим."""
    q = cappi.norm_full(query)
    menu = cappi.cloud_prices()

    def matches(code, p):
        if q == str(code).lower():
            return True
        name = cappi.norm_full(p["name"])
        alias = " ".join(ALIASES.get(code, []))
        # Каждое слово запроса должно найтись, иначе «сливочная креветка»
        # вернёт все креветки подряд.
        return all(any(n in name or n in alias for n in _needles(w))
                   for w in q.split())

    hits = [(c, p) for c, p in menu.items() if matches(c, p)]
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
    say(chat, txt, inline=[
        [{"text": "✏️ Изменить цену", "callback_data": f"ed:{code}"}],
        [{"text": "👀 Где и почём показывается", "callback_data": f"sh:{code}"}],
    ])


def cmd_price(chat, query):
    if not query:
        _await[chat] = {"what": "search"}
        return say(chat, "Что искать? Напиши часть названия или артикул.")
    hits = find(query)
    if not hits:
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


def cmd_plan(chat, args):
    """/plan 45000 — на сегодня; /plan месяц 1350000 — на текущий месяц."""
    p = report.load_plan()
    if not args:
        today = date.today()
        сумма, откуда = report.plan_for(today)
        строки = [f"План на {today:%d.%m}: "
                  + (f"<b>{report.money(сумма)} ₴</b> <i>({откуда})</i>"
                     if сумма else "<i>не задан</i>")]
        if p.get("months"):
            строки.append("")
            for m, v in sorted(p["months"].items()):
                строки.append(f"  {m}: {report.money(v)} ₴")
        строки += ["", "Поставить:", "<code>/plan 45000</code> — на сегодня",
                   "<code>/plan месяц 1350000</code> — на месяц, разделится по дням"]
        return say(chat, "\n".join(строки))

    месяц = args[0].lower() in ("месяц", "month")
    сырое = args[1] if месяц else args[0]
    try:
        сумма = float(re.sub(r"[\s ]", "", сырое).replace(",", "."))
    except (ValueError, IndexError):
        return say(chat, f"Не понял сумму: <b>{сырое}</b>")
    if месяц:
        p.setdefault("months", {})[date.today().strftime("%Y-%m")] = сумма
        report.save_plan(p)
        return say(chat, f"План на {date.today():%B %Y}: <b>{report.money(сумма)} ₴</b>\n"
                         f"По дням разойдётся сам.")
    p.setdefault("days", {})[date.today().isoformat()] = сумма
    report.save_plan(p)
    say(chat, f"План на {date.today():%d.%m}: <b>{report.money(сумма)} ₴</b>")


# --------------------------------------------------------------- смена цены
def _default_date():
    """Завтра — чтобы цена сменилась ночью, а не в рабочий день."""
    return date.today() + timedelta(days=1) if DEFAULT_TOMORROW else date.today()


def prepare(chat, code, new_price, when, user):
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
    for uid in ALLOWED:
        try:
            say(uid, текст)
        except Exception:
            traceback.print_exc()


def watcher():
    """Догоняет отложенные проверки и присылает итоги дня."""
    sent = {}
    while True:
        try:
            _send_daily(sent)
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
    if q["from"]["id"] not in ALLOWED:
        return
    act, _, arg = q["data"].partition(":")
    tg("answerCallbackQuery", callback_query_id=q["id"])
    who = q["from"].get("username") or str(q["from"]["id"])

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
BUTTONS = {
    "🔍 найти позицию": lambda chat: cmd_price(chat, ""),
    "📊 сверка витрин": cmd_check,
    "📈 показатели": cmd_report,
    "⏳ на проверке": cmd_pending,
    "❓ помощь": lambda chat: say(chat, HELP),
}


def on_message(m):
    text, chat = m.get("text", ""), (m.get("chat") or {}).get("id")
    if not text or chat is None:
        return
    uid = m["from"]["id"]
    if uid not in ALLOWED:
        return say(chat, "Нет доступа.\n\nТвой id: <code>%d</code>\n"
                         "Впиши его в TELEGRAM_ALLOWED_IDS в ~/.cappi/api.env "
                         "и перезапусти бота." % uid, keyboard=False)
    who = m["from"].get("username") or str(uid)

    if text.lower() in BUTTONS:
        return BUTTONS[text.lower()](chat)

    # ждём ответа на заданный вопрос?
    st = _await.pop(chat, None)
    if st and not text.startswith("/"):
        if st["what"] == "search":
            return cmd_price(chat, text)
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
    elif cmd == "/plan":
        cmd_plan(chat, args)
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
    else:
        # Любой текст — это поиск. Так естественнее, чем отчитывать за команду.
        cmd_price(chat, text)


# ---------------------------------------------------------------- главный цикл
def main():
    if not TOKEN:
        raise SystemExit("Нет TELEGRAM_BOT_TOKEN в ~/.cappi/api.env")
    if not ALLOWED:
        print("⚠ TELEGRAM_ALLOWED_IDS пуст — бот никого не пустит")
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
