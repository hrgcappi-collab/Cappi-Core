#!/usr/bin/env python3
"""Стоп-лист: посмотреть, поставить, снять.

Позиция в стопе — это деньги, которые не заработаются, пока она там висит.
Три бао-бургера по 339 ₴ в стопе — заметная дыра в меню, и без этого экрана
о ней никто не узнаёт до конца смены.

Работает через Syrve Cloud API: /stop_lists, /stop_lists/add, /stop_lists/remove.
Стоп задаётся не для заведения целиком, а для терминальной группы — то есть
для точки. Поэтому у каждой позиции здесь всегда есть терминал, а «снять со
стопа» снимает ровно там, где стояло.
"""
import json
import os
from datetime import datetime

import cappi

СЛЕПОК = os.path.expanduser("~/.cappi/stoplist.json")   # чтобы замечать новое


def _token():
    c = cappi.cfg()
    r = cappi.форма(cappi._post(f"{c['SYRVE_CLOUD_URL']}/api/1/access_token",
                                {"apiLogin": c["SYRVE_CLOUD_API_KEY"]}, timeout=40),
                    dict, "Syrve Cloud")
    if not r.get("token"):
        raise cappi.ВнешнийСбой("Syrve Cloud", "не выдал токен")
    return r["token"]


def _post(path, body):
    c = cappi.cfg()
    return cappi._post(f"{c['SYRVE_CLOUD_URL']}/api/1/{path}", body,
                       {"Authorization": f"Bearer {_token()}"}, timeout=60)


def терминалы():
    """id → название точки."""
    c = cappi.cfg()
    d = cappi.форма(_post("terminal_groups", {"organizationIds": [c["SYRVE_ORG_ID"]]}),
                    dict, "Syrve Cloud")
    return {it["id"]: it.get("name", "?")
            for g in (d.get("terminalGroups") or []) if isinstance(g, dict)
            for it in (g.get("items") or []) if isinstance(it, dict) and "id" in it}


def список():
    """Что сейчас в стопе: позиция, цена, точка."""
    c = cappi.cfg()
    d = cappi.форма(_post("stop_lists", {"organizationIds": [c["SYRVE_ORG_ID"]]}),
                    dict, "Syrve Cloud")
    меню = {p["id"]: p for p in cappi.cloud_menu() if isinstance(p, dict) and "id" in p}
    точки = терминалы()
    out = []
    for группа in (d.get("terminalGroupStopLists") or []):
        if not isinstance(группа, dict):
            continue
        for терминал in (группа.get("items") or []):
            if not isinstance(терминал, dict):
                continue
            tid = терминал.get("terminalGroupId")
            for it in (терминал.get("items") or []):
                if not isinstance(it, dict):
                    continue
                pid = it.get("productId")
                p = меню.get(pid) or {}
                цена = ((p.get("sizePrices") or [{}])[0].get("price") or {}).get("currentPrice")
                out.append({
                    "productId": pid,
                    "название": p.get("name") or "неизвестная позиция",
                    "код": p.get("code"),
                    "цена": цена,
                    "terminalGroupId": tid,
                    "точка": точки.get(tid, "?"),
                    "остаток": it.get("balance"),
                })
    return sorted(out, key=lambda x: (-(x["цена"] or 0), x["название"]))


def поставить(product_id, terminal_group_id, остаток=0):
    """Ставит позицию в стоп на конкретной точке. Реальная продажа прекратится."""
    c = cappi.cfg()
    return _post("stop_lists/add", {
        "organizationId": c["SYRVE_ORG_ID"],
        "terminalGroupId": terminal_group_id,
        "items": [{"productId": product_id, "balance": остаток}],
    })


def снять(product_id, terminal_group_id):
    """Снимает позицию со стопа — она снова продаётся."""
    c = cappi.cfg()
    return _post("stop_lists/remove", {
        "organizationId": c["SYRVE_ORG_ID"],
        "terminalGroupId": terminal_group_id,
        "items": [{"productId": product_id}],
    })


# ------------------------------------------------------- что изменилось
def _ключ(п):
    return f"{п['productId']}@{п['terminalGroupId']}"


def изменения():
    """Что появилось в стопе и что ушло с прошлой проверки.

    Слепок нужен именно потому, что API отдаёт только «как сейчас».
    Без него момент попадания в стоп проходит незамеченным — а он и есть
    то, о чём стоит сказать сразу.
    """
    сейчас = список()
    было = {}
    try:
        было = json.load(open(СЛЕПОК)).get("позиции", {})
    except Exception:
        pass
    стало = {_ключ(п): п for п in сейчас}
    новые = [п for k, п in стало.items() if k not in было]
    ушли = [п for k, п in было.items() if k not in стало]
    json.dump({"позиции": стало, "когда": datetime.now().isoformat()},
              open(СЛЕПОК, "w"), ensure_ascii=False)
    # Первый запуск: слепка не было, и весь текущий стоп — не новость.
    первый = not было
    return {"новые": [] if первый else новые, "ушли": [] if первый else ушли,
            "всего": сейчас}


if __name__ == "__main__":
    for п in список():
        print(f"  {п['код'] or '—':<8} {п['название'][:42]:<42} "
              f"{п['цена'] or '—':>6} ₴  {п['точка']}")
