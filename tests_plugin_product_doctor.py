"""
Функциональные тесты плагина product_doctor.py на моках.
Запуск: cd FPC && python3 tests_plugin_product_doctor.py
"""
import importlib.util
import json
import os
import shutil
import sys
import time
from configparser import ConfigParser
from types import SimpleNamespace

sys.path.insert(0, ".")
sys.path.append("plugins")

spec = importlib.util.spec_from_file_location("plugins.product_doctor", "plugins/product_doctor.py")
plugin = importlib.util.module_from_spec(spec)
sys.modules["plugins.product_doctor"] = plugin
spec.loader.exec_module(plugin)

plugin._auto_check_loop = lambda crd: None  # поток тестируем через _auto_check_tick

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("✓ " if cond else "✗ FAIL: ") + name)


def reset_settings(**kwargs):
    os.makedirs(plugin.PLUGIN_DIR, exist_ok=True)
    base = json.loads(json.dumps(plugin.DEFAULT_SETTINGS))
    base.update(kwargs)
    with open(plugin.SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(base, f, ensure_ascii=False)


PRODUCTS_DIR = "storage/products"
F_DUPS = "pd_test_dups.txt"
F_CLEAN = "pd_test_clean.txt"
F_ORPHAN = "pd_test_orphan.txt"


def write_products(fname, content):
    os.makedirs(PRODUCTS_DIR, exist_ok=True)
    with open(os.path.join(PRODUCTS_DIR, fname), "w", encoding="utf-8") as f:
        f.write(content)


def read_products(fname):
    with open(os.path.join(PRODUCTS_DIR, fname), "r", encoding="utf-8") as f:
        return f.read()


def cleanup_files():
    for fname in os.listdir(PRODUCTS_DIR) if os.path.isdir(PRODUCTS_DIR) else []:
        if fname.startswith("pd_test_") or fname.startswith("stkm_test_"):
            try:
                os.remove(os.path.join(PRODUCTS_DIR, fname))
            except OSError:
                pass


def make_ad_cfg():
    cfg = ConfigParser(delimiters=(":",), interpolation=None)
    cfg.optionxform = str
    cfg["Lot Dups"] = {"response": "Товар: $product", "productsFileName": F_DUPS}
    cfg["Lot Clean"] = {"response": "Товар: $product", "productsFileName": F_CLEAN}
    return cfg


# ───────────────────────────── моки ─────────────────────────────
class MockBot:
    def __init__(self):
        self.answers, self.edits, self.sent, self.replies = [], [], [], []

    def answer_callback_query(self, cb_id, text=None, show_alert=False):
        self.answers.append((cb_id, text, show_alert))

    def edit_message_text(self, text, chat_id, message_id, parse_mode=None, reply_markup=None):
        self.edits.append((text, reply_markup))
        return SimpleNamespace(id=message_id)

    def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))
        return SimpleNamespace(id=len(self.sent) + 100, chat=SimpleNamespace(id=chat_id))

    def reply_to(self, m, text, parse_mode=None):
        self.replies.append(text)

    def delete_message(self, chat_id, message_id):
        pass


def kb_buttons(kb) -> str:
    if kb is None:
        return ""
    try:
        return " | ".join(b.text for row in kb.keyboard for b in row if b.text)
    except Exception:
        return str(kb)


class MockTelegram:
    def __init__(self):
        self.bot = MockBot()
        self.cbq_handlers, self.msg_handlers, self.user_states = [], [], {}
        self.notification_settings, self.notifications = {}, []

    def cbq_handler(self, handler, func, **kwargs):
        self.cbq_handlers.append((handler, func))

    def msg_handler(self, handler, **kwargs):
        self.msg_handlers.append((handler, kwargs))

    def set_state(self, chat_id, message_id, user_id, state, data=None):
        self.user_states[(chat_id, user_id)] = {"state": state, "mid": message_id, "data": data or {}}

    def get_state(self, chat_id, user_id):
        return self.user_states.get((chat_id, user_id))

    def check_state(self, chat_id, user_id, state):
        return self.user_states.get((chat_id, user_id), {}).get("state") == state

    def clear_state(self, chat_id, user_id, del_msg=False):
        st = self.user_states.pop((chat_id, user_id), None)
        return st.get("mid") if st else None

    def send_notification(self, text, notification_type=None, **kwargs):
        self.notifications.append((text, notification_type))


