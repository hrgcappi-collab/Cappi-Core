#!/usr/bin/env python3
"""Телеграм-бот Cappi: смена цен в Syrve и контроль, что цена доехала до витрин.

Запуск:  python3 bot.py
Конфиг:  ~/.cappi/api.env  (TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_IDS)
"""
import json, os, re, threading, time, traceback, urllib.parse, urllib.request
from difflib import SequenceMatcher
from datetime import date, datetime, timedelta

import cappi

CFG = cappi.cfg()
TOKEN = CFG.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED = {int(x) for x in re.findall(r"\d+", CFG.get("TELEGRAM_ALLOWED_IDS", ""))}
API = f"https://api.telegram.org/bot{TOKEN}"

STATE_DIR = os.path.expanduser("~/.cappi")
PENDING = os.path.join(STATE_DIR, "pending.json")   # отложенные проверки витрин
AUDIT = os.path.join(STATE_DIR, "changes.log")      # журнал изменений цен

CHECK_AFTER_MIN = 30        # через сколько проверять, доехала ли цена
MAX_CHANGE_PCT = 50         # скачок больше этого бот не проводит

_confirm = {}               # токен → подготовленное изменение
_await = {}                 # чат → чего ждём от следующего сообщения
_lock = threading.Lock()

# Постоянная клавиатура снизу — основные действия под рукой.
KB_MAIN = {
    "keyboard": [
        [{"text": "🔍 Найти позицию"}, {"text": "📊 Сверка витрин"}],
        [{"text": "⏳ На проверке"}, {"text": "❓ Помощь"}],
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
<code>/set 03275 46</code> — сменить цену
<code>/set 03275 46 завтра</code> — приказ на завтра
<code>/check</code> — сверка витрин
<code>/pending</code> — что стоит на проверке

Как это работает: бот создаёт в Syrve приказ об изменении прейскуранта.
Выгрузка на сайт идёт автоматически раз в 20 минут, Glovo подтягивается
позже. Через {CHECK_AFTER_MIN} минут бот сам проверит витрины и напишет,
доехала ли цена.

Скачок больше {MAX_CHANGE_PCT}% бот не проводит — защита от опечатки."""


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
        f"<code>{i['code']}</code> {i['name'][:30]} → {fmt(i['price'])} ₴  "
        f"проверю в {datetime.fromisoformat(i['due']):%H:%M}" for i in items))


# --------------------------------------------------------------- смена цены
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
    other = date.today() + timedelta(days=1) if when == date.today() else date.today()
    say(chat,
        f"<b>{p['name']}</b>\nартикул <code>{code}</code>\n\n"
        f"<b>{fmt(cur)} ₴  →  {fmt(new_price)} ₴</b>\n"
        f"с {when:%d.%m.%Y}"
        + ("  <i>(сейчас, включая открытые кассовые смены)</i>"
           if when == date.today() else "  <i>(ночью)</i>"),
        inline=[
            [{"text": "✅ Провести", "callback_data": f"go:{tok}"},
             {"text": "✖️ Отмена", "callback_data": f"no:{tok}"}],
            [{"text": f"📅 Перенести на {other:%d.%m}", "callback_data": f"dt:{tok}"}],
        ])


def do_change(c):
    with cappi.Syrve() as s:
        doc = s.set_price(c["pid"], c["dep"], c["price"], c["date"])
    audit(f'{c["user"]}\t{c["code"]}\t{c["name"]}\t{c["old"]} -> {c["price"]}\t'
          f'с {c["date"]}\tприказ №{doc["documentNumber"]}')
    items = load_pending()
    items.append({**{k: c[k] for k in ("code", "name", "price", "old", "chat")},
                  "guid": c["pid"],
                  "doc": doc["documentNumber"],
                  "due": (datetime.now() + timedelta(minutes=CHECK_AFTER_MIN)).isoformat()})
    save_pending(items)
    return doc


# ------------------------------------------------------- фоновая проверка
def watcher():
    """Через CHECK_AFTER_MIN минут смотрим, доехала ли цена до витрин."""
    while True:
        try:
            keep = []
            for it in load_pending():
                if datetime.now() < datetime.fromisoformat(it["due"]):
                    keep.append(it)
                    continue
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
                       "\n\n<i>Выгрузка идёт раз в 20 минут, Glovo подтягивается "
                       "позже — до часа. Проверь ещё раз кнопкой «Сверка витрин».</i>"))
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
            return prepare(chat, st["code"], price, date.today(), who)

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
    elif cmd == "/set":
        if len(args) < 2:
            return say(chat, "Формат: <code>/set &lt;артикул&gt; &lt;цена&gt; [завтра]</code>")
        try:
            price = float(args[1].replace(",", "."))
        except ValueError:
            return say(chat, f"Не понял цену: <b>{args[1]}</b>")
        when = date.today()
        if len(args) > 2 and args[2].lower() in ("завтра", "tomorrow"):
            when += timedelta(days=1)
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
