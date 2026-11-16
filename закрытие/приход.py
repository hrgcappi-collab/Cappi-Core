"""Входящие цены: месяц против базового месяца. Только чтение.

    python3 закрытие/приход.py 2026-10 [--база 2026-09] [--папка ПУТЬ]

База по умолчанию — предыдущий месяц; `--база 2025-10` даст год к году.
Выгрузка накладных включительно по `to`, поэтому просим последний день
месяца, а не первое число следующего (см. закрытие/README.md).
"""
import sys, os, csv
import xml.etree.ElementTree as ET
from datetime import date
from calendar import monthrange

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cappi


def месяц(строка):
    год, мес = (int(ч) for ч in строка.split("-"))
    return date(год, мес, 1), date(год, мес, monthrange(год, мес)[1])


def предыдущий(подпись):
    первый, _ = месяц(подпись)
    п = (date(первый.year - 1, 12, 1) if первый.month == 1
         else date(первый.year, первый.month - 1, 1))
    return f"{п.year}-{п.month:02d}"


def позиции(s, первый, последний):
    """Строки приходных накладных за месяц. Удалённые и допрасходы — мимо."""
    r = cappi._get(f"{s.host}/resto/api/documents/export/incomingInvoice"
                   f"?key={s.key}&from={первый.isoformat()}"
                   f"&to={последний.isoformat()}", timeout=240)
    out = []
    for d in ET.fromstring(r).findall("document"):
        if (d.findtext("status") or "").upper() == "DELETED":
            continue
        for it in d.find("items") or []:
            if (it.findtext("isAdditionalExpense") or "") == "true":
                continue
            try:
                # Количество — фактическое: при недопоставке сумма идёт по нему.
                кол = float(it.findtext("actualAmount")
                            or it.findtext("amount") or 0)
                сум = float(it.findtext("sum") or 0)
            except ValueError:
                continue
            if кол <= 0:
                continue
            out.append({"код": (it.findtext("productArticle") or "").strip(),
                        "товар": it.findtext("product"), "кол": кол, "сум": сум})
    return out


def свод(строки):
    итог = {}
    for x in строки:
        a = итог.setdefault(x["товар"], {"кол": 0.0, "сум": 0.0, "код": x["код"]})
        a["кол"] += x["кол"]
        a["сум"] += x["сум"]
    return итог


def main(аргументы):
    if not аргументы or аргументы[0].startswith("-"):
        print(__doc__)
        return 2
    подпись = аргументы[0]
    база = предыдущий(подпись)
    папка = os.path.expanduser(f"~/Downloads/Закрытие-{подпись}")
    хвост = аргументы[1:]
    while хвост:
        флаг = хвост.pop(0)
        if флаг == "--база" and хвост:
            база = хвост.pop(0)
        elif флаг == "--папка" and хвост:
            папка = os.path.expanduser(хвост.pop(0))
        else:
            print(f"не понял аргумент: {флаг}\n")
            print(__doc__)
            return 2
    os.makedirs(папка, exist_ok=True)
    print(f"входящие цены: {подпись} против {база}")
    print(f"csv → {папка}\n")

    with cappi.Syrve() as s:
        имена = {p["id"]: (p.get("name") or "").strip()
                 for p in s.products() if isinstance(p, dict) and "id" in p}
        своды = {}
        for подп in (база, подпись):
            строки = позиции(s, *месяц(подп))
            своды[подп] = свод(строки)
            print(f"  {подп}: {len(строки)} строк накладных, "
                  f"{len(своды[подп])} товаров, "
                  f"{sum(v['сум'] for v in своды[подп].values()):,.0f} ₴")

    б, э = своды[база], своды[подпись]
    итог = []
    for pid, v in э.items():
        цэ = v["сум"] / v["кол"]
        vб = б.get(pid)
        цб = (vб["сум"] / vб["кол"]) if vб and vб["кол"] else None
        итог.append({
            "код": v["код"], "товар": имена.get(pid, "?"),
            "кол_э": v["кол"], "сумма_э": v["сум"], "цена_э": цэ,
            "кол_б": vб["кол"] if vб else None,
            "сумма_б": vб["сум"] if vб else None, "цена_б": цб,
            "изм_%": ((цэ / цб - 1) * 100) if цб else None,
            # Потеря: насколько дороже обошёлся объём месяца из-за цены.
            "потеря": ((цэ - цб) * v["кол"]) if цб else None,
        })

    сравнимые = [x for x in итог if x["изм_%"] is not None]
    дороже = sorted((x for x in сравнимые if x["потеря"] > 0),
                    key=lambda x: -x["потеря"])
    дешевле = sorted((x for x in сравнимые if x["потеря"] < 0),
                     key=lambda x: x["потеря"])
    print(f"\n  сравнимых товаров (есть в оба месяца): {len(сравнимые)}")
    print(f"  только в {подпись}: {len(итог) - len(сравнимые)}")
    print(f"  подорожали: {len(дороже)} · на {sum(x['потеря'] for x in дороже):,.0f} ₴")
    print(f"  подешевели: {len(дешевле)} · на {sum(x['потеря'] for x in дешевле):,.0f} ₴")
    print(f"  ИТОГО эффект цены на объём {подпись}: "
          f"{sum(x['потеря'] for x in сравнимые):,.0f} ₴")

    print(f"\n  {'код':<8}{'товар':<36}{'цена ' + база:>13}"
          f"{'цена ' + подпись:>13}{'изм':>8}{'потеря ₴':>12}")
    for заголовок, набор in (("— подорожало сильнее всего по деньгам —", дороже[:15]),
                             ("— подешевело сильнее всего по деньгам —", дешевле[:10])):
        print(f"  {заголовок}")
        for x in набор:
            print(f"  {x['код']:<8}{x['товар'][:34]:<36}{x['цена_б']:>13,.2f}"
                  f"{x['цена_э']:>13,.2f}{x['изм_%']:>7.0f}%{x['потеря']:>12,.0f}")

    путь = os.path.join(папка, f"входящие-цены-{база}-{подпись}.csv")
    with open(путь, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["код", "товар",
                    f"кол-во {база}", f"сумма {база}", f"цена {база}",
                    f"кол-во {подпись}", f"сумма {подпись}", f"цена {подпись}",
                    "изменение, %", f"эффект на объём {подпись}, ₴"])
        for x in sorted(итог, key=lambda y: -(y["потеря"] or 0)):
            w.writerow([x["код"], x["товар"],
                        f"{x['кол_б']:.3f}" if x["кол_б"] else "",
                        f"{x['сумма_б']:.2f}" if x["сумма_б"] else "",
                        f"{x['цена_б']:.4f}" if x["цена_б"] else "",
                        f"{x['кол_э']:.3f}", f"{x['сумма_э']:.2f}",
                        f"{x['цена_э']:.4f}",
                        f"{x['изм_%']:.1f}" if x["изм_%"] is not None else "",
                        f"{x['потеря']:.2f}" if x["потеря"] is not None else ""])
    print(f"\n  полная таблица: {путь} ({len(итог)} товаров)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
