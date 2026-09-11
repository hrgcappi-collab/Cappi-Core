#!/usr/bin/env python3
"""Общая библиотека Cappi: Syrve Server API, Cloud API, сайт, Glovo."""
import hashlib, json, os, re, time, urllib.error, urllib.parse, urllib.request
from html import unescape

ENV = os.path.expanduser("~/.cappi/api.env")
UA = {"User-Agent": "Mozilla/5.0"}

# Сайт отдаёт всё меню одним JSON. Ключевое — поле api_guid: это ровно
# productId из Syrve, так что сверять можно точно, а не угадывать по названиям.
SITE_MENU_URL = "https://api.cappi.ua/api/v2/categories/{city}"
SITE_CITY = "1"                       # Одесса
GLOVO_URL = "https://glovoapp.com/uk/ua/odesa/stores/cappi-ods"


def cfg():
    d = {}
    for line in open(ENV):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1); d[k] = v
    return d


class _R308(urllib.request.HTTPRedirectHandler):
    """308 сохраняет метод и тело — в отличие от 301.

    Обработчик 301 у POST меняет метод на GET и выбрасывает тело. Для
    приказа о цене это значило бы, что запись молча превращается в чтение:
    бот отчитается об успехе, а цена в кассу не уйдёт.
    """

    def http_error_308(self, req, fp, code, msg, headers):
        новый = headers.get("Location")
        if not новый:
            return None
        req = urllib.request.Request(
            urllib.parse.urljoin(req.full_url, новый), data=req.data,
            headers=dict(req.header_items()), method=req.get_method())
        return self.parent.open(req, timeout=req.timeout)


_OPENER = urllib.request.build_opener(_R308)


# Сколько раз переждать 429. Syrve ограничивает частоту запросов, и под
# нагрузкой — сводный отчёт, просчёт себестоимости, фоновые сторожа —
# упереться в лимит нормально. Ненормально показывать человеку «ошибка
# 429» вместо цифры, которую достаточно было подождать секунду.
ПОВТОРОВ_ПРИ_429 = 3


def _подождать(попытка):
    time.sleep(min(2 ** попытка, 8))


def _get(url, headers=None, timeout=60):
    for попытка in range(ПОВТОРОВ_ПРИ_429 + 1):
        req = urllib.request.Request(url, headers={**UA, **(headers or {})})
        try:
            with _OPENER.open(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code != 429 or попытка == ПОВТОРОВ_ПРИ_429:
                raise
            _подождать(попытка)


def _post(url, body, headers=None, timeout=60, попытка=0):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **UA, **(headers or {})})
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            тело = r.read()
        if not тело.strip():
            return {}
        try:
            return json.loads(тело)
        except ValueError:
            # Шлюз вернул HTML или мусор с кодом 2xx. Внятная ошибка лучше
            # JSONDecodeError, по которому не понять, что произошло.
            raise RuntimeError(
                f"ответ не JSON: {тело[:150].decode('utf-8', 'replace')}") from None
    except urllib.error.HTTPError as e:
        if e.code == 429 and попытка < ПОВТОРОВ_ПРИ_429:
            _подождать(попытка)
            return _post(url, body, headers, timeout, попытка + 1)
        # Syrve объясняет отказ в теле ответа. Без него остаётся голое
        # «409 Conflict», по которому невозможно понять, что не так.
        detail = e.read().decode("utf-8", "replace")[:300].strip()
        raise RuntimeError(f"HTTP {e.code}: {detail or e.reason}") from None


# Меню украинское, ищут часто по-русски: «Филадельфия» vs «Філадельфія».
# Сводим оба алфавита к одному виду, иначе поиск и сопоставление витрин
# молча промахиваются на каждой второй позиции.
_ALPHA = str.maketrans({
    "і": "и", "ї": "и", "ы": "и",
    "є": "е", "э": "е", "ё": "е",
    "ґ": "г",
    "'": "", "\u2019": "", "\u02bc": "", "\u2018": "", "`": "", "ъ": "",
})