class MockCardinal:
    def __init__(self, telegram):
        self.telegram = telegram
        self.AD_CFG = make_ad_cfg()
        self.plugins = {plugin.UUID: SimpleNamespace(enabled=True)}
        self.registered_commands = None

    def add_telegram_commands(self, uuid, commands):
        self.registered_commands = commands


def make_crd(with_tg=True):
    tg = MockTelegram() if with_tg else None
    return MockCardinal(tg), tg


# ═══════════════════════ ТЕСТ 1: метаданные ═══════════════════════
from uuid import UUID as _UUID
check("1.1 метаданные плагина", all(hasattr(plugin, f) for f in
      ["NAME", "VERSION", "DESCRIPTION", "CREDITS", "SETTINGS_PAGE", "UUID", "BIND_TO_DELETE"]))
check("1.2 UUID валиден", str(_UUID(plugin.UUID, version=4)) == plugin.UUID)

# ═══════════════════════ ТЕСТ 2: сбор и анализ файлов ═══════════════════════
cleanup_files()
write_products(F_DUPS, "login1:pass1\nlogin2:pass2\nlogin1:pass1\n\n  login3:pass3  \nlogin1:pass1\nab\n")
write_products(F_CLEAN, "good1:pass\ngood2:pass\ngood3:pass\n")
write_products(F_ORPHAN, "orph1:pass\norph1:pass\n")
write_products(F_DUPS + ".bak", "old backup")

crd, tg = make_crd(with_tg=False)
files = plugin._collect_files(crd)
paths = {info["name"]: (p, info) for p, info in files.items()}
check("2.1 файлы из AD_CFG собраны с привязкой к лотам",
      F_DUPS in paths and paths[F_DUPS][1]["lots"] == ["Lot Dups"] and not paths[F_DUPS][1]["orphan"])
check("2.2 файл-сирота найден и помечен", F_ORPHAN in paths and paths[F_ORPHAN][1]["orphan"] is True)
check("2.3 .bak-файлы исключены", not any(n.endswith(".bak") for n in paths))

a = plugin._analyze(os.path.join(PRODUCTS_DIR, F_DUPS))
check("2.4 анализ: дубликаты (login1 ×3 → 2 лишних)", a["dups"] == 2)
check("2.5 анализ: пустые строки", a["empty"] == 1)
check("2.6 анализ: пробелы по краям", a["whitespace"] == 1)
check("2.7 анализ: короткая подозрительная строка", a["short"] == 1)
check("2.8 анализ: товаров (непустых строк) 6", a["products"] == 6)
check("2.9 dup_items содержит маскируемые значения", a["dup_items"] and a["dup_items"][0][1] == 3)

a2 = plugin._analyze(os.path.join(PRODUCTS_DIR, F_CLEAN))
check("2.10 чистый файл: проблем нет",
      a2["dups"] == 0 and a2["empty"] == 0 and a2["whitespace"] == 0)
check("2.11 отсутствующий файл → None",
      plugin._analyze(os.path.join(PRODUCTS_DIR, "нет_такого.txt")) is None)

check("2.12 маскирование: секрет не виден целиком",
      plugin._mask("login1:pass1") == "log***" and "pass1" not in plugin._mask("login1:pass1"))
check("2.13 маскирование коротких строк", plugin._mask("ab") == "***")

# ═══════════════════════ ТЕСТ 3: очистка файла ═══════════════════════
path_dups = os.path.join(PRODUCTS_DIR, F_DUPS)
stats = plugin._clean_file(path_dups)
content = read_products(F_DUPS)
check("3.1 статистика очистки", stats["removed_dups"] == 2 and stats["removed_empty"] >= 1
      and stats["stripped"] == 1 and stats["after"] == 4)
check("3.2 дубликаты удалены, порядок сохранен (первое вхождение), короткая строка не тронута",
      content.splitlines() == ["login1:pass1", "login2:pass2", "login3:pass3", "ab"])
