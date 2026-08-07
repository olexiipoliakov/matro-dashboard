"""
fetch_gsc.py — тянет данные из Google Search Console и сохраняет seo_data.json
Запускается через GitHub Actions ежедневно.
"""
import json, os, re, sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ── Классификатор разделов сайта по URL ─────────────────────────────────────
# Порядок важен: сначала более специфичные правила, потом общие типы товара.
CATEGORY_RULES = [
    ("horeca",    "HoReCa",                 ["horeca"]),
    ("komplekty", "Комплекти меблів",       ["komplekty-mebeli", "mebel-dlya"]),
    ("aksessuary","Текстиль та аксесуари",  ["aksessuary", "odeyala", "podushki",
                                              "namatrasnik", "zashchita-dlya-matrasa", "chehly"]),
    ("kuhni",     "Кухні",                  ["kuhni", "kuchni"]),
    ("korpus",    "Корпусні меблі",         ["korpusnaya", "stoly", "tumby", "komod",
                                              "polki", "penal", "konsoli"]),
    ("matrasy",   "Матраци",                ["matras", "mattress", "topper", "futon"]),
    ("divany",    "Дивани",                 ["divan"]),
    ("krovati",   "Ліжка",                  ["krovat", "podium"]),
    ("shkafy",    "Шафи",                   ["shkaf", "shafa"]),
    ("service",   "Сервісні сторінки",      ["o-kompanii", "markets", "dostavka", "vakansiyi",
                                              "dlya-dyzayneriv", "kontakty", "aktsii",
                                              "compare-products", "wishlist", "novelties"]),
]

def classify_category(page_url):
    try:
        path = urlparse(page_url).path.lower()
    except Exception:
        path = str(page_url).lower()
    path = re.sub(r'^/(ua|ru)/', '/', path)
    if path in ('', '/'):
        return ("home", "Головна")
    for key, name, keywords in CATEGORY_RULES:
        if any(k in path for k in keywords):
            return (key, name)
    return ("other", "Інше")

# ── Классификатор пошукових запитів ─────────────────────────────────────────
# Порядок важливий: брендові запити перевіряються першими (навіть якщо запит
# також згадує тип товару — це все одно брендовий трафік).
BRAND_SUBSTR = ["matro", "матро", "matroluxe", "leo", "лео", "toscano", "тоскано", "plume", "плюм"]
BRAND_WORDS  = ["сан", "тео", "нардо"]  # короткі назви — тільки як окреме слово, щоб не ловити зайве

QUERY_CATEGORY_RULES = [
    ("topery",  "Топери",  ["топер", "topper"]),
    ("matrasy", "Матраци", ["матрас", "matras", "mattress"]),
    ("divany",  "Дивани",  ["диван"]),
    ("lizhka",  "Ліжка",   ["ліжко", "ліжка", "кровать", "кровати", "krovat"]),
    ("shafy",   "Шафи",    ["шафа", "шафи", "шкаф"]),
]

def classify_query(q):
    text = (q or "").lower()
    if any(b in text for b in BRAND_SUBSTR) or any(re.search(rf'\b{w}\b', text) for w in BRAND_WORDS):
        return ("brand", "Брендовий трафік")
    for key, name, keywords in QUERY_CATEGORY_RULES:
        if any(k in text for k in keywords):
            return (key, name)
    return ("other", "Інше (нішеві / загальні запити)")

# ── Настройки ──────────────────────────────────────────────────────────────
SITES = [
    {"id": "https://matroluxe.ua/",  "name": "Matroluxe UA"},
]

OUT = Path(__file__).parent / "seo_data.json"
SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]

# Ключ — либо из переменной окружения (GitHub Actions), либо из файла рядом
KEY_ENV  = os.environ.get("GSC_SERVICE_ACCOUNT_JSON", "")
KEY_FILE = Path(__file__).parent / "gsc_key.json"

# ── Авторизация ────────────────────────────────────────────────────────────
def get_service():
    if KEY_ENV:
        info = json.loads(KEY_ENV)
    elif KEY_FILE.exists():
        info = json.loads(KEY_FILE.read_text())
    else:
        raise RuntimeError("GSC ключ не найден: задайте GSC_SERVICE_ACCOUNT_JSON или положите gsc_key.json рядом")
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)

# ── Запрос данных ──────────────────────────────────────────────────────────
GSC_API_PAGE_LIMIT = 25000  # жорсткий максимум GSC API за один запит

