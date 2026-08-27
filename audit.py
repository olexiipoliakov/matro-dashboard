"""
audit.py — аудит карток товарів на matroluxe.ua.

Що робить:
  1. Бере /ua/sitemap.xml (український основний — міські sitemap'и дублюють
     ті самі товари під префіксом міста, тому їх свідомо не чіпаємо).
  2. Викидає блог і службові сторінки, обходить решту й на кожній сторінці
     визначає: це картка товару чи категорія/стаття.
  3. Для карток товару перевіряє чотири речі:
       no_description    — вкладки «Опис» немає або вона порожня
       short_description — опис є, але коротший за SHORT_LIMIT символів
       no_photo          — фото немає або стоїть заглушка
       page_error        — сторінка є в sitemap, але віддає 404/500/таймаут
  4. Пише audit_data.json (результат) і audit_progress.json (прогрес,
     який читає /api/audit/status, щоб показувати «150 з 362» на сторінці).

Окремий режим для перевірки самих правил на одній сторінці:
    python3 audit.py --probe https://matroluxe.ua/ua/matras-arlon
Він друкує, який селектор спрацював і що саме знайшлось — зручно, коли
верстка на сайті зміниться і треба зрозуміти, чому аудит став брехати.
"""
import json
import os
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).parent
OUT_FILE = BASE_DIR / "audit_data.json"
PROGRESS_FILE = BASE_DIR / "audit_progress.json"

# Через змінні середовища, щоб можна було прогнати аудит на тестовому
# майданчику, не чіпаючи бойовий сайт.
SITE = os.environ.get("AUDIT_SITE", "https://matroluxe.ua").rstrip("/")
SITEMAP = os.environ.get("AUDIT_SITEMAP", f"{SITE}/ua/sitemap.xml")

# Опис коротший за це вважаємо «відпискою в один рядок».
SHORT_LIMIT = int(os.environ.get("AUDIT_SHORT_LIMIT", "300"))
# Менше цього — вважаємо, що опису фактично немає (лишились крихти розмітки).
EMPTY_LIMIT = 40

WORKERS = int(os.environ.get("AUDIT_WORKERS", "4"))
DELAY = float(os.environ.get("AUDIT_DELAY", "0.35"))   # пауза між запитами в одному потоці
TIMEOUT = 20

UA = "Mozilla/5.0 (compatible; MatroDashboardAudit/1.0; +https://matroluxe.ua)"

ISSUE_TYPES = [
    {"key": "page_error",        "label": "Сторінка не відкривається", "color": "danger"},
    {"key": "no_description",    "label": "Без опису",                 "color": "danger"},
    {"key": "short_description", "label": "Короткий опис",             "color": "warning"},
    {"key": "no_photo",          "label": "Без фото",                  "color": "info"},
]

# Сторінки, які не є товарами й не мають потрапляти в аудит.
SKIP_PATTERNS = (
    "/blog", "/news", "/about", "/contact", "/delivery", "/payment",
    "/warranty", "/oplata", "/dostavka", "/kontakty", "/pro-nas",
    "/index.php", "/search", "/login", "/cart", "/checkout", "/sitemap",
)

PLACEHOLDER_IMG = ("no_image", "noimage", "no-image", "placeholder", "default.png")


# ── прогрес ──────────────────────────────────────────────────────────────
_lock = threading.Lock()
_done = 0
_total = 0
_last_write = 0.0


