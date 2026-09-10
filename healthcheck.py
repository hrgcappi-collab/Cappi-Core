#!/usr/bin/env python3
"""Проверка всех доступов Cappi: что работает, что отвалилось.

Запуск:  python3 healthcheck.py
Только чтение — ничего не меняет.
"""
import hashlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from datetime import date

import cappi
import report as report_mod

CFG = cappi.cfg()
OK, FAIL, SKIP = "✅", "❌", "⏭"
results = []


def check(name, fn):
    """Прогоняем одну проверку, ловим всё, меряем время."""
    t = time.time()
    try:
        detail = fn()
        results.append((OK, name, detail, time.time() - t))
    except _Skip as e:
        results.append((SKIP, name, str(e), 0))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:120].replace("\n", " ")
        results.append((FAIL, name, f"HTTP {e.code} · {body}", time.time() - t))
    except Exception as e:
        results.append((FAIL, name, f"{type(e).__name__}: {e}"[:150], time.time() - t))


class _Skip(Exception):
    pass


def need(*keys):
    for k in keys:
        if not CFG.get(k):
            raise _Skip(f"не заполнено {k} в ~/.cappi/api.env")


# ----------------------------------------------------------- Syrve Server API
def _server(url_key, login_key, pass_key, label):
    need(url_key, login_key, pass_key)
    host = CFG[url_key].rstrip("/")
    h = hashlib.sha1(CFG[pass_key].encode()).hexdigest()
    q = urllib.parse.urlencode({"login": CFG[login_key], "pass": h})
    key = cappi._get(f"{host}/resto/api/auth?{q}", timeout=40).strip()
    try:
        info = cappi._get(f"{host}/resto/get_server_info.jsp?encoding=UTF-8", timeout=30)
        name = _tag(info, "serverName")
        ver = _tag(info, "version")
        ed = _tag(info, "edition")
        prods = json.loads(cappi._get(
            f"{host}/resto/api/v2/entities/products/list?key={key}", timeout=90))
        return f"{name} · {ed} · {ver} · номенклатура {len(prods)} позиций"
    finally:
        try:
            cappi._get(f"{host}/resto/api/logout?key={key}", timeout=20)
        except Exception:
            pass


def _tag(xml, tag):
    import re
    m = re.search(rf"<{tag}>(.*?)</{tag}>", xml)
    return m.group(1) if m else "?"


def syrve_tp():
    return _server("SYRVE_TP_URL", "SYRVE_TP_LOGIN", "SYRVE_TP_PASSWORD", "ТП")


def syrve_hq():
    return _server("SYRVE_SERVER_URL", "SYRVE_API_LOGIN", "SYRVE_API_PASSWORD", "HQ")


def syrve_orders():
    """Приказы о ценах — то, чем бот меняет цены."""
    need("SYRVE_TP_URL", "SYRVE_TP_LOGIN", "SYRVE_TP_PASSWORD")
    from datetime import date, timedelta
    with cappi.Syrve() as s:
        since = (date.today() - timedelta(days=30)).isoformat()
        docs = s.orders(since, date.today().isoformat())
        # номера повторяются по годам, поэтому сортируем по дате И номеру
        last = max(docs, key=lambda x: (x["dateIncoming"], x["documentNumber"])) if docs else None
        tail = f" · последний №{last['documentNumber']} от {last['dateIncoming']}" if last else ""
        return f"приказов за 30 дней: {len(docs)}{tail}"


# ----------------------------------------------------------------- Cloud API
def cloud():
    need("SYRVE_CLOUD_URL", "SYRVE_CLOUD_API_KEY", "SYRVE_ORG_ID")
    tok = cappi._post(f"{CFG['SYRVE_CLOUD_URL']}/api/1/access_token",
                      {"apiLogin": CFG["SYRVE_CLOUD_API_KEY"]}, timeout=40)["token"]
    orgs = cappi._post(f"{CFG['SYRVE_CLOUD_URL']}/api/1/organizations", {},
                       {"Authorization": f"Bearer {tok}"}, timeout=40)["organizations"]
    org = next((o for o in orgs if o["id"] == CFG["SYRVE_ORG_ID"]), orgs[0] if orgs else None)
    prods = cappi.cloud_prices()
    in_menu = sum(1 for p in prods.values() if p["in_menu"])
    planned = sum(1 for p in prods.values() if p["next"] is not None)
    tail = f" · запланированных смен цен: {planned}" if planned else ""
    return f"{org['name'] if org else '?'} · позиций {len(prods)}, в меню {in_menu}{tail}"


def stop_lists():
    need("SYRVE_CLOUD_URL", "SYRVE_CLOUD_API_KEY", "SYRVE_ORG_ID")
    import stoplist
    п = stoplist.список()
    потери = sum(x["цена"] or 0 for x in п)
    return (f"в стопе {len(п)} позиций на {потери:,.0f} ₴ по прайсу"
            .replace(",", " ") if п else "стоп-лист пуст")


