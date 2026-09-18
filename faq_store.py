# -*- coding: utf-8 -*-
"""
faq_store.py — сховище правил (FAQ) для сторінки менеджерів.

Чому Google Таблиця, а не файл на диску: диск на Render одноразовий, і все,
що сервер записав під час роботи, зникає при наступному передеплої. Правило,
яке менеджер додав уранці, після вечірнього оновлення коду просто зникло б.
Таблиця живе окремо від сервера, її видно людям і в ній можна правити руками.

Ключ беремо той самий, що для Search Console (GSC_SERVICE_ACCOUNT_JSON) —
нових токенів заводити не треба. Потрібно лише:
  1) увімкнути Google Sheets API в тому ж проєкті Cloud;
  2) дати сервісному акаунту доступ "Редактор" до таблиці;
  3) покласти її ID у змінну середовища FAQ_SHEET_ID.

Поки цього немає, сховище працює на локальному файлі й чесно повідомляє
сторінці, що записи тимчасові (`storage: "local"`), щоб ніхто не вважав,
ніби правило збережено назавжди.
"""
import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
LOCAL_FILE = BASE_DIR / "faq_local.json"

SHEET_ID = os.environ.get("FAQ_SHEET_ID", "").strip()
SHEET_TAB = os.environ.get("FAQ_SHEET_TAB", "FAQ").strip() or "FAQ"
KEY_ENV = os.environ.get("GSC_SERVICE_ACCOUNT_JSON", "")
KEY_FILE = BASE_DIR / "gsc_key.json"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
HEADER = ["id", "created_at", "updated_at", "author", "question", "answer"]

class StorageError(Exception):
    """Не вдалося записати в таблицю. Піднімаємо нагору, щоб людина побачила
    причину, а не вирішила, що правило збережено."""


def _readable(e):
    """Перекладаємо типові відповіді Google у зрозумілу фразу."""
    text = str(e)
    if "403" in text or "PERMISSION_DENIED" in text or "insufficient" in text.lower():
        return ("Таблиця відкрита лише для читання. Дайте сервісному акаунту "
                "доступ «Редактор» до неї — зараз він може читати, але не писати.")
    if "404" in text or "notFound" in text:
        return "Таблицю не знайдено: перевірте FAQ_SHEET_ID у налаштуваннях сервера."
    if "Unable to parse range" in text or "not found" in text.lower():
        return f"Немає вкладки «{SHEET_TAB}» у таблиці — створіть її або змініть FAQ_SHEET_TAB."
    return f"Помилка запису в таблицю: {type(e).__name__}"

_lock = threading.Lock()
_service = None
_service_error = ""


# ── Google Sheets ─────────────────────────────────────────────────────────
def _credentials_info():
    if KEY_ENV:
        return json.loads(KEY_ENV)
    if KEY_FILE.exists():
        return json.loads(KEY_FILE.read_text(encoding="utf-8"))
    return None


def _get_service():
    """Повертає клієнт Sheets або None. Помилку не піднімаємо: сторінка має
    відкриватись навіть тоді, коли таблиця ще не налаштована."""
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


_tab_cache = None


def _tab_name(svc):
    """Реальна назва вкладки в таблиці.

    Раніше тут жорстко стояло "FAQ", і в щойно створеній таблиці, де єдина
    вкладка називається «Аркуш1», кожне звернення падало з «Unable to parse
    range». Ззовні це виглядало як «правила не зберігаються» без жодної
    підказки. Тепер беремо вкладку FAQ, якщо вона є, інакше — першу.
    """
    global _tab_cache
    if _tab_cache:
        return _tab_cache
    meta = svc.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    titles = [sh["properties"]["title"] for sh in meta.get("sheets", [])]
    _tab_cache = SHEET_TAB if SHEET_TAB in titles else (titles[0] if titles else SHEET_TAB)
    return _tab_cache


def _sheet_values():
    svc = _get_service()
    if not svc:
        return None
    resp = svc.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"{_tab_name(svc)}!A1:F10000").execute()
    return resp.get("values", [])


def _ensure_header(rows):
    """Перший запуск на порожній таблиці: дописуємо шапку."""
    if rows:
        return
    svc = _get_service()
    if not svc:
        return
    svc.spreadsheets().values().update(
        spreadsheetId=SHEET_ID, range=f"{_tab_name(svc)}!A1:F1",
        valueInputOption="RAW", body={"values": [HEADER]}).execute()


def _rows_to_items(rows):
    if not rows:
        return []
    body = rows[1:] if rows and rows[0] and str(rows[0][0]).strip() == "id" else rows
    items = []
    for r in body:
        r = list(r) + [""] * (len(HEADER) - len(r))
        if not str(r[0]).strip():
            continue
        items.append({
            "id": str(r[0]).strip(),
            "created_at": r[1], "updated_at": r[2],
            "author": r[3], "question": r[4], "answer": r[5],
        })
    return items


