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
def query(service, site_url, start, end, dimensions, row_limit=1000):
    body = {
        "startDate": str(start),
        "endDate":   str(end),
        "dimensions": dimensions,
        "rowLimit": row_limit,
        "dataState": "final",
    }
    resp = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
    return resp.get("rows", [])

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

def fetch_query_categories(service, url, start, end, row_limit=10000):
    """Тянет ВСІ пошукові запити за період (не топ-50, як у fetch_site) і групує
    їх за категорією: брендовий трафік / матраци / топери / дивани / ліжка / шафи / інше."""
    rows = query(service, url, start, end, ["query"], row_limit=row_limit)
    buckets = defaultdict(lambda: {"clicks": 0, "impressions": 0, "pos_w": 0.0, "queries": 0})
    cat_names = {}
    for r in rows:
        q_text = r["keys"][0]
        key, name = classify_query(q_text)
        cat_names[key] = name
        clicks = r.get("clicks", 0)
        impressions = r.get("impressions", 0)
        b = buckets[key]
        b["clicks"] += clicks
        b["impressions"] += impressions
        b["pos_w"] += r.get("position", 0) * max(impressions, 1)
        b["queries"] += 1

    total_clicks = sum(b["clicks"] for b in buckets.values()) or 1
    result = []
    for key, b in buckets.items():
        result.append({
            "key": key,
            "name": cat_names[key],
            "clicks": b["clicks"],
            "impressions": b["impressions"],
            "position": round(b["pos_w"] / b["impressions"], 1) if b["impressions"] else None,
            "queries_count": b["queries"],
            "share": round(b["clicks"] / total_clicks * 100, 1),
        })
    result.sort(key=lambda x: -x["clicks"])
    return result

def _weekly_history(rows, key_index, top_n, weeks_span_start_end=None):
    """Спільна логіка: із рядків (date, <key>) будує тижневу історію позицій
    для top_n найкліковіших значень <key> (запит або сторінка)."""
    totals = defaultdict(int)
    for r in rows:
        totals[r["keys"][key_index]] += r.get("clicks", 0)
    top_keys = sorted(totals.keys(), key=lambda k: -totals[k])[:top_n]
    top_set = set(top_keys)

    buckets = defaultdict(lambda: defaultdict(lambda: {"clicks": 0, "impressions": 0, "pos_w": 0.0}))
    for r in rows:
        k = r["keys"][key_index]
        if k not in top_set:
            continue
        d = date.fromisoformat(r["keys"][0])
        week_start = str(d - timedelta(days=d.weekday()))
        clicks = r.get("clicks", 0)
        impressions = r.get("impressions", 0)
        b = buckets[k][week_start]
        b["clicks"] += clicks
        b["impressions"] += impressions
        b["pos_w"] += r.get("position", 0) * max(impressions, 1)

    weeks_sorted = sorted({wk for kd in buckets.values() for wk in kd.keys()})
    result = []
    for k in top_keys:
        weekly = []
        for wk in weeks_sorted:
            b = buckets[k].get(wk)
            if b and b["impressions"]:
                weekly.append({
                    "week_start": wk, "clicks": b["clicks"], "impressions": b["impressions"],
                    "position": round(b["pos_w"] / b["impressions"], 1),
                })
            else:
                weekly.append({"week_start": wk, "clicks": 0, "impressions": 0, "position": None})
        result.append({"key": k, "total_clicks": totals[k], "weekly": weekly})
    return result

def fetch_query_history(service, url, weeks=12, top_n=20):
    """Тижнева історія позицій для топ-N запитів за останні `weeks` тижнів,
    з датою кожного зрізу (week_start = понеділок тижня)."""
    today = date.today()
    end = today - timedelta(days=3)
    start = end - timedelta(days=weeks * 7 - 1)
    rows = query(service, url, start, end, ["date", "query"], row_limit=25000)
    history = _weekly_history(rows, key_index=1, top_n=top_n)
    return [{"query": h["key"], "total_clicks": h["total_clicks"], "weekly": h["weekly"]} for h in history]

def fetch_page_history(service, url, weeks=12, top_n=20):
    """Тижнева історія позицій для топ-N сторінок за останні `weeks` тижнів."""
    today = date.today()
    end = today - timedelta(days=3)
    start = end - timedelta(days=weeks * 7 - 1)
    rows = query(service, url, start, end, ["date", "page"], row_limit=25000)
    history = _weekly_history(rows, key_index=1, top_n=top_n)
    return [{"page": h["key"], "total_clicks": h["total_clicks"], "weekly": h["weekly"]} for h in history]

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

    print(f"  → Запити за категоріями (бренд/матраци/топери/дивани/ліжка/шафи)")
    query_categories = fetch_query_categories(service, url, start_28, end)

    print(f"  → Тижнева історія позицій: топ-запити")
    query_history = fetch_query_history(service, url, weeks=12, top_n=20)

    print(f"  → Тижнева історія позицій: топ-сторінки")
    page_history = fetch_page_history(service, url, weeks=12, top_n=20)

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
        "query_history": query_history,
        "page_history": page_history,
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
