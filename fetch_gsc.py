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

    return {
        "site_url":  url,
        "site_name": site["name"],
        "period":    f"{start_28} — {end}",
        "daily":     daily,
        "monthly":   monthly,
        "queries":   queries,
        "pages":     pages,
        "categories": categories,
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
