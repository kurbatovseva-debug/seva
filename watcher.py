#!/usr/bin/env python3
"""
Amazon Deal Watcher (бесплатная версия — прямой парсинг Amazon)
===============================================================
Отслеживает скидки на Amazon.com по брендам, размерам и порогу скидки,
парся страницы поиска напрямую (без платных API). При нахождении
подходящего предложения шлёт уведомление в Telegram. Дедуплицирует.

ВАЖНО: Amazon блокирует автоматические запросы с серверных IP (капча).
Скрипт распознаёт капчу и предупреждает, но обойти её не может. Это
осознанный компромисс ради полностью бесплатного решения.

Запускается как бесконечный цикл (для сервера/Render) либо разово (cron).
"""

import os
import re
import sys
import time
import json
import random
import logging
import threading
from pathlib import Path
from datetime import datetime
from urllib.parse import quote_plus
from http.server import BaseHTTPRequestHandler, HTTPServer

import yaml
import requests
from bs4 import BeautifulSoup


# --------------------------------------------------------------------------- #
# Конфигурация и логирование
# --------------------------------------------------------------------------- #

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "seen_deals.json"
CONFIG_FILE = BASE_DIR / "config.yaml"

DOMAINS = {"US": "www.amazon.com", "DE": "www.amazon.de", "UK": "www.amazon.co.uk"}

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(BASE_DIR / "watcher.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("watcher")


def load_config() -> dict:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def env(name: str, required: bool = True, default: str | None = None) -> str | None:
    val = os.environ.get(name, default)
    if required and not val:
        log.error("Не задана переменная окружения %s — см. .env.example", name)
        sys.exit(1)
    return val


# --------------------------------------------------------------------------- #
# Дедуп
# --------------------------------------------------------------------------- #

def load_seen() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_seen(seen: dict) -> None:
    cutoff = time.time() - 30 * 86400
    seen = {k: v for k, v in seen.items() if v > cutoff}
    STATE_FILE.write_text(json.dumps(seen), encoding="utf-8")


def deal_key(asin: str, price_cents: int) -> str:
    return f"{asin}:{price_cents}"


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #

def send_telegram(token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(
            url,
            data={"chat_id": chat_id, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": False},
            timeout=20,
        )
        if r.status_code != 200:
            log.error("Telegram error %s: %s", r.status_code, r.text)
            return False
        return True
    except requests.RequestException as e:
        log.error("Telegram request failed: %s", e)
        return False


# --------------------------------------------------------------------------- #
# Парсинг Amazon
# --------------------------------------------------------------------------- #

def price_to_cents(text: str) -> int | None:
    if not text:
        return None
    m = re.search(r"[\d,]+\.\d{2}", text)
    if not m:
        return None
    return int(round(float(m.group(0).replace(",", "")) * 100))


def is_captcha(html: str) -> bool:
    markers = ["Enter the characters you see below",
               "Type the characters you see in this image",
               "/errors/validateCaptcha", "Robot Check",
               "api-services-support@amazon.com"]
    return any(m in html for m in markers)


def fetch_search(session: requests.Session, domain_host: str, query: str) -> str | None:
    url = f"https://{domain_host}/s?k={quote_plus(query)}"
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"https://{domain_host}/",
        "Upgrade-Insecure-Requests": "1",
    }
    try:
        r = session.get(url, headers=headers, timeout=25)
    except requests.RequestException as e:
        log.error("Запрос '%s' упал: %s", query, e)
        return None
    if r.status_code != 200:
        log.warning("HTTP %s для запроса '%s'", r.status_code, query)
    if is_captcha(r.text):
        log.warning("⚠️  Amazon показал капчу для '%s' — IP заблокирован для парсинга", query)
        return None
    return r.text


def parse_results(html: str, domain_host: str) -> list[dict]:
    """Достаёт карточки товаров из HTML страницы поиска."""
    soup = BeautifulSoup(html, "lxml")
    items = []
    for card in soup.select('div[data-component-type="s-search-result"]'):
        asin = card.get("data-asin") or ""
        if not asin:
            continue

        # заголовок + ссылка
        h2 = card.select_one("h2")
        title = h2.get_text(strip=True) if h2 else ""
        link_el = card.select_one("h2 a") or card.select_one("a.a-link-normal")
        href = link_el.get("href") if link_el else ""
        url = f"https://{domain_host}{href}" if href.startswith("/") else \
              (href or f"https://{domain_host}/dp/{asin}")

        # текущая цена
        cur_el = card.select_one("span.a-price > span.a-offscreen")
        current = price_to_cents(cur_el.get_text()) if cur_el else None

        # прежняя (зачёркнутая) цена
        was_el = card.select_one("span.a-price.a-text-price > span.a-offscreen")
        reference = price_to_cents(was_el.get_text()) if was_el else None

        if current is None:
            continue

        items.append({
            "asin": asin, "title": title, "url": url,
            "current": current, "reference": reference,
        })
    return items


# --------------------------------------------------------------------------- #
# Фильтрация
# --------------------------------------------------------------------------- #

def size_matches(title: str, wanted_sizes: list[str]) -> bool:
    if not wanted_sizes:
        return True
    blob = (title or "").lower()
    for s in wanted_sizes:
        s = str(s).lower().strip()
        if re.search(rf"(^|[^0-9a-z]){re.escape(s)}([^0-9a-z]|$)", blob):
            return True
    return False


def discount_percent(current: int | None, reference: int | None) -> float | None:
    if not current or not reference or reference <= current:
        return None
    return round((reference - current) / reference * 100, 1)


def format_price(cents: int | None) -> str:
    if cents is None or cents < 0:
        return "—"
    return f"${cents / 100:.2f}"


def check_watch(session, watch: dict) -> list[dict]:
    hits = []
    label = watch.get("label", watch.get("brand", "—"))
    query = watch["query"]
    min_discount = watch["min_discount"]
    wanted_sizes = watch.get("sizes", [])
    domain_host = DOMAINS.get(watch.get("domain", "US"), DOMAINS["US"])

    log.info("→ '%s' (скидка ≥%s%%, размеры %s)",
             query, min_discount, wanted_sizes or "любые")

    html = fetch_search(session, domain_host, query)
    if not html:
        return hits

    for p in parse_results(html, domain_host):
        disc = discount_percent(p["current"], p["reference"])
        if disc is None or disc < min_discount:
            continue
        if not size_matches(p["title"], wanted_sizes):
            continue
        hits.append({**p, "brand": watch.get("brand", ""),
                     "category": label, "discount": disc})

    log.info("   '%s': подходящих — %d", query, len(hits))
    return hits


def build_message(hits: list[dict]) -> str:
    today = datetime.now().strftime("%d.%m.%Y %H:%M")
    lines = [f"🛒 <b>Amazon — подходящие скидки</b> ({today})", ""]
    by_cat: dict[str, list[dict]] = {}
    for h in hits:
        by_cat.setdefault(h["category"], []).append(h)
    for cat, items in by_cat.items():
        items.sort(key=lambda x: x["discount"], reverse=True)
        lines.append(f"<b>{cat}</b>")
        for h in items:
            title = h["title"]
            if len(title) > 70:
                title = title[:67] + "…"
            lines.append(
                f'• <a href="{h["url"]}">{title}</a>\n'
                f'   {format_price(h["current"])} '
                f'(было {format_price(h["reference"])}, −{h["discount"]:.0f}%)'
            )
        lines.append("")
    lines.append("⚡️ Хорошие размеры разбирают быстро — проверьте наличие сразу.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Health-эндпоинт для Render
# --------------------------------------------------------------------------- #

_LAST_RUN = {"ts": None, "hits": 0}


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({
            "status": "alive",
            "last_run": _LAST_RUN["ts"],
            "last_hits": _LAST_RUN["hits"],
        }).encode("utf-8"))

    def log_message(self, *args):
        pass


def start_health_server():
    port = os.environ.get("PORT")
    if not port:
        return
    server = HTTPServer(("0.0.0.0", int(port)), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Health-сервер запущен на порту %s", port)


# --------------------------------------------------------------------------- #
# Основной цикл
# --------------------------------------------------------------------------- #

def run_once(cfg, telegram_token, telegram_chat) -> None:
    seen = load_seen()
    all_hits = []
    session = requests.Session()

    for i, watch in enumerate(cfg["watches"]):
        hits = check_watch(session, watch)
        for h in hits:
            key = deal_key(h["asin"], h["current"] or 0)
            if key in seen:
                continue
            seen[key] = time.time()
            all_hits.append(h)
        # случайная пауза между брендами, чтобы меньше походить на бота
        if i < len(cfg["watches"]) - 1:
            time.sleep(random.uniform(5, 15))

    _LAST_RUN["ts"] = datetime.now().isoformat(timespec="seconds")
    _LAST_RUN["hits"] = len(all_hits)

    if all_hits:
        msg = build_message(all_hits)
        if send_telegram(telegram_token, telegram_chat, msg):
            log.info("Отправлено уведомление: %d новых предложений", len(all_hits))
            save_seen(seen)
        else:
            log.error("Не удалось отправить — повторим позже")
    else:
        log.info("Новых подходящих предложений нет — уведомление не шлём")
        save_seen(seen)


def main() -> None:
    cfg = load_config()
    telegram_token = env("TELEGRAM_BOT_TOKEN")
    telegram_chat = env("TELEGRAM_CHAT_ID")

    interval = int(os.environ.get("CHECK_INTERVAL_MINUTES",
                                  cfg.get("check_interval_minutes", 20)))
    run_mode = os.environ.get("RUN_MODE", "loop")

    log.info("Запуск Amazon Deal Watcher (парсинг; режим: %s, интервал: %d мин)",
             run_mode, interval)
    start_health_server()

    # Само-тест доставки в Telegram: задайте env SEND_TEST=1 и перезапустите,
    # чтобы получить одно тестовое сообщение (проверка бота, минуя парсинг).
    if os.environ.get("SEND_TEST") == "1":
        ok = send_telegram(
            telegram_token, telegram_chat,
            "✅ <b>Amazon Deal Watcher</b>\nТестовое сообщение — бот и уведомления работают. "
            "Уберите переменную SEND_TEST, чтобы не повторять."
        )
        log.info("SEND_TEST: тестовое сообщение отправлено — %s", ok)

    if run_mode == "once":
        run_once(cfg, telegram_token, telegram_chat)
        return

    while True:
        try:
            run_once(cfg, telegram_token, telegram_chat)
        except Exception as e:
            log.exception("Ошибка в цикле проверки: %s", e)
        log.info("Следующая проверка через %d мин", interval)
        time.sleep(interval * 60)


if __name__ == "__main__":
    main()
