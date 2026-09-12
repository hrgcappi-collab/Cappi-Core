"""AC-04: при отказе внешнего сервиса человек получает понятный ответ.

Отказ приходит оттуда, откуда придёт в жизни — из сетевого вызова, — а
экран вызывается так, как его вызовет бот: через _обработать. Проверяем,
что в ответе названа причина, а не «Сломалось» и не трассировка.
"""
import sys, urllib.error; sys.path.insert(0,'.')
import bot, access, cappi, jamshut, report, stoplist

access.можно=lambda u,ч: True; access.есть_доступ=lambda u: True
access.роль=lambda u: "админ"; access.все=lambda: {"1":{"роль":"админ"}}
bot.tg=lambda m,**k: {"ok":True,"result":{}}
ответы=[]; bot.say=lambda c,t,**k: ответы.append(t)

# Какой сервис лежит — определяем по адресу, как это делает cappi._сервис.
СЕРВИСЫ={"Syrve":"syrve.online","Syrve Cloud":"syrve.live","Джамшут":"cappi.ua/dzhamshut",
         "сайт":"cappi.ua","Glovo":"glovo","Loopa":"loopa"}
ЭКРАНЫ=[к for к in bot.BUTTONS if к not in ("◀️ назад","🔌 проверка связи")]

родной_get, родной_post = cappi._get, cappi._post
итог={"понятный отказ":0,"данные":0,"СЛОМАЛОСЬ":0,"молчит":0}
плохие=[]

for сервис, кусок in СЕРВИСЫ.items():
    def лежит(url, *a, _к=кусок, _с=сервис, **k):
        if _к in url:
            raise cappi.ВнешнийСбой(_с, "connection refused (тест)")
        return родной_get(url, *a, **k)
    def лежит_post(url, *a, _к=кусок, _с=сервис, **k):
        if _к in url:
            raise cappi.ВнешнийСбой(_с, "connection refused (тест)")
        return родной_post(url, *a, **k)
    cappi._get, cappi._post = лежит, лежит_post
    for э in ЭКРАНЫ:
        ответы.clear(); bot._await.clear()
        bot._обработать({"message":{"chat":{"id":1},"from":{"id":1,"username":"t"},"text":э}}, 1)
        т=" ".join(ответы)
        if not т: итог["молчит"]+=1; плохие.append((сервис,э,"молчит"))
        elif "Сломалось" in т or "Traceback" in т:
            итог["СЛОМАЛОСЬ"]+=1; плохие.append((сервис,э,т[:80].replace("\n"," ")))
        elif "не отвечает" in т or "недоступ" in т: итог["понятный отказ"]+=1
        else: итог["данные"]+=1
    cappi._get, cappi._post = родной_get, родной_post

print(f"проверено: {len(СЕРВИСЫ)} сервисов × {len(ЭКРАНЫ)} экранов")
print("ИТОГ:", итог)
for с,э,т in плохие[:15]: print(f"  ❌ [{с}] {э:<24} {т}")
sys.exit(1 if итог["СЛОМАЛОСЬ"] or итог["молчит"] else 0)
