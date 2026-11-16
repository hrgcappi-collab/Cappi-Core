"""Отказ сети — это ВнешнийСбой, а не «Сломалось». И у _get, и у _post.

02.11.2026 `tests/отказы.py` покраснел на экране «📊 сайт и glovo»: тест
ронял Loopa, экран пошёл в Syrve Cloud за номенклатурой, живой запрос на
четыре мегабайта отвалился по таймауту — и `socket.timeout` ушёл наружу
сырым. Человек получил «Сломалось» и запись в журнале ошибок вместо
«сервис не отвечает».

Причина была в перекосе: у `_get` ветка на отказ сети стояла с самого
начала, у `_post` её не было. А POST'ами идут отчёты OLAP, номенклатура
Cloud, доставки и приказ о цене — то есть именно там, где «Сломалось»
дороже всего: по такому ответу нельзя понять, прошёл приказ или нет.

Проверяем обе ручки одинаково, чтобы перекос не вернулся.
"""
import socket
import sys; sys.path.insert(0, '.')
import urllib.error

import cappi

ПРОВАЛ = []
cappi.след = lambda *a, **k: None          # журнал интеграций в тесте не нужен


class _Ответ:
    def __init__(self, тело=b'{"ok":1}'):
        self._т = тело

    def read(self):
        return self._т

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def подменить(что):
    """Заставляем единственный opener вести себя как сломанная сеть."""
    def open(req, timeout=None):
        if isinstance(что, Exception):
            raise что
        return что
    cappi._OPENER.open = open


родной = cappi._OPENER.open
ОТКАЗЫ = [
    ("таймаут чтения", socket.timeout("The read operation timed out")),
    ("нет сети", urllib.error.URLError("[Errno 8] nodename nor servname provided")),
    ("соединение сброшено", ConnectionResetError(54, "Connection reset by peer")),
    ("TimeoutError", TimeoutError("timed out")),
]

print("отказ сети должен стать cappi.ВнешнийСбой")
for ручка, зов in (("_get", lambda: cappi._get("https://cappi-co.syrve.online/x")),
                   ("_post", lambda: cappi._post("https://cappi-co.syrve.online/x", {"a": 1}))):
    for имя, искл in ОТКАЗЫ:
        подменить(искл)
        try:
            зов()
            итог = "вернулся без ошибки"
        except cappi.ВнешнийСбой:
            итог = "ВнешнийСбой"
        except BaseException as e:
            итог = f"{type(e).__name__}"
        ок = итог == "ВнешнийСбой"
        print(f"  {'OK ' if ок else '❌ '} {ручка:<6} {имя:<22} → {итог}")
        if not ок:
            ПРОВАЛ.append(f"{ручка} на «{имя}» дал {итог}, а должен ВнешнийСбой")

# HTTPError — подкласс URLError. Если его ветку случайно опустят ниже
# новой, Syrve перестанет объяснять отказ телом ответа: вместо
# «409: приказ на эту дату уже есть» останется «сервис не отвечает».
print("\nHTTPError не должен провалиться в ветку отказа сети")
class _HTTP(urllib.error.HTTPError):
    def __init__(self):
        super().__init__("https://x/y", 409, "Conflict", {}, None)

    def read(self):
        return b"order already exists"

подменить(_HTTP())
try:
    cappi._post("https://cappi-co.syrve.online/x", {"a": 1})
    итог = "вернулся без ошибки"
except cappi.ВнешнийСбой as e:
    итог = f"ВнешнийСбой ({e})"
except RuntimeError as e:
    итог = f"RuntimeError: {e}"
except BaseException as e:
    итог = type(e).__name__
ок = итог.startswith("RuntimeError") and "409" in итог and "order already exists" in итог
print(f"  {'OK ' if ок else '❌ '} _post на HTTP 409 → {итог[:70]}")
if not ок:
    ПРОВАЛ.append(f"_post на HTTP 409 дал «{итог[:60]}», а должен RuntimeError с телом ответа")

# Здоровый ответ по-прежнему проходит: ветка отказа не должна ловить всё.
print("\nживой ответ не задет")
подменить(_Ответ())
try:
    r = cappi._post("https://cappi-co.syrve.online/x", {"a": 1})
    ок = r == {"ok": 1}
    print(f"  {'OK ' if ок else '❌ '} _post вернул {r}")
    if not ок:
        ПРОВАЛ.append(f"_post на здоровом ответе вернул {r}")
except BaseException as e:
    print(f"  ❌  _post на здоровом ответе упал: {type(e).__name__}: {e}")
    ПРОВАЛ.append(f"_post на здоровом ответе упал: {type(e).__name__}")

cappi._OPENER.open = родной

# ── «🔌 проверка связи»: таймаут подпроцесса тоже не «Сломалось».
# У суточного сторожа try/except стоял с самого начала, у кнопки не было,
# и 02.11.2026 она упала с TimeoutExpired, когда сайт залип на 30 секунд.
# Экран ровно про доступность источников — он обязан ответить всегда.
print("\n«проверка связи» при таймауте подпроцесса")
import subprocess as _sp

import bot

_ответы = []
bot.say = lambda chat, text, **k: _ответы.append(text)
bot._печатает = lambda chat: None
_родной_run = _sp.run


def _зависший(*a, **k):
    raise _sp.TimeoutExpired(cmd=a[0] if a else "healthcheck.py", timeout=90,
                             output="✅ Syrve  0.2с\n✅ Телеграм  0.1с\n")


_sp.run = _зависший
try:
    bot.cmd_healthcheck(1)
    ответ = " ".join(_ответы)
    ок = bool(ответ) and "не уложилась" in ответ and "Syrve" in ответ
    печать = ответ.replace("\n", " ⏎ ")[:90]
except BaseException as e:
    ок = False
    печать = f"упал: {type(e).__name__}: {e}"
finally:
    _sp.run = _родной_run
print(f"  {'OK ' if ок else '❌ '} {печать}")
if not ок:
    ПРОВАЛ.append("«проверка связи» при таймауте не ответила частичным результатом")

print("\nПРОВАЛОВ:", len(ПРОВАЛ))
for p in ПРОВАЛ:
    print("   ", p)
sys.exit(1 if ПРОВАЛ else 0)
