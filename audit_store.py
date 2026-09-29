# -*- coding: utf-8 -*-
"""
audit_store.py — позначки роботи над описами товарів на сторінці аудиту.

Навіщо взагалі позначки, якщо сканер і так перевіряє сайт щоночі.
Сканер бачить лише кінцевий результат: опис на сторінці є або його немає.
Він нічого не знає про проміжок між «текст написали» і «текст залили на
сайт», а саме в цьому проміжку люди й плутаються — картка висить у списку,
і незрозуміло, її ще ніхто не брав чи текст уже готовий і просто чекає.
Тому статусів тут лише два робочі:

    in_progress  «в роботі»       — хтось узяв картку, щоб двоє не писали одне
    ready        «текст готовий»  — написаний і переданий, чекає на сайт

Статусу «зроблено» руками ніхто не ставить: його ставить сам сканер. Коли
опис з'являється на сторінці, картка зникає зі списку проблем, і позначка
автоматично переходить у `done` (див. list_marks). Так галочка не може
збрехати: вона або збігається з тим, що реально на сайті, або сама себе
закриває.

Зворотний бік тієї ж логіки: якщо позначка «текст готовий» стоїть давно, а
сканер картку все ще знаходить — текст десь загубився дорогою. Сторінка таке
підсвічує, і це, власне, головна користь усієї затії.

Сховище — Google Таблиця, той самий файл і той самий сервісний акаунт, що й
у FAQ (faq_store.py), просто інша вкладка. Диск на Render одноразовий, і все
записане під час роботи зникає при найближчому передеплої.

Код підключення до Sheets тут навмисно свій, а не імпортований із
faq_store.py: FAQ працює, і чіпати робочий модуль заради економії двадцяти
рядків — поганий обмін.

Змінні середовища:
    FAQ_SHEET_ID         id таблиці (та сама, що для FAQ)
    AUDIT_SHEET_TAB      назва вкладки, за замовчуванням "AUDIT"
    GSC_SERVICE_ACCOUNT_JSON  ключ сервісного акаунта
"""
import json
import os
import threading
from datetime import date, datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
LOCAL_FILE = BASE_DIR / "audit_marks_local.json"

SHEET_ID = os.environ.get("FAQ_SHEET_ID", "").strip()
SHEET_TAB = os.environ.get("AUDIT_SHEET_TAB", "AUDIT").strip() or "AUDIT"
KEY_ENV = os.environ.get("GSC_SERVICE_ACCOUNT_JSON", "")
KEY_FILE = BASE_DIR / "gsc_key.json"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
HEADER = ["url", "status", "author", "updated_at", "done_at", "note"]

# Скільки днів позначка «текст готовий» може висіти, поки сканер усе ще
# бачить проблему, перш ніж вважати, що текст загубився. Сім днів — це вже
# точно не «завтра заллємо».
LOST_AFTER_DAYS = 7

VALID_STATUSES = ("in_progress", "ready")


class StorageError(Exception):
    """Не вдалося записати в таблицю. Піднімаємо нагору, щоб людина побачила
    причину, а не вирішила, що позначка збереглась."""


def _readable(e):
    text = str(e)
    if "403" in text or "PERMISSION_DENIED" in text or "insufficient" in text.lower():
        return ("Таблиця відкрита лише для читання. Дайте сервісному акаунту "
                "доступ «Редактор» до неї.")
    if "404" in text or "notFound" in text:
        return "Таблицю не знайдено: перевірте FAQ_SHEET_ID у налаштуваннях сервера."
    return f"Помилка запису в таблицю: {type(e).__name__}"


_lock = threading.RLock()
_service = None
_service_error = ""
_tab_ready = False


# ── Google Sheets ─────────────────────────────────────────────────────────
def _credentials_info():
    if KEY_ENV:
        return json.loads(KEY_ENV)
    if KEY_FILE.exists():
        return json.loads(KEY_FILE.read_text(encoding="utf-8"))
    return None


def _get_service():
    """Клієнт Sheets або None. Помилку не піднімаємо: сторінка аудиту має
    відкриватись і без таблиці — позначки це надбудова, а не основа."""
    global _service, _service_error
    if _service is not None:
        return _service
    if not SHEET_ID:
        _service_error = "FAQ_SHEET_ID не задано"
        return None
    info = _credentials_info()
    if not info:
        _service_error = "немає ключа сервісного акаунта"
        return None
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        _service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        return _service
    except Exception as e:
        _service_error = f"{type(e).__name__}: {e}"
        return None


def _ensure_tab(svc):
    """Створюємо вкладку, якщо її немає.

    З FAQ ми вже маємо цей урок: там назва вкладки стояла жорстко, реальна
    вкладка називалась інакше, і кожен запис падав з «Unable to parse range»,
    а ззовні це виглядало просто як «нічого не зберігається». Тут не вгадуємо
    назву, а заводимо вкладку самі, якщо її ще нема.
    """
    global _tab_ready
    if _tab_ready:
        return
    meta = svc.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    titles = [sh["properties"]["title"] for sh in meta.get("sheets", [])]
    if SHEET_TAB not in titles:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": SHEET_TAB}}}]}).execute()
        svc.spreadsheets().values().update(
            spreadsheetId=SHEET_ID, range=f"{SHEET_TAB}!A1:F1",
            valueInputOption="RAW", body={"values": [HEADER]}).execute()
    _tab_ready = True


def _read_rows(svc):
    _ensure_tab(svc)
    resp = svc.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"{SHEET_TAB}!A1:F20000").execute()
    return resp.get("values", [])


