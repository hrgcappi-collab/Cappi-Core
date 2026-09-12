import sys, traceback, itertools; sys.path.insert(0,'.')
import bot, access, bugs
access.можно=lambda u,ч: True; access.роль=lambda u:"админ"; access.есть_доступ=lambda u:True
отправлено=[]
bot.say=lambda chat,t,**k: отправлено.append(t)
bot.tg=lambda m,**k: {"ok":True,"result":{}}
падения=[]

ТЕКСТЫ = [
 "", " ", "\n\n\n", "/", "//", "/нет_такой", "/set", "/set 999", "/set abc xyz",
 "/set 03275", "/set 03275 -5", "/set 03275 0", "/set 03275 99999999",
 "/set 03275 46 32.13", "/set 03275 46 вчера", "/secret", "/secret Лазарева",
 "/secret 5 4 Лазарева", "/plan", "/plan абв", "/plan месяц", "/people",
 "/people шеф_id", "/access", "/access 123", "/special", "/special ЙЦУ",
 "/kpi", "/report 2026-13-45", "/report абв", "/итоги 0000-00-00",
 "а"*5000, "🙂"*300, "<b>xss</b>", "'; DROP TABLE--", "%s %d {}", "{'a':1}",
 "0.5", "1.5", "-1", "1e999", "NaN", "0", "00000",
 "за 32.13", "за вчера", "итоги за позавчера", "отмены 45.99",
 "Лазарева 0/0", "Лазарева 5/9", "Лазарева -1/-2", "Лазарева 5/", "Лазарева /4",
 "Д Соус 36\nД Тофу 46\nД Гриби 30",           # список
 "Д Соус\nД Тофу\nД Гриби",                     # список без цен
 "позиция 0\nпозиция 0\nпозиция 0",             # нулевые цены
 "а 1\nб 2\nв 3",                               # несуществующие
 "\n".join(f"Товар{i} {i}" for i in range(200)),# очень длинный список
 "пн 1000\nвт 2000\nср 3000\nчт 4000",          # план
]
for t in ТЕКСТЫ:
    try:
        bot.on_message({"chat":{"id":1},"from":{"id":1,"username":"t"},"text":t})
    except SystemExit: raise
    except Exception as e:
        падения.append(("текст", repr(t[:40]), f"{type(e).__name__}: {e}"))

КОЛБЭКИ = ["", ":", "go:", "no:", "gl:", "gj:", "ld:", "вр:", "вр:хз",
 "bg:", "bg:нет", "bd:абв", "bd:-5", "bf:чушь", "bs:x", "bs:x:чушь",
 "ar:", "ar:1", "ar:1:нетроли", "ax:999", "ac:999", "ag:999:чушь",
 "zc:абв", "zd:абв:абв", "zy:x:y", "zo:", "zk:абв",
 "sa:zzz", "sr:zzz", "sy:zzz", "sn:zzz", "sx:zzz", "ed:zzz", "sh:zzz",
 "pc:чушь", "kr:чушь", "km:чушь:чушь", "dt:нет", "hz:hz", "a"*200]
for d in КОЛБЭКИ:
    try:
        bot.on_button({"id":"1","data":d,"from":{"id":1,"username":"t"},
                       "message":{"chat":{"id":1},"message_id":1}})
    except SystemExit: raise
    except Exception as e:
        падения.append(("кнопка", d[:40], f"{type(e).__name__}: {e}"))

print(f"проверено: {len(ТЕКСТЫ)} текстов, {len(КОЛБЭКИ)} кнопок")
print(f"ПАДЕНИЙ: {len(падения)}\n")
for вид,вход,ошибка in падения: print(f"  {вид:<7} {вход:<42} {ошибка[:90]}")

# --- производство
ПРОИЗВОДСТВО = ["сс:", "сс:99999", "сз:", "сз:00087", "нб:нет", "тк:нет",
                "нг:", "нг:zzz"]
падения2=[]
for d in ПРОИЗВОДСТВО:
    try:
        bot.on_button({"id":"1","data":d,"from":{"id":1,"username":"t"},
                       "message":{"chat":{"id":1},"message_id":1}})
    except Exception as e:
        падения2.append((d, f"{type(e).__name__}: {e}"))
for t in ("рис 130 г\nчепуха 5 кг", "ааа 0 г", "рис -5 г", "рис", "0 0 0"):
    try:
        bot._await[1]={"what":"состав"}
        bot.on_message({"chat":{"id":1},"from":{"id":1,"username":"t"},"text":t})
    except Exception as e:
        падения2.append((t[:30], f"{type(e).__name__}: {e}"))
print(f"\nпроизводство: проверено {len(ПРОИЗВОДСТВО)+5} · падений {len(падения2)}")
for a,b in падения2: print("   ", a, "→", b[:90])
