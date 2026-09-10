#!/usr/bin/env python3
"""Цены Cappi из Syrve Cloud API. Только чтение."""
import json, os, sys, time, urllib.request

ENV = os.path.expanduser("~/.cappi/api.env")
cfg = {}
for line in open(ENV):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        cfg[k] = v

HOST = cfg["SYRVE_CLOUD_URL"]
ORG  = cfg["SYRVE_ORG_ID"]


def post(path, body, token=None):
    req = urllib.request.Request(
        f"{HOST}/api/1/{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {token}"} if token else {})},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def menu():
    tok = post("access_token", {"apiLogin": cfg["SYRVE_CLOUD_API_KEY"]})["token"]
    return post("nomenclature", {"organizationId": ORG}, tok)["products"]


def price_of(p):
    sp = (p.get("sizePrices") or [{}])[0].get("price") or {}
    return sp.get("currentPrice"), sp.get("nextPrice"), sp.get("nextDatePrice")


def show(p):
    cur, nxt, when = price_of(p)
    line = f"  {p.get('code') or '—':<8} {p['name']:<40} {cur:>8} грн"
    if nxt is not None:
        line += f"   ⏭  станет {nxt} грн с {(when or '')[:10]}"
    return line


def find(q):
    q = q.lower()
    return [p for p in menu()
            if q in (p.get("name") or "").lower() or q == (p.get("code") or "")]


def watch(q, every=60):
    """Ждём, пока у позиции появится запланированная смена цены."""
    hits = find(q)
    if not hits:
        sys.exit(f"не нашёл: {q}")
    base = {p["id"]: price_of(p) for p in hits}
    print("Слежу за:")
    for p in hits:
        print(show(p))
    print(f"\nОпрос каждые {every} с. Ctrl+C — стоп.\n")
    while True:
        time.sleep(every)
        for p in find(q):
            now = price_of(p)
            if now != base.get(p["id"]):
                print(f"\n🔔 ИЗМЕНЕНИЕ  {time.strftime('%H:%M:%S')}")
                print(f"  было:  {base.get(p['id'])}")
                print(f"  стало: {now}")
                print(show(p))
                base[p["id"]] = now
            else:
                print(f"  {time.strftime('%H:%M:%S')}  без изменений", end="\r")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("использование:\n  price.py <поиск>\n  price.py watch <поиск> [секунды]")
    if sys.argv[1] == "watch":
        watch(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 60)
    else:
        found = find(" ".join(sys.argv[1:]))
        print(f"найдено: {len(found)}")
        for p in sorted(found, key=lambda x: x["name"]):
            print(show(p))