def _write_all(svc, marks):
    """Переписуємо вкладку цілком.

    Позначок тут сотні, не десятки тисяч, а цілісний перезапис рятує від
    розсинхрону номера рядка й позиції запису — на цьому вже спіткнувся FAQ,
    де видалення рядка зсувало все, що нижче.
    """
    _ensure_tab(svc)
    rows = [HEADER] + [[m["url"], m["status"], m.get("author", ""),
                        m.get("updated_at", ""), m.get("done_at", ""),
                        m.get("note", "")] for m in marks.values()]
    svc.spreadsheets().values().clear(
        spreadsheetId=SHEET_ID, range=f"{SHEET_TAB}!A1:F20000", body={}).execute()
    svc.spreadsheets().values().update(
        spreadsheetId=SHEET_ID, range=f"{SHEET_TAB}!A1",
        valueInputOption="RAW", body={"values": rows}).execute()


def _rows_to_marks(rows):
    if not rows:
        return {}
    body = rows[1:] if rows and rows[0] and str(rows[0][0]).strip() == "url" else rows
    marks = {}
    for r in body:
        r = list(r) + [""] * (len(HEADER) - len(r))
        url = str(r[0]).strip()
        if not url:
            continue
        marks[url] = {"url": url, "status": str(r[1]).strip(),
                      "author": r[2], "updated_at": r[3],
                      "done_at": r[4], "note": r[5]}
    return marks


# ── Локальний запасний файл ───────────────────────────────────────────────
def _local_read():
    if not LOCAL_FILE.exists():
        return {}
    try:
        return json.loads(LOCAL_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _local_write(marks):
    tmp = LOCAL_FILE.with_name(LOCAL_FILE.name + ".tmp")
    tmp.write_text(json.dumps(marks, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, LOCAL_FILE)


# ── Публічне API ──────────────────────────────────────────────────────────
def status():
    svc = _get_service()
    return {
        "storage": "sheet" if svc else "local",
        # Останню помилку показуємо навіть тоді, коли клієнт створився —
        # саме цей випадок у FAQ виглядав як «все гаразд, але нічого не
        # зберігається».
        "detail": _service_error,
        "tab": SHEET_TAB,
        "lost_after_days": LOST_AFTER_DAYS,
        "sheet_url": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit" if (svc and SHEET_ID) else "",
    }


def _days_since(stamp):
    """Скільки днів минуло від позначки. Порожня або крива дата — 0, бо
    вигадувати прострочення на порожньому місці гірше, ніж не показати його."""
    try:
        return (date.today() - datetime.strptime(str(stamp)[:10], "%Y-%m-%d").date()).days
    except Exception:
        return 0


def list_marks(current_urls=None):
    """Усі позначки. Якщо передати адреси, які сканер ЗАРАЗ вважає
    проблемними, позначки на все інше автоматично закриваються: сканер більше
    не бачить проблеми, отже опис на сайті з'явився.

    `current_urls` = None означає «список проблем невідомий» (файл аудиту ще
    не прочитався) — тоді нічого не закриваємо, щоб через тимчасовий збій не
    оголосити зробленим те, що не зроблено.
    """
    with _lock:
        svc = _get_service()
        try:
            marks = _rows_to_marks(_read_rows(svc)) if svc else _local_read()
        except Exception as e:
            global _service_error
            _service_error = _readable(e)
            marks = _local_read()
            svc = None

        changed = False
        if current_urls is not None:
            today = str(date.today())
            for url, m in marks.items():
                if m["status"] in VALID_STATUSES and url not in current_urls:
                    m["status"] = "done"
                    m["done_at"] = today
                    changed = True

        for m in marks.values():
            m["days"] = _days_since(m.get("updated_at"))
            m["lost"] = bool(m["status"] == "ready" and m["days"] >= LOST_AFTER_DAYS)

        if changed:
            try:
                if svc:
                    _write_all(svc, marks)
                else:
                    _local_write(marks)
            except Exception as e:
                # Закриття позначок — приємний бонус, а не те, заради чого
                # людина відкрила сторінку. Не вдалось записати — покажемо
                # актуальну картину й спробуємо наступного разу.
                _service_error = _readable(e)
        return marks


def set_mark(url, status_value, author="", note=""):
    if status_value not in VALID_STATUSES:
        raise ValueError(f"невідомий статус: {status_value}")
    with _lock:
        svc = _get_service()
        try:
            marks = _rows_to_marks(_read_rows(svc)) if svc else _local_read()
        except Exception as e:
            raise StorageError(_readable(e)) from e
        marks[url] = {"url": url, "status": status_value,
                      "author": (author or "").strip(),
                      "updated_at": str(date.today()), "done_at": "",
                      "note": (note or "").strip()}
        try:
            if svc:
                _write_all(svc, marks)
            else:
                _local_write(marks)
        except Exception as e:
            global _service_error
            _service_error = f"{type(e).__name__}: {e}"
            raise StorageError(_readable(e)) from e
        m = marks[url]
        m["days"] = 0
        m["lost"] = False
        return m


def clear_mark(url):
    with _lock:
        svc = _get_service()
        try:
            marks = _rows_to_marks(_read_rows(svc)) if svc else _local_read()
        except Exception as e:
            raise StorageError(_readable(e)) from e
        if url not in marks:
            return False
        del marks[url]
        try:
            if svc:
                _write_all(svc, marks)
            else:
                _local_write(marks)
        except Exception as e:
            global _service_error
            _service_error = f"{type(e).__name__}: {e}"
            raise StorageError(_readable(e)) from e
        return True
