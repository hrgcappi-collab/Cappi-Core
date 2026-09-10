#!/usr/bin/env python3
"""Телеграм-бот Cappi: смена цен в Syrve и контроль, что цена доехала до витрин.

Запуск:  python3 bot.py
Конфиг:  ~/.cappi/api.env  (TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_IDS)
"""
import json, os, re, sys, threading, time, traceback, urllib.parse, urllib.request
from difflib import SequenceMatcher
from datetime import date, datetime, timedelta

import cappi
import report
import webhook

HERE = os.path.dirname(os.path.abspath(__file__))
START = time.time()

CFG = cappi.cfg()
TOKEN = CFG.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED = {int(x) for x in re.findall(r"\d+", CFG.get("TELEGRAM_ALLOWED_IDS", ""))}
API = f"https://api.telegram.org/bot{TOKEN}"

STATE_DIR = os.path.expanduser("~/.cappi")
PENDING = os.path.join(STATE_DIR, "pending.json")   # отложенные проверки витрин
AUDIT = os.path.join(STATE_DIR, "changes.log")      # журнал изменений цен
UNKNOWN = os.path.join(STATE_DIR, "unknown.log")    # что бот не понял — на ревизию

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

# Меню трёхуровневое: главный экран → модуль → действия. Плоский список из
# семи кнопок читался как свалка; здесь каждый блок живёт отдельно, и добавить
# в него кнопку можно, не трогая остальные.
МЕНЮ = {
    "главное": [["💰 Цены", "📈 Показатели"],
                ["⚙️ Админка"]],
    "цены": [["🔍 Найти позицию"],
             ["📊 Сверка витрин", "⏳ На проверке"],
             ["◀️ Назад"]],
    "показатели": [["📈 Сейчас", "📅 За вчера"],
                   ["🎯 План", "🚧 Зоны"],
                   ["😠 Жалобы"],
                   ["◀️ Назад"]],
    "админка": [["🔌 Проверка связи", "📜 Журнал цен"],
                ["🗣 Непонятые", "👥 Доступ"],
                ["🤖 Состояние бота"],
                ["◀️ Назад"]],
}

_menu = {}          # чат → в каком модуле он сейчас


def keyboard(chat):
    return {"keyboard": [[{"text": b} for b in row]
                         for row in МЕНЮ[_menu.get(chat, "главное")]],
            "resize_keyboard": True}


# ------------------------------------------------------------------ Telegram
def tg(method, **params):
    data = urllib.parse.urlencode(
        {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
         for k, v in params.items() if v is not None}).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=70) as r:
        return json.loads(r.read())


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
    строки = ["<b>Доступ к боту</b>", ""]
    for uid in sorted(ALLOWED):
        строки.append(f"  <code>{uid}</code>" + ("  ← ты" if uid == chat else ""))
    строки += ["", "<i>Список правится в TELEGRAM_ALLOWED_IDS "
                   "(~/.cappi/api.env), затем перезапуск бота.</i>"]
    say(chat, "\n".join(строки))


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


def _зона_изменилась(e):
    """Пришло событие от Джамшута — сообщаем сразу, не дожидаясь отчёта.
    Закрытая зона это деньги, которые не заработаются, пока она закрыта."""
    закрытие = e.get("event") == "zone.close"
    район = e.get("district") or e.get("branch") or "—"
    ctx = e.get("context") or {}
    штат = ctx.get("staff") or {}
    строки = [("🚧 <b>Зона закрыта</b>" if закрытие else "✅ <b>Зона открыта</b>"),
              f"{район} · {e.get('branch','—')}"]
    if закрытие and e.get("duration_min"):
        строки.append(f"на {e['duration_min']} мин")
    кто = "автоматически" if e.get("auto") else (e.get("actor") or "—")
    строки.append(f"кто: {кто}")
    if ctx:
        строки.append("")
        строки.append(f"в работе {ctx.get('in_work','?')} · "
                      f"кухня {ctx.get('kitchen','?')} · в пути {ctx.get('onway','?')}")
        if штат:
            строки.append(f"на смене: поваров {штат.get('cooks','?')}, "
                          f"курьеров {штат.get('couriers','?')}")
    for uid in ALLOWED:
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
    """Догоняет отложенные проверки, опрашивает Джамшута, шлёт итоги дня."""
    sent = {}
    последний_опрос = [0.0]
    while True:
        try:
            _send_daily(sent)
            _pull_zones(последний_опрос)
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
    (r"^(за )?(\d{1,2})\.(\d{1,2})(\.(\d{4}))?$",
     lambda chat, m, txt: cmd_report(
         chat, date(int(m.group(5) or date.today().year),
                    int(m.group(3)), int(m.group(2))), live=False)),
    (r"^(план|сколько нужно|сколько надо)", lambda chat, m, txt: cmd_plan(chat, "")),
    (r"(в работе|сейчас готов|активные заказ|что готовится)",
     lambda chat, m, txt: cmd_live(chat)),
    (r"^(отмен|сколько отмен|причины отмен)", lambda chat, m, txt: cmd_cancels(chat)),

    # цены
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
    (r"^(помощ|что умеешь|команды|help)", lambda chat, m, txt: say(chat, HELP)),

    # то, чего ещё нет — честно говорим, а не молчим
    (r"^(жалоб|негатив|отзыв)", lambda chat, m, txt: cmd_complaints(chat)),
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
        строки += ["", "🗑 <b>Удаления блюд</b>"]
        строки += [f"    {п} — {n}" for п, n in sorted(уд.items(), key=lambda x: -x[1])]
    say(chat, "\n".join(строки))


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


BUTTONS = {
    # главное меню — вход в модули
    "💰 цены": lambda chat: открыть(chat, "цены",
        "<b>Цены</b>\nПоиск позиций, смена цены, сверка витрин."),
    "📈 показатели": lambda chat: открыть(chat, "показатели",
        "<b>Показатели</b>\nВыручка против плана, заказы, отмены, живые заказы."),
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
    "🎯 план": lambda chat: cmd_plan(chat, ""),
    "🚧 зоны": cmd_zones,
    "😠 жалобы": cmd_complaints,

    # модуль «Админка»
    "🔌 проверка связи": cmd_healthcheck,
    "📜 журнал цен": cmd_audit,
    "👥 доступ": cmd_access,
    "🗣 непонятые": cmd_unknown,
    "🤖 состояние бота": cmd_botstate,
}


def on_message(m):
    text, chat = m.get("text", ""), (m.get("chat") or {}).get("id")
    if not text or chat is None:
        return
    uid = m["from"]["id"]
    if uid not in ALLOWED:
        return say(chat, "Нет доступа.\n\nТвой id: <code>%d</code>\n"
                         "Впиши его в TELEGRAM_ALLOWED_IDS в ~/.cappi/api.env "
                         "и перезапусти бота." % uid, keys=False)
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
    if not ALLOWED:
        print("⚠ TELEGRAM_ALLOWED_IDS пуст — бот никого не пустит")
    порт = webhook.serve()
    if порт:
        webhook.СЛУШАТЕЛИ.append(_зона_изменилась)
        print(f"приёмник событий Джамшута на порту {порт}")
    else:
        print("CORE_WEBHOOK_TOKEN не задан — события зон не принимаются")
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