def query(service, site_url, start, end, dimensions, row_limit=1000):
    """Тягне рядки з GSC, автоматично пагінуючи через startRow, якщо
    row_limit перевищує ліміт API одного запиту (25000) — інакше дані
    за частину дат/запитів обрізаються мовчки (спостерігалось на практиці:
    навіть популярні запити типу "дивани" зникали з останнього періоду)."""
    all_rows = []
    start_row = 0
    remaining = row_limit
    while remaining > 0:
        page_size = min(remaining, GSC_API_PAGE_LIMIT)
        body = {
            "startDate": str(start),
            "endDate":   str(end),
            "dimensions": dimensions,
            "rowLimit": page_size,
            "startRow": start_row,
            "dataState": "final",
        }
        resp = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
        rows = resp.get("rows", [])
        all_rows.extend(rows)
        if len(rows) < page_size:
            break  # останній рядок — далі даних немає
        start_row += page_size
        remaining -= page_size
    return all_rows

def fetch_categories(service, url, weeks=12):
    """Тянет (дата, страница) за последние `weeks` недель и группирует
    по разделу сайта и неделе: клики, показы, средневзвешенная позиция."""
    today = date.today()
    end = today - timedelta(days=3)
    start = end - timedelta(days=weeks * 7 - 1)
    rows = query(service, url, start, end, ["date", "page"], row_limit=25000)

    buckets = defaultdict(lambda: defaultdict(lambda: {"clicks": 0, "impressions": 0, "pos_w": 0.0}))
    cat_names = {}
    for r in rows:
        d = date.fromisoformat(r["keys"][0])
        page = r["keys"][1]
        key, name = classify_category(page)
        cat_names[key] = name
        week_start = str(d - timedelta(days=d.weekday()))
        clicks = r.get("clicks", 0)
        impressions = r.get("impressions", 0)
        b = buckets[week_start][key]
        b["clicks"] += clicks
        b["impressions"] += impressions
        b["pos_w"] += r.get("position", 0) * max(impressions, 1)

    weeks_sorted = sorted(buckets.keys())
    categories = []
    for key in sorted(cat_names.keys()):
        weekly = []
        for wk in weeks_sorted:
            b = buckets[wk].get(key)
            if b and b["impressions"]:
                weekly.append({
                    "week_start": wk,
                    "clicks": b["clicks"],
                    "impressions": b["impressions"],
                    "position": round(b["pos_w"] / b["impressions"], 1),
                })
            else:
                weekly.append({"week_start": wk, "clicks": 0, "impressions": 0, "position": None})
        categories.append({"key": key, "name": cat_names[key], "weekly": weekly})
    return categories

def fetch_query_categories(service, url, start_28, end, weeks=12, period_days=14, row_limit=250000):
    """Тягне (date, query) за `weeks` тижнів (щоб мати й 28-денний зріз, і повну історію),
    класифікує кожен запит у категорію, і будує:
      - агрегати категорій за останні 28 днів (для барів і сортування)
      - для кожного запиту в категорії — клики/показы/позиція за 28 днів
        ТА історію позицій зрізами по `period_days` днів за весь період `weeks` тижнів."""
    start_hist = end - timedelta(days=weeks * 7 - 1)
    rows = query(service, url, start_hist, end, ["date", "query"], row_limit=row_limit)

    cat_of_cache = {}
    cat_names = {}
    def cat_of(q):
        if q not in cat_of_cache:
            key, name = classify_query(q)
            cat_of_cache[q] = key
            cat_names[key] = name
        return cat_of_cache[q]

    cat_agg = defaultdict(lambda: {"clicks": 0, "impressions": 0, "pos_w": 0.0})
    query_28 = defaultdict(lambda: {"clicks": 0, "impressions": 0, "pos_w": 0.0})
    period_buckets = defaultdict(lambda: defaultdict(lambda: {"clicks": 0, "impressions": 0, "pos_w": 0.0}))

    for r in rows:
        d = date.fromisoformat(r["keys"][0])
        q_text = r["keys"][1]
        key = cat_of(q_text)
        clicks = r.get("clicks", 0)
        impressions = r.get("impressions", 0)
        position = r.get("position", 0)

        if start_28 <= d <= end:
            b = cat_agg[key]
            b["clicks"] += clicks
            b["impressions"] += impressions
            b["pos_w"] += position * max(impressions, 1)
            qb = query_28[q_text]
            qb["clicks"] += clicks
            qb["impressions"] += impressions
            qb["pos_w"] += position * max(impressions, 1)

        idx = (d - start_hist).days // period_days
        period_start = str(start_hist + timedelta(days=idx * period_days))
        pb = period_buckets[q_text][period_start]
        pb["clicks"] += clicks
        pb["impressions"] += impressions
        pb["pos_w"] += position * max(impressions, 1)

    periods_sorted = sorted({p for qd in period_buckets.values() for p in qd.keys()})
    all_queries = set(query_28.keys()) | set(period_buckets.keys())

    queries_by_cat = defaultdict(list)
    for q_text in all_queries:
        key = cat_of(q_text)
        qb = query_28.get(q_text)
        periods = []
        for p in periods_sorted:
            pb = period_buckets[q_text].get(p)
            if pb and pb["impressions"]:
                periods.append({
                    "period_start": p, "clicks": pb["clicks"], "impressions": pb["impressions"],
                    "position": round(pb["pos_w"] / pb["impressions"], 1),
                })
            else:
                periods.append({"period_start": p, "clicks": 0, "impressions": 0, "position": None})
        queries_by_cat[key].append({
            "query": q_text,
            "clicks": qb["clicks"] if qb else 0,
            "impressions": qb["impressions"] if qb else 0,
            "position": round(qb["pos_w"] / qb["impressions"], 1) if qb and qb["impressions"] else None,
            "periods": periods,
        })

    total_clicks = sum(b["clicks"] for b in cat_agg.values()) or 1
    result = []
    for key, b in cat_agg.items():
        qs = sorted(queries_by_cat[key], key=lambda x: -x["clicks"])
        result.append({
            "key": key,
            "name": cat_names[key],
            "clicks": b["clicks"],
            "impressions": b["impressions"],
            "position": round(b["pos_w"] / b["impressions"], 1) if b["impressions"] else None,
            "queries_count": len(qs),
            "share": round(b["clicks"] / total_clicks * 100, 1),
            "queries": qs,
        })
    result.sort(key=lambda x: -x["clicks"])
    return result