check("3.3 бэкап создан и содержит исходник",
      os.path.exists(path_dups + ".bak") and "login1:pass1" in read_products(F_DUPS + ".bak")
      and read_products(F_DUPS + ".bak").count("login1:pass1") == 3)
check("3.4 повторная очистка идемпотентна", plugin._clean_file(path_dups)["removed_dups"] == 0)

# ═══════════════════════ ТЕСТ 4: авто-проверка ═══════════════════════
crd, tg = make_crd(with_tg=True)
write_products(F_DUPS, "a1:pass\na1:pass\na2:pass\n")
reset_settings(auto_check=True, warned={})
tg.notifications.clear()
newly = plugin._auto_check_tick(crd)
check("4.1 авто-проверка нашла новые дубликаты", F_DUPS in newly)
check("4.2 уведомление 🚨 с именем файла (маскированно, без секретов)",
      any("🚨" in t and F_DUPS in t for t, _ in tg.notifications)
      and not any("a1:pass" in t for t, _ in tg.notifications))
check("4.3 warned-состояние сохранено", plugin._load_settings()["warned"].get(F_DUPS) == 1)

tg.notifications.clear()
newly = plugin._auto_check_tick(crd)
check("4.4 повторный тик — без спама", newly == [] and tg.notifications == [])

write_products(F_DUPS, "a1:pass\na1:pass\na1:pass\na2:pass\n")
tg.notifications.clear()
newly = plugin._auto_check_tick(crd)
check("4.5 дубликатов стало больше — снова предупредили", F_DUPS in newly
      and plugin._load_settings()["warned"].get(F_DUPS) == 2)

plugin._clean_file(os.path.join(PRODUCTS_DIR, F_DUPS))
tg.notifications.clear()
newly = plugin._auto_check_tick(crd)
check("4.6 после очистки warned-состояние сброшено",
      F_DUPS not in plugin._load_settings()["warned"])

reset_settings(auto_check=False)
write_products(F_DUPS, "b1:pass\nb1:pass\n")
tg.notifications.clear()
newly = plugin._auto_check_tick(crd)
check("4.7 auto_check=False — проверок нет", newly == [] and tg.notifications == [])

# ═══════════════════════ ТЕСТ 5: Telegram UI ═══════════════════════
crd, tg = make_crd(with_tg=True)
plugin.init(crd)
bot = tg.bot
write_products(F_DUPS, "c1:pass\nc1:pass\nc2:pass\n")
write_products(F_CLEAN, "good1:pass\ngood2:pass\n")
write_products(F_ORPHAN, "orph1:pass\n")  # сирота без дубликатов для детерминизма
reset_settings()
check("5.1 команда /products зарегистрирована",
      any(kw.get("commands") == ["products"] for _, kw in tg.msg_handlers))


def dispatch_cb(data, chat_id=111, msg_id=555, user_id=222):
    c = SimpleNamespace(data=data, id="cb1",
                        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), id=msg_id),
                        from_user=SimpleNamespace(id=user_id))
    for handler, func in tg.cbq_handlers:
        try:
            matched = func(c)
        except Exception:
            matched = False
        if matched:
            handler(c)
            return handler.__name__
    return None


def dispatch_msg(text, chat_id=111, user_id=222):
    m = SimpleNamespace(chat=SimpleNamespace(id=chat_id), from_user=SimpleNamespace(id=user_id),
                        text=text)
    for h, kw in tg.msg_handlers:
        f = kw.get("func")
        try:
            if f and f(m):
                h(m)
                return h.__name__
        except Exception:
            pass
    return None


def last_text():
    if bot.edits:
        return bot.edits[-1][0]
    return bot.sent[-1][1] if bot.sent else ""


def last_kb():
    return kb_buttons(bot.edits[-1][1]) if bot.edits else ""


name = dispatch_cb(f"47:{plugin.UUID}:0")
check("5.2 страница настроек открывает меню", name == "open_home"
      and "Доктор товарных файлов" in last_text())
check("5.3 главная показывает счетчик дубликатов", "С дубликатами: <b>1</b>" in last_text()
      and "лишних копий: <b>1</b>" in last_text())

