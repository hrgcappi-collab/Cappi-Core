#!/usr/bin/env python3
"""Телеграм-бот Cappi: смена цен в Syrve и контроль, что цена доехала до витрин.

Запуск:  python3 bot.py
Конфиг:  ~/.cappi/api.env  (TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_IDS)
"""
import json, os, re, threading, time, traceback, urllib.parse, urllib.request
from datetime import date, datetime, timedelta

import cappi

CFG = cappi.cfg()
TOKEN = CFG.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED = {int(x) for x in re.findall(r"\d+", CFG.get("TELEGRAM_ALLOWED_IDS", ""))}
API = f"https://api.telegram.org/bot{TOKEN}"

STATE_DIR = os.path.expanduser("~/.cappi")
PENDING = os.path.join(STATE_DIR, "pending.json")   # отложенные проверки
AUDIT = os.path.join(STATE_DIR, "changes.log")      # журнал изменений

CHECK_AFTER_MIN = 30        # через сколько проверять, доехала ли цена
MAX_CHANGE_PCT = 50         # скачок больше этого требует ключевого слова

_confirm = {}               # токен → подготовленное изменение
_lock = threading.Lock()


# ------------------------------------------------------------------ Telegram
def tg(method, **params):
    data = urllib.parse.urlencode(
        {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
         for k, v in params.items() if v is not None}).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=70) as r:
        return json.loads(r.read())


def say(chat, text, keyboard=None):
    return tg("sendMessage", chat_id=chat, text=text, parse_mode="HTML",
              reply_markup={"inline_keyboard": keyboard} if keyboard else None)


# -------------------------------------------------------------------- helpers
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


def find(query):
    """Ищем позицию по артикулу или куску названия. Цену берём из Cloud API."""
    q = query.strip().lower()
    out = []
    for code, p in cappi.cloud_prices().items():
        if q == str(code).lower() or q in p["name"].lower():
            out.append((code, p))
    return sorted(out, key=lambda x: x[1]["name"])


def where_shown(name):
    """Что показывают витрины. Медленно — только по явному запросу."""
    n = cappi.norm(name)
    site = glovo = None
    try:
        site = cappi.site_prices().get(n)
    except Exception:
        pass
    try:
        glovo = cappi.glovo_prices().get(n)
    except Exception:
        pass
    return site, glovo


# ------------------------------------------------------------------- команды
HELP = """<b>Бот цен Cappi</b>

<code>/price окрошка</code> — найти позицию и её цену
<code>/set 03275 46</code> — сменить цену (с подтверждением)
<code>/set 03275 46 завтра</code> — приказ на завтра
<code>/check</code> — сверить Syrve ↔ сайт ↔ Glovo
<code>/pending</code> — что стоит на проверке

После смены цены бот сам проверит через {n} минут, доехала ли она
до сайта и Glovo, и напишет результат.""".format(n=CHECK_AFTER_MIN)


def cmd_price(chat, args):
    if not args:
        return say(chat, "Что искать? Например: <code>/price окрошка</code>")
    hits = find(" ".join(args))
    if not hits:
        return say(chat, f"Не нашёл: <b>{' '.join(args)}</b>")
    if len(hits) > 12:
        return say(chat, f"Слишком много совпадений ({len(hits)}). Уточни запрос.")
    lines = []
    for code, p in hits:
        s = f"<code>{code}</code>  {p['name']}\n     <b>{p['price']:g} ₴</b>"
        if p["next"] is not None:
            s += f"  →  <b>{p['next']:g} ₴</b> с {(p['next_date'] or '')[:10]}"
        if not p["in_menu"]:
            s += "  <i>(не в меню)</i>"
        lines.append(s)
    say(chat, "\n".join(lines))