def write_progress(phase, force=False):
    """Пише прогрес на диск не частіше разу на 1.5с, щоб не смикати диск
    на кожній з сотень сторінок."""
    global _last_write
    now = time.time()
    if not force and now - _last_write < 1.5:
        return
    _last_write = now
    try:
        PROGRESS_FILE.write_text(json.dumps({
            "phase": phase, "done": _done, "total": _total,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# ── збір адрес ───────────────────────────────────────────────────────────
def fetch_sitemap_urls(session):
    r = session.get(SITEMAP, timeout=TIMEOUT)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    # namespace у sitemap'ах стандартний, але буває, що його немає —
    # тому шукаємо по локальному імені тега, а не по повному.
    urls = [el.text.strip() for el in root.iter()
            if el.tag.split("}")[-1] == "loc" and el.text]

    out = []
    for u in urls:
        path = u.replace(SITE, "").rstrip("/")
        if not path.startswith("/ua/"):
            continue
        slug = path[len("/ua/"):]
        if not slug or "/" in slug:          # категорії другого рівня і блог
            continue
        if any(p.strip("/") == slug or slug.startswith(p.strip("/") + "-")
               for p in SKIP_PATTERNS):
            continue
        out.append(u)
    return sorted(set(out))


# ── розбір сторінки ──────────────────────────────────────────────────────
def find_description(soup):
    """Повертає (текст, назва_стратегії). Пробуємо кілька варіантів верстки,
    бо OpenCart по-різному називає вкладку залежно від теми й версії."""
    candidates = []

    el = soup.select_one("#tab-description")
    if el:
        candidates.append((el, "#tab-description"))

    for a in soup.select('a[data-toggle="tab"], a[data-bs-toggle="tab"]'):
        if a.get_text(strip=True).lower() in ("опис", "описание", "description"):
            href = (a.get("href") or "").lstrip("#")
            tgt = soup.find(id=href) if href else None
            if tgt:
                candidates.append((tgt, f"tab «{a.get_text(strip=True)}»"))

    el = soup.select_one('[itemprop="description"]')
    if el:
        candidates.append((el, "itemprop=description"))

    for el in soup.find_all(id=re.compile("description", re.I)):
        candidates.append((el, f"id~{el.get('id')}"))

    for el, how in candidates:
        for junk in el.find_all(["script", "style", "noscript"]):
            junk.decompose()
        text = el.get_text(" ", strip=True)
        if len(text) >= EMPTY_LIMIT:
            return text, how
    if candidates:
        return "", candidates[0][1] + " (порожній)"
    return None, None


def has_photo(soup):
    sels = ['[itemprop="image"]', ".thumbnails img", "#product img",
            ".product-image img", ".product-images img", "a.thumbnail img",
            'meta[property="og:image"]']
    for s in sels:
        for el in soup.select(s):
            src = el.get("content") or el.get("src") or el.get("data-src") or ""
            if src and not any(p in src.lower() for p in PLACEHOLDER_IMG):
                return True
    return False


def is_product(soup):
    if soup.select_one("#button-cart, [id^=button-cart]"):
        return True
    if soup.select_one('input[name="product_id"]'):
        return True
    og = soup.select_one('meta[property="og:type"]')
    if og and "product" in (og.get("content") or "").lower():
        return True
    if soup.select_one('[itemprop="price"], [itemprop="offers"]'):
        return True
    return False


def product_name(soup, url):
    h1 = soup.select_one("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)
    og = soup.select_one('meta[property="og:title"]')
    if og and og.get("content"):
        return og["content"].strip()
    return url.rstrip("/").split("/")[-1]


def product_category(soup):
    """Категорія — передостанній пункт хлібних крихт (останній — сам товар)."""
    crumbs = [a.get_text(strip=True) for a in soup.select(".breadcrumb a, nav[aria-label] a")
              if a.get_text(strip=True)]
    crumbs = [c for c in crumbs if c.lower() not in ("головна", "главная", "home")]
    if len(crumbs) >= 2:
        return crumbs[-1]
    return crumbs[0] if crumbs else "—"


def check_url(session, url):
    """Повертає (це_товар, список_знахідок) по одній сторінці."""
    try:
        r = session.get(url, timeout=TIMEOUT)
    except Exception:
        try:
            time.sleep(1.0)
            r = session.get(url, timeout=TIMEOUT)
        except Exception as e:
            return True, [{"name": url.rstrip("/").split("/")[-1], "url": url,
                           "category": "—", "issue": "page_error",
                           "detail": f"немає відповіді: {type(e).__name__}"}]

    if r.status_code >= 400:
        return True, [{"name": url.rstrip("/").split("/")[-1], "url": url,
                       "category": "—", "issue": "page_error",
                       "detail": f"HTTP {r.status_code}"}]

    soup = BeautifulSoup(r.text, "html.parser")
    if not is_product(soup):
        return False, []               # категорія або стаття — не наша справа

    name = product_name(soup, url)
    cat = product_category(soup)
    found = []

    text, _ = find_description(soup)
    if not text:
        found.append({"name": name, "url": url, "category": cat,
                      "issue": "no_description", "detail": ""})
    elif len(text) < SHORT_LIMIT:
        found.append({"name": name, "url": url, "category": cat,
                      "issue": "short_description",
                      "detail": f"{len(text)} символів"})

    if not has_photo(soup):
        found.append({"name": name, "url": url, "category": cat,
                      "issue": "no_photo", "detail": ""})

    return True, found


# ── прогін ───────────────────────────────────────────────────────────────
def make_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "uk,ru;q=0.8"})
    return s


def scan():
    global _done, _total
    started = time.time()
    write_progress("Читаю sitemap", force=True)

    session = make_session()
    urls = fetch_sitemap_urls(session)
    _total = len(urls)
    print(f"[audit] до перевірки {_total} сторінок", flush=True)
    write_progress("Перевіряю сторінки", force=True)

    items, errors, products = [], 0, 0
    local = threading.local()

    def worker(url):
        global _done
        if not hasattr(local, "session"):
            local.session = make_session()
        res = check_url(local.session, url)
        time.sleep(DELAY)
        with _lock:
            _done += 1
            write_progress("Перевіряю сторінки")
        return res

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for was_product, res in pool.map(worker, urls):
            if was_product:
                products += 1
            for row in res:
                if row["issue"] == "page_error":
                    errors += 1
                items.append(row)

    # Рахуємо тільки сторінки, які виявились картками товару. Категорії й
    # статті з знаменника викидаємо — інакше «здоров'я каталогу» вийде
    # завищеним за рахунок сторінок, які ми й не перевіряли.
    checked = products
    counts = {t["key"]: 0 for t in ISSUE_TYPES}
    for row in items:
        counts[row["issue"]] = counts.get(row["issue"], 0) + 1

    data = {
        "demo": False,
        "generated_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "duration_sec": round(time.time() - started, 1),
        "total_checked": checked,
        "total_errors": errors,
        "issue_types": [dict(t, count=counts.get(t["key"], 0)) for t in ISSUE_TYPES
                        if counts.get(t["key"], 0) > 0],
        "items": items,
    }
    return data


if __name__ == "__main__":
    if "--probe" in sys.argv:
        url = sys.argv[sys.argv.index("--probe") + 1]
        s = make_session()
        r = s.get(url, timeout=TIMEOUT)
        soup = BeautifulSoup(r.text, "html.parser")
        text, how = find_description(soup)
        print(f"HTTP           : {r.status_code}")
        print(f"це товар       : {is_product(soup)}")
        print(f"назва          : {product_name(soup, url)}")
        print(f"категорія      : {product_category(soup)}")
        print(f"опис знайдено  : {how or 'НІ — жодна стратегія не спрацювала'}")
        print(f"довжина опису  : {len(text) if text else 0} символів "
              f"(поріг короткого — {SHORT_LIMIT})")
        print(f"фото           : {'є' if has_photo(soup) else 'НЕМАЄ'}")
        if text:
            print(f"початок опису  : {text[:160]}…")
        sys.exit(0)

    try:
        data = scan()
        OUT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        write_progress("Готово", force=True)
        print(f"[audit] {data['total_checked']} сторінок, "
              f"{len(data['items'])} знахідок, {data['total_errors']} помилок, "
              f"{data['duration_sec']}с", flush=True)
    except Exception as e:
        write_progress(f"Помилка: {e}", force=True)
        print(f"[audit] критична помилка: {e}", flush=True)
        raise
