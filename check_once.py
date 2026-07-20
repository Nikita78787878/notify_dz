# -*- coding: utf-8 -*-
"""
Разовая проверка для GitHub Actions (или cron).
Делает ОДИН заход: берёт задания, сравнивает с seen.json, шлёт в Telegram
только НОВЫЕ, обновляет seen.json — и выходит.

Секреты берутся из переменных окружения (GitHub Secrets):
  TELEGRAM_TOKEN    — токен бота
  TELEGRAM_CHAT_ID  — твой chat_id
  PS_COOKIE         — строка Cookie для hr.productstar.ru (из request.curl, после -b)
  HOMEWORKS_URL     — (необязательно) ссылка, по умолчанию страница ДЗ
"""
import os
import sys
import json

import re
import requests
import watcher  # переиспользуем разбор и отправку

SEEN_FILE = "seen.json"  # лежит в корне репозитория, коммитится обратно


def normalize_cookie(raw):
    """
    Принимает ЛИБО чистую строку Cookie, ЛИБО весь текст request.curl —
    и возвращает строку Cookie. Так удобнее вставлять в секрет.
    """
    raw = (raw or "").strip()
    if "curl" not in raw.lower() and "http" not in raw.lower():
        return raw  # это уже готовая строка cookie
    m = (re.search(r"-b\s+'([^']*)'", raw)
         or re.search(r'-b\s+"([^"]*)"', raw)
         or re.search(r"--cookie\s+'([^']*)'", raw)
         or re.search(r"(?:-H\s+['\"]?cookie:\s*)([^'\"]+)", raw, re.I))
    return m.group(1).strip() if m else raw

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")


def need(name):
    v = os.environ.get(name)
    if not v:
        print(f"[ERROR] Нет обязательного секрета {name}", flush=True)
        sys.exit(1)
    return v


def cfg_from_env():
    """Собирает конфиг каналов из переменных окружения (GitHub Secrets)."""
    channels = [c.strip() for c in os.environ.get("CHANNELS", "telegram").split(",") if c.strip()]
    return {
        "channels": channels,
        "telegram_token": os.environ.get("TELEGRAM_TOKEN", ""),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
        "max_token": os.environ.get("MAX_TOKEN", ""),
        "max_chat_id": os.environ.get("MAX_CHAT_ID", ""),
        "max_api_base": os.environ.get("MAX_API_BASE", ""),
    }


def load_seen():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return None  # первый запуск


def save_seen(ids):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, ensure_ascii=False, indent=2)


def main():
    cfg = cfg_from_env()
    cookie = normalize_cookie(need("PS_COOKIE"))
    url = os.environ.get("HOMEWORKS_URL", "https://hr.productstar.ru/homeworks")

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru,en;q=0.9",
        "Cookie": cookie,
    }

    try:
        resp = requests.get(url, headers=headers, timeout=30)
    except requests.RequestException as e:
        print(f"[WARN] Сетевая ошибка: {e}", flush=True)
        sys.exit(0)

    print(f"HTTP {resp.status_code}", flush=True)

    if resp.status_code in (401, 403):
        watcher.notify(cfg,
            "⚠️ Слетела авторизация на ProductStar.\n"
            "Обнови секрет PS_COOKIE в настройках репозитория GitHub "
            "(Settings → Secrets → PS_COOKIE).")
        sys.exit(0)
    if resp.status_code != 200:
        print("[WARN] Не 200, пропускаю.", flush=True)
        sys.exit(0)

    items = watcher.extract_items(resp.text, resp.headers.get("Content-Type", ""))
    if items is None:
        print("[WARN] Не распознал список заданий.", flush=True)
        sys.exit(0)

    current = {i[0]: (i[1], i[2]) for i in items}
    seen = load_seen()

    if seen is None:
        # первый запуск — просто запоминаем, не спамим
        save_seen(set(current))
        print(f"Первый запуск. Запомнил {len(current)} заданий.", flush=True)
        return

    new_ids = set(current) - seen
    if new_ids:
        print(f"НОВЫХ ЗАДАНИЙ: {len(new_ids)}", flush=True)
        for nid in new_ids:
            title, link = current[nid]
            msg = watcher.build_message("🔔 Новое домашнее задание!", title, link or url)
            ok = watcher.notify(cfg, msg)
            print(f"  уведомил {nid}: {ok}", flush=True)
        save_seen(seen | set(current))
    else:
        print(f"Без изменений ({len(current)} заданий).", flush=True)


if __name__ == "__main__":
    main()