def cmd_set(chat, args, user):
    if len(args) < 2:
        return say(chat, "Формат: <code>/set &lt;артикул&gt; &lt;цена&gt; [завтра]</code>")
    code, raw = args[0], args[1]
    try:
        new_price = float(raw.replace(",", "."))
    except ValueError:
        return say(chat, f"Не понял цену: <b>{raw}</b>")
    when = date.today()
    if len(args) > 2 and args[2].lower() in ("завтра", "tomorrow"):
        when = when + timedelta(days=1)

    hits = [h for h in find(code) if str(h[0]).lower() == code.lower()]
    if not hits:
        return say(chat, f"Нет позиции с артикулом <code>{code}</code>. "
                         f"Найди через <code>/price</code>.")
    _, p = hits[0]
    old = p["price"]
    if abs(new_price - old) / max(old, 1) * 100 > MAX_CHANGE_PCT:
        return say(chat, f"⚠️ Скачок больше {MAX_CHANGE_PCT}%: "
                         f"<b>{old:g} → {new_price:g} ₴</b>.\nЭто точно не опечатка? "
                         f"Если да — меняй в Syrve руками, бот такие не проводит.")

    with cappi.Syrve() as s:
        prods = s.products()
        match = [x for x in prods if str(x.get("num")) == str(code)]
        if not match:
            return say(chat, f"Артикул <code>{code}</code> есть в меню, но нет в номенклатуре.")
        pid = match[0]["id"]
        cur, dep = s.price_of(pid, when.isoformat())
    if dep is None:
        return say(chat, f"У позиции нет действующей цены на {when:%d.%m} — "
                         f"приказ вслепую делать не буду.")

    tok = f"{int(time.time())}{chat % 1000}"
    with _lock:
        _confirm[tok] = {"pid": pid, "dep": dep, "price": new_price, "date": when.isoformat(),
                         "code": code, "name": p["name"], "old": cur, "chat": chat, "user": user}
    say(chat,
        f"<b>{p['name']}</b>\nартикул <code>{code}</code>\n\n"
        f"<b>{cur:g} ₴  →  {new_price:g} ₴</b>\n"
        f"с {when:%d.%m.%Y}"
        + ("  <i>(сегодня, включая открытые кассовые смены)</i>"
           if when == date.today() else "  <i>(ночью)</i>"),
        keyboard=[[{"text": "✅ Провести", "callback_data": f"go:{tok}"},
                   {"text": "✖️ Отмена", "callback_data": f"no:{tok}"}]])


def do_change(c):
    with cappi.Syrve() as s:
        doc = s.create_price_order(c["pid"], c["dep"], c["price"], c["date"])
    audit(f'{c["user"]}\t{c["code"]}\t{c["name"]}\t{c["old"]} -> {c["price"]}\t'
          f'с {c["date"]}\tприказ №{doc["documentNumber"]}')
    items = load_pending()
    items.append({**{k: c[k] for k in ("code", "name", "price", "old", "chat")},
                  "doc": doc["documentNumber"],
                  "due": (datetime.now() + timedelta(minutes=CHECK_AFTER_MIN)).isoformat()})
    save_pending(items)
    return doc


def cmd_check(chat):
    say(chat, "Сверяю Syrve ↔ сайт ↔ Glovo, это займёт минуту…")
    syr = {cappi.norm(v["name"]): (v["price"], k)
           for k, v in cappi.cloud_prices().items() if v["in_menu"]}
    site, glovo = cappi.site_prices(), cappi.glovo_prices()
    bad = []
    for n, (price, code) in sorted(syr.items()):
        s, g = site.get(n), glovo.get(n)
        if s is None and g is None:
            continue
        if (s is not None and s != price) or (g is not None and abs(g - price) > 0.01):
            bad.append(f"<code>{code}</code> {n[:34]}\n"
                       f"     Syrve <b>{price:g}</b> · сайт {s if s is not None else '—'}"
                       f" · Glovo {g if g is not None else '—'}")
    head = f"Syrve {len(syr)} · сайт {len(site)} · Glovo {len(glovo)}\n\n"
    say(chat, head + ("❌ <b>Расхождения</b>\n\n" + "\n".join(bad[:25])
                      if bad else "✅ Расхождений нет"))


def cmd_pending(chat):
    items = [i for i in load_pending() if i["chat"] == chat]
    if not items:
        return say(chat, "Ничего не стоит на проверке.")
    say(chat, "\n".join(
        f"<code>{i['code']}</code> {i['name'][:30]} → {i['price']:g} ₴  "
        f"проверю в {datetime.fromisoformat(i['due']):%H:%M}" for i in items))