def norm(s):
    """Общий вид названия. Приставку «напій» срезаем: в Syrve её нет, на сайте
    и в Glovo бывает, и без этого позиция не сопоставляется."""
    return re.sub(r"^(напий|напиток|напой)\s+", "", norm_full(s))


def norm_full(s):
    """То же, но без срезания приставки — для поиска, где она может быть запросом."""
    s = unescape(s).lower().translate(_ALPHA)
    s = re.sub(r"[«»\".,()]", "", s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------- Syrve Server
class ТехкартаЗакрыта(RuntimeError):
    """Syrve не даёт менять техкарты через API на этой установке."""

    def __init__(self):
        super().__init__("Syrve не принимает техкарты через API "
                         "(ASSEMBLY_CHART_IS_NOT_EDITABLE)")


class ЧужойОтдел(RuntimeError):
    """Попытка тронуть подразделение, которое мы не ведём."""


def отдел():
    """Подразделение, с которым работает бот.

    В базе их два: «Cappi Одесса» — единственное, которое что-либо
    продаёт (155 980 заказов с 2025 года), и «Cappi Днепр» — пустая
    запись справочника с нулём продаж за всю историю. Днепр не наш: мы
    его не считаем, не показываем и, главное, в него не пишем.
    """
    из_конфига = cfg().get("SYRVE_DEPARTMENT")
    if not из_конфига:
        raise ЧужойОтдел(
            "не задан SYRVE_DEPARTMENT — без него приказ уйдёт в то "
            "подразделение, которое попадётся первым")
    return из_конфига


_ИМЯ_ОТДЕЛА = {}


def отдел_имя():
    """Название нашего подразделения. В отчёте TRANSACTIONS подразделение
    фильтруется по имени: поля с id там просто нет."""
    ид = отдел()
    if ид not in _ИМЯ_ОТДЕЛА:
        with Syrve() as s:
            _ИМЯ_ОТДЕЛА.update(s.departments())
    return _ИМЯ_ОТДЕЛА.get(ид)


class PriceOrderExists(RuntimeError):
    """На эту дату по этой позиции приказ уже есть — второй Syrve не примет."""

    def __init__(self, number, date, позиции=None):
        self.number, self.date = number, date
        self.позиции = позиции or []   # для пачки: какие именно строки заняты
        super().__init__(f"на {date} по этой позиции уже есть приказ №{number}")


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

    def price_of(self, product_id, date, department=None):
        """Действующая цена и подразделение.

        Подразделений в прейскуранте больше одного: «Cappi Одесса» и
        «Cappi Днепр» — разные города с разными ценами. Раньше здесь
        возвращалось первое попавшееся, и какое именно, зависело от порядка
        строк в ответе. 11.09.2026 из-за этого пять позиций прейскуранта
        уехали в Днепр, а в Одессе остались старые цены.

        Теперь подразделение задаётся явно и по умолчанию берётся из
        конфига. Если в нужном подразделении цены нет — возвращаем пусто,
        а не «ну хоть где-то нашлось»: приказ не в тот город хуже, чем
        отсутствие приказа.
        """
        отдел = department or cfg().get("SYRVE_DEPARTMENT") or None
        for r in self.prices(date):
            if r["productId"] != product_id or not r["prices"]:
                continue
            if отдел and r["departmentId"] != отдел:
                continue
            p = sorted(r["prices"], key=lambda x: x["dateFrom"])[-1]
            return p["price"], r["departmentId"]
        return None, None

    def departments(self):
        """Подразделения прейскуранта: id → название."""
        xml = _get(f"{self.host}/resto/api/corporation/departments?key={self.key}")
        out = {}
        for кусок in re.findall(r"<corporateItemDto>(.*?)</corporateItemDto>",
                                xml, re.S):
            ид = re.search(r"<id>(.*?)</id>", кусок)
            имя = re.search(r"<name>(.*?)</name>", кусок)
            if ид and имя:
                out[ид.group(1)] = имя.group(1)
        return out

    def создать_товар(self, карточка):
        """Новая позиция номенклатуры.

        Артикул и быстрый код Syrve присваивает сам — переданные
        игнорирует. И save только создаёт: повторная отправка той же
        карточки с тем же id делает вторую позицию, а не обновляет первую.
        Проверено 11.09.2026, пришлось убирать дубль.
        """
        r = _post(f"{self.host}/resto/api/v2/entities/products/save?key={self.key}",
                  карточка, timeout=60)
        if r.get("result") != "SUCCESS":
            raise RuntimeError(f"Syrve отказал: {r.get('errors') or r}")
        return r["response"]

    def удалить_товары(self, ids):
        """Пометить позиции удалёнными. Формат придирчивый: список
        объектов с id, не список строк."""
        r = _post(f"{self.host}/resto/api/v2/entities/products/delete?key={self.key}",
                  {"items": [{"id": i} for i in ids]}, timeout=60)
        if r.get("result") != "SUCCESS":
            raise RuntimeError(f"Syrve отказал: {r.get('errors') or r}")
        return r["response"]

    def сохранить_техкарту(self, карта):
        """Техкарта. На нашей сборке Syrve это запрещает.

        Сервер отвечает ASSEMBLY_CHART_IS_NOT_EDITABLE при любом наборе
        полей, любой дате и с id и без. Пользователь api — системный
        администратор, так что права ни при чём: ручка закрыта на стороне
        Syrve. Оставлено рабочим на случай, если её откроют.
        """
        r = _post(f"{self.host}/resto/api/v2/assemblyCharts/save?key={self.key}",
                  карта, timeout=60)
        if r.get("result") != "SUCCESS":
            ошибки = r.get("errors") or []
            коды = {e.get("code") for e in ошибки if isinstance(e, dict)}
            if "ASSEMBLY_CHART_IS_NOT_EDITABLE" in коды:
                raise ТехкартаЗакрыта()
            raise RuntimeError(f"Syrve отказал: {ошибки or r}")
        return r["response"]

    def orders(self, date_from, date_to):
        d = json.loads(self.get("v2/documents/menuChange",
                                dateFrom=date_from, dateTo=date_to))
        return d["response"]

    def set_price(self, product_id, department_id, price, date):
        """Меняет цену позиции на дату — всегда новым приказом.

        Редактировать существующий приказ нельзя, хотя API это позволяет и
        Syrve цену внутри меняет: наружу такая правка не уходит. 10.09.2026
        отредактированный приказ так и не доехал до сайта за полтора часа,
        тогда как новый доезжал за сорок минут. Выгрузка, судя по всему,
        отдаёт изменения по документам и правку за изменение не считает.

        Поэтому при столкновении — честный отказ. Тихо сделать то, что не
        работает, хуже, чем сказать «не могу»: в кассе будет одна цена, на
        витрине другая, и никто об этом не узнает.
        """
        for doc in self.orders(date, date):
            if any(i["productId"] == product_id
                   and i["departmentId"] == department_id
                   for i in doc["items"]):
                raise PriceOrderExists(doc["documentNumber"], date)
        return self.create_price_order(product_id, department_id, price, date)

    def set_prices(self, строки, date):
        """Меняет цены пачкой — одним приказом на все позиции.

        Тридцать отдельных приказов и один приказ на тридцать строк для
        Syrve не одно и то же: выгрузка идёт по документам, и тридцать
        документов растянут доставку на витрины, а часть попадёт в разные
        двадцатиминутные окна. Прейскурант должен меняться целиком.

        Проверка занятых позиций одна на всю пачку и до отправки: половина
        применённого списка — худший исход из возможных.
        """
        # Занятость считаем по паре «позиция + подразделение»: одна и та же
        # позиция может законно менять цену и в Одессе, и в Днепре одним
        # днём — это разные прейскуранты.
        занято = {}
        for doc in self.orders(date, date):
            for i in doc["items"]:
                занято.setdefault((i["productId"], i["departmentId"]),
                                  doc["documentNumber"])
        конфликт = [(r, занято[(r["pid"], r["dep"])]) for r in строки
                    if (r["pid"], r["dep"]) in занято]
        if конфликт:
            raise PriceOrderExists(конфликт[0][1], date, конфликт)
        return self.create_price_order(строки, date)

    def create_price_order(self, product_id, department_id=None, price=None,
                           date=None):
        """Создаёт новый приказ об изменении прейскуранта.

        Первым аргументом либо одна позиция (product_id, department_id,
        price), либо готовый список строк — тогда всё едет одним документом.
        """
        if isinstance(product_id, (list, tuple)):
            строки, date = product_id, department_id
        else:
            строки = [{"pid": product_id, "dep": department_id, "price": price}]
        # Блок на чужие подразделения. Не «по умолчанию наше», а запрет:
        # 11.09.2026 пять позиций прейскурента уехали в Днепр именно
        # потому, что код молча соглашался на любое подразделение.
        наш = отдел()
        чужие = sorted({r["dep"] for r in строки if r["dep"] != наш})
        if чужие:
            raise ЧужойОтдел(
                f"приказ пытается тронуть подразделение не наше "
                f"({', '.join(чужие)}). Бот ведёт только {наш}.")
        body = {
            "dateIncoming": date,
            "status": "PROCESSED",
            "deletePreviousMenu": False,
            "dateTo": "2500-01-01",
            "items": [{
                "departmentId": r["dep"],
                "productId": r["pid"],
                "productSizeId": None,
                "including": True,
                "price": r["price"],
                "taxCategoryId": None,
                "taxCategoryEnabled": False,
                "dishOfDay": False,
                "flyerProgram": False,
                "pricesForCategories": [],
                "includeForCategories": [],
            } for r in строки],
        }
        return self._send(body)

    def _send(self, body):
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
                "id": p["id"],              # тот же guid, что api_guid на сайте
                "name": p["name"],
                "price": sp["currentPrice"],
                "in_menu": sp.get("isIncludedInMenu"),
                "next": sp.get("nextPrice"),
                "next_date": sp.get("nextDatePrice"),
            }
    return out