dispatch_cb(f"{plugin.CB_FILES}:0")
check("5.4 список файлов: проблемный с 🚨 сверху", "🚨" in last_kb() and F_DUPS in last_kb())
check("5.5 сирота помечен 👻", "👻" in last_kb() and F_ORPHAN in last_kb())

hash_dups = plugin._file_hash(os.path.join(PRODUCTS_DIR, F_DUPS))
name = dispatch_cb(f"{plugin.CB_FILE}:{hash_dups}")
check("5.6 страница файла: дубликаты маскированно (секрет не виден), привязка к лоту",
      name == "open_file" and "c1:***" in last_text() and "c1:pass" not in last_text()
      and "Lot Dups" in last_text())

dispatch_cb(f"{plugin.CB_CLEAN}:{hash_dups}")
check("5.7 кнопка «Очистить файл» чистит и перерисовывает",
      plugin._analyze(os.path.join(PRODUCTS_DIR, F_DUPS))["dups"] == 0
      and "Дубликатов нет" in last_text())

# массовая очистка
write_products(F_DUPS, "d1:pass\nd1:pass\n")
write_products(F_ORPHAN, "o1:pass\no1:pass\n")
dispatch_cb(plugin.CB_CLEANALL)
check("5.8 запрос подтверждения массовой очистки", "во ВСЕХ файлах" in last_text())
name = dispatch_cb(plugin.CB_CLEANALLY)
check("5.9 массовая очистка выполнена",
      name == "clean_all"
      and plugin._analyze(os.path.join(PRODUCTS_DIR, F_DUPS))["dups"] == 0
      and plugin._analyze(os.path.join(PRODUCTS_DIR, F_ORPHAN))["dups"] == 0
      and "Массовая очистка завершена" in last_text())

name = dispatch_cb(plugin.CB_AUTOCHECK)
check("5.10 авто-проверка переключается", name == "toggle_autocheck"
      and plugin._load_settings()["auto_check"] is False)
dispatch_cb(plugin.CB_AUTOCHECK)
check("5.11 и включается обратно", plugin._load_settings()["auto_check"] is True)

dispatch_cb(plugin.CB_INT)
st = tg.user_states.get((111, 222), {})
check("5.12 кнопка интервала ставит состояние", st.get("state") == plugin.ST_INT)
dispatch_msg("2")
check("5.13 интервал сохранен (2 ч = 7200 с)", plugin._load_settings()["check_interval"] == 7200)
dispatch_cb(plugin.CB_INT)
dispatch_msg("0")
check("5.14 интервал зажимается (0 → 30 мин)", plugin._load_settings()["check_interval"] == 1800)
dispatch_cb(plugin.CB_INT)
dispatch_msg("abc")
check("5.15 нечисловой ввод отклоняется", plugin._load_settings()["check_interval"] == 1800
      and "❌" in bot.replies[-1])
dispatch_cb(f"{plugin.CB_FILE}:deadbeef00")
check("5.16 неизвестный хэш файла — вежливая заглушка", "не найден" in last_text())

# ═══════════════════════ ТЕСТ 6: нормализация настроек ═══════════════════════
os.makedirs(plugin.PLUGIN_DIR, exist_ok=True)
with open(plugin.SETTINGS_PATH, "w", encoding="utf-8") as f:
    json.dump({"auto_check": 1, "check_interval": 10, "warned": None}, f)
s = plugin._load_settings()
check("6.1 битые значения нормализуются", s["auto_check"] is True
      and s["check_interval"] == 1800 and s["warned"] == {})
with open(plugin.SETTINGS_PATH, "w") as f:
    f.write("{broken")
s = plugin._load_settings()
check("6.2 битый JSON → дефолты", s["check_interval"] == 21600)

cleanup_files()
reset_settings()

print(f"\n{'=' * 60}\nПройдено: {len(PASS)}  Провалено: {len(FAIL)}")
if FAIL:
    print("Проваленные тесты:")
    for f_ in FAIL:
        print("  ✗", f_)
    sys.exit(1)
print("ВСЕ ТЕСТЫ ПРОЙДЕНЫ ✅")
