"""A8: время ответа экрана и сколько он делает запросов наружу."""
import sys, time, collections; sys.path.insert(0,'.')
import bot, access, cappi
access.можно=lambda u,ч:True; access.есть_доступ=lambda u:True; access.роль=lambda u:"админ"
bot.say=lambda c,t,**k:None; bot.tg=lambda m,**k:{"ok":True,"result":{}}
счёт=collections.Counter()
for имя in ("_get","_post"):
    родной=getattr(cappi,имя)
    def обёртка(*a,_р=родной,_и=имя,**k):
        счёт[_и]+=1; return _р(*a,**k)
    setattr(cappi,имя,обёртка)
ПРОПУСК={"◀️ назад","🔌 проверка связи"}
итог=[]
for кнопка,f in bot.BUTTONS.items():
    if кнопка in ПРОПУСК: continue
    счёт.clear(); bot._await.clear(); t=time.time()
    try: f(1)
    except Exception as e: итог.append((кнопка, -1, 0, f"{type(e).__name__}")); continue
    итог.append((кнопка, time.time()-t, счёт["_get"]+счёт["_post"], ""))
итог.sort(key=lambda x:-x[1])
print(f"{'экран':<24} {'сек':>6} {'запросов':>9}")
for к,с,з,ош in итог[:14]:
    print(f"{к:<24} {с:>6.1f} {з:>9}" + (f"  ❌ {ош}" if ош else ""))
медленные=[x for x in итог if x[1]>5]
многозапросные=[x for x in итог if x[2]>6]
print(f"\nдольше 5 с: {len(медленные)} · больше 6 запросов: {len(многозапросные)}")
