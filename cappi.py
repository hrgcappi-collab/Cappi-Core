#!/usr/bin/env python3
"""Общая библиотека Cappi: Syrve Server API, Cloud API, сайт, Glovo."""
import hashlib, json, os, re, urllib.parse, urllib.request
from html import unescape

ENV = os.path.expanduser("~/.cappi/api.env")
UA = {"User-Agent": "Mozilla/5.0"}

SITE_CATS = ["roli", "tempura-roli", "zapeceni-roli", "roli-bez-risu", "seti",
             "pica", "bouli", "supi", "salati", "zakuski", "deserti", "napoyi",
             "wok", "kombo", "onigiri", "susirito", "susi-burgeri", "bao-burgeri",
             "sprinh-rol-1", "promo", "aktsiya-misyatsya-1"]
GLOVO_URL = "https://glovoapp.com/uk/ua/odesa/stores/cappi-ods"


def cfg():
    d = {}
    for line in open(ENV):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1); d[k] = v
    return d


class _R308(urllib.request.HTTPRedirectHandler):
    def http_error_308(self, req, fp, code, msg, headers):
        return self.http_error_301(req, fp, 301, msg, headers)


_OPENER = urllib.request.build_opener(_R308)


def _get(url, headers=None, timeout=60):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with _OPENER.open(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _post(url, body, headers=None, timeout=60):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **UA, **(headers or {})})
    with _OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read())


# Меню украинское, ищут часто по-русски: «Филадельфия» vs «Філадельфія».
# Сводим оба алфавита к одному виду, иначе поиск и сопоставление витрин
# молча промахиваются на каждой второй позиции.
_ALPHA = str.maketrans({
    "і": "и", "ї": "и", "ы": "и",
    "є": "е", "э": "е", "ё": "е",
    "ґ": "г",
    "'": "", "'": "", "`": "", "ʼ": "", "ъ": "",
})


def norm(s):
    """Названия в Syrve, на сайте и в Glovo отличаются — приводим к общему виду."""
    s = unescape(s).lower().translate(_ALPHA)
    s = re.sub(r"^(напiй|напиток|напой)\s+", "", s)
    s = re.sub(r"[«»\".,()]", "", s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------- Syrve Server
class Syrve:
    """Сессия к Syrve Server API. Занимает слот лицензии — всегда закрывать."""

    def __init__(self, host=None, login=None, password=None):
        c = cfg()
        self.host = (host or c.get("SYRVE_TP_URL") or "https://cappi.syrve.online").rstrip("/")
        self.login = login or c.get("SYRVE_TP_LOGIN", "BragaD")
        self.password = password or c.get("SYRVE_TP_PASSWORD", "")
        self.key = None

    def __enter__(self):
        h = hashlib.sha1(self.password.encode()).hexdigest()
        q = urllib.parse.urlencode({"login": self.login, "pass": h})
        self.key = _get(f"{self.host}/resto/api/auth?{q}").strip()
        return self

    def __exit__(self, *a):
        try:
            _get(f"{self.host}/resto/api/logout?key={self.key}", timeout=20)
        except Exception:
            pass

    def get(self, path, **params):
        params["key"] = self.key
        return _get(f"{self.host}/resto/api/{path}?{urllib.parse.urlencode(params)}")

    def products(self):
        return json.loads(self.get("v2/entities/products/list"))

    def prices(self, date):
        d = json.loads(self.get("v2/price", dateFrom=date, dateTo=date))
        return d["response"]

    def price_of(self, product_id, date):
        """Действующая цена и подразделение. Карточке товара не верим — она устаревает."""
        for r in self.prices(date):
            if r["productId"] == product_id and r["prices"]:
                p = sorted(r["prices"], key=lambda x: x["dateFrom"])[-1]
                return p["price"], r["departmentId"]
        return None, None

    def orders(self, date_from, date_to):
        d = json.loads(self.get("v2/documents/menuChange",
                                dateFrom=date_from, dateTo=date_to))
        return d["response"]

    def create_price_order(self, product_id, department_id, price, date):
        """Создаёт приказ об изменении прейскуранта. Реальное изменение цены."""
        body = {
            "dateIncoming": date,
            "status": "PROCESSED",
            "deletePreviousMenu": False,
            "dateTo": "2500-01-01",
            "items": [{
                "departmentId": department_id,
                "productId": product_id,
                "productSizeId": None,
                "including": True,
                "price": price,
                "taxCategoryId": None,
                "taxCategoryEnabled": False,
                "dishOfDay": False,
                "flyerProgram": False,
                "pricesForCategories": [],
                "includeForCategories": [],
            }],
        }
        url = f"{self.host}/resto/api/v2/documents/menuChange?key={self.key}"
        r = _post(url, body)
        if r.get("result") != "SUCCESS":
            raise RuntimeError(f"Syrve отказал: {r.get('errors') or r}")
        return r["response"]


# ------------------------------------------------------------------ Cloud API
def cloud_menu():
    """Меню доставки с реальными ценами — то, что видят внешние системы."""
    c = cfg()
    tok = _post(f"{c['SYRVE_CLOUD_URL']}/api/1/access_token",
                {"apiLogin": c["SYRVE_CLOUD_API_KEY"]})["token"]
    return _post(f"{c['SYRVE_CLOUD_URL']}/api/1/nomenclature",
                 {"organizationId": c["SYRVE_ORG_ID"]},
                 {"Authorization": f"Bearer {tok}"}, timeout=90)["products"]


def cloud_prices():
    out = {}
    for p in cloud_menu():
        sp = (p.get("sizePrices") or [{}])[0].get("price") or {}
        if sp.get("currentPrice") is not None:
            out[p.get("code") or p["id"]] = {
                "name": p["name"],
                "price": sp["currentPrice"],
                "in_menu": sp.get("isIncludedInMenu"),
                "next": sp.get("nextPrice"),
                "next_date": sp.get("nextDatePrice"),
            }
    return out


# ------------------------------------------------------------- Сайт и Glovo
def site_prices():
    out = {}
    for cat in SITE_CATS:
        try:
            h = _get(f"https://cappi.ua/odesa/{cat}", timeout=40)
        except Exception:
            continue
        for m in re.finditer(
                r'goods__name">(.*?)</div>.*?goods-price__actual">\s*([\d\s]+)\s*₴', h, re.S):
            out[norm(m.group(1))] = int(re.sub(r"\D", "", m.group(2)))
    return out


def glovo_prices():
    h = _get(GLOVO_URL, timeout=60)
    out = {}
    for m in re.finditer(r'<img alt="(.*?)"[^>]*>(.*?)(\d[\d\s ]*,\d\d)\s*₴', h, re.S):
        if len(m.group(2)) < 3000:
            out[norm(m.group(1))] = float(
                re.sub(r"[\s ]", "", m.group(3)).replace(",", "."))
    return out
