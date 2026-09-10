#!/usr/bin/env python3
"""Приём событий от Джамшута: закрытие и открытие зон доставки.

Запускается ниткой внутри бота, отдельно поднимать не нужно. Настройки в
~/.cappi/api.env:

    CORE_WEBHOOK_TOKEN   тот же токен, что у отправителя
    CORE_WEBHOOK_PORT    по умолчанию 8787

Контракт согласован с отправителем:

    POST /webhook/zones
    Authorization: Bearer <token>
    {"v": 1, "event": "zone.close", "at": "...", "zone_ids": [24, 25],
     "district": "центр", "branch": "Лазарева", "duration_min": 30,
     "auto": false, "actor": "Дмитро", "source": "human",
     "context": {"in_work": 7, "kitchen": 3, "onway": 3,
                 "staff": {"cooks": 3, "couriers": 2}}}

Коды ответа выбраны под их логику повторов: 4xx отправитель считает
окончательным и больше не шлёт, 5xx и таймаут — поводом повторить.
Отсюда правило: отвечаем 4xx только когда повтор точно не поможет —
плохой токен, неразбираемое тело, чужая версия схемы. Любая наша
внутренняя поломка — 500, чтобы событие вернулось, а не пропало.

Про дубли. Повтор случается и после успешной записи: если наш ответ не
дошёл, отправитель повторит то же событие. Идентификатора события в схеме
нет, поэтому считаем отпечаток от тела — одинаковые события с одинаковым
временем это один факт, а не два.
"""
import hashlib
import json
import os
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cappi

EVENTS = os.path.expanduser("~/.cappi/zone_events.jsonl")
СХЕМА = 1                     # версию отправитель обещал не менять молча
_seen = set()                 # отпечатки принятых событий, против дублей
_lock = threading.Lock()


def _load_seen():
    """Отпечатки уже принятых событий — чтобы дубли не пережили перезапуск."""
    try:
        with open(EVENTS) as f:
            for line in f:
                try:
                    _seen.add(json.loads(line)["_fp"])
                except Exception:
                    continue
    except FileNotFoundError:
        pass