def _period_history(rows, key_index, top_n, start, period_days=14):
    """Спільна логіка: із рядків (date, <key>) будує історію позицій зрізами по
    `period_days` днів для top_n найкліковіших значень <key> (запит або сторінка)."""
    totals = defaultdict(lambda: {"clicks": 0, "impressions": 0})
    for r in rows:
        k = r["keys"][key_index]
        totals[k]["clicks"] += r.get("clicks", 0)
        totals[k]["impressions"] += r.get("impressions", 0)
    top_keys = sorted(totals.keys(), key=lambda k: -totals[k]["clicks"])[:top_n]
    top_set = set(top_keys)

    buckets = defaultdict(lambda: defaultdict(lambda: {"clicks": 0, "impressions": 0, "pos_w": 0.0}))
    for r in rows:
        k = r["keys"][key_index]
        if k not in top_set:
            continue
        d = date.fromisoformat(r["keys"][0])
        idx = (d - start).days // period_days
        period_start = str(start + timedelta(days=idx * period_days))
        clicks = r.get("clicks", 0)
        impressions = r.get("impressions", 0)
        b = buckets[k][period_start]
        b["clicks"] += clicks
        b["impressions"] += impressions
        b["pos_w"] += r.get("position", 0) * max(impressions, 1)

    periods_sorted = sorted({p for kd in buckets.values() for p in kd.keys()})
    result = []
    for k in top_keys:
        periods = []
        for p in periods_sorted:
            b = buckets[k].get(p)
            if b and b["impressions"]:
                periods.append({
                    "period_start": p, "clicks": b["clicks"], "impressions": b["impressions"],
                    "position": round(b["pos_w"] / b["impressions"], 1),
                })
            else:
                periods.append({"period_start": p, "clicks": 0, "impressions": 0, "position": None})
        result.append({
            "key": k,
            "total_clicks": totals[k]["clicks"],
            "total_impressions": totals[k]["impressions"],  # аналог "частотності" — реального обсягу пошукового попиту GSC не дає
            "periods": periods,
        })
    return result

def fetch_page_history(service, url, weeks=12, top_n=20, period_days=14):
    """Історія позицій зрізами по 2 тижні для топ-N сторінок за останні `weeks` тижнів."""
    today = date.today()
    end = today - timedelta(days=3)
    start = end - timedelta(days=weeks * 7 - 1)
    rows = query(service, url, start, end, ["date", "page"], row_limit=25000)
    history = _period_history(rows, key_index=1, top_n=top_n, start=start, period_days=period_days)
    return [{"page": h["key"], "total_clicks": h["total_clicks"], "total_impressions": h["total_impressions"], "periods": h["periods"]} for h in history]

