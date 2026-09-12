"""Хаос на уровне HTTP и через настоящий путь обработки.

Прошлый хаос-тест подменял сами функции — и моя проверка формы внутри
них не запускалась. Здесь мусор приходит оттуда, откуда придёт в жизни:
из ответа сервера. А экран вызывается так, как его вызовет бот, — через
_обработать, где сбой превращается в сообщение человеку."""
import sys, json; sys.path.insert(0,'.')
import bot, access, cappi, report
access.можно=lambda u,ч:True; access.есть_доступ=lambda u:True; access.роль=lambda u:"админ"
access.все=lambda: {"1":{"роль":"админ"}}
bot.tg=lambda m,**k:{"ok":True,"result":{}}
ответы=[]; bot.say=lambda c,t,**k: ответы.append(t)
МУСОР_JSON=["null","{}","[]","\"строка\"","{\"data\":[{\"нет\":1}]}","[{\"нет\":1}]",
            "{\"closed\":[{\"id\":1}],\"count\":1}","<html>502 Bad Gateway</html>",""]
ЭКРАНЫ=["🚦 зоны сейчас","🚧 закрыть зону","✅ открыть зону","📜 история","👤 кто закрывал","🩺 здоровье",
        "🏷 акционные","📊 сайт и glovo","🛑 стоп-лист","🚚 в работе","😠 жалобы","📈 сейчас","🗑 списания",
        "❌ отмены","👥 смена сегодня","⏱ время работы","🧮 себестоимость блюда","🔍 найти позицию"]
исход={"данные":0,"понятный отказ":0,"СЛОМАЛОСЬ":0,"молчит":0}
плохие=[]
def прогнать(метка):
    for э in ЭКРАНЫ:
        ответы.clear()
        bot._обработать({"message":{"chat":{"id":1},"from":{"id":1,"username":"t"},"text":э}}, 1)
        т=" ".join(ответы)
        if not т: исход["молчит"]+=1; плохие.append((метка,э,"молчит"))
        elif "Сломалось" in т: исход["СЛОМАЛОСЬ"]+=1; плохие.append((метка,э,т[:70].replace("\n"," ")))
        elif "не отвечает" in т or "недоступ" in т: исход["понятный отказ"]+=1
        else: исход["данные"]+=1
# 1) мусор в теле ответа
родной_get, родной_post = cappi._get, cappi._post
for м in МУСОР_JSON:
    cappi._get=lambda url,*a,_м=м,**k: _м          # строка, как отдаёт сеть
    def _p(url,body,*a,_м=м,**k):
        if not _м.strip(): return {}
        try: return json.loads(_м)
        except ValueError: raise cappi.ВнешнийСбой("Syrve", f"ответ не JSON: {_м[:30]}")
    cappi._post=_p
    прогнать(f"json={м[:18]}")
cappi._get, cappi._post = родной_get, родной_post
# 2) сеть лежит
import urllib.error
def сеть_лежит(*a,**k): raise urllib.error.URLError("connection refused (тест)")
cappi._get=lambda *a,**k: (_ for _ in ()).throw(cappi.ВнешнийСбой("Syrve","connection refused"))
cappi._post=lambda *a,**k: (_ for _ in ()).throw(cappi.ВнешнийСбой("Syrve","connection refused"))
прогнать("сеть лежит")
cappi._get, cappi._post = родной_get, родной_post
print("ИТОГ:", исход, flush=True)
import collections
for (м,э,т),n in collections.Counter(плохие).most_common(20): print(f"  {n}× {э:<22} [{м}] {т}")
