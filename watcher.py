#!/usr/bin/env python3
"""
Amazon Deal Watcher
===================
Отслеживает скидки на Amazon через Keepa API по заданным брендам, размерам
и порогам скидки. При нахождении подходящего предложения шлёт уведомление
в Telegram. Дедуплицирует уже отправленные предложения.

Запускается как бесконечный цикл (для сервера/Docker) либо разово (для cron).

Автор: сгенерировано для Seva.
"""

import os
import sys
import time
import json
import logging
import threading
from pathlib import Path
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import yaml
import requests

try:
    import keepa
except ImportError:
    print("Не установлен пакет 'keepa'. Выполните: pip install -r requirements.txt")
    sys.exit(1)


# --------------------------------------------------------------------------- #
# Конфигурация и логирование
# --------------------------------------------------------------------------- #

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "seen_deals.json"       # дедуп уже отправленных
CONFIG_FILE = BASE_DIR / "config.yaml"

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
# Хранилище отправленных предложений (дедуп)
# --------------------------------------------------------------------------- #

def load_seen() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_seen(seen: dict) -> None:
    # чистим записи старше 30 дней, чтобы файл не рос бесконечно
    cutoff = time.time() - 30 * 86400
    seen = {k: v for k, v in seen.items() if v > cutoff}
    STATE_FILE.write_text(json.dumps(seen), encoding="utf-8")


def deal_key(asin: str, price_cents: int) -> str:
    """Уникальный ключ предложения = ASIN + цена. Изменилась цена → новое уведомление."""
    return f"{asin}:{price_cents}"


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #

