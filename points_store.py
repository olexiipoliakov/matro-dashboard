# -*- coding: utf-8 -*-
"""
points_store.py — налаштування нарахування балів менеджерам.

Три числа: скільки балів за успішний дзвінок, який крок суми аксесуарів і
скільки балів за кожен повний крок. Їх задає бізнес, а не код, тому вони
лежать окремо від скриптів.

Чому Google Таблиця, а не localStorage у браузері. Бали — спільний рахунок
конкурсу. Якщо ваги зберігати в браузері, у кожного менеджера будуть свої
коефіцієнти й свої бали за той самий день; перший же спір про те, хто
попереду, закінчиться недовірою до всього модуля. Таблиця одна на всіх, і
змінену вагу одразу бачать усі.

Та сама таблиця, що для FAQ (FAQ_SHEET_ID), окрема вкладка. Вкладку модуль
створює сам при першому зверненні — урок із FAQ, де жорстко задана назва
вкладки падала з «Unable to parse range», а ззовні це виглядало просто як
«нічого не зберігається».
"""
import json
import os
import threading
from pathlib import Path

BASE_DIR = Path(__file__).parent
LOCAL_FILE = BASE_DIR / "points_settings_local.json"

SHEET_ID = os.environ.get("FAQ_SHEET_ID", "").strip()
SHEET_TAB = os.environ.get("POINTS_SHEET_TAB", "POINTS").strip() or "POINTS"
KEY_ENV = os.environ.get("GSC_SERVICE_ACCOUNT_JSON", "")
KEY_FILE = BASE_DIR / "gsc_key.json"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
HEADER = ["key", "value"]

# Значення за замовчуванням — із ТЗ. Апрув пріоритетніший за аксесуари, тому
# вага дзвінка вища.
DEFAULTS = {"w_call": 2.0, "step_grn": 500.0, "w_acc": 1.0}

# Межі розумного. Нуль у кроці — ділення на нуль; від'ємна вага — бали за
# те, що менеджер працював. Обидва варіанти хтось рано чи пізно впише.
LIMITS = {"w_call": (0, 1000), "step_grn": (1, 1000000), "w_acc": (0, 1000)}


class StorageError(Exception):
    """Не вдалося записати. Піднімаємо нагору, щоб людина побачила причину."""


_lock = threading.RLock()
_service = None
_service_error = ""
_tab_ready = False


def _credentials_info():
    if KEY_ENV:
        return json.loads(KEY_ENV)
    if KEY_FILE.exists():
        return json.loads(KEY_FILE.read_text(encoding="utf-8"))
    return None


def _get_service():
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
            spreadsheetId=SHEET_ID, range=f"{SHEET_TAB}!A1:B1",
            valueInputOption="RAW", body={"values": [HEADER]}).execute()
    _tab_ready = True


def _clean(raw):
    """Лишаємо тільки відомі ключі, числа і тільки в межах розумного.

    Все, що не вкладається, мовчки замінюємо на значення за замовчуванням:
    зіпсована комірка в таблиці не повинна обвалити сторінку з балами.
    """
    out = dict(DEFAULTS)
    for k, default in DEFAULTS.items():
        try:
            v = float(str(raw.get(k, default)).replace(",", "."))
        except Exception:
            continue
        lo, hi = LIMITS[k]
        if lo <= v <= hi:
            out[k] = v
    return out


def get_settings():
    with _lock:
        svc = _get_service()
        if not svc:
            if LOCAL_FILE.exists():
                try:
                    return _clean(json.loads(LOCAL_FILE.read_text(encoding="utf-8")))
                except Exception:
                    pass
            return dict(DEFAULTS)
        try:
            _ensure_tab(svc)
            rows = svc.spreadsheets().values().get(
                spreadsheetId=SHEET_ID, range=f"{SHEET_TAB}!A1:B50").execute().get("values", [])
            raw = {}
            for r in rows:
                if len(r) >= 2 and str(r[0]).strip() != "key":
                    raw[str(r[0]).strip()] = r[1]
            return _clean(raw)
        except Exception as e:
            global _service_error
            _service_error = f"{type(e).__name__}: {e}"
            return dict(DEFAULTS)


def save_settings(values):
    cleaned = _clean(values or {})
    with _lock:
        svc = _get_service()
        rows = [HEADER] + [[k, cleaned[k]] for k in DEFAULTS]
        if not svc:
            tmp = LOCAL_FILE.with_name(LOCAL_FILE.name + ".tmp")
            tmp.write_text(json.dumps(cleaned, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, LOCAL_FILE)
            return cleaned
        try:
            _ensure_tab(svc)
            svc.spreadsheets().values().clear(
                spreadsheetId=SHEET_ID, range=f"{SHEET_TAB}!A1:B50", body={}).execute()
            svc.spreadsheets().values().update(
                spreadsheetId=SHEET_ID, range=f"{SHEET_TAB}!A1",
                valueInputOption="RAW", body={"values": rows}).execute()
            return cleaned
        except Exception as e:
            global _service_error
            _service_error = f"{type(e).__name__}: {e}"
            text = str(e)
            if "403" in text or "PERMISSION_DENIED" in text:
                raise StorageError("Таблиця відкрита лише для читання. Дайте сервісному "
                                   "акаунту доступ «Редактор».") from e
            if "404" in text:
                raise StorageError("Таблицю не знайдено: перевірте FAQ_SHEET_ID.") from e
            raise StorageError(f"Помилка запису в таблицю: {type(e).__name__}") from e


def status():
    svc = _get_service()
    return {
        "storage": "sheet" if svc else "local",
        "detail": _service_error,
        "tab": SHEET_TAB,
        "defaults": DEFAULTS,
    }