def _item_to_row(it):
    return [it["id"], it.get("created_at", ""), it.get("updated_at", ""),
            it.get("author", ""), it.get("question", ""), it.get("answer", "")]


# ── Локальний запасний файл ───────────────────────────────────────────────
def _local_read():
    if not LOCAL_FILE.exists():
        return []
    try:
        return json.loads(LOCAL_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _local_write(items):
    tmp = LOCAL_FILE.with_name(LOCAL_FILE.name + ".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, LOCAL_FILE)


# ── Публічне API ──────────────────────────────────────────────────────────
def status():
    svc = _get_service()
    return {
        "storage": "sheet" if svc else "local",
        # Показуємо останню помилку навіть тоді, коли клієнт до таблиці
        # створився: саме цей випадок і виглядав як «все гаразд, але нічого
        # не зберігається».
        "detail": _service_error,
        "tab": _tab_cache or SHEET_TAB,
        "sheet_url": f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit" if (svc and SHEET_ID) else "",
    }


def list_items():
    with _lock:
        svc = _get_service()
        if not svc:
            return _local_read()
        try:
            rows = _sheet_values()
            _ensure_header(rows)
            return _rows_to_items(rows)
        except Exception as e:
            # Таблиця могла стати недоступною (забрали доступ, збій API).
            # Віддаємо те, що є локально, щоб сторінка не падала, але помилку
            # більше не ховаємо — вона поїде у відповідь разом зі списком.
            global _service_error
            _service_error = _readable(e)
            return _local_read()


def add_item(author, question, answer):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    item = {"id": uuid.uuid4().hex[:12], "created_at": now, "updated_at": now,
            "author": (author or "").strip(), "question": (question or "").strip(),
            "answer": (answer or "").strip()}
    with _lock:
        svc = _get_service()
        if svc:
            try:
                _ensure_header(_sheet_values())
                svc.spreadsheets().values().append(
                    spreadsheetId=SHEET_ID, range=f"{_tab_name(svc)}!A1:F1",
                    valueInputOption="RAW", insertDataOption="INSERT_ROWS",
                    body={"values": [_item_to_row(item)]}).execute()
                return item
            except Exception as e:
                global _service_error
                _service_error = f"{type(e).__name__}: {e}"
                raise StorageError(_readable(e)) from e
        items = _local_read()
        items.append(item)
        _local_write(items)
        return item


def update_item(item_id, question=None, answer=None, author=None):
    with _lock:
        svc = _get_service()
        if svc:
            try:
                rows = _sheet_values() or []
                items = _rows_to_items(rows)
                for idx, it in enumerate(items):
                    if it["id"] != item_id:
                        continue
                    if question is not None:
                        it["question"] = question.strip()
                    if answer is not None:
                        it["answer"] = answer.strip()
                    if author is not None:
                        it["author"] = author.strip()
                    it["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                    # +2: рядок 1 — шапка, нумерація в таблиці з одиниці
                    svc.spreadsheets().values().update(
                        spreadsheetId=SHEET_ID,
                        range=f"{_tab_name(svc)}!A{idx + 2}:F{idx + 2}",
                        valueInputOption="RAW",
                        body={"values": [_item_to_row(it)]}).execute()
                    return it
                return None
            except Exception as e:
                global _service_error
                _service_error = f"{type(e).__name__}: {e}"
                raise StorageError(_readable(e)) from e
        items = _local_read()
        for it in items:
            if it["id"] == item_id:
                if question is not None:
                    it["question"] = question.strip()
                if answer is not None:
                    it["answer"] = answer.strip()
                if author is not None:
                    it["author"] = author.strip()
                it["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                _local_write(items)
                return it
        return None


def delete_item(item_id):
    """Видаляємо рядок фізично: інакше в таблиці накопичуються порожні рядки,
    і номер рядка перестає збігатися з позицією запису при оновленні."""
    with _lock:
        svc = _get_service()
        if svc:
            try:
                rows = _sheet_values() or []
                items = _rows_to_items(rows)
                for idx, it in enumerate(items):
                    if it["id"] != item_id:
                        continue
                    meta = svc.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
                    sheet_id = None
                    for sh in meta.get("sheets", []):
                        if sh["properties"]["title"] == _tab_name(svc):
                            sheet_id = sh["properties"]["sheetId"]
                            break
                    if sheet_id is None:
                        return False
                    svc.spreadsheets().batchUpdate(
                        spreadsheetId=SHEET_ID,
                        body={"requests": [{"deleteDimension": {"range": {
                            "sheetId": sheet_id, "dimension": "ROWS",
                            "startIndex": idx + 1, "endIndex": idx + 2}}}]}).execute()
                    return True
                return False
            except Exception as e:
                global _service_error
                _service_error = f"{type(e).__name__}: {e}"
                raise StorageError(_readable(e)) from e
        items = _local_read()
        rest = [i for i in items if i["id"] != item_id]
        if len(rest) == len(items):
            return False
        _local_write(rest)
        return True