# ------------------------------------------------------------- Сайт и Glovo
def site_menu(city=SITE_CITY):
    """Всё меню сайта одним запросом: категории с товарами."""
    return json.loads(_get(SITE_MENU_URL.format(city=city), timeout=90))


def site_prices(city=SITE_CITY):
    """{guid: {name, price, cross}} — сверка по guid, без угадывания по названию.

    `cross` — перечёркнутая «старая цена» для бейджа скидки; 0 значит скидки нет.
    """
    out = {}
    for cat in site_menu(city):
        for it in cat.get("items", []):
            g = it.get("api_guid")
            if g:
                out[g] = {"name": it.get("name", ""),
                          "price": it.get("price"),
                          "cross": it.get("price_cross") or 0,
                          "category": cat.get("name")}
    return out


def glovo_prices():
    """Цены Glovo с карточек витрины.

    У позиции со скидкой на карточке две цены: сначала зачёркнутая старая,
    потом текущая. Брать первую попавшуюся значило сравнивать Syrve со
    старой ценой и получать расхождение на ровном месте. Берём наименьшую:
    акционная цена и есть та, по которой продают.
    """
    h = _get(GLOVO_URL, timeout=60)
    out = {}
    for m in re.finditer(r'<img alt="(.*?)"[^>]*>(.*?)(\d[\d\s\u00a0]*,\d\d)\s*₴', h, re.S):
        if len(m.group(2)) >= 3000:
            continue
        имя = norm(m.group(1))
        цена = float(re.sub(r"[\s\u00a0]", "", m.group(3)).replace(",", "."))
        out[имя] = min(цена, out[имя]) if имя in out else цена
    return out
