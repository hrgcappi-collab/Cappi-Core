"""A6 · AC-07: право проверяется при ВЫПОЛНЕНИИ, а не только при показе кнопки.

Сценарий: человек открыл карточку с правами, права отобрали, он жмёт
кнопку. Ни одно денежное действие не должно пройти.
"""
import sys, time; sys.path.insert(0,'.')
import bot, access, cappi, jamshut, stoplist, kpi, promo, cost
from datetime import date, timedelta

ответы=[]; bot.say=lambda c,t,**k: ответы.append(t); bot.tg=lambda m,**k:{"ok":True,"result":{}}
access.есть_доступ=lambda u: True
access.роль=lambda u: "смотрящий"
access.все=lambda: {"1":{"роль":"смотрящий"}}

# Любая запись наружу считается провалом теста.
ПРОВАЛ=[]
def запрет(имя):
    def f(*a, **k):
        ПРОВАЛ.append(имя); raise AssertionError(f"ЗАПИСЬ БЕЗ ПРАВА: {имя}")
    return f
for модуль, атрибут in ((cappi.Syrve,"set_price"),(cappi.Syrve,"set_prices"),
                        (cappi.Syrve,"создать_товар"),(cappi.Syrve,"сохранить_техкарту"),
                        (cappi.Syrve,"удалить_товары"),
                        (jamshut,"закрыть_зону"),(jamshut,"открыть_зону"),
                        (jamshut,"погода_включить"),(jamshut,"погода_выключить"),
                        (stoplist,"снять"),(stoplist,"поставить"),
                        (access,"добавить"),(access,"сменить_роль"),(access,"убрать"),
                        (promo,"добавить_спец"),(promo,"убрать_спец")):
    setattr(модуль, атрибут, запрет(f"{getattr(модуль,'__name__',модуль.__name__ if hasattr(модуль,'__name__') else модуль)}.{атрибут}"))
_корр=kpi.корректировки
kpi.корректировки=lambda *a, **k: (ПРОВАЛ.append("kpi.корректировки") or (_ for _ in ()).throw(AssertionError("ЗАПИСЬ"))) if len(a)>1 else _корр(*a, **k)
_тайники=kpi.тайники
kpi.тайники=lambda *a, **k: (ПРОВАЛ.append("kpi.тайники") or (_ for _ in ()).throw(AssertionError("ЗАПИСЬ"))) if len(a)>1 or k.get("значение") is not None else _тайники(*a, **k)

сегодня=date.today().isoformat(); завтра=(date.today()+timedelta(days=1)).isoformat()
# готовые токены, как будто карточка была открыта при правах
bot._confirm.update({
 "T1":{"создан":time.time(),"date":завтра,"pid":"x","dep":"d","price":1.0,"code":"00585","name":"Тест","old":1,"chat":1,"user":"t"},
 "T2":{"создан":time.time(),"date":завтра,"строки":[{"pid":"x","dep":"d","price":1.0,"название":"Тест","было":1,"код":"1"}],"скачки":[],"chat":1,"user":"t"},
 "T3":{"создан":time.time(),"блюдо":{"имя":"Т","группа":{"id":"g","name":"Г"},"цена":1,"выход":0.1},"chat":1},
 "T4":{"создан":time.time(),"товар":{"id":"x","name":"Т","num":"1","defaultSalePrice":1},"chat":1},
 "T5":{"создан":time.time(),"товар":{"id":"x","name":"Т","num":"1"},"цена":1,"сс":1,"chat":1},
})
КНОПКИ=["go:T1","gl:T2","gv:T1","gj:T2","ld:T2","dt:T1","нб:T3","тк:T4","нц:T5",
        "zy:17:30","zk:17","zo:17","нгв:on","нгв:off",
        "sr:zzz","sa:zzz","sy:zzz","sn:zzz","sx:03275",
        "ar:1:админ","ax:1","ag:1:админ","bs:x:починен"]
прошло=0
for d in КНОПКИ:
    ответы.clear()
    try:
        bot.on_button({"id":"1","data":d,"from":{"id":1,"username":"t"},
                       "message":{"chat":{"id":1},"message_id":1}})
        прошло+=1
    except AssertionError as e:
        print(f"  ❌ {d:<16} {e}")
    except Exception as e:
        print(f"  ⚠️  {d:<16} {type(e).__name__}: {str(e)[:50]}")
# текстовые пути записи
ТЕКСТЫ=["Лазарева 5/4","/secret Лазарева 5 4","/set 00585 99","/access 123 админ",
        "/special 03275","/fix Иванов +5","Д Соус Барбекю 36\nД Тофу 46\nД Гриби 30"]
for t in ТЕКСТЫ:
    ответы.clear(); bot._await.clear()
    try:
        bot.on_message({"chat":{"id":1},"from":{"id":1,"username":"t"},"text":t})
    except AssertionError as e:
        print(f"  ❌ текст {t[:24]:<24} {e}")
    except Exception as e:
        print(f"  ⚠️  текст {t[:24]:<24} {type(e).__name__}: {str(e)[:40]}")
print(f"\nпроверено кнопок {len(КНОПКИ)} + текстов {len(ТЕКСТЫ)}")
print("ЗАПИСЕЙ БЕЗ ПРАВА:", len(ПРОВАЛ), ПРОВАЛ or "— нет")