# ------------------------------------------------------- фоновая проверка
def watcher():
    """Через CHECK_AFTER_MIN минут смотрим, доехала ли цена до витрин."""
    while True:
        try:
            items, keep = load_pending(), []
            for it in items:
                if datetime.now() < datetime.fromisoformat(it["due"]):
                    keep.append(it); continue
                site, glovo = where_shown(it["name"])
                p = it["price"]
                ok_site = site is not None and abs(site - p) < 0.01
                ok_glovo = glovo is not None and abs(glovo - p) < 0.01
                mark = lambda ok, val: ("✅" if ok else "❌") + f" {val if val is not None else 'нет'}"
                verdict = "✅ Цена доехала везде" if (ok_site and ok_glovo) else \
                          "⚠️ Цена доехала не везде"
                say(it["chat"],
                    f"{verdict}\n\n<b>{it['name']}</b>\n"
                    f"приказ №{it['doc']}, {it['old']:g} → {p:g} ₴\n\n"
                    f"сайт:  {mark(ok_site, site)}\n"
                    f"Glovo: {mark(ok_glovo, glovo)}"
                    + ("" if (ok_site and ok_glovo) else
                       "\n\n<i>Выгрузка идёт раз в 20 минут — если прошло меньше, "
                       "проверь ещё раз через /check.</i>"))
            save_pending(keep)
        except Exception:
            traceback.print_exc()
        time.sleep(60)


# ----------------------------------------------------------------- главный цикл
def handle(u):
    if "callback_query" in u:
        q = u["callback_query"]
        chat = q["message"]["chat"]["id"]
        if q["from"]["id"] not in ALLOWED:
            return
        act, _, tok = q["data"].partition(":")
        with _lock:
            c = _confirm.pop(tok, None)
        tg("answerCallbackQuery", callback_query_id=q["id"])
        if not c:
            return say(chat, "Запрос устарел, повтори <code>/set</code>.")
        if act == "no":
            return say(chat, "Отменено, ничего не менял.")
        try:
            doc = do_change(c)
            say(chat, f"✅ Приказ <b>№{doc['documentNumber']}</b> проведён\n"
                      f"{c['name']}: <b>{c['old']:g} → {c['price']:g} ₴</b> с "
                      f"{datetime.fromisoformat(c['date']):%d.%m}\n\n"
                      f"Проверю витрины через {CHECK_AFTER_MIN} минут.")
        except Exception as e:
            say(chat, f"❌ Не получилось: {e}")
        return

    m = u.get("message") or {}
    text, chat = m.get("text", ""), (m.get("chat") or {}).get("id")
    if not text or chat is None:
        return
    uid = m["from"]["id"]
    if uid not in ALLOWED:
        return say(chat, "Нет доступа.\n\nТвой id: <code>%d</code>\n"
                         "Впиши его в TELEGRAM_ALLOWED_IDS в ~/.cappi/api.env "
                         "и перезапусти бота." % uid)
    cmd, *args = text.split()
    cmd = cmd.lower().split("@")[0]
    who = m["from"].get("username") or str(uid)
    try:
        if cmd in ("/start", "/help"):  say(chat, HELP)
        elif cmd == "/price":           cmd_price(chat, args)
        elif cmd == "/set":             cmd_set(chat, args, who)
        elif cmd == "/check":           cmd_check(chat)
        elif cmd == "/pending":         cmd_pending(chat)
    except Exception as e:
        traceback.print_exc()
        say(chat, f"❌ Ошибка: {e}")


def main():
    if not TOKEN:
        raise SystemExit("Нет TELEGRAM_BOT_TOKEN в ~/.cappi/api.env")
    threading.Thread(target=watcher, daemon=True).start()
    print("бот запущен")
    offset = None
    while True:
        try:
            r = tg("getUpdates", offset=offset, timeout=50)
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                handle(u)
        except Exception:
            traceback.print_exc(); time.sleep(5)


if __name__ == "__main__":
    main()
