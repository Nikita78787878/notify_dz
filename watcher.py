# -*- coding: utf-8 -*-
"""
ProductStar homework watcher.

Опрашивает страницу/запрос личного кабинета, и как только появляется
новое домашнее задание — присылает уведомление в Telegram.

Запуск:  python watcher.py
Настройки — в файле config.json (рядом со скриптом).
Запрос к сайту берётся из файла request.curl (см. README).
"""

import json
import re
import time
import shlex
import html
import hashlib
import os
import sys
import threading
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone, timedelta

import requests

# Иркутское время — UTC+8, круглый год (без переходов на летнее время)
IRKUTSK_TZ = timezone(timedelta(hours=8))

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
CURL_PATH = os.path.join(HERE, "request.curl")
STATE_PATH = os.path.join(HERE, "seen.json")
RAWDUMP_PATH = os.path.join(HERE, "last_response.txt")
COOKIES_PATH = os.path.join(HERE, "cookies.json")
LOG_PATH = os.path.join(HERE, "watcher.log")

# Единая HTTP-сессия: сама подхватывает свежие куки из ответов сайта
# (за счёт remember_web ручная замена нужна очень редко).
SESSION = requests.Session()

# Текущий запрос к сайту (обновляется при замене кук через бота).
STATE = {"url": None, "method": "GET", "headers": {}, "data": None}
STATE_LOCK = threading.Lock()

# --- Логирование: в файл с ротацией (5 × 10 МБ) + в консоль/journald ---
def _irkutsk_time(timestamp):
    """Конвертер времени для логов -> иркутское (UTC+8)."""
    return datetime.fromtimestamp(timestamp, IRKUTSK_TZ).timetuple()


_logger = logging.getLogger("watcher")
if not _logger.handlers:
    _logger.setLevel(logging.INFO)
    _fmt = logging.Formatter("%(asctime)s (ИРК) %(message)s", "%Y-%m-%d %H:%M:%S")
    _fmt.converter = _irkutsk_time
    try:
        _fh = RotatingFileHandler(LOG_PATH, maxBytes=10 * 1024 * 1024,
                                  backupCount=5, encoding="utf-8")
        _fh.setFormatter(_fmt)
        _logger.addHandler(_fh)
    except Exception:
        pass  # если писать в файл нельзя — не падаем, остаётся консоль
    _sh = logging.StreamHandler(sys.stdout)
    _sh.setFormatter(_fmt)
    _logger.addHandler(_sh)


def log(msg):
    _logger.info(msg)


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_curl(path):
    """Разбирает файл 'Copy as cURL (bash)' из DevTools в url/method/headers/body."""
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()

    # Склеиваем перенос строк (в bash cURL строки заканчиваются '\')
    raw = raw.replace("\\\n", " ").replace("^\n", " ").replace("`\n", " ")
    raw = raw.replace("\r", " ").replace("\n", " ")

    tokens = shlex.split(raw)
    url = None
    method = "GET"
    headers = {}
    data = None

    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t == "curl":
            pass
        elif t in ("-X", "--request"):
            i += 1
            method = tokens[i]
        elif t in ("-H", "--header"):
            i += 1
            if ":" in tokens[i]:
                k, v = tokens[i].split(":", 1)
                headers[k.strip()] = v.strip()
        elif t in ("-b", "--cookie"):
            i += 1
            headers["Cookie"] = tokens[i]
        elif t in ("--data", "--data-raw", "--data-binary", "-d", "--data-urlencode"):
            i += 1
            data = tokens[i]
            method = "POST"
        elif t.startswith("http"):
            url = t
        i += 1

    if url is None:
        raise ValueError("Не нашёл URL в request.curl")
    return url, method, headers, data


def _cookies_from_header(cookie_str):
    """'a=b; c=d' -> {'a':'b','c':'d'}"""
    jar = {}
    for part in (cookie_str or "").split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            jar[k.strip()] = v.strip()
    return jar


