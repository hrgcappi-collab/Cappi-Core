"""Тест отказов: каждый источник падает исключением — какие экраны
отвечают по-человечески, а какие «Сломалось»."""
import sys; sys.path.insert(0,'.')
import bot, access, cappi, jamshut, report, stoplist, promo, cost, nomenclature
access.можно=lambda u,ч:True; access.есть_доступ=lambda u:True; access.роль=lambda u:"админ"
bot.tg=lambda m,**k:{"ok":True,"result":{}}
ответы=[]; bot.say=lambda c,t,**k: ответы.append(t)
def упасть(*a,**k): raise RuntimeError("сервис недоступен (тест)")
ИСТОЧНИКИ={"Syrve":(cappi,"Syrve"),"Cloud":(cappi,"cloud_menu"),"сайт":(cappi,"site_prices"),
           "Glovo":(cappi,"glovo_prices"),"Loopa":(report,"_loopa"),"Джамшут":(jamshut,"_get"),
           "стоп-лист":(stoplist,"список")}
ЭКРАНЫ=[k for k in bot.BUTTONS if not k.startswith("◀️") and k not in
        ("💰 цены","🛒 продажи","📈 показатели","🧑‍🍳 персонал","🏭 производство","🤖 джамшут","⚙️ админка")]
итог={}
for имя,(м,а) in ИСТОЧНИКИ.items():
    родное=getattr(м,а); setattr(м,а,упасть)
    упало=[]; тихо=[]
    for э in ЭКРАНЫ:
        ответы.clear()
        try:
            bot.BUTTONS[э](1)
            if not ответы: тихо.append(э)
        except Exception as e:
            упало.append((э, f"{type(e).__name__}"))
    setattr(м,а,родное)
    итог[имя]=(упало,тихо)
    print(f"{имя:<10} падает: {len(упало):>2}  молчит: {len(тихо):>2}   "
          + ", ".join(э for э,_ in упало[:6]))
