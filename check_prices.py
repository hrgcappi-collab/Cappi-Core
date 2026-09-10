#!/usr/bin/env python3
"""Сверка цен Cappi: Syrve ↔ cappi.ua ↔ Glovo. Только чтение."""
import json, os, re, sys, urllib.request
from html import unescape

ENV = os.path.expanduser("~/.cappi/api.env")
cfg = {}
for line in open(ENV):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1); cfg[k] = v

SITE_CATS = ["roli", "tempura-roli", "zapeceni-roli", "roli-bez-risu", "seti",
             "pica", "bouli", "supi", "salati", "zakuski", "deserti", "napoyi",
             "wok", "kombo", "onigiri", "susirito", "susi-burgeri", "bao-burgeri",
             "sprinh-rol-1", "promo", "aktsiya-misyatsya-1"]
GLOVO_URL = "https://glovoapp.com/uk/ua/odesa/stores/cappi-ods"
UA = {"User-Agent": "Mozilla/5.0"}


class _R308(urllib.request.HTTPRedirectHandler):
    def http_error_308(self, req, fp, code, msg, headers):
        return self.http_error_301(req, fp, 301, msg, headers)


_OPENER = urllib.request.build_opener(_R308)


def get(url, headers=None, timeout=45):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with _OPENER.open(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def norm(s):
    """Приводим названия к сравнимому виду — они везде чуть разные."""
    s = unescape(s).lower()
    s = re.sub(r"^(напій|напиток|напой)\s+", "", s)
    s = re.sub(r"[«»\"'`.,()]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def syrve_prices():
    tok = json.loads(urllib.request.urlopen(urllib.request.Request(
        f"{cfg['SYRVE_CLOUD_URL']}/api/1/access_token",
        data=json.dumps({"apiLogin": cfg["SYRVE_CLOUD_API_KEY"]}).encode(),
        headers={"Content-Type": "application/json"}), timeout=45).read())["token"]
    req = urllib.request.Request(
        f"{cfg['SYRVE_CLOUD_URL']}/api/1/nomenclature",
        data=json.dumps({"organizationId": cfg["SYRVE_ORG_ID"]}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {tok}"})
    prods = json.loads(urllib.request.urlopen(req, timeout=90).read())["products"]
    out = {}
    for p in prods:
        sp = (p.get("sizePrices") or [{}])[0].get("price") or {}
        if sp.get("currentPrice") is not None and sp.get("isIncludedInMenu"):
            out[norm(p["name"])] = (sp["currentPrice"], p.get("code") or "—")
    return out


def site_prices():
    out = {}
    for cat in SITE_CATS:
        try:
            h = get(f"https://cappi.ua/odesa/{cat}")
        except Exception:
            continue
        for m in re.finditer(
            r'goods__name">(.*?)</div>.*?goods-price__actual">\s*([\d\s]+)\s*₴', h, re.S):
            out[norm(m.group(1))] = int(re.sub(r"\D", "", m.group(2)))
    return out


def glovo_prices():
    h = get(GLOVO_URL)
    out = {}
    for m in re.finditer(r'<img alt="(.*?)"[^>]*>(.*?)(\d[\d\s]*,\d\d)\s*₴', h, re.S):
        name, price = m.group(1), m.group(3)
        if len(m.group(2)) < 3000:
            out[norm(name)] = float(re.sub(r"[\s\u00a0]", "", price).replace(",", "."))
    return out


def main():
    print("читаю Syrve…", flush=True);  syr = syrve_prices()
    print("читаю cappi.ua…", flush=True); sit = site_prices()
    print("читаю Glovo…", flush=True);  glv = glovo_prices()
    print(f"\nSyrve: {len(syr)}   сайт: {len(sit)}   Glovo: {len(glv)}\n")

    rows, missing = [], []
    for name, (price, code) in sorted(syr.items()):
        s, g = sit.get(name), glv.get(name)
        if s is None and g is None:
            missing.append((code, name)); continue
        bad = (s is not None and s != price) or (g is not None and abs(g - price) > 0.01)
        if bad:
            rows.append((code, name, price, s, g))

    if rows:
        print(f"❌ РАСХОЖДЕНИЯ — {len(rows)}\n")
        print(f"  {'арт':<8} {'позиция':<44} {'Syrve':>7} {'сайт':>7} {'Glovo':>8}")
        for code, name, p, s, g in rows:
            print(f"  {code:<8} {name[:44]:<44} {p:>7} "
                  f"{(s if s is not None else '—'):>7} {(g if g is not None else '—'):>8}")
    else:
        print("✅ Расхождений нет — все три источника сходятся")

    if missing and "-v" in sys.argv:
        print(f"\nнет ни на сайте, ни в Glovo ({len(missing)}):")
        for code, n in missing[:40]:
            print(f"  {code:<8} {n}")
    elif missing:
        print(f"\n({len(missing)} позиций Syrve не найдено ни на сайте, ни в Glovo — покажу с ключом -v)")


if __name__ == "__main__":
    main()