def seed_session_from_headers(headers):
    """Переносит Cookie из заголовков в жар сессии. Возвращает заголовки без Cookie."""
    headers = dict(headers)
    cookie_str = headers.pop("Cookie", None) or headers.pop("cookie", None)
    if cookie_str:
        for k, v in _cookies_from_header(cookie_str).items():
            SESSION.cookies.set(k, v, domain="hr.productstar.ru")
    return headers


def save_cookies():
    """Сохраняет актуальные куки сессии — чтобы пережить перезапуск."""
    try:
        data = {c.name: c.value for c in SESSION.cookies}
        with open(COOKIES_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        log(f"Не смог сохранить cookies.json: {e}")


def load_cookies():
    """Подгружает ранее сохранённые (более свежие) куки, если есть."""
    if os.path.exists(COOKIES_PATH):
        try:
            with open(COOKIES_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in data.items():
                SESSION.cookies.set(k, v, domain="hr.productstar.ru")
            return True
        except Exception as e:
            log(f"Не смог загрузить cookies.json: {e}")
    return False


def apply_request_from_curl():
    """
    Читает request.curl, кладёт запрос в STATE и засевает куки в сессию.
    Вызывается на старте и при обновлении кук через бота.
    """
    url, method, headers, data = parse_curl(CURL_PATH)
    headers = seed_session_from_headers(headers)
    with STATE_LOCK:
        STATE["url"] = url
        STATE["method"] = method
        STATE["headers"] = headers
        STATE["data"] = data


def fetch(url=None, method=None, headers=None, data=None, timeout=30):
    """
    Делает запрос через общую сессию (сессия сама обновляет куки из ответа).
    Без аргументов берёт текущий запрос из STATE.
    """
    if url is None:
        with STATE_LOCK:
            url, method = STATE["url"], STATE["method"]
            headers, data = dict(STATE["headers"]), STATE["data"]
    return SESSION.request(method, url, headers=headers, data=data, timeout=timeout)


def extract_items(text, content_type):
    """
    Достаёт список заданий из ответа.
    Возвращает список (item_id, title).
    Заточено под ProductStar (Laravel + Inertia): данные лежат в
    <script data-page="app">...json...</script>, а список домашек —
    в props.dataPaginator.data.
    """
    page = _load_inertia_page(text)
    if page is not None:
        props = page.get("props", {}) if isinstance(page, dict) else {}
        paginator = props.get("dataPaginator")
        if isinstance(paginator, dict) and "data" in paginator:
            rows = paginator.get("data") or []
            return [(_homework_id(hw), _homework_title(hw), _homework_link(hw, None))
                    for hw in rows]
        # data-page есть, но dataPaginator не нашли — сообщим отладочно
        log("В data-page не найден dataPaginator — структура изменилась?")

    # --- Fallback: JSON целиком (вдруг это Inertia-XHR ответ) ---
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            data = json.loads(text)
            paginator = (
                data.get("props", {}).get("dataPaginator")
                if isinstance(data, dict) else None
            )
            if isinstance(paginator, dict) and "data" in paginator:
                rows = paginator.get("data") or []
                return [(_homework_id(hw), _homework_title(hw), _homework_link(hw, None))
                        for hw in rows]
            found = []
            _walk_json(data, found)
            if found:
                return [(i[0], i[1], None) for i in found]
        except Exception:
            pass

    # --- Крайний fallback: не смогли разобрать структуру ---
    # Возвращаем "сырой" маркер, только если это НЕ похоже на нормальную
    # пустую страницу (чтобы не слать ложные уведомления).
    log("Не удалось найти dataPaginator ни в data-page, ни в JSON.")
    return None


def _load_inertia_page(text):
    """
    Достаёт и парсит JSON со страницы Inertia. Два варианта размещения:
      1) ProductStar: <script data-page="app" ...>{...JSON...}</script>  (JSON в теле)
      2) классический Inertia: <div id="app" data-page="{...escaped JSON...}">
    """
    # Вариант 1 — JSON в теле <script data-page=...>
    m = re.search(r'<script[^>]*\bdata-page\b[^>]*>(.*?)</script>', text, re.S)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except Exception as e:
            log(f"Не смог разобрать <script data-page>: {e}")

    # Вариант 2 — JSON в атрибуте data-page="..." (html-экранирован)
    m = re.search(r'data-page="(\{.*?\})"', text, re.S)
    if m:
        try:
            return json.loads(html.unescape(m.group(1)))
        except Exception as e:
            log(f"Не смог разобрать атрибут data-page: {e}")

    return None


def _dig(obj, *keys):
    """Безопасно достаёт вложенное значение по цепочке ключей."""
    cur = obj
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return None
    return cur


def _homework_id(hw):
    if not isinstance(hw, dict):
        return f"hash:{hashlib.sha256(str(hw).encode()).hexdigest()[:16]}"
    for k in ("id", "assignment_id", "uuid", "homework_id"):
        if hw.get(k) not in (None, ""):
            return f"id:{hw[k]}"
    return f"hash:{hashlib.sha256(json.dumps(hw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]}"


# Коды статусов ProductStar (можно дополнять)
STATUS_MAP = {
    0: "Черновик",
    1: "Отправлено на проверку",
    2: "На проверке",
    3: "Проверено",
    4: "Требует доработки",
}


def _homework_title(hw):
    """Собирает читабельное описание задания под структуру ProductStar."""
    if not isinstance(hw, dict):
        return str(hw)[:200]

    parts = []

    # урок / курс
    lesson = _dig(hw, "lesson", "name")
    course = _dig(hw, "course", "name")
    if lesson:
        parts.append(f"📝 {lesson}")
    if course:
        parts.append(f"📚 {course}")

    # студент: student.first_name + student.last_name
    first = _dig(hw, "student", "first_name")
    last = _dig(hw, "student", "last_name")
    name = " ".join(x for x in (first, last) if x)
    if name:
        parts.append(f"👤 {name}")

    # время сдачи
    when = hw.get("firstSubmission") or hw.get("lastActivity")
    if when:
        parts.append(f"⏰ {when}")

    if not parts:
        # неизвестная структура — покажем сырой дамп, чтобы всё равно было видно
        return json.dumps(hw, ensure_ascii=False)[:300]
    return "\n".join(parts)


def _homework_link(hw, base):
    """Прямая ссылка на проверку задания, если есть id."""
    hid = hw.get("id") if isinstance(hw, dict) else None
    if hid:
        return f"https://hr.productstar.ru/homeworks/{hid}/check"
    return base


def _walk_json(node, out, depth=0):
    """Ищет в JSON объекты, похожие на задание (есть id + название/статус)."""
    if depth > 8:
        return
    if isinstance(node, list):
        for el in node:
            _walk_json(el, out, depth + 1)
    elif isinstance(node, dict):
        keys = {k.lower() for k in node.keys()}
        id_key = next((k for k in node if k.lower() in ("id", "uuid", "homework_id", "hw_id")), None)
        title_key = next((k for k in node if k.lower() in ("title", "name", "lesson", "task", "course", "student")), None)
        if id_key is not None and (title_key is not None or "status" in keys):
            hid = str(node[id_key])
            title = str(node.get(title_key, "")) if title_key else ""
            out.append((f"id:{hid}", title or f"id {hid}"))
        for v in node.values():
            _walk_json(v, out, depth + 1)


def load_seen():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return None  # None == первый запуск


def save_seen(ids):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, ensure_ascii=False, indent=2)


def send_telegram(token, chat_id, text):
    """Отправка в Telegram. text — обычный текст (без HTML)."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, data={
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }, timeout=30)
        if r.status_code != 200:
            log(f"Telegram ошибка {r.status_code}: {r.text[:200]}")
        return r.status_code == 200
    except requests.RequestException as e:
        log(f"Telegram недоступен: {e}")
        return False


# База API МАКС. Если платформа сменит домен — поменять тут
# (встречаются platform-api.max.ru и platform-api2.max.ru).
MAX_API_BASE = "https://platform-api.max.ru"


def send_max(token, chat_id, text, api_base=None):
    """
    Отправка в МАКС через Bot API.
    token   — токен бота (из dev.max.ru, после модерации)
    chat_id — идентификатор диалога с тобой (узнаётся из /updates после старта бота)
    """
    base = (api_base or MAX_API_BASE).rstrip("/")
    url = f"{base}/messages"
    try:
        r = requests.post(
            url,
            params={"chat_id": chat_id},
            headers={"Authorization": str(token), "Content-Type": "application/json"},
            json={"text": text},
            timeout=30,
        )
        if r.status_code != 200:
            log(f"МАКС ошибка {r.status_code}: {r.text[:200]}")
            return False
        return True
    except requests.RequestException as e:
        log(f"МАКС недоступен: {e}")
        return False


def get_max_chat_ids(token, api_base=None):
    """
    Возвращает список chat_id из последних апдейтов МАКС.
    Нужно один раз после старта бота, чтобы узнать max_chat_id.
    """
    base = (api_base or MAX_API_BASE).rstrip("/")
    try:
        r = requests.get(f"{base}/updates",
                         headers={"Authorization": str(token)}, timeout=30)
        data = r.json()
    except Exception as e:
        log(f"МАКС /updates ошибка: {e}")
        return []
    found = []

    def _dig_chat(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "chat_id" and v is not None:
                    found.append(str(v))
                else:
                    _dig_chat(v)
        elif isinstance(obj, list):
            for el in obj:
                _dig_chat(el)

    _dig_chat(data)
    # уникальные, сохраняя порядок
    seen = set()
    return [x for x in found if not (x in seen or seen.add(x))]


def notify(cfg, text):
    """
    Рассылает уведомление по активным каналам из cfg['channels'].
    Возможные значения: 'telegram', 'max' (можно оба).
    Возвращает True, если хотя бы один канал доставил.
    """
    channels = cfg.get("channels") or ["telegram"]
    if isinstance(channels, str):
        channels = [channels]
    ok_any = False

    if "telegram" in channels:
        tok = cfg.get("telegram_token", "")
        chat = cfg.get("telegram_chat_id", "")
        if tok and chat and "СЮДА" not in str(tok):
            if send_telegram(tok, chat, text):
                ok_any = True
                log("→ отправлено в Telegram")
        else:
            log("Telegram выбран, но не настроен (token/chat_id).")

    if "max" in channels:
        tok = cfg.get("max_token", "")
        chat = cfg.get("max_chat_id", "")
        if tok and chat and "СЮДА" not in str(tok):
            if send_max(tok, chat, text, cfg.get("max_api_base")):
                ok_any = True
                log("→ отправлено в МАКС")
        else:
            log("МАКС выбран, но не настроен (max_token/max_chat_id).")

    return ok_any


def reply_telegram(cfg, text):
    """Ответ в Telegram (для команд управления)."""
    send_telegram(cfg.get("telegram_token"), cfg.get("telegram_chat_id"), text)


def update_cookies_from_text(cfg, payload):
    """
    Обновляет куки одним из двух способов:
      1) прислан полный cURL (есть 'curl'/'http') -> перезаписываем request.curl;
      2) прислана только строка кук ('a=b; c=d') -> обновляем жар сессии.
    """
    payload = payload.strip()
    is_full_curl = ("curl" in payload.lower() and "http" in payload.lower())
    # убираем возможный префикс "Cookie:"
    cookie_only = re.sub(r"^\s*cookie:\s*", "", payload, flags=re.I)
    looks_like_cookies = ("=" in cookie_only and ";" in cookie_only)

    try:
        if is_full_curl:
            with open(CURL_PATH, "w", encoding="utf-8") as f:
                f.write(payload)
            SESSION.cookies.clear()
            apply_request_from_curl()
            save_cookies()
            reply_telegram(cfg, "✅ Куки обновлены (полный cURL). Продолжаю слежение.")
            log("Куки обновлены через бота (cURL).")
        elif looks_like_cookies:
            SESSION.cookies.clear()
            for k, v in _cookies_from_header(cookie_only).items():
                SESSION.cookies.set(k, v, domain="hr.productstar.ru")
            save_cookies()
            reply_telegram(cfg, "✅ Куки обновлены (строка Cookie). Продолжаю слежение.")
            log("Куки обновлены через бота (cookie-строка).")
        else:
            reply_telegram(cfg,
                "❌ Не распознал. Пришли ОДНО из двух:\n"
                "• только строку куки: /cookie имя1=знач1; имя2=знач2; ...\n"
                "• или полный cURL: /cookie curl 'https://...' -b '...'")
    except Exception as e:
        reply_telegram(cfg, f"❌ Ошибка при обновлении кук: {e}")
        log(f"Ошибка обновления кук: {e}")


def handle_command(cfg, text):
    t = (text or "").strip()
    low = t.lower()
    if low.startswith("/ping"):
        reply_telegram(cfg, "🏓 На связи, слежу за домашками.")
    elif low.startswith("/status"):
        seen = load_seen()
        n = 0 if seen is None else len(seen)
        reply_telegram(cfg, f"📊 Работаю. Известных заданий в памяти: {n}.")
    elif low.startswith("/cookie") or low.startswith("curl "):
        payload = t[len("/cookie"):].strip() if low.startswith("/cookie") else t
        update_cookies_from_text(cfg, payload)
    # прочее молча игнорируем


def telegram_listener(cfg):
    """Фоновый поток: слушает команды от твоего chat_id (замена кук с телефона)."""
    token = cfg.get("telegram_token")
    allowed = str(cfg.get("telegram_chat_id"))
    if not token or "СЮДА" in str(token) or not allowed:
        return
    api = f"https://api.telegram.org/bot{token}"
    offset = None
    log("Слушаю команды в Telegram: /cookie, /ping, /status.")
    while True:
        try:
            params = {"timeout": 50}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(f"{api}/getUpdates", params=params, timeout=60)
            data = r.json()
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue
                chat = str(msg.get("chat", {}).get("id"))
                if chat != allowed:
                    continue  # команды принимаем ТОЛЬКО от тебя
                handle_command(cfg, msg.get("text", ""))
        except requests.RequestException:
            time.sleep(5)
        except Exception as e:
            log(f"Ошибка Telegram-листенера: {e}")
            time.sleep(5)


def build_message(header, title, link):
    """Собирает уведомление обычным текстом (работает и в Telegram, и в ВК)."""
    lines = [header]
    if title:
        lines.append(title)
    return "\n".join(lines)


def run_test(cfg, url, method, headers, data, page_link):
    """Разовая проверка: берёт текущие задания и шлёт по активным каналам."""
    log(f"РЕЖИМ ПРОВЕРКИ. Каналы: {cfg.get('channels')}")
    headers = seed_session_from_headers(headers)
    resp = fetch(url, method, headers, data)
    with open(RAWDUMP_PATH, "w", encoding="utf-8") as f:
        f.write(resp.text)
    log(f"HTTP {resp.status_code}")
    items = extract_items(resp.text, resp.headers.get("Content-Type", ""))
    if not items:
        notify(cfg, "🧪 Проверка: сейчас на сайте нет заданий (Всего: 0), "
                    "но связь работает.")
        log("Заданий нет — отправил тестовое сообщение.")
        return
    for iid, title, link in items:
        msg = build_message("🧪 Проверка — текущее задание:", title, link or page_link)
        notify(cfg, msg)
    log(f"Отправлено заданий: {len(items)}. Проверка завершена.")


def main():
    cfg = load_config()
    interval = cfg.get("interval_seconds", 30)
    page_link = cfg.get("homeworks_url", "https://hr.productstar.ru/homeworks")

    # Готовим запрос + сессию (куки уходят в жар сессии, дальше она их обновляет)
    apply_request_from_curl()
    load_cookies()  # если есть более свежие куки с прошлого запуска

    # --- Режим проверки: один прогон и выход ---
    if "--test" in sys.argv:
        with STATE_LOCK:
            u, m, h, d = STATE["url"], STATE["method"], dict(STATE["headers"]), STATE["data"]
        run_test(cfg, u, m, h, d, page_link)
        return

    with STATE_LOCK:
        log(f"Мониторю: {STATE['method']} {STATE['url']}")
    log(f"Интервал: {interval} сек. Каналы: {cfg.get('channels')}. Ctrl+C для остановки.")

    # Фоновый слушатель команд управления (замена кук с телефона)
    if "telegram" in (cfg.get("channels") or ["telegram"]):
        threading.Thread(target=telegram_listener, args=(cfg,), daemon=True).start()

    # Стартовое уведомление — заодно проверка, что каналы настроены
    if notify(cfg, "✅ Watcher запущен. Слежу за новыми домашками на ProductStar."):
        log("Стартовое сообщение отправлено.")
    else:
        log("ВНИМАНИЕ: стартовое сообщение не ушло ни в один канал — проверь настройки.")

    fail_streak = 0
    idle_ticks = 0
    # "Без изменений" пишем не каждый раз, а раз в ~N циклов (чтобы не спамить лог)
    idle_every = max(1, int(600 / max(interval, 1)))  # примерно раз в 10 минут
    while True:
        try:
            resp = fetch()  # берёт актуальный запрос из STATE через сессию
            ct = resp.headers.get("Content-Type", "")

            with open(RAWDUMP_PATH, "w", encoding="utf-8") as f:
                f.write(resp.text)

            if resp.status_code in (401, 403):
                log(f"Похоже, слетела авторизация (HTTP {resp.status_code}). "
                    f"Пришли боту команду /cookie с новым cURL.")
                if fail_streak == 0:
                    notify(cfg, "⚠️ Слетела авторизация на ProductStar.\n"
                                "Скопируй свежий Copy as cURL (bash) и пришли боту:\n"
                                "/cookie <вставь сюда>")
                fail_streak += 1
                time.sleep(interval)
                continue

            if resp.status_code != 200:
                log(f"HTTP {resp.status_code}, пропускаю.")
                time.sleep(interval)
                continue

            fail_streak = 0
            save_cookies()  # запоминаем свежие куки, если сайт их обновил
            items = extract_items(resp.text, ct)
            if items is None:
                log("Не смог распознать список заданий — пропускаю цикл. "
                    "Проверь last_response.txt.")
                time.sleep(interval)
                continue
            current_ids = {i[0] for i in items}
            titles = {i[0]: i[1] for i in items}
            links = {i[0]: (i[2] if len(i) > 2 else None) for i in items}

            seen = load_seen()
            if seen is None:
                save_seen(current_ids)
                log(f"Первый запуск. Запомнил {len(current_ids)} заданий. Жду новых...")
                time.sleep(interval)
                continue

            new_ids = current_ids - seen
            if new_ids:
                idle_ticks = 0
                log(f"🔔 НОВЫХ ЗАДАНИЙ: {len(new_ids)}")
                for nid in new_ids:
                    title = (titles.get(nid) or "").strip()
                    link = links.get(nid) or page_link
                    msg = build_message("🔔 Новое", title, link)
                    notify(cfg, msg)
                save_seen(seen | current_ids)
            else:
                # пишем «всё тихо» редко, чтобы лог не засорять
                idle_ticks += 1
                if idle_ticks % idle_every == 0:
                    log(f"Работаю, без изменений ({len(current_ids)} заданий).")

        except requests.RequestException as e:
            log(f"Сетевая ошибка: {e}")
        except Exception as e:
            log(f"Ошибка: {e}")

        time.sleep(interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Остановлено.")
        sys.exit(0)