# ------------------------------------------------------------- Витрины
def site():
    p = cappi.site_prices()
    if not p:
        raise RuntimeError("страницы открылись, но цены не распознались — "
                           "возможно, поменялась вёрстка сайта")
    disc = sum(1 for v in p.values() if v["cross"])
    return f"cappi.ua · позиций {len(p)}" + (f", со скидкой {disc}" if disc else "")


def glovo():
    p = cappi.glovo_prices()
    if not p:
        raise RuntimeError("страница открылась, но цены не распознались — "
                           "возможно, поменялась вёрстка Glovo")
    return f"Glovo · разобрано позиций: {len(p)}"


def loopa():
    need("LOOPA_URL", "LOOPA_TOKEN")
    from datetime import timedelta
    ж = report_mod.complaints(date.today())
    неделя = report_mod._loopa(**{"from": (date.today() - timedelta(days=6)).isoformat(),
                                  "to": date.today().isoformat(), "tone": "negative"})
    return (f"жалоб сегодня: {ж['жалоб']} из {ж['отзывов']} отзывов · "
            f"за неделю: {неделя.get('total', 0)}")


def jamshut():
    need("JAMSHUT_URL", "JAMSHUT_TOKEN")
    import webhook
    st = webhook.state()
    закрыто = st.get("count", 0)
    события = len(webhook.events(None))
    хост = CFG["JAMSHUT_URL"].split("//")[-1].split("/")[0]
    return (f"{хост} · зон закрыто сейчас: {закрыто} · "
            f"событий в истории: {события}")


def cappi_admin():
    need("CAPPI_ADMIN_URL")
    if not CFG.get("CAPPI_ADMIN_TOKEN"):
        raise _Skip("нет CAPPI_ADMIN_TOKEN — синхронизация сайта не автоматизирована")
    raise _Skip("эндпоинт синхронизации не известен, ждём ответа разработчика")


# ------------------------------------------------------------- Telegram
def telegram():
    need("TELEGRAM_BOT_TOKEN")
    d = json.loads(cappi._get(
        f"https://api.telegram.org/bot{CFG['TELEGRAM_BOT_TOKEN']}/getMe", timeout=30))
    if not d.get("ok"):
        raise RuntimeError(d.get("description", "не авторизован"))
    ids = CFG.get("TELEGRAM_ALLOWED_IDS", "").strip()
    who = f"доступ у {len(ids.split(','))} чел." if ids else "⚠ список доступа ПУСТ"
    return f"@{d['result']['username']} · {who}"


# ------------------------------------------------------------- Сводка цен
def consistency():
    """Главная проверка: сходятся ли цены в Syrve, на сайте и в Glovo."""
    syr = [v for v in cappi.cloud_prices().values() if v["in_menu"]]
    s, g = cappi.site_prices(), cappi.glovo_prices()
    bad = []
    for v in syr:
        row = s.get(v["id"])
        gl = g.get(cappi.norm(v["name"]))
        if (row and row["price"] != v["price"]) or (gl is not None and abs(gl - v["price"]) > 0.01):
            bad.append(v["name"])
    if bad:
        raise RuntimeError(f"расходятся {len(bad)} позиций — подробности: "
                           f"python3 check_prices.py")
    return "Syrve, сайт и Glovo сходятся"


CHECKS = [
    ("Syrve ТП (приказы, номенклатура)", syrve_tp),
    ("Syrve ТП — приказы о ценах", syrve_orders),
    ("Syrve HQ (chain)", syrve_hq),
    ("Syrve Cloud API", cloud),
    ("Syrve Cloud — стоп-листы", stop_lists),
    ("Сайт cappi.ua", site),
    ("Glovo", glovo),
    ("Loopa (жалобы)", loopa),
    ("Джамшут (зоны)", jamshut),
    ("Cappi Admin", cappi_admin),
    ("Telegram-бот", telegram),
    ("Сходимость цен", consistency),
]


def main():
    only = sys.argv[1].lower() if len(sys.argv) > 1 else None
    print("Проверяю доступы Cappi…\n")
    for name, fn in CHECKS:
        if only and only not in name.lower():
            continue
        # прогресс в stderr: в терминале видно, в лог/пайп не попадёт
        print(f"  … {name}", end="\r", file=sys.stderr, flush=True)
        check(name, fn)
        mark, n, detail, dt = results[-1]
        print(f"  {mark} {n:<34} {detail}"
              + (f"  ({dt:.1f}с)" if dt > 2 else ""))

    ok = sum(1 for r in results if r[0] == OK)
    bad = sum(1 for r in results if r[0] == FAIL)
    skip = sum(1 for r in results if r[0] == SKIP)
    print(f"\n  работает: {ok}   не работает: {bad}   пропущено: {skip}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
