"""
Доктор товарных файлов (ProductDoctor) — плагин для FunPay Cardinal v0.1.17+

🧹 Проверяет и лечит товарные файлы автовыдачи (storage/products/*.txt):
находит дубликаты товаров (риск выдать один товар дважды!), пустые строки,
лишние пробелы по краям и подозрительно короткие строки.

Возможности:
 • Сканирует все товарные файлы из конфига автовыдачи + «сироты» в папке
   storage/products (не привязанные к лотам).
 • 🚨 Дубликаты: показывает сколько и какие (в маскированном виде — секреты
   покупателей никогда не покидают сервер в открытом виде).
 • 🧹 Кнопка «Очистить файл»: убирает дубликаты (оставляет первое вхождение),
   пустые строки и пробелы по краям. Перед очисткой создаётся бэкап (.bak).
 • 🧹 Массовая очистка дубликатов во всех файлах одной кнопкой.
 • 🔁 Авто-проверка (по умолчанию раз в 6 часов): предупредит, если в файлах
   ПОЯВИЛИСЬ новые дубликаты (без спама — только об изменениях).
 • Все операции с файлами идут под штатным локом Кардинала
   (get_products_file_lock) — безопасно даже во время автовыдачи.

Команды Telegram:
 • /products — открыть меню плагина.

Установка: положить этот файл в папку plugins/ и перезапустить Кардинал.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
from collections import Counter
from typing import TYPE_CHECKING

from telebot.types import InlineKeyboardMarkup as K, InlineKeyboardButton as B, CallbackQuery, Message
from telebot.apihelper import ApiTelegramException

try:
    from tg_bot import CBT, static_keyboards as skb, utils as tg_utils
except Exception:
    CBT = None
    skb = None
    tg_utils = None

if TYPE_CHECKING:
    from cardinal import Cardinal

from Utils import cardinal_tools

logger = logging.getLogger("FPC.ProductDoctor")

# ══════════════════════════════ МЕТАДАННЫЕ ПЛАГИНА ══════════════════════════════
NAME = "ProductDoctor (Product Files Doctor)"
VERSION = "1.0.1"
DESCRIPTION = ("Проверяет и лечит товарные файлы автовыдачи: дубликаты (риск двойной выдачи!), "
               "пустые строки, лишние пробелы. Очистка с бэкапом, массовая очистка, "
               "авто-проверка раз в 6 часов. Команда: /products")
CREDITS = "yungspider"
UUID = "89c4b93a-9392-4992-8b0e-4ab5ae232392"
SETTINGS_PAGE = True

# ══════════════════════════════ НАСТРОЙКИ ПЛАГИНА ═══════════════════════════════
PLUGIN_DIR = os.path.join("storage", "plugins", "product_doctor")
SETTINGS_PATH = os.path.join(PLUGIN_DIR, "settings.json")
PRODUCTS_DIR = os.path.join("storage", "products")

DEFAULT_SETTINGS = {
    "auto_check": True,        # авто-проверка на новые дубликаты
    "check_interval": 21600,   # интервал авто-проверки, сек (6 часов)
    "warned": {},              # {имя файла: кол-во дубликатов, о которых уже предупредили}
}
INTERVAL_RANGE = (1800, 172800)  # 30 мин — 48 часов
PER_PAGE = 10
SHORT_LINE_LEN = 3

# ══════════════════════════════ CALLBACK-КОДЫ ═══════════════════════════════════
CB_HOME = "pdoc:home"
CB_FILES = "pdoc:files"        # pdoc:files:{offset}
CB_FILE = "pdoc:file"          # pdoc:file:{hash}
CB_CLEAN = "pdoc:clean"        # pdoc:clean:{hash}
CB_CLEANALL = "pdoc:cleanall"  # спросить массовую очистку
CB_CLEANALLY = "pdoc:cleanally"
CB_AUTOCHECK = "pdoc:autocheck"
CB_INT = "pdoc:int"
CB_NOOP = "pdoc:noop"
ST_INT = "pdoc:st:int"

_stop_event = threading.Event()
_settings_lock = threading.Lock()
_cardinal: "Cardinal | None" = None


# ══════════════════════════════ УТИЛИТЫ ═════════════════════════════════════════
def _clamp(value, min_v, max_v):
    return max(min_v, min(max_v, value))


def _load_settings() -> dict:
    with _settings_lock:
        data = {}
        if os.path.exists(SETTINGS_PATH):
            try:
                with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                logger.error("ProductDoctor: не удалось прочитать настройки, использую значения по умолчанию.")
                logger.debug("TRACEBACK", exc_info=True)
                data = {}
        settings = json.loads(json.dumps(DEFAULT_SETTINGS))
        if isinstance(data, dict):
            settings.update({k: v for k, v in data.items() if k in settings})
        settings["auto_check"] = bool(settings["auto_check"])
        try:
            settings["check_interval"] = _clamp(int(settings["check_interval"]), *INTERVAL_RANGE)
        except (TypeError, ValueError):
            settings["check_interval"] = DEFAULT_SETTINGS["check_interval"]
        if not isinstance(settings["warned"], dict):
            settings["warned"] = {}
        return settings


def _save_settings(settings: dict) -> None:
    with _settings_lock:
        try:
            os.makedirs(PLUGIN_DIR, exist_ok=True)
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.error("ProductDoctor: не удалось сохранить настройки.")
            logger.debug("TRACEBACK", exc_info=True)


def _short(text: str, limit: int = 40) -> str:
    text = str(text or "").replace("\n", " ")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _escape(text) -> str:
    if tg_utils:
        return tg_utils.escape(str(text))
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _plugin_enabled(crd: "Cardinal") -> bool:
    try:
        return crd.plugins[UUID].enabled
    except Exception:
        return False


def _mask(product: str) -> str:
    """Маскирует товар для показа в Telegram: видно только первые 3 символа."""
    product = str(product or "").strip()
    if len(product) <= 3:
        return "***"
    return product[:3] + "***"


def _file_hash(path: str) -> str:
    return hashlib.md5(str(path).encode("utf-8")).hexdigest()[:10]


def _notify(crd: "Cardinal", text: str) -> None:
    if not crd.telegram:
        return
    kwargs = {"notification_type": tg_utils.NotificationTypes.other} if tg_utils else {}
    try:
        crd.telegram.send_notification(text, **kwargs)
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)


# ══════════════════════════════ СКАНИРОВАНИЕ И АНАЛИЗ ══════════════════════════
def _collect_files(crd: "Cardinal") -> dict:
    """
    Собирает товарные файлы: из конфига автовыдачи + все из storage/products.

    :return: {путь: {"name": имя, "lots": [названия лотов], "orphan": bool}}
    """
    files: dict = {}
    try:
        for title in crd.AD_CFG.sections():
            try:
                fname = crd.AD_CFG[title].get("productsFileName")
            except Exception:
                continue
            if not fname:
                continue
            path = os.path.join(PRODUCTS_DIR, fname)
            entry = files.setdefault(path, {"name": fname, "lots": [], "orphan": False})
            if title not in entry["lots"]:
                entry["lots"].append(title)
    except Exception:
        logger.debug("ProductDoctor: AD_CFG недоступен.")
    try:
        for fname in sorted(os.listdir(PRODUCTS_DIR)):
            path = os.path.join(PRODUCTS_DIR, fname)
            if not os.path.isfile(path) or fname.endswith(".bak"):
                continue
            if path not in files:
                files[path] = {"name": fname, "lots": [], "orphan": True}
    except OSError:
        pass
    return files


def _analyze(path: str) -> dict | None:
    """Анализирует товарный файл. Возвращает None, если файла нет."""
    try:
        with cardinal_tools.get_products_file_lock(path):
            if not os.path.exists(path):
                return None
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
    except Exception:
        logger.error(f"ProductDoctor: не удалось прочитать файл {path}.")
        logger.debug("TRACEBACK", exc_info=True)
        return None

    lines = raw.splitlines()
    products = [l for l in lines if l.strip()]
    stripped_products = [l.strip() for l in products]
    counts = Counter(stripped_products)
    dups = {k: v for k, v in counts.items() if v > 1}
    dup_excess = sum(v - 1 for v in dups.values())
    return {
        "lines": len(lines),
        "products": len(products),
        "empty": len(lines) - len(products),
        "whitespace": sum(1 for l in products if l != l.strip()),
        "dups": dup_excess,
        "dup_items": sorted(dups.items(), key=lambda kv: -kv[1])[:5],
        "short": sum(1 for l in stripped_products if len(l) < SHORT_LINE_LEN),
        "exists": True,
    }


def _clean_file(path: str) -> dict | None:
    """
    Чистит товарный файл: дубликаты (оставляет первое вхождение), пустые строки,
    пробелы по краям. Делает бэкап <path>.bak. Возвращает статистику или None.
    """
    try:
        with cardinal_tools.get_products_file_lock(path):
            if not os.path.exists(path):
                return None
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
            lines = raw.splitlines()
            before = len([l for l in lines if l.strip()])

            seen = set()
            out = []
            removed_dups = removed_empty = stripped = 0
            for line in lines:
                s = line.strip()
                if not s:
                    removed_empty += 1
                    continue
                if s != line:
                    stripped += 1
                if s in seen:
                    removed_dups += 1
                    continue
                seen.add(s)
                out.append(s)

            shutil.copyfile(path, path + ".bak")
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(out) + ("\n" if out else ""))
        result = {"before": before, "after": len(out), "removed_dups": removed_dups,
                  "removed_empty": removed_empty, "stripped": stripped}
        logger.info(f"ProductDoctor: очистил {path}: -{removed_dups} дубликатов, "
                    f"-{removed_empty} пустых, {stripped} обрезано пробелов.")
        return result
    except Exception:
        logger.error(f"ProductDoctor: не удалось очистить файл {path}.")
        logger.debug("TRACEBACK", exc_info=True)
        return None


def _scan_all(crd: "Cardinal") -> dict:
    """{путь: {"info": данные файла, "analysis": анализ или None}}"""
    result = {}
    for path, info in _collect_files(crd).items():
        result[path] = {"info": info, "analysis": _analyze(path)}
    return result


def _summary(scanned: dict) -> tuple:
    """(всего файлов, файлов с дубликатами, всего дубликатов, файлов с мелкими проблемами)"""
    total = len(scanned)
    with_dups = dups_total = minor = 0
    for entry in scanned.values():
        a = entry["analysis"]
        if not a:
            continue
        if a["dups"]:
            with_dups += 1
            dups_total += a["dups"]
        if a["empty"] or a["whitespace"] or a["short"]:
            minor += 1
    return total, with_dups, dups_total, minor


def _auto_check_tick(crd: "Cardinal") -> list:
    """Авто-проверка: предупреждает о НОВЫХ дубликатах. Возвращает список имен файлов."""
    settings = _load_settings()
    if not settings["auto_check"]:
        return []
    warned = dict(settings["warned"])
    newly = []
    for path, entry in _scan_all(crd).items():
        a = entry["analysis"]
        name = entry["info"]["name"]
        dups = a["dups"] if a else 0
        prev = int(warned.get(name, 0) or 0)
        if dups > prev:
            newly.append((name, dups, path))
            warned[name] = dups
        elif dups == 0 and name in warned:
            del warned[name]
        elif dups < prev:
            warned[name] = dups
    if newly:
        lines = "\n".join(f"• <code>{_escape(n)}</code> — {d} шт." for n, d, _ in newly)
        _notify(crd, f"🚨 <b>Доктор товаров: найдены дубликаты!</b>\n\n{lines}\n\n"
                     f"Дубликаты грозят выдачей одного товара дважды (потеря денег и негативный отзыв).\n"
                     f"Очистите файлы: команда /products → файл → 🧹 Очистить.")
        for n, d, _ in newly:
            logger.warning(f"ProductDoctor: в файле {n} обнаружено дубликатов: {d}.")
    settings = _load_settings()
    settings["warned"] = warned
    _save_settings(settings)
    return [n for n, _, _ in newly]


def _auto_check_loop(crd: "Cardinal") -> None:
    logger.info("ProductDoctor: авто-проверка запущена.")
    last = 0.0
    while not _stop_event.wait(10):
        try:
            if not _plugin_enabled(crd):
                last = time.time()
                continue
            interval = _load_settings()["check_interval"]
            if time.time() - last < interval:
                continue
            last = time.time()
            _auto_check_tick(crd)
        except Exception:
            logger.error("ProductDoctor: ошибка в цикле авто-проверки.")
            logger.debug("TRACEBACK", exc_info=True)


# ══════════════════════════ TELEGRAM-ИНТЕРФЕЙС ══════════════════════════════════
def _home_text(crd: "Cardinal") -> str:
    settings = _load_settings()
    scanned = _scan_all(crd)
    total, with_dups, dups_total, minor = _summary(scanned)
    auto = f"ВКЛ 🟢 (каждые {settings['check_interval'] // 3600} ч.)" if settings["auto_check"] else "ВЫКЛ 🔴"
    text = (f"🧹 <b>{_escape(NAME)}</b> <i>v{VERSION}</i>\n\n"
            f"Товарных файлов: <b>{total}</b>\n")
    if with_dups:
        text += f"🚨 С дубликатами: <b>{with_dups}</b> (лишних копий: <b>{dups_total}</b>)\n"
    else:
        text += "🚨 Дубликаты: <b>не найдены</b> ✅\n"
    text += (f"⚠️ Мелкие проблемы (пустые строки/пробелы): <b>{minor}</b>\n"
             f"🔁 Авто-проверка: <b>{auto}</b>\n\n"
             f"<i>Дубликаты — риск выдать один товар дважды. Очистка делает бэкап (.bak).</i>")
    return text


def _home_kb(crd: "Cardinal", scanned: dict | None = None) -> K:
    settings = _load_settings()
    scanned = scanned if scanned is not None else _scan_all(crd)
    _, with_dups, _, _ = _summary(scanned)
    kb = K(row_width=2)
    kb.add(B("📦 Файлы товаров", None, f"{CB_FILES}:0"))
    if with_dups:
        kb.add(B(f"🧹 Очистить дубликаты везде ({with_dups})", None, CB_CLEANALL))
    kb.add(B(f"🔁 Авто-проверка: {'ВКЛ' if settings['auto_check'] else 'ВЫКЛ'}", None, CB_AUTOCHECK))
    kb.add(B(f"⏱ Интервал: {settings['check_interval'] // 3600} ч.", None, CB_INT))
    kb.add(B("🔄 Пересканировать", None, CB_HOME))
    if CBT:
        kb.add(B("🔙 Назад к плагину", None, f"{CBT.EDIT_PLUGIN}:{UUID}:0"))
    return kb


def _files_kb(crd: "Cardinal", scanned: dict, offset: int) -> K:
    items = sorted(scanned.items(),
                   key=lambda kv: (-(kv[1]["analysis"]["dups"] if kv[1]["analysis"] else 0),
                                   kv[1]["info"]["name"]))
    kb = K(row_width=1)
    for path, entry in items[offset:offset + PER_PAGE]:
        a = entry["analysis"]
        info = entry["info"]
        if a is None:
            mark, count = "❓", "?"
        elif a["dups"]:
            mark, count = "🚨", a["products"]
        elif a["empty"] or a["whitespace"] or a["short"]:
            mark, count = "⚠️", a["products"]
        else:
            mark, count = "✅", a["products"]
        orphan = " 👻" if info["orphan"] else ""
        kb.add(B(f"{mark} {_short(info['name'], 36)}{orphan} ({count} шт.)", None,
                 f"{CB_FILE}:{_file_hash(path)}"))
    pages = max(1, -(-len(items) // PER_PAGE))
    if pages > 1:
        row = []
        if offset > 0:
            row.append(B("⬅️", None, f"{CB_FILES}:{offset - PER_PAGE}"))
        row.append(B(f"{offset // PER_PAGE + 1}/{pages}", None, CB_NOOP))
        if offset + PER_PAGE < len(items):
            row.append(B("➡️", None, f"{CB_FILES}:{offset + PER_PAGE}"))
        kb.row(*row)
    kb.add(B("🏠 Главное меню", None, CB_HOME))
    return kb


def _find_by_hash(crd: "Cardinal", h: str):
    for path, entry in _scan_all(crd).items():
        if _file_hash(path) == h:
            return path, entry
    return None, None


def _file_text(path: str, entry: dict) -> str:
    a = entry["analysis"]
    info = entry["info"]
    text = f"📦 <b>{_escape(info['name'])}</b>\n\n"
    if info["lots"]:
        lots = "\n".join(f"• <code>{_escape(_short(l, 50))}</code>" for l in info["lots"])
        text += f"Лоты:\n{lots}\n\n"
    else:
        text += "<i>👻 Файл не привязан ни к одному лоту автовыдачи («сирота»).</i>\n\n"
    if a is None:
        return text + "❓ Файл не найден на диске."
    text += f"Товаров: <b>{a['products']}</b> (строк всего: {a['lines']})\n"
    if a["dups"]:
        text += f"\n🚨 <b>Дубликаты: {a['dups']} лишних копий</b>\n"
        for product, cnt in a["dup_items"]:
            text += f"• <code>{_escape(_mask(product))}</code> × {cnt}\n"
        text += "<i>(товары показаны маскированно)</i>\n"
    else:
        text += "\n✅ Дубликатов нет\n"
    problems = []
    if a["empty"]:
        problems.append(f"пустых строк: {a['empty']}")
    if a["whitespace"]:
        problems.append(f"строк с пробелами по краям: {a['whitespace']}")
    if a["short"]:
        problems.append(f"подозрительно коротких (&lt;{SHORT_LINE_LEN} симв.): {a['short']}")
    if problems:
        text += "⚠️ " + "; ".join(problems) + "\n"
    bak = path + ".bak"
    if os.path.exists(bak):
        text += f"\n💾 Бэкап последней очистки: <code>{_escape(os.path.basename(bak))}</code>"
    return text


def _file_kb(path: str, entry: dict) -> K:
    kb = K(row_width=2)
    a = entry["analysis"]
    if a and (a["dups"] or a["empty"] or a["whitespace"]):
        kb.add(B("🧹 Очистить файл", None, f"{CB_CLEAN}:{_file_hash(path)}"))
    kb.add(B("🔙 К списку файлов", None, f"{CB_FILES}:0"))
    return kb


def _answer(bot, c: CallbackQuery, text: str | None = None, alert: bool = False) -> None:
    try:
        bot.answer_callback_query(c.id, text, show_alert=alert)
    except Exception:
        pass


def _edit_or_send(bot, c: CallbackQuery, text: str, kb: K | None) -> None:
    try:
        bot.edit_message_text(text, c.message.chat.id, c.message.id, parse_mode="HTML", reply_markup=kb)
    except ApiTelegramException as e:
        if "message is not modified" not in str(e):
            try:
                bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
            except Exception:
                logger.debug("TRACEBACK", exc_info=True)
    except Exception:
        try:
            bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)


def init(crd: "Cardinal", *args) -> None:
    """Инициализация плагина (BIND_TO_PRE_INIT)."""
    global _cardinal
    _cardinal = crd
    _load_settings()

    tg = crd.telegram
    if tg is not None:
        bot = tg.bot

        def open_home(c: CallbackQuery) -> None:
            _answer(bot, c)
            scanned = _scan_all(crd)
            _edit_or_send(bot, c, _home_text(crd), _home_kb(crd, scanned))

        def open_files(c: CallbackQuery) -> None:
            _answer(bot, c)
            offset = 0
            parts = str(c.data).split(":")
            if len(parts) > 2 and parts[2].isdigit():
                offset = int(parts[2])
            scanned = _scan_all(crd)
            total, with_dups, dups_total, _ = _summary(scanned)
            text = (f"📦 <b>Товарные файлы</b> ({total})\n\n"
                    f"🚨 дубликаты ({with_dups} файл.) | ⚠️ мелкие проблемы | ✅ чисто | 👻 не привязан к лоту")
            _edit_or_send(bot, c, text, _files_kb(crd, scanned, offset))

        def open_file(c: CallbackQuery) -> None:
            _answer(bot, c)
            h = str(c.data).split(":")[2]
            path, entry = _find_by_hash(crd, h)
            if entry is None:
                _edit_or_send(bot, c, "❓ Файл не найден (конфиг изменился?).",
                              K().add(B("🔙 К списку", None, f"{CB_FILES}:0")))
                return
            _edit_or_send(bot, c, _file_text(path, entry), _file_kb(path, entry))

        def clean_file(c: CallbackQuery) -> None:
            h = str(c.data).split(":")[2]
            path, entry = _find_by_hash(crd, h)
            if entry is None:
                _answer(bot, c, "Файл не найден.", alert=True)
                return
            stats = _clean_file(path)
            if stats is None:
                _answer(bot, c, "Не удалось очистить файл (нет на диске?).", alert=True)
                return
            _answer(bot, c, f"🧹 Очищено: -{stats['removed_dups']} дублей, "
                            f"-{stats['removed_empty']} пустых, {stats['stripped']} обрезано. "
                            f"Бэкап: .bak", alert=True)
            _, entry = _find_by_hash(crd, h)
            if entry is not None:
                _edit_or_send(bot, c, _file_text(path, entry), _file_kb(path, entry))

        def ask_clean_all(c: CallbackQuery) -> None:
            _answer(bot, c)
            kb = K(row_width=2).add(B("✅ Очистить все", None, CB_CLEANALLY),
                                    B("❌ Отмена", None, CB_HOME))
            _edit_or_send(bot, c, "🧹 <b>Очистить дубликаты во ВСЕХ файлах?</b>\n\n"
                                  "Пустые строки и лишние пробелы тоже будут убраны.\n"
                                  "Для каждого файла создастся бэкап (.bak).", kb)

        def clean_all(c: CallbackQuery) -> None:
            _answer(bot, c, "🧹 Очищаю все файлы...")
            scanned = _scan_all(crd)
            cleaned = dups = 0
            for path, entry in scanned.items():
                a = entry["analysis"]
                if a and (a["dups"] or a["empty"] or a["whitespace"]):
                    stats = _clean_file(path)
                    if stats:
                        cleaned += 1
                        dups += stats["removed_dups"]
            scanned = _scan_all(crd)
            text = _home_text(crd) + f"\n\n✅ <b>Массовая очистка завершена:</b> файлов обработано {cleaned}, удалено дубликатов {dups}."
            _edit_or_send(bot, c, text, _home_kb(crd, scanned))

        def toggle_autocheck(c: CallbackQuery) -> None:
            settings = _load_settings()
            settings["auto_check"] = not settings["auto_check"]
            _save_settings(settings)
            _answer(bot, c, f"🔁 Авто-проверка: {'ВКЛ' if settings['auto_check'] else 'ВЫКЛ'}")
            scanned = _scan_all(crd)
            _edit_or_send(bot, c, _home_text(crd), _home_kb(crd, scanned))

        def act_interval(c: CallbackQuery) -> None:
            s = _load_settings()
            result = bot.send_message(
                c.message.chat.id,
                f"⏱ Введите интервал авто-проверки в ЧАСАХ "
                f"(от {INTERVAL_RANGE[0] // 3600} до {INTERVAL_RANGE[1] // 3600}).\n\n"
                f"Текущее значение: <b>{s['check_interval'] // 3600}</b> ч.",
                parse_mode="HTML", reply_markup=skb.CLEAR_STATE_BTN() if skb else None)
            tg.set_state(c.message.chat.id, result.id, c.from_user.id, ST_INT)
            _answer(bot, c)

        def _state_eq(m: Message, state: str) -> bool:
            try:
                return tg.check_state(m.chat.id, m.from_user.id, state)
            except Exception:
                return False

        def edit_interval(m: Message) -> None:
            tg.clear_state(m.chat.id, m.from_user.id, True)
            try:
                hours = int(str(m.text).strip())
            except (ValueError, AttributeError):
                bot.reply_to(m, "❌ Нужно ввести число часов. Попробуйте еще раз.")
                return
            settings = _load_settings()
            settings["check_interval"] = _clamp(hours * 3600, *INTERVAL_RANGE)
            _save_settings(settings)
            bot.reply_to(m, f"✅ Интервал авто-проверки: <b>{settings['check_interval'] // 3600}</b> ч.",
                         parse_mode="HTML")

        def cmd_open(m: Message) -> None:
            scanned = _scan_all(crd)
            bot.send_message(m.chat.id, _home_text(crd), parse_mode="HTML",
                             reply_markup=_home_kb(crd, scanned))

        def noop(c: CallbackQuery) -> None:
            _answer(bot, c)

        tg.cbq_handler(open_home, lambda c: c.data.startswith(f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
        tg.cbq_handler(open_home, lambda c: c.data == CB_HOME)
        tg.cbq_handler(open_files, lambda c: c.data.startswith(f"{CB_FILES}:"))
        tg.cbq_handler(open_file, lambda c: c.data.startswith(f"{CB_FILE}:"))
        tg.cbq_handler(clean_file, lambda c: c.data.startswith(f"{CB_CLEAN}:"))
        tg.cbq_handler(ask_clean_all, lambda c: c.data == CB_CLEANALL)
        tg.cbq_handler(clean_all, lambda c: c.data == CB_CLEANALLY)
        tg.cbq_handler(toggle_autocheck, lambda c: c.data == CB_AUTOCHECK)
        tg.cbq_handler(act_interval, lambda c: c.data == CB_INT)
        tg.cbq_handler(noop, lambda c: c.data == CB_NOOP)
        tg.msg_handler(edit_interval, func=lambda m: _state_eq(m, ST_INT))
        tg.msg_handler(cmd_open, commands=["products"])

        try:
            crd.add_telegram_commands(UUID, [("products", "Доктор товарных файлов (дубликаты, очистка).", True)])
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)

        logger.info(f"ProductDoctor v{VERSION}: Telegram-меню зарегистрировано (команда /products).")
    else:
        logger.info(f"ProductDoctor v{VERSION}: Telegram выключен — работает только авто-проверка.")

    threading.Thread(target=_auto_check_loop, args=(crd,), daemon=True).start()


def on_delete(crd: "Cardinal", c=None) -> None:
    """Вызывается при удалении плагина через ПУ (BIND_TO_DELETE)."""
    _stop_event.set()


# ══════════════════════════ ПРИВЯЗКА К СОБЫТИЯМ КАРДИНАЛА ═══════════════════════
BIND_TO_PRE_INIT = [init]
BIND_TO_DELETE = on_delete