def _fingerprint(payload):
    важное = {k: payload.get(k) for k in ("event", "at", "zone_ids", "branch")}
    return hashlib.sha1(
        json.dumps(важное, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def save(payload):
    """Записывает событие. Возвращает False, если такое уже было."""
    fp = _fingerprint(payload)
    with _lock:
        if fp in _seen:
            return False
        # Помечаем виденным только после успешной записи. Иначе сбой диска
        # означал бы, что событие потеряно и повторную доставку мы тоже
        # отбросим как дубль — зона останется закрытой, а мы об этом не
        # узнаем.
        with open(EVENTS, "a") as f:
            f.write(json.dumps({**payload, "_fp": fp,
                                "_received": datetime.now().isoformat()},
                               ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        _seen.add(fp)
    return True


def events(day=None):
    """События за день (по полю at отправителя, не по времени приёма)."""
    out = []
    try:
        with open(EVENTS) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if day is None or (e.get("at", "")[:10] == day.isoformat()):
                    out.append(e)
    except FileNotFoundError:
        pass
    return out


def zones_summary(day):
    """Сколько зон закрывали и на сколько.

    Длительность берём фактическую — от закрытия до открытия той же зоны.
    Если открытия ещё не было, используем `duration_min` из закрытия: это
    намерение, а не факт, и в отчёте оно помечено отдельно.
    """
    закрытия, открытия = [], []
    for e in events(day):
        (закрытия if e.get("event") == "zone.close" else открытия).append(e)

    def врем(e):
        try:
            return datetime.fromisoformat(e["at"])
        except Exception:
            return None

    итог = {"закрытий": len(закрытия), "минут": 0, "открыто_назад": 0,
            "ещё_закрыты": 0, "по_районам": {}, "авто": 0}
    for c in закрытия:
        t0 = врем(c)
        зоны = set(c.get("zone_ids") or [])
        if c.get("auto"):
            итог["авто"] += 1
        парное = next((o for o in открытия
                       if врем(o) and t0 and врем(o) > t0
                       and set(o.get("zone_ids") or []) & зоны), None)
        if парное:
            минут = (врем(парное) - t0).total_seconds() / 60
            итог["открыто_назад"] += 1
        else:
            минут = c.get("duration_min") or 0
            итог["ещё_закрыты"] += 1
        итог["минут"] += минут
        # branch у Котовского филиала null — тогда опираемся на район.
        район = c.get("district") or c.get("branch") or "без района"
        r = итог["по_районам"].setdefault(район, {"раз": 0, "минут": 0})
        r["раз"] += 1
        r["минут"] += минут
    return итог


# ------------------------------------------------------------------- опрос
# Второй способ получить те же события: сходить к Джамшуту самим. Он нужен,
# потому что вебхуку требуется публичный адрес, а бот пока живёт на ноутбуке.
# Оба пути пишут в одно хранилище и отсекаются одним отпечатком, так что
# включённые одновременно они не задваивают историю.

def _api(path):
    c = cappi.cfg()
    if not c.get("JAMSHUT_TOKEN"):
        return None
    return json.loads(cappi._get(
        c["JAMSHUT_URL"].rstrip("/") + path,
        {"Authorization": f"Bearer {c['JAMSHUT_TOKEN']}"}, timeout=40))


def state():
    """Какие зоны закрыты прямо сейчас — по данным Джамшута, а не по нашей
    истории: он знает и о том, что было до того, как мы начали слушать."""
    return _api("/zones/state")


def pull(с=None, по=None, шаг=7):
    """Забирает события за период и складывает к себе.

    Период режется на куски: за nginx у них таймаут 30 секунд, и запрос
    за месяц одним разом в него не укладывается. Возвращает, сколько
    событий оказалось новыми.
    """
    с = с or datetime.now().date()
    по = по or с
    новых, начало = 0, с
    while начало <= по:
        конец = min(начало + timedelta(days=шаг - 1), по)
        try:
            r = _api(f"/zones/events?from={начало}&to={конец}&limit=10000")
        except Exception:
            начало = конец + timedelta(days=1)
            continue
        for e in (r or {}).get("data", []):
            try:
                if save(e):
                    новых += 1
            except Exception:
                continue
        начало = конец + timedelta(days=1)
    return новых


# ------------------------------------------------------------------- сервер
class _Handler(BaseHTTPRequestHandler):
    server_version = "CappiCore/1"

    def _ответ(self, код, тело):
        данные = json.dumps(тело, ensure_ascii=False).encode()
        self.send_response(код)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(данные)))
        self.end_headers()
        self.wfile.write(данные)

    def do_GET(self):
        # Отправителю нужен способ убедиться, что приёмник жив.
        if self.path in ("/health", "/webhook/zones/health"):
            return self._ответ(200, {"ok": True, "service": "cappi-core"})
        self._ответ(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/webhook/zones":
            return self._ответ(404, {"error": "not found"})

        токен = (cappi.cfg().get("CORE_WEBHOOK_TOKEN") or "").strip()
        дано = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not токен or дано != токен:
            # 401 — повтор не поможет, пока не поправят токен.
            return self._ответ(401, {"error": "bad token"})

        try:
            длина = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(длина) or b"{}")
        except Exception as e:
            return self._ответ(400, {"error": f"bad json: {e}"})

        if payload.get("v") != СХЕМА:
            return self._ответ(400, {"error": f"schema v{payload.get('v')} "
                                              f"not supported, expected v{СХЕМА}"})
        if payload.get("event") not in ("zone.close", "zone.open"):
            return self._ответ(400, {"error": f"unknown event {payload.get('event')}"})

        try:
            новое = save(payload)
        except Exception as e:
            # Наша поломка — просим повторить, иначе событие пропадёт.
            return self._ответ(500, {"error": str(e)[:200]})

        self._ответ(200, {"ok": True, "duplicate": not новое})
        if новое:
            for уведомить in СЛУШАТЕЛИ:
                try:
                    уведомить(payload)
                except Exception:
                    pass

    def log_message(self, *a):
        pass          # свой лог не нужен, события и так пишутся в файл


СЛУШАТЕЛИ = []        # кого дёрнуть при новом событии (бот шлёт в телеграм)


def serve():
    """Поднимает приёмник. Молчит, если токен не задан, — так у отправителя
    и задумано: без токена он ничего не шлёт, и слушать нечего."""
    c = cappi.cfg()
    if not c.get("CORE_WEBHOOK_TOKEN"):
        return None
    порт = int(c.get("CORE_WEBHOOK_PORT") or 8787)
    _load_seen()
    сервер = ThreadingHTTPServer(("0.0.0.0", порт), _Handler)
    threading.Thread(target=сервер.serve_forever, daemon=True).start()
    return порт


if __name__ == "__main__":
    порт = serve()
    print(f"приёмник на порту {порт}" if порт else "нет CORE_WEBHOOK_TOKEN")
    if порт:
        threading.Event().wait()