def send_telegram(token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(
            url,
            data={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
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
# Логика фильтрации
# --------------------------------------------------------------------------- #

def size_matches(product: dict, wanted_sizes: list[str]) -> bool:
    """
    Best-effort проверка размера. Keepa не гарантирует наличие размера в стоке,
    поэтому проверяем по названию и атрибутам вариаций. Если размеры не заданы —
    считаем, что подходит любой.
    """
    if not wanted_sizes:
        return True

    haystacks = []
    title = (product.get("title") or "").lower()
    haystacks.append(title)

    # атрибуты вариаций (Keepa возвращает variationAttributes при наличии)
    for attr in product.get("variationAttributes", []) or []:
        val = str(attr.get("value", "")).lower()
        haystacks.append(val)

    blob = " ".join(haystacks)
    for s in wanted_sizes:
        s = str(s).lower().strip()
        # окружаем нецифробуквенными границами, чтобы "10" не ловил "100"
        import re
        if re.search(rf"(^|[^0-9a-z]){re.escape(s)}([^0-9a-z]|$)", blob):
            return True
    return False


def discount_percent(product: dict) -> float | None:
    """
    Считает % скидки от list price / прежней цены к текущей цене NEW.
    Возвращает None, если данных недостаточно.
    """
    stats = product.get("stats") or {}
    # текущая цена (в центах); -1 = нет данных
    current = None
    cur_arr = stats.get("current")
    if isinstance(cur_arr, list) and len(cur_arr) > 1:
        current = cur_arr[1]  # индекс 1 = NEW
        if current is not None and current < 0:
            current = None

    # опорная (прежняя) цена: берём максимум из list price и 90-дневного среднего
    reference_candidates = []
    if product.get("listPrice") and product["listPrice"] > 0:
        reference_candidates.append(product["listPrice"])
    avg90 = stats.get("avg90")
    if isinstance(avg90, list) and len(avg90) > 1 and avg90[1] and avg90[1] > 0:
        reference_candidates.append(avg90[1])

    if current is None or not reference_candidates:
        return None

    reference = max(reference_candidates)
    if reference <= 0 or current >= reference:
        return None

    return round((reference - current) / reference * 100, 1)


def format_price(cents: int | None) -> str:
    if cents is None or cents < 0:
        return "—"
    return f"${cents / 100:.2f}"


def check_watch(api, watch: dict) -> list[dict]:
    """Проверяет один блок правил (обувь или одежда конкретного бренда)."""
    hits = []
    brand = watch["brand"]
    min_discount = watch["min_discount"]
    wanted_sizes = watch.get("sizes", [])
    domain = watch.get("domain", "US")

    log.info("→ Проверяю бренд '%s' (скидка ≥%s%%, размеры %s)",
             brand, min_discount, wanted_sizes or "любые")

    # 1) находим ASIN'ы бренда через Product Finder
    selection = {
        "brand": [brand],
        "productType": [0, 1],   # 0 = стандарт, 1 = вариация
        "perPage": watch.get("max_products", 50),
        "sort": [["current_SALES", "asc"]],
    }
    # ограничим категорией, если задана
    if watch.get("root_category"):
        selection["rootCategory"] = watch["root_category"]

    try:
        asins = api.product_finder(selection, domain=domain)
    except Exception as e:
        log.error("Product Finder ошибка для '%s': %s", brand, e)
        return hits

    if not asins:
        log.info("   бренд '%s': товаров не найдено", brand)
        return hits

    # 2) запрашиваем детали (с историей и статистикой)
    try:
        products = api.query(asins, domain=domain, stats=90, history=False)
    except Exception as e:
        log.error("Query ошибка для '%s': %s", brand, e)
        return hits

    for p in products:
        disc = discount_percent(p)
        if disc is None or disc < min_discount:
            continue
        if not size_matches(p, wanted_sizes):
            continue

        stats = p.get("stats") or {}
        cur_arr = stats.get("current") or []
        current = cur_arr[1] if len(cur_arr) > 1 else None

        hits.append({
            "asin": p.get("asin"),
            "title": p.get("title", "—"),
            "brand": brand,
            "category": watch.get("label", brand),
            "current": current,
            "reference": max(
                [x for x in [p.get("listPrice"),
                             (stats.get("avg90") or [None, None])[1]] if x and x > 0]
                or [0]
            ),
            "discount": disc,
            "sizes": wanted_sizes,
            "url": f"https://www.amazon.com/dp/{p.get('asin')}",
        })
    log.info("   бренд '%s': подходящих предложений — %d", brand, len(hits))
    return hits


def build_message(hits: list[dict]) -> str:
    today = datetime.now().strftime("%d.%m.%Y %H:%M")
    lines = [f"🛒 <b>Amazon — подходящие скидки</b> ({today})", ""]

    # группируем по label (Обувь / Одежда)
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
# Health-эндпоинт для Render (чтобы free web-сервис не засыпал)
# --------------------------------------------------------------------------- #

_LAST_RUN = {"ts": None, "hits": 0}


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        body = json.dumps({
            "status": "alive",
            "last_run": _LAST_RUN["ts"],
            "last_hits": _LAST_RUN["hits"],
        })
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *args):  # тишина в логах пинга
        pass


def start_health_server():
    """Render задаёт PORT. Открываем порт, чтобы сервис считался живым web-сервисом."""
    port = os.environ.get("PORT")
    if not port:
        return
    server = HTTPServer(("0.0.0.0", int(port)), HealthHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    log.info("Health-сервер запущен на порту %s", port)


# --------------------------------------------------------------------------- #
# Основной цикл
# --------------------------------------------------------------------------- #

def run_once(api, cfg, telegram_token, telegram_chat) -> None:
    seen = load_seen()
    all_hits = []

    for watch in cfg["watches"]:
        hits = check_watch(api, watch)
        for h in hits:
            key = deal_key(h["asin"], h["current"] or 0)
            if key in seen:
                continue          # уже уведомляли об этом предложении
            seen[key] = time.time()
            all_hits.append(h)

    _LAST_RUN["ts"] = datetime.now().isoformat(timespec="seconds")
    _LAST_RUN["hits"] = len(all_hits)

    if all_hits:
        msg = build_message(all_hits)
        if send_telegram(telegram_token, telegram_chat, msg):
            log.info("Отправлено уведомление: %d новых предложений", len(all_hits))
            save_seen(seen)
        else:
            log.error("Не удалось отправить — не сохраняю дедуп, повторим позже")
    else:
        log.info("Новых подходящих предложений нет — уведомление не шлём")
        save_seen(seen)


def main() -> None:
    cfg = load_config()

    keepa_key = env("KEEPA_API_KEY")
    telegram_token = env("TELEGRAM_BOT_TOKEN")
    telegram_chat = env("TELEGRAM_CHAT_ID")

    interval = int(os.environ.get("CHECK_INTERVAL_MINUTES",
                                  cfg.get("check_interval_minutes", 15)))
    run_mode = os.environ.get("RUN_MODE", "loop")  # loop | once

    log.info("Запуск Amazon Deal Watcher (режим: %s, интервал: %d мин)",
             run_mode, interval)

    start_health_server()   # только если задан PORT (Render)

    api = keepa.Keepa(keepa_key)

    if run_mode == "once":
        run_once(api, cfg, telegram_token, telegram_chat)
        return

    while True:
        try:
            run_once(api, cfg, telegram_token, telegram_chat)
        except Exception as e:
            log.exception("Ошибка в цикле проверки: %s", e)
        log.info("Следующая проверка через %d мин", interval)
        time.sleep(interval * 60)


if __name__ == "__main__":
    main()