def fetch_site(service, site):
    url = site["id"]
    today = date.today()
    # GSC лагает на 3 дня
    end   = today - timedelta(days=3)
    start_28  = end - timedelta(days=27)   # последние 28 дней
    start_12m = end - timedelta(days=364)  # последние 12 месяцев

    print(f"  → Дневные данные {start_28} — {end}")
    daily_rows = query(service, url, start_28, end, ["date"])
    daily = []
    for r in daily_rows:
        daily.append({
            "date":       r["keys"][0],
            "clicks":     r.get("clicks", 0),
            "impressions": r.get("impressions", 0),
            "ctr":        round(r.get("ctr", 0) * 100, 2),
            "position":   round(r.get("position", 0), 1),
        })

    prev_start = start_28 - timedelta(days=28)
    prev_end   = start_28 - timedelta(days=1)
    print(f"  → Попередній період (порівняння) {prev_start} — {prev_end}")
    prev_rows = query(service, url, prev_start, prev_end, [], row_limit=1)  # без dimensions = один рядок-агрегат
    prev_agg = prev_rows[0] if prev_rows else {}
    prev_period = {
        "period":      f"{prev_start} — {prev_end}",
        "clicks":      prev_agg.get("clicks", 0),
        "impressions": prev_agg.get("impressions", 0),
        "ctr":         round(prev_agg.get("ctr", 0) * 100, 2),
        "position":    round(prev_agg.get("position", 0), 1) if prev_agg else None,
    }

    print(f"  → Месячные данные {start_12m} — {end}")
    monthly_rows = query(service, url, start_12m, end, ["date"])
    # группируем по месяцу
    by_month = {}
    for r in monthly_rows:
        m = r["keys"][0][:7]  # YYYY-MM
        if m not in by_month:
            by_month[m] = {"clicks": 0, "impressions": 0, "position_sum": 0, "count": 0}
        by_month[m]["clicks"]       += r.get("clicks", 0)
        by_month[m]["impressions"]  += r.get("impressions", 0)
        by_month[m]["position_sum"] += r.get("position", 0) * r.get("clicks", 1)
        by_month[m]["count"]        += r.get("clicks", 1)
    monthly = []
    for m, v in sorted(by_month.items()):
        monthly.append({
            "month":      m,
            "clicks":     v["clicks"],
            "impressions": v["impressions"],
            "position":   round(v["position_sum"] / v["count"], 1) if v["count"] else 0,
            "ctr":        round(v["clicks"] / v["impressions"] * 100, 2) if v["impressions"] else 0,
        })

    print(f"  → Топ запросы")
    query_rows = query(service, url, start_28, end, ["query"], row_limit=50)
    queries = []
    for r in query_rows:
        queries.append({
            "query":      r["keys"][0],
            "clicks":     r.get("clicks", 0),
            "impressions": r.get("impressions", 0),
            "ctr":        round(r.get("ctr", 0) * 100, 2),
            "position":   round(r.get("position", 0), 1),
        })

    print(f"  → Топ страницы")
    page_rows = query(service, url, start_28, end, ["page"], row_limit=50)
    pages = []
    for r in page_rows:
        pages.append({
            "page":       r["keys"][0],
            "clicks":     r.get("clicks", 0),
            "impressions": r.get("impressions", 0),
            "ctr":        round(r.get("ctr", 0) * 100, 2),
            "position":   round(r.get("position", 0), 1),
        })

    print(f"  → Категорії по неділях")
    categories = fetch_categories(service, url, weeks=12)

    print(f"  → Запити за категоріями (бренд/матраци/топери/дивани/ліжка/шафи) + історія позицій по 2 тижні")
    query_categories = fetch_query_categories(service, url, start_28, end, weeks=12, period_days=14)

    print(f"  → Історія позицій по 2 тижні: топ-сторінки")
    page_history = fetch_page_history(service, url, weeks=12, top_n=20, period_days=14)

    return {
        "site_url":  url,
        "site_name": site["name"],
        "period":    f"{start_28} — {end}",
        "daily":     daily,
        "monthly":   monthly,
        "queries":   queries,
        "pages":     pages,
        "categories": categories,
        "query_categories": query_categories,
        "page_history": page_history,
        "prev_period": prev_period,
    }

# ── Main ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=== GSC Data Fetch ===")
    try:
        service = get_service()
    except Exception as e:
        print(f"Ошибка авторизации: {e}")
        sys.exit(1)

    result = {"generated_at": str(date.today()), "sites": []}
    ok = True
    for site in SITES:
        print(f"\n[{site['name']}]")
        try:
            data = fetch_site(service, site)
            result["sites"].append(data)
            print(f"  ✓ готово: {len(data['daily'])} дней, {len(data['queries'])} запросов")
        except Exception as e:
            print(f"  ✗ ошибка: {e}")
            ok = False

    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n✓ seo_data.json сохранён")
    if not ok:
        sys.exit(1)
