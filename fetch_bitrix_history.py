# -*- coding: utf-8 -*-
"""
fetch_bitrix_history.py — архів усієї історії CRM у вигляді щоденних підсумків.

Навіщо окремо від основного збору. bitrix_data.json тримає 90 днів «сирими»:
кожна угода з товарними позиціями, щоб на сторінці можна було розкрити
менеджера й побачити конкретні продажі. Для історії за роки такий формат не
годиться — файл виріс би до десятків мегабайт, і сторінка качала б його при
кожному відкритті. Тому тут зберігаються не угоди, а числа по днях:

    дата · менеджер · угод · сума · забраковано · лідів · забраковано лідів

Рік такої історії важить кілька сотень кілобайт замість мегабайтів.

Що тут НЕ зберігається і чому. Розбивки за товарами (матраци / аксесуари /
ліжка) в архіві немає: щоб її порахувати, Bitrix треба питати товарні позиції
кожної угоди окремо, а це по запиту на угоду — для кількох десятків тисяч
угод години роботи й ризик упертись у ліміти порталу. Тому сума в архіві —
це сума угод (OPPORTUNITY, «валом»), а не «за товарами», як на сторінці CRM
за останні 90 днів. Числа за той самий день із двох джерел відрізнятимуться,
і це не помилка: у них різні означення. На сторінці це треба підписати.

Як запускається. Один раз вручну — збирає все від першої угоди в порталі до
початку «живого» вікна (сьогодні мінус DAYS_BACK). Далі місяці, які вже є у
файлі, не перезбираються, тому повторний запуск лише дописує нові — це
секунди. Якщо в CRM чистили старі угоди й архів треба перебудувати, видаліть
bitrix_history.json або запустіть із --rebuild.

    python3 fetch_bitrix_history.py            # дозібрати те, чого немає
    python3 fetch_bitrix_history.py --rebuild  # перебудувати все з нуля
    python3 fetch_bitrix_history.py --from 2024-01  # тільки з цього місяця
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path

import fetch_bitrix as fb

OUT = Path(__file__).parent / "bitrix_history.json"

# Скільки днів лишається «живими» в основному файлі. Архів закінчується там,
# де починається вони, щоб ті самі дні не рахувались двічі.
LIVE_DAYS = fb.DAYS_BACK

HIST_DEAL_FIELDS = ["ID", "STAGE_ID", "OPPORTUNITY", "DATE_CREATE",
                    "ASSIGNED_BY_ID", "SOURCE_ID"]
HIST_LEAD_FIELDS = ["ID", "STATUS_ID", "STATUS_SEMANTIC_ID", "SOURCE_ID",
                    "DATE_CREATE", "ASSIGNED_BY_ID"]


def months_between(first, last):
    """Перелік місяців як ('2024-03', дата_початку, дата_кінця_включно)."""
    y, m = first.year, first.month
    out = []
    while (y, m) <= (last.year, last.month):
        start = date(y, m, 1)
        end = date(y, m, monthrange(y, m)[1])
        out.append((f"{y}-{m:02d}", start, min(end, last)))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def call_first_page(method, params):
    """Один запит без перелистування.

    fb.call() завжди доходить до кінця списку — для місячних вибірок це
    правильно, але тут нам потрібен лише найперший рядок. Через fb.call()
    питання «коли почався портал» коштувало б вигрузки всіх угод за всі
    роки (сотні запитів) замість одного.

    URL із вебхуком у текст помилки не потрапляє: він містить ключ доступу,
    а лог збору видно в Actions.
    """
    q = dict(params)
    q["start"] = 0
    url = f"{fb.WEBHOOK_URL}/{method}.json?{urllib.parse.urlencode(q, doseq=True)}"
    req = urllib.request.Request(url, headers=fb.HTTP_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise RuntimeError(f"Bitrix API {method}: {type(e).__name__}")
    if "error" in data:
        raise RuntimeError(f"Bitrix API {method}: "
                           f"{data.get('error_description', data['error'])}")
    result = data.get("result") or []
    return result if isinstance(result, list) else []


def first_deal_date():
    """Дата найранішої угоди в порталі. Якщо порожньо — сьогодні."""
    rows = call_first_page("crm.deal.list", {
        "select[]": ["ID", "DATE_CREATE"],
        "order[DATE_CREATE]": "ASC",
    })
    for r in rows[:1]:
        d = fb.to_date(r.get("DATE_CREATE"))
        if d:
            return date.fromisoformat(d)
    return date.today()


def fetch_range(method, fields, start, end):
    return fb.call(method, {
        "filter[>=DATE_CREATE]": start.isoformat(),
        "filter[<=DATE_CREATE]": end.isoformat() + "T23:59:59",
        "select[]": fields,
        "order[DATE_CREATE]": "ASC",
    })


def aggregate_month(start, end, stage_names):
    """Підсумки одного місяця: {(дата, менеджер): лічильники}."""
    cells = {}

    def cell(d, mid):
        key = (d, mid)
        if key not in cells:
            cells[key] = {"deals": 0, "sum": 0.0, "deals_lost": 0, "sum_lost": 0.0,
                          "leads": 0, "leads_lost": 0}
        return cells[key]

    for d in fetch_range("crm.deal.list", HIST_DEAL_FIELDS, start, end):
        day = fb.to_date(d.get("DATE_CREATE"))
        if not day:
            continue
        mid = str(d.get("ASSIGNED_BY_ID") or "")
        amount = float(d.get("OPPORTUNITY") or 0)
        name = stage_names.get(d.get("STAGE_ID", ""), {}).get("name", "").strip().lower()
        c = cell(day, mid)
        # Відмови й помилкові рахуємо окремо — тією самою міркою, що й на
        # сторінці CRM, інакше архів і живі дані міряли б різне.
        if name in fb.EXCLUDED_STAGE_NAMES:
            c["deals_lost"] += 1
            c["sum_lost"] += amount
        else:
            c["deals"] += 1
            c["sum"] += amount

    for l in fetch_range("crm.lead.list", HIST_LEAD_FIELDS, start, end):
        day = fb.to_date(l.get("DATE_CREATE"))
        if not day:
            continue
        c = cell(day, str(l.get("ASSIGNED_BY_ID") or ""))
        c["leads"] += 1
        # Забракований лід — той, у якого Bitrix сам позначив статус як
        # провалений (STATUS_SEMANTIC_ID = "F"). Саме це поле читає й
        # основний збір, тому архів і живі дані рахують те саме. За назвою
        # статусу тут орієнтуватись не можна: «CONVERTED» — це лід, який
        # перетворили на угоду, тобто успіх, а не брак.
        if str(l.get("STATUS_SEMANTIC_ID") or "").upper() == "F":
            c["leads_lost"] += 1

    return cells


def main():
    if not fb.WEBHOOK_URL:
        print("⚠  Задайте BITRIX_WEBHOOK_URL.")
        sys.exit(1)

    rebuild = "--rebuild" in sys.argv
    from_month = None
    if "--from" in sys.argv:
        from_month = sys.argv[sys.argv.index("--from") + 1]

    data = {}
    if OUT.exists() and not rebuild:
        try:
            data = json.loads(OUT.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    months_done = set(data.get("months_done") or [])
    rows = {f'{r["date"]}|{r["manager_id"]}': r for r in (data.get("rows") or [])}

    stage_names = fb.fetch_deal_stages()
    users = fb.fetch_users()

    last = date.today() - timedelta(days=LIVE_DAYS + 1)   # архів до початку живого вікна
    first = first_deal_date()
    print(f"=== Архів CRM: {first} … {last} ===")
    if first > last:
        print("Уся історія вкладається в живе вікно — архів не потрібен.")
        return

    todo = months_between(first, last)
    if from_month:
        todo = [m for m in todo if m[0] >= from_month]
    todo = [m for m in todo if m[0] not in months_done]
    if not todo:
        print("Нових місяців немає — архів актуальний.")
        return
    print(f"  місяців до збору: {len(todo)} (вже у файлі: {len(months_done)})")

    started = datetime.now()
    for i, (label, start, end) in enumerate(todo, 1):
        try:
            cells = aggregate_month(start, end, stage_names)
        except Exception as e:
            # Обірвався один місяць — не втрачаємо решту: що зібрали, те
            # зберігаємо, а місяць лишається «не зробленим» і дозбереться
            # наступним запуском.
            print(f"  ✗ {label}: {type(e).__name__}: {e}")
            break
        for (day, mid), c in cells.items():
            rows[f"{day}|{mid}"] = dict(c, date=day, manager_id=mid)
        months_done.add(label)
        print(f"  [{i}/{len(todo)}] {label}: днів {len({d for d, _ in cells})}, "
              f"рядків {len(cells)}")

        # Зберігаємо після кожного місяця: збір усієї історії може тривати
        # десятки хвилин, і впасти на середині — нормальна подія.
        save(data, rows, months_done, users, first, last)

    sec = (datetime.now() - started).total_seconds()
    print(f"✓ готово за {sec / 60:.1f} хв · рядків у архіві: {len(rows)} · "
          f"файл {OUT.stat().st_size / 1024:.0f} КБ")


def save(data, rows, months_done, users, first, last):
    out = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "from": str(first),
        "to": str(last),
        "live_days": LIVE_DAYS,
        # Сума тут — «валом» (OPPORTUNITY), без розбивки за товарами.
        # Сторінка має це підписати, щоб ніхто не порівнював її з «за
        # товарами» за останні 90 днів і не шукав неіснуючу розбіжність.
        "sum_kind": "gross",
        "users": users,
        "months_done": sorted(months_done),
        "rows": sorted(rows.values(), key=lambda r: (r["date"], r["manager_id"])),
    }
    tmp = OUT.with_name(OUT.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(out, fp, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT)
    data.clear()
    data.update(out)


if __name__ == "__main__":
    main()
