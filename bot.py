import asyncio
import logging
import math
import os
import sqlite3
import time
from datetime import datetime

import aiohttp
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
SUPPORT = os.getenv("SUPPORT_USERNAME", "@admin")
CARD_NUMBER = os.getenv("CARD_NUMBER", "")
CARD_OWNER = os.getenv("CARD_OWNER", "")
MIN_TOPUP = int(os.getenv("MIN_TOPUP", "5000"))
START_BONUS = 0

# --- Nakrutka (SMM panel) sozlamalari ---
SMM_URL = os.getenv("SMM_API_URL", "https://topsmm.uz/api/v2")
SMM_KEY = os.getenv("SMM_API_KEY", "")
RUB_TO_UZS = float(os.getenv("RUB_TO_UZS", "150"))   # 1 rubl necha so'm (o'zingiz yangilang!)
MARGIN = float(os.getenv("MARGIN_PERCENT", "30"))    # sizning foydangiz, %
AUTO_APPROVE = os.getenv("AUTO_APPROVE", "0") == "1"  # 1 = admin tasdiqlamasdan avto yuboradi
TEST_MODE = os.getenv("TEST_MODE", "1") == "1"        # 1 = API'ga haqiqiy buyurtma yuborilmaydi

logging.basicConfig(level=logging.INFO)
dp = Dispatcher()

# ---------- Ma'lumotlar bazasi ----------
DB_PATH = os.getenv("DB_PATH", "bot.db")  # Railway Volume uchun: /data/bot.db
os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT,
    full_name TEXT,
    balance INTEGER DEFAULT 0,
    orders_count INTEGER DEFAULT 0,
    referrer_id INTEGER,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS channels (
    chat_id INTEGER PRIMARY KEY,
    title TEXT,
    link TEXT
);
CREATE TABLE IF NOT EXISTS pay_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    amount INTEGER,
    status TEXT DEFAULT 'pending',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT,
    title TEXT,
    price INTEGER
);
CREATE TABLE IF NOT EXISTS man_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    kind TEXT,
    product_id INTEGER,
    title TEXT,
    target TEXT,
    price INTEGER,
    refunded INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS nk_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    service_id INTEGER,
    service_name TEXT,
    link TEXT,
    quantity INTEGER,
    price INTEGER,
    refunded INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',
    api_order TEXT,
    created_at TEXT
);
"""
)
db.commit()

SEED = [
    ("stars", "⭐ 50 Stars", 13000), ("stars", "⭐ 100 Stars", 25000),
    ("stars", "⭐ 250 Stars", 62000), ("stars", "⭐ 500 Stars", 124000),
    ("stars", "⭐ 1000 Stars", 248000),
    ("premium", "💎 Premium 3 oy", 180000), ("premium", "💎 Premium 6 oy", 240000),
    ("premium", "💎 Premium 12 oy", 420000),
    ("gift", "🧸 Ayiqcha gift", 15000), ("gift", "❤️ Yurak gift", 15000),
    ("gift", "🌹 Atirgul gift", 30000), ("gift", "🎁 Sovg'a qutisi", 30000),
    ("number", "🇺🇿 O'zbekiston raqami", 15000), ("number", "🇷🇺 Rossiya raqami", 20000),
    ("number", "🇺🇸 AQSh raqami", 25000),
]
if not db.execute("SELECT 1 FROM products").fetchone():
    db.executemany("INSERT INTO products (kind, title, price) VALUES (?,?,?)", SEED)
    db.commit()


def get_user(uid):
    return db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def add_user(uid, username, full_name, referrer=None):
    if get_user(uid):
        return False
    db.execute(
        "INSERT INTO users (id, username, full_name, balance, referrer_id, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (uid, username, full_name, START_BONUS, referrer, datetime.now().isoformat(timespec="seconds")),
    )
    db.commit()
    return True


def change_balance(uid, delta):
    db.execute("UPDATE users SET balance = balance + ? WHERE id=?", (delta, uid))
    db.commit()


def fmt(n):
    return f"{int(n):,}".replace(",", " ")


# ---------- Admin paneldan o'zgartiriladigan sozlamalar ----------
def setting(key, default):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    db.commit()


def cur_rate():
    return float(setting("rub_to_uzs", str(RUB_TO_UZS)))


def cur_margin():
    return float(setting("margin", str(MARGIN)))


def test_mode():
    return setting("test_mode", "1" if TEST_MODE else "0") == "1"


def auto_approve():
    return setting("auto_approve", "1" if AUTO_APPROVE else "0") == "1"


def get_channels():
    return db.execute("SELECT * FROM channels").fetchall()


# ---------- Menyular ----------
def main_menu(is_admin=False):
    kb = InlineKeyboardBuilder()
    kb.button(text="🛍 Do'kon — barcha xizmatlar bir joyda", callback_data="shop")
    kb.button(text="📱 Raqam olish", callback_data="svc:number")
    kb.button(text="⭐ Stars olish", callback_data="svc:stars")
    kb.button(text="💎 Premium olish", callback_data="svc:premium")
    kb.button(text="🎁 Gift olish", callback_data="svc:gift")
    kb.button(text="📈 Nakrutka", callback_data="nk")
    kb.button(text="👤 Profil", callback_data="profile")
    kb.button(text="🧾 Buyurtmalar", callback_data="orders")
    kb.button(text="🏆 Top", callback_data="top")
    kb.button(text="🆘 Qo'llab-quvvatlash", callback_data="support")
    kb.button(text="💳 Hisobni to'ldirish", callback_data="topup")
    sizes = [1, 1, 2, 1, 1, 2, 2, 1]
    if is_admin:
        kb.button(text="🛠 Admin panel", callback_data="adm")
        sizes.append(1)
    kb.adjust(*sizes)
    return kb.as_markup()


def shop_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="📱 Raqam olish", callback_data="svc:number")
    kb.button(text="⭐ Stars olish", callback_data="svc:stars")
    kb.button(text="💎 Premium olish", callback_data="svc:premium")
    kb.button(text="🎁 Gift olish", callback_data="svc:gift")
    kb.button(text="📈 Nakrutka", callback_data="nk")
    kb.button(text="⬅️ Orqaga", callback_data="back")
    kb.adjust(1, 2, 1, 1, 1)
    return kb.as_markup()


def back_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Orqaga", callback_data="back")
    return kb.as_markup()


async def show(call: CallbackQuery, text: str, markup):
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except Exception:
        await call.message.answer(text, reply_markup=markup)
    await call.answer()


# ---------- SMM panel API ----------
async def smm(action, **params):
    data = {"key": SMM_KEY, "action": action, **params}
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post(SMM_URL, data=data) as r:
            return await r.json(content_type=None)


DEMO = [
    {"service": 9001, "name": "DEMO Telegram obunachi (jonli)", "category": "Telegram obunachilar", "rate": 90.0, "min": 50, "max": 10000, "refill": True, "cancel": False},
    {"service": 9002, "name": "DEMO Telegram post ko'rishlar", "category": "Telegram ko'rishlar", "rate": 5.0, "min": 100, "max": 100000, "refill": False, "cancel": False},
    {"service": 9003, "name": "DEMO Instagram obunachi (jonli)", "category": "Instagram obunachilar", "rate": 357.0, "min": 50, "max": 10000, "refill": True, "cancel": True},
    {"service": 9004, "name": "DEMO Instagram layk", "category": "Instagram layklar", "rate": 40.0, "min": 20, "max": 5000, "refill": False, "cancel": True},
]
_cache = {"t": 0.0, "data": []}


async def get_services():
    return visible(await _get_services_raw())


async def _get_services_raw():
    if _cache["data"] and time.time() - _cache["t"] < 600:
        return _cache["data"]
    if not SMM_KEY:
        data = DEMO
    else:
        try:
            raw = await smm("services")
            if not isinstance(raw, list):
                raise ValueError(f"API javobi: {raw}")
            data = []
            for x in raw:
                try:
                    data.append({
                        "service": int(x["service"]),
                        "name": str(x["name"]),
                        "category": str(x.get("category", "Boshqa")),
                        "rate": float(x["rate"]),
                        "min": int(x["min"]),
                        "max": int(x["max"]),
                        "refill": bool(x.get("refill")),
                        "cancel": bool(x.get("cancel")),
                    })
                except (KeyError, ValueError, TypeError):
                    continue
        except Exception:
            logging.exception("services olinmadi")
            return _cache["data"]
    _cache["t"] = time.time()
    _cache["data"] = data
    return data


def hidden_list():
    return [x for x in setting("hidden", "").split("|") if x]


def visible(items):
    hid = [h.lower() for h in hidden_list()]
    if not hid:
        return items
    out = []
    for s in items:
        text = s["name"].lower()
        if any((h == str(s["service"])) or (not h.isdigit() and h in text) for h in hid):
            continue
        out.append(s)
    return out


def sell_per_1000(rate):
    return rate * cur_rate() * (1 + cur_margin() / 100)


def calc_price(rate, qty):
    return max(1, math.ceil(rate / 1000 * qty * cur_rate() * (1 + cur_margin() / 100)))


PLATFORMS = {
    "tg": ("Telegram", "✈️"),
    "ig": ("Instagram", "📸"),
    "yt": ("YouTube", "▶️"),
    "tt": ("TikTok", "🎵"),
}
PAGE = 8


async def platform_services(p):
    key = PLATFORMS[p][0].lower()
    items = await get_services()
    return [s for s in items if key in (s["category"] + " " + s["name"]).lower()]


async def categories(p):
    return sorted({s["category"] for s in await platform_services(p)})


def add_nav(kb, prev_cb, next_cb, back_cb):
    row = []
    if prev_cb:
        row.append(InlineKeyboardButton(text="⬅️", callback_data=prev_cb))
    row.append(InlineKeyboardButton(text="↩️ Orqaga", callback_data=back_cb))
    if next_cb:
        row.append(InlineKeyboardButton(text="➡️", callback_data=next_cb))
    kb.row(*row)


# ---------- Nakrutka: holatlar ----------
class Order(StatesGroup):
    quantity = State()
    link = State()


STATUS_TEXT = {
    "pending": "⏳ Tasdiq kutilmoqda",
    "sent": "🔄 Bajarilmoqda",
    "completed": "✅ Bajarildi",
    "partial": "◐ Qisman bajarildi",
    "canceled": "❌ Bekor qilindi",
    "failed": "⚠️ Xato",
}


def order_row(oid):
    return db.execute("SELECT * FROM nk_orders WHERE id=?", (oid,)).fetchone()


def set_status(oid, status, api_order=None):
    if api_order is None:
        db.execute("UPDATE nk_orders SET status=? WHERE id=?", (status, oid))
    else:
        db.execute("UPDATE nk_orders SET status=?, api_order=? WHERE id=?", (status, api_order, oid))
    db.commit()


def refund(oid, amount):
    o = order_row(oid)
    amount = max(0, min(int(amount), o["price"] - o["refunded"]))
    if amount:
        change_balance(o["user_id"], amount)
        db.execute("UPDATE nk_orders SET refunded = refunded + ? WHERE id=?", (amount, oid))
        db.commit()
    return amount


async def notify(bot, uid, text):
    try:
        await bot.send_message(uid, text)
    except Exception:
        logging.warning("xabar yuborilmadi: %s", uid)


async def process_order(bot, oid):
    """Buyurtmani yetkazib beruvchiga yuboradi. Matn qaytaradi (adminga)."""
    o = order_row(oid)
    if not o or o["status"] != "pending":
        return "Bu buyurtma allaqachon ko'rib chiqilgan."
    if test_mode() or not SMM_KEY:
        set_status(oid, "sent", f"TEST{oid}")
        await notify(bot, o["user_id"], f"🔄 Buyurtmangiz #{oid} qabul qilindi va bajarilmoqda.")
        return f"🧪 #{oid} TEST rejimida 'yuborildi' (haqiqiy buyurtma ketmadi)."
    try:
        res = await smm("add", service=o["service_id"], link=o["link"], quantity=o["quantity"])
    except Exception as e:
        logging.exception("add xato")
        return f"⚠️ #{oid}: API bilan aloqa xatosi ({e}). Buyurtma kutishda, keyinroq qayta urinib ko'ring."
    if isinstance(res, dict) and "order" in res:
        set_status(oid, "sent", str(res["order"]))
        await notify(bot, o["user_id"], f"🔄 Buyurtmangiz #{oid} qabul qilindi va bajarilmoqda.")
        return f"✅ #{oid} yuborildi (panel ID: {res['order']})."
    err = res.get("error") if isinstance(res, dict) else str(res)
    refund(oid, o["price"])
    set_status(oid, "failed")
    await notify(bot, o["user_id"], f"⚠️ Buyurtma #{oid} bajarilmadi. Pul balansingizga qaytarildi.")
    return f"❌ #{oid} API xatosi: {err}\nMijozga pul qaytarildi."


async def track_once(bot):
    rows = db.execute("SELECT * FROM nk_orders WHERE status='sent'").fetchall()
    real = []
    for o in rows:
        if str(o["api_order"]).startswith("TEST"):
            set_status(o["id"], "completed")
            db.execute("UPDATE users SET orders_count = orders_count + 1 WHERE id=?", (o["user_id"],))
            db.commit()
            await notify(bot, o["user_id"], f"✅ Buyurtma #{o['id']} bajarildi (test).")
        else:
            real.append(o)
    for i in range(0, len(real), 100):
        chunk = real[i:i + 100]
        res = await smm("status", orders=",".join(str(o["api_order"]) for o in chunk))
        if not isinstance(res, dict):
            continue
        for o in chunk:
            info = res.get(str(o["api_order"]))
            if not isinstance(info, dict) or "error" in info:
                continue
            st = str(info.get("status", "")).lower()
            oid = o["id"]
            if st == "completed":
                set_status(oid, "completed")
                db.execute("UPDATE users SET orders_count = orders_count + 1 WHERE id=?", (o["user_id"],))
                db.commit()
                await notify(bot, o["user_id"], f"✅ Buyurtma #{oid} bajarildi!")
            elif st == "partial":
                remains = int(float(info.get("remains", 0) or 0))
                back = refund(oid, o["price"] * remains / max(o["quantity"], 1))
                set_status(oid, "partial")
                await notify(bot, o["user_id"], f"◐ Buyurtma #{oid} qisman bajarildi. Qaytarildi: {fmt(back)} so'm.")
            elif st in ("canceled", "cancelled"):
                back = refund(oid, o["price"])
                set_status(oid, "canceled")
                await notify(bot, o["user_id"], f"❌ Buyurtma #{oid} bekor qilindi. Qaytarildi: {fmt(back)} so'm.")


async def tracker(bot):
    while True:
        await asyncio.sleep(30 if test_mode() else 300)
        try:
            await track_once(bot)
        except Exception:
            logging.exception("tracker xato")


# ---------- Buyruqlar ----------
@dp.message(CommandStart())
async def start(msg: Message, state: FSMContext):
    await state.clear()
    referrer = None
    parts = (msg.text or "").split()
    if len(parts) > 1 and parts[1].isdigit() and int(parts[1]) != msg.from_user.id:
        referrer = int(parts[1])
    add_user(msg.from_user.id, msg.from_user.username, msg.from_user.full_name, referrer)
    await msg.answer(
        f"Assalomu alaykum, {msg.from_user.full_name}! 👋\n\nQuyidagi menyudan keraklisini tanlang:",
        reply_markup=main_menu(msg.from_user.id == ADMIN_ID),
    )


@dp.callback_query(F.data == "back")
async def back(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(call, "Quyidagi menyudan keraklisini tanlang:", main_menu(call.from_user.id == ADMIN_ID))


@dp.callback_query(F.data == "shop")
async def shop(call: CallbackQuery):
    await show(call, "🛍 Do'kon — barcha xizmatlar bir joyda:", shop_menu())


# ---------- Stars / Premium / Gift / Raqam (qo'lda bajariladi) ----------
class Man(StatesGroup):
    target = State()


KINDS = {
    "stars": ("⭐ Stars olish", "Stars qabul qiluvchi @username ni yuboring:"),
    "premium": ("💎 Premium olish", "Premium qabul qiluvchi @username ni yuboring:"),
    "gift": ("🎁 Gift olish", "Gift qabul qiluvchi @username ni yuboring:"),
    "number": ("📱 Raqam olish", "Raqam qaysi ilova uchun kerak? (masalan: Telegram):"),
}
KIND_WORDS = {
    "stars": (["stars"], True),
    "premium": (["premium"], True),
    "gift": (["gift", "sovg"], True),
    "number": (["raqam", "nomer", "number", "sms"], False),
}
MAN_STATUS = {"pending": "⏳ Kutilmoqda", "done": "✅ Bajarildi", "canceled": "❌ Bekor qilindi"}


# ---------- Raqam olish: coresmm.uz API (avtomatik) ----------
CORE_URL = os.getenv("CORE_API_URL", "https://coresmm.uz/api/v1").rstrip("/")
CORE_KEY = os.getenv("CORE_API_KEY", "")
CORE_LIVE = os.getenv("CORE_LIVE", "0") == "1"           # 1 = haqiqiy raqam olinadi (pul ketadi!)
NUM_MARGIN = float(os.getenv("NUM_MARGIN_PERCENT", "20"))  # raqamdan foydangiz, %
NUM_PAGE = 10

db.executescript(
    """
CREATE TABLE IF NOT EXISTS num_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    country TEXT,
    country_name TEXT,
    price INTEGER,
    api_id TEXT,
    phone TEXT,
    code TEXT,
    status TEXT DEFAULT 'waiting',
    refunded INTEGER DEFAULT 0,
    created_at TEXT
);
"""
)
db.commit()

_core_cache = {"t": 0.0, "data": []}


async def core(method, path, **params):
    headers = {"X-Api-Key": CORE_KEY}
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
        if method == "GET":
            r = await s.get(f"{CORE_URL}{path}", params=params or None)
        else:
            r = await s.post(f"{CORE_URL}{path}", json=params)
        return await r.json(content_type=None)


def _first(d, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def _as_list(res):
    if isinstance(res, list):
        return res
    if isinstance(res, dict):
        for k in ("countries", "data", "items", "result"):
            if isinstance(res.get(k), list):
                return res[k]
    return []


def num_sell_price(cost):
    return int(math.ceil(float(cost) * (1 + NUM_MARGIN / 100) / 100.0) * 100)


async def core_countries():
    if time.time() - _core_cache["t"] < 60 and _core_cache["data"]:
        return _core_cache["data"]
    res = await core("GET", "/countries")
    out = []
    for it in _as_list(res):
        code = _first(it, "code", "country", "iso", "id")
        price = _first(it, "price", "cost", "amount")
        if code is None or price is None:
            continue
        stock = _first(it, "available", "count", "stock", "qty", default=1)
        try:
            if int(stock) <= 0:
                continue
        except (TypeError, ValueError):
            pass
        out.append({"code": str(code), "name": str(_first(it, "name", "title", default=code)),
                    "cost": float(price), "stock": stock})
    out.sort(key=lambda x: x["cost"])
    _core_cache.update(t=time.time(), data=out)
    return out


# Serverlar: (raqam, nom, tavsif). Faqat CORE_SERVERS_ON dagilar ishlaydi (hujjatga ko'ra hozir faqat Server 1 API'da bor).
NUM_SERVERS = [
    (1, "Server 1 - Kafolatli", "Spamsiz, toza akauntlar"),
    (2, "Server 2 - Arzon (kafolat 50%)", "Kod bilan keladi, past narx"),
    (3, "Server 3 - Spam-blokli", "Spam-blok bor: notanishlarga birinchi yozib bo'lmaydi. Arzonroq"),
    (4, "Server 4 - Kafolatsiz", "Eng arzon, qanday bo'lsa shunday — kafolat yo'q"),
]
NUM_ON = {int(x) for x in os.getenv("CORE_SERVERS_ON", "1").split(",") if x.strip().isdigit()}
NUM_EMOJI = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣"}


@dp.callback_query(F.data == "svc:number")
async def num_menu(call: CallbackQuery):
    lines = ["😎 Telegram uchun tayyor raqamlarni (akauntlarni) xarid qilishingiz mumkin!", "", "🚀 Server tanlang:", ""]
    kb = InlineKeyboardBuilder()
    for n, name, desc in NUM_SERVERS:
        soon = "" if n in NUM_ON else " (tez kunda)"
        lines.append(f"{NUM_EMOJI[n]} {name} — {desc}{soon}")
        kb.button(text=("📲 " if n in NUM_ON else "🔒 ") + name, callback_data=f"nsv:{n}")
    lines += ["", "⚠️ Virtual raqamlarni rasmiy Telegram ilovasida ishlatmang!"]
    kb.button(text="⬅️ Orqaga", callback_data="back")
    kb.adjust(1)
    await show(call, "\n".join(lines), kb.as_markup())


@dp.callback_query(F.data.startswith("nsv:"))
async def num_server(call: CallbackQuery):
    n = int(call.data.split(":")[1])
    if n not in NUM_ON:
        await call.answer("Bu server hozircha ulanmagan. Tez kunda!", show_alert=True)
        return
    await num_page(call, 0)


@dp.callback_query(F.data.startswith("npg:"))
async def num_page_cb(call: CallbackQuery):
    await num_page(call, int(call.data.split(":")[1]))


async def num_page(call: CallbackQuery, page: int):
    if not CORE_KEY:
        await show(call, "📱 Raqam xizmati hozircha sozlanmagan.", back_menu())
        return
    try:
        items = await core_countries()
    except Exception:
        logging.exception("countries xato")
        await show(call, "⚠️ Xizmat vaqtincha ishlamayapti. Keyinroq urinib ko'ring.", back_menu())
        return
    if not items:
        await show(call, "😔 Hozircha raqam yo'q.", back_menu())
        return
    pages = max(1, math.ceil(len(items) / NUM_PAGE))
    page = max(0, min(page, pages - 1))
    kb = InlineKeyboardBuilder()
    for it in items[page * NUM_PAGE:(page + 1) * NUM_PAGE]:
        kb.button(text=f"{it['name']} — {fmt(num_sell_price(it['cost']))} so'm", callback_data=f"nc:{it['code']}")
    kb.adjust(1)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"npg:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"npg:{page + 1}"))
    kb.row(*nav)
    kb.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data="svc:number"))
    await show(call, "📱 Server 1 - Kafolatli\n\nDavlatni tanlang:", kb.as_markup())


@dp.callback_query(F.data == "noop")
async def noop(call: CallbackQuery):
    await call.answer()


@dp.callback_query(F.data.startswith("nc:"))
async def num_country(call: CallbackQuery):
    code = call.data.split(":", 1)[1]
    items = {x["code"]: x for x in await core_countries()}
    it = items.get(code)
    if not it:
        await show(call, "Bu davlat hozir mavjud emas.", back_menu())
        return
    price = num_sell_price(it["cost"])
    u = get_user(call.from_user.id)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Raqam olish", callback_data=f"ncok:{code}")
    kb.button(text="⬅️ Orqaga", callback_data="npg:0")
    kb.adjust(1)
    await show(call, f"📱 {it['name']}\n💰 Narx: {fmt(price)} so'm\n💳 Balansingiz: {fmt(u['balance'])} so'm\n\n"
                     f"Raqam olgach, uni Telegram'ga kiriting. Kod bu yerga avtomatik keladi (30 daqiqagacha). "
                     f"Kod kelmasa, pul qaytariladi.", kb.as_markup())


@dp.callback_query(F.data.startswith("ncok:"))
async def num_buy(call: CallbackQuery):
    code = call.data.split(":", 1)[1]
    items = {x["code"]: x for x in await core_countries()}
    it = items.get(code)
    u = get_user(call.from_user.id)
    if not it or not u:
        await show(call, "Bu davlat hozir mavjud emas.", back_menu())
        return
    price = num_sell_price(it["cost"])
    if u["balance"] < price:
        await call.message.answer(f"💳 Balans yetarli emas. Kerak: {fmt(price)} so'm, sizda: {fmt(u['balance'])} so'm.\n"
                                  f"Hisobni to'ldirish: /start → Hisobni to'ldirish")
        await call.answer()
        return
    change_balance(call.from_user.id, -price)
    cur = db.execute(
        "INSERT INTO num_orders (user_id, country, country_name, price, created_at) VALUES (?,?,?,?,?)",
        (call.from_user.id, code, it["name"], price, datetime.now().isoformat(timespec="seconds")))
    db.commit()
    oid = cur.lastrowid
    await call.answer()

    def fail(text):
        change_balance(call.from_user.id, price)
        db.execute("UPDATE num_orders SET status='failed', refunded=price WHERE id=?", (oid,))
        db.commit()
        return text

    if not CORE_LIVE:
        db.execute("UPDATE num_orders SET api_id=?, phone=?, status='waiting' WHERE id=?", (f"TEST{oid}", "+000000000", oid))
        db.commit()
        await call.message.answer(f"🧪 TEST rejim. Raqam: +000000000\nBuyurtma N{oid}. Haqiqiy raqam olinmadi.")
        return
    try:
        res = await core("POST", "/numbers", country=code)
    except Exception:
        logging.exception("numbers xato")
        await call.message.answer(fail("⚠️ Xizmat bilan aloqa xatosi. Pul balansingizga qaytarildi."))
        return
    api_id = _first(res, "id", "number_id", "order_id")
    phone = _first(res, "phone", "number", "msisdn")
    if isinstance(res, dict) and isinstance(res.get("data"), dict):
        api_id = api_id or _first(res["data"], "id", "number_id", "order_id")
        phone = phone or _first(res["data"], "phone", "number", "msisdn")
    if not api_id or not phone:
        logging.warning("numbers javobi: %s", res)
        await call.message.answer(fail("😔 Raqam olinmadi. Pul balansingizga qaytarildi. Keyinroq urinib ko'ring."))
        return
    db.execute("UPDATE num_orders SET api_id=?, phone=? WHERE id=?", (str(api_id), str(phone), oid))
    db.commit()
    await call.message.answer(f"✅ Raqamingiz: {phone}\n\nUni Telegram'ga kiriting va 'Kod yuborish'ni bosing. "
                              f"Kod shu yerga avtomatik keladi (30 daqiqagacha). Buyurtma N{oid}.")


async def num_track_once(bot):
    rows = db.execute("SELECT * FROM num_orders WHERE status='waiting'").fetchall()
    for o in rows:
        try:
            created = datetime.fromisoformat(o["created_at"])
        except Exception:
            created = datetime.now()
        age = (datetime.now() - created).total_seconds()
        if str(o["api_id"] or "").startswith("TEST") or not o["api_id"]:
            if age > 600:
                db.execute("UPDATE num_orders SET status='expired' WHERE id=?", (o["id"],))
                db.commit()
            continue
        res = await core("GET", f"/numbers/{o['api_id']}")
        d = res.get("data") if isinstance(res, dict) and isinstance(res.get("data"), dict) else res
        status = str(_first(d, "status", default="")).lower()
        if status == "ok":
            sms = _first(d, "code", "sms")
            db.execute("UPDATE num_orders SET status='done', code=? WHERE id=?", (str(sms), o["id"]))
            db.execute("UPDATE users SET orders_count = orders_count + 1 WHERE id=?", (o["user_id"],))
            db.commit()
            await notify(bot, o["user_id"], f"🔑 Kod: {sms}\n📱 Raqam: {o['phone']} (N{o['id']})")
        elif status in ("expired", "canceled", "cancelled", "failed") or age > 35 * 60:
            row = db.execute("SELECT refunded FROM num_orders WHERE id=?", (o["id"],)).fetchone()
            if not row["refunded"]:
                change_balance(o["user_id"], o["price"])
                db.execute("UPDATE num_orders SET status='expired', refunded=price WHERE id=?", (o["id"],))
                db.commit()
                await notify(bot, o["user_id"], f"⌛ Kod kelmadi (N{o['id']}). Pul balansingizga qaytarildi.")


async def num_tracker(bot):
    while True:
        await asyncio.sleep(7)
        try:
            await num_track_once(bot)
        except Exception:
            logging.exception("num_tracker xato")


@dp.message(Command("coretest"))
async def coretest(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    if not CORE_KEY:
        await msg.answer("CORE_API_KEY kiritilmagan.")
        return
    out = []
    for p in ("/balance", "/countries"):
        try:
            out.append(f"{p}:\n{str(await core('GET', p))[:1200]}")
        except Exception as e:
            out.append(f"{p}: xato {e}")
    await msg.answer("\n\n".join(out) + f"\n\nCORE_LIVE={int(CORE_LIVE)}")


# ---------- Stars / Premium: MyStars (mystars.tg) API ----------
# Yarim-avtomatik: bot buyurtmani yaratadi, to'lov ma'lumotini ADMINGA yuboradi,
# admin Tonkeeper'dan o'zi to'laydi. Hamyon kaliti botga BERILMAYDI.
import re
import uuid  # noqa: F401

MYS_URL = os.getenv("MYSTARS_API_URL", "https://api.mystars.tg/v1").rstrip("/")
MYS_KEY = os.getenv("MYSTARS_API_KEY", "")
MYS_CUR = os.getenv("MYSTARS_CURRENCY", "ton")  # ton yoki usdt_ton

db.executescript(
    """
CREATE TABLE IF NOT EXISTS mys_orders (
    man_id INTEGER PRIMARY KEY,
    api_id TEXT,
    status TEXT,
    done INTEGER DEFAULT 0
);
"""
)
db.commit()


async def mys(method, path, body=None, idem=None):
    headers = {"X-Api-Key": MYS_KEY}
    if idem:
        headers["Idempotency-Key"] = idem
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.request(method, f"{MYS_URL}{path}", json=body, headers=headers) as r:
            try:
                data = await r.json(content_type=None)
            except Exception:
                data = {}
            return r.status, data


def _mys_body(p, target):
    user = target.strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", user):
        return None
    body = {"recipient": {"username": user}, "payment_currency": MYS_CUR}
    if p["kind"] == "stars":
        m = re.search(r"(\d+)", p["title"])
        if not m or int(m.group(1)) < 50:
            return None
        body.update(type="stars", quantity=int(m.group(1)))
    elif p["kind"] == "premium":
        m = re.search(r"(\d+)\s*oy", p["title"].lower())
        if not m or int(m.group(1)) not in (3, 6, 12):
            return None
        body.update(type="premium", months=int(m.group(1)))
    else:
        return None
    return body


async def mys_attach(bot, oid, p, target):
    """'' = qo'lda bajariladi; matn = adminga qo'shimcha; None = buyurtma bekor qilindi va pul qaytarildi."""
    if not MYS_KEY or p["kind"] not in ("stars", "premium"):
        return ""
    body = _mys_body(p, target)
    if not body:
        return ""
    try:
        st, data = await mys("POST", "/orders", body, idem=f"man-{oid}")
    except Exception:
        logging.exception("mystars create xato")
        return "\n\n⚠️ MyStars bilan aloqa yo'q. Buyurtmani qo'lda bajaring."
    if st in (200, 201) and data.get("order_id"):
        pay = data.get("payment") or {}
        db.execute("INSERT OR REPLACE INTO mys_orders (man_id, api_id, status) VALUES (?,?,?)",
                   (oid, data["order_id"], data.get("status", "awaiting_payment")))
        db.commit()
        return (f"\n\n🤖 MyStars buyurtma: {data['order_id']}\n"
                f"💳 To'lang: {pay.get('amount')} {pay.get('amount_units', '')}\n"
                f"📍 Manzil: {pay.get('pay_to_address')}\n"
                f"📝 Memo (majburiy): {pay.get('memo')}\n"
                f"⏳ Muddati: {data.get('expires_at')}\n"
                f"To'lov kelgach bot o'zi mijozga xabar beradi.")
    if st == 422:
        row = db.execute("SELECT * FROM man_orders WHERE id=?", (oid,)).fetchone()
        change_balance(row["user_id"], row["price"])
        db.execute("UPDATE man_orders SET status='canceled', refunded=price WHERE id=?", (oid,))
        db.commit()
        msg = (data.get("error") or {}).get("telegram_message") or "Qabul qiluvchi bu mahsulotni ola olmaydi."
        await notify(bot, row["user_id"], f"❌ Buyurtma M{oid} bajarilmadi: {msg}\nPul balansingizga qaytarildi.")
        return None
    logging.warning("mystars create javobi: %s %s", st, data)
    return f"\n\n⚠️ MyStars xato ({st}). Buyurtmani qo'lda bajaring."


async def mys_cancel(bot, oid):
    row = db.execute("SELECT * FROM mys_orders WHERE man_id=? AND done=0", (oid,)).fetchone()
    if not row:
        return
    try:
        st, _ = await mys("POST", f"/orders/{row['api_id']}/cancel")
    except Exception:
        return
    if st == 409 and ADMIN_ID:
        await notify(bot, ADMIN_ID, f"⚠️ M{oid}: MyStars buyurtmasi allaqachon to'langan, bekor qilib bo'lmadi. Mijozga pul qaytarildi.")
    db.execute("UPDATE mys_orders SET done=1 WHERE man_id=?", (oid,))
    db.commit()


async def mys_track_once(bot):
    for r in db.execute("SELECT * FROM mys_orders WHERE done=0").fetchall():
        st, data = await mys("GET", f"/orders/{r['api_id']}")
        if st != 200:
            continue
        status = str(data.get("status", ""))
        mo = db.execute("SELECT * FROM man_orders WHERE id=?", (r["man_id"],)).fetchone()
        if status in ("delivered", "completed"):
            if mo and mo["status"] == "pending":
                db.execute("UPDATE man_orders SET status='done' WHERE id=?", (mo["id"],))
                db.execute("UPDATE users SET orders_count = orders_count + 1 WHERE id=?", (mo["user_id"],))
                await notify(bot, mo["user_id"], f"✅ Buyurtma M{mo['id']} ({mo['title']}) bajarildi!")
            db.execute("UPDATE mys_orders SET done=1, status=? WHERE man_id=?", (status, r["man_id"]))
            db.commit()
        elif status in ("failed", "reversed", "expired", "cancelled"):
            if mo and mo["status"] == "pending":
                change_balance(mo["user_id"], mo["price"])
                db.execute("UPDATE man_orders SET status='canceled', refunded=price WHERE id=?", (mo["id"],))
                await notify(bot, mo["user_id"], f"❌ Buyurtma M{mo['id']} bajarilmadi. Pul balansingizga qaytarildi.")
                if ADMIN_ID:
                    await notify(bot, ADMIN_ID, f"ℹ️ M{mo['id']} MyStars holati: {status}. Mijozga pul qaytarildi.")
            db.execute("UPDATE mys_orders SET done=1, status=? WHERE man_id=?", (status, r["man_id"]))
            db.commit()
        elif status != r["status"]:
            db.execute("UPDATE mys_orders SET status=? WHERE man_id=?", (status, r["man_id"]))
            db.commit()


async def mys_tracker(bot):
    while True:
        await asyncio.sleep(20)
        try:
            if MYS_KEY:
                await mys_track_once(bot)
        except Exception:
            logging.exception("mys_tracker xato")


@dp.message(Command("mystest"))
async def mystest(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    if not MYS_KEY:
        await msg.answer("MYSTARS_API_KEY kiritilmagan.")
        return
    try:
        st, data = await mys("GET", "/orders?limit=1")
        await msg.answer(f"MyStars javobi: {st}\n{str(data)[:500]}\n(200 bo'lsa kalit to'g'ri)")
    except Exception as e:
        await msg.answer(f"Xato: {e}")


@dp.callback_query(F.data.startswith("svc:"))
async def service(call: CallbackQuery):
    kind = call.data.split(":")[1]
    if kind not in KINDS:
        await show(call, "Xizmat topilmadi.", back_menu())
        return
    rows = db.execute("SELECT * FROM products WHERE kind=? AND price>0 ORDER BY id", (kind,)).fetchall()
    kb = InlineKeyboardBuilder()
    # topsmm.uz dagi mos xizmatlar (avtomatik)
    kw, need_tg = KIND_WORDS[kind]
    auto = []
    for x in await get_services():
        txt = (x["category"] + " " + x["name"]).lower()
        if any(w in txt for w in kw) and (not need_tg or "telegram" in txt):
            auto.append(x)
    auto.sort(key=lambda x: x["rate"])
    for x in auto[:15]:
        kb.button(text=f"🤖 {fmt(sell_per_1000(x['rate']))} so'm/1000 · {x['name'][:30]}",
                  callback_data=f"nks:{x['service']}")
    for r in rows:
        kb.button(text=f"{r['title']} — {fmt(r['price'])} so'm", callback_data=f"mp:{r['id']}")
    kb.button(text="⬅️ Orqaga", callback_data="back")
    kb.adjust(1)
    await show(call, f"{KINDS[kind][0]}\n\nMahsulotni tanlang:", kb.as_markup())


@dp.callback_query(F.data.startswith("mp:"))
async def man_pick(call: CallbackQuery, state: FSMContext):
    p = db.execute("SELECT * FROM products WHERE id=?", (int(call.data.split(":")[1]),)).fetchone()
    if not p:
        await show(call, "Mahsulot topilmadi.", back_menu())
        return
    await state.set_state(Man.target)
    await state.update_data(pid=p["id"])
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Bekor qilish", callback_data="nkx")
    await show(call, f"{p['title']}\n💰 Narx: {fmt(p['price'])} so'm\n\n{KINDS[p['kind']][1]}", kb.as_markup())


@dp.message(Man.target)
async def man_target(msg: Message, state: FSMContext):
    target = (msg.text or "").strip()
    data = await state.get_data()
    p = db.execute("SELECT * FROM products WHERE id=?", (data.get("pid"),)).fetchone()
    if not p:
        await state.clear()
        await msg.answer("Mahsulot topilmadi. /start bosing.")
        return
    if len(target) < 2 or len(target) > 100:
        await msg.answer("Noto'g'ri. Qayta yuboring:")
        return
    await state.update_data(target=target)
    u = get_user(msg.from_user.id)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Tasdiqlash", callback_data="mok")
    kb.button(text="❌ Bekor qilish", callback_data="nkx")
    kb.adjust(2)
    await msg.answer(
        f"🧾 Buyurtmani tekshiring\n\n📦 {p['title']}\n👤 {target}\n"
        f"💰 Narx: {fmt(p['price'])} so'm\n💳 Balansingiz: {fmt(u['balance'])} so'm",
        reply_markup=kb.as_markup(),
    )


@dp.callback_query(F.data == "mok")
async def man_confirm(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    p = db.execute("SELECT * FROM products WHERE id=?", (data.get("pid"),)).fetchone()
    target = data.get("target")
    u = get_user(call.from_user.id)
    if not p or not target or not u:
        await state.clear()
        await show(call, "Ma'lumot topilmadi. Qaytadan boshlang.", main_menu(call.from_user.id == ADMIN_ID))
        return
    if u["balance"] < p["price"]:
        await call.message.answer(
            f"💳 Balans yetarli emas. Kerak: {fmt(p['price'])} so'm, sizda: {fmt(u['balance'])} so'm.\n"
            f"Hisobni to'ldirish: {SUPPORT} (ID: {call.from_user.id})")
        await call.answer()
        return
    change_balance(call.from_user.id, -p["price"])
    cur = db.execute(
        "INSERT INTO man_orders (user_id, kind, product_id, title, target, price, created_at) VALUES (?,?,?,?,?,?,?)",
        (call.from_user.id, p["kind"], p["id"], p["title"], target, p["price"],
         datetime.now().isoformat(timespec="seconds")))
    db.commit()
    oid = cur.lastrowid
    await state.clear()
    await call.message.answer(f"✅ Buyurtma M{oid} qabul qilindi. {fmt(p['price'])} so'm yechildi.\nTez orada bajariladi.")
    await call.answer()
    extra = await mys_attach(call.bot, oid, p, target)
    if extra is None:
        return
    if ADMIN_ID:
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Tayyor", callback_data=f"mda:{oid}")
        kb.button(text="❌ Rad etish", callback_data=f"mdr:{oid}")
        kb.adjust(2)
        await call.bot.send_message(
            ADMIN_ID,
            f"🆕 Buyurtma M{oid}\n👤 {call.from_user.full_name} ({call.from_user.id})\n"
            f"📦 {p['title']}\n🎯 {target}\n💰 {fmt(p['price'])} so'm" + extra,
            reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("mda:"))
async def man_admin_ok(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Ruxsat yo'q", show_alert=True)
        return
    oid = int(call.data.split(":")[1])
    o = db.execute("SELECT * FROM man_orders WHERE id=?", (oid,)).fetchone()
    if not o or o["status"] != "pending":
        await call.answer("Allaqachon ko'rib chiqilgan", show_alert=True)
        return
    db.execute("UPDATE man_orders SET status='done' WHERE id=?", (oid,))
    db.execute("UPDATE users SET orders_count = orders_count + 1 WHERE id=?", (o["user_id"],))
    db.commit()
    await notify(call.bot, o["user_id"], f"✅ Buyurtma M{oid} ({o['title']}) bajarildi!")
    try:
        await call.message.edit_text(call.message.text + "\n\n✅ Bajarildi")
    except Exception:
        pass
    await call.answer()


@dp.callback_query(F.data.startswith("mdr:"))
async def man_admin_no(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Ruxsat yo'q", show_alert=True)
        return
    oid = int(call.data.split(":")[1])
    o = db.execute("SELECT * FROM man_orders WHERE id=?", (oid,)).fetchone()
    if not o or o["status"] != "pending":
        await call.answer("Allaqachon ko'rib chiqilgan", show_alert=True)
        return
    await mys_cancel(call.bot, oid)
    change_balance(o["user_id"], o["price"])
    db.execute("UPDATE man_orders SET status='canceled', refunded=price WHERE id=?", (oid,))
    db.commit()
    await notify(call.bot, o["user_id"], f"❌ Buyurtma M{oid} rad etildi. Pul balansingizga qaytarildi.")
    try:
        await call.message.edit_text(call.message.text + "\n\n❌ Rad etildi, pul qaytarildi")
    except Exception:
        pass
    await call.answer()


@dp.message(Command("hide"))
async def hide_cmd(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer("Format: /hide ID yoki so'z\nMasalan: /hide 2143  yoki  /hide arzon\nQaytarish: /unhide ...\nRo'yxat: /hidden")
        return
    h = parts[1].strip()
    cur = hidden_list()
    if h not in cur:
        cur.append(h)
    set_setting("hidden", "|".join(cur))
    await msg.answer(f"🙈 Yashirildi: {h}")


@dp.message(Command("unhide"))
async def unhide_cmd(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer("Format: /unhide ID yoki so'z")
        return
    cur = [x for x in hidden_list() if x != parts[1].strip()]
    set_setting("hidden", "|".join(cur))
    await msg.answer(f"👁 Qaytarildi: {parts[1].strip()}")


@dp.message(Command("hidden"))
async def hidden_cmd(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    cur = hidden_list()
    await msg.answer("🙈 Yashirilganlar:\n" + ("\n".join(cur) if cur else "Yo'q, hamma xizmat ko'rinadi."))


@dp.message(Command("products"))
async def products_list(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    rows = db.execute("SELECT * FROM products ORDER BY id").fetchall()
    text = "🏷 Narxlar (ID · nomi · narx)\n\n" + "\n".join(
        f"{r['id']} · {r['title']} · {fmt(r['price'])}" for r in rows
    ) + "\n\nO'zgartirish: /setprice ID summa\n(0 = yashirish)\nQo'shish: /addproduct tur summa nomi\n(tur: stars, premium, gift, number)"
    await msg.answer(text)


@dp.message(Command("setprice"))
async def setprice(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    try:
        _, pid, price = msg.text.split()
        pid, price = int(pid), int(price)
    except ValueError:
        await msg.answer("Format: /setprice ID summa")
        return
    if not db.execute("SELECT 1 FROM products WHERE id=?", (pid,)).fetchone():
        await msg.answer("Mahsulot topilmadi. /products")
        return
    db.execute("UPDATE products SET price=? WHERE id=?", (price, pid))
    db.commit()
    await msg.answer(f"✅ {pid}: yangi narx {fmt(price)} so'm")


@dp.message(Command("addproduct"))
async def addproduct(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    parts = (msg.text or "").split(maxsplit=3)
    if len(parts) < 4 or parts[1] not in KINDS or not parts[2].isdigit():
        await msg.answer("Format: /addproduct tur summa nomi\nMasalan: /addproduct stars 5000 ⭐ 20 Stars")
        return
    db.execute("INSERT INTO products (kind, title, price) VALUES (?,?,?)", (parts[1], parts[3], int(parts[2])))
    db.commit()
    await msg.answer("✅ Qo'shildi. /products")


# ---------- Nakrutka: menyu ----------
@dp.callback_query(F.data == "nk")
async def nk_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardBuilder()
    for p, (name, emoji) in PLATFORMS.items():
        kb.button(text=f"{emoji} {name}", callback_data=f"nkp:{p}:0")
    kb.button(text="⬅️ Orqaga", callback_data="back")
    kb.adjust(2, 2, 1)
    note = "\n\n🧪 TEST rejimi: haqiqiy buyurtma yuborilmaydi." if (test_mode() or not SMM_KEY) else ""
    await show(call, "📈 Nakrutka: platformani tanlang." + note, kb.as_markup())


@dp.callback_query(F.data.startswith("nkp:"))
async def nk_categories(call: CallbackQuery):
    _, p, page = call.data.split(":")
    page = int(page)
    name, emoji = PLATFORMS[p]
    cats = await categories(p)
    if not cats:
        await show(call, f"{emoji} {name} bo'yicha xizmat topilmadi.", back_menu())
        return
    chunk = cats[page * PAGE:(page + 1) * PAGE]
    kb = InlineKeyboardBuilder()
    for i, c in enumerate(chunk, page * PAGE):
        kb.button(text=c[:55], callback_data=f"nkc:{p}:{i}:0")
    kb.adjust(1)
    add_nav(
        kb,
        f"nkp:{p}:{page - 1}" if page > 0 else None,
        f"nkp:{p}:{page + 1}" if (page + 1) * PAGE < len(cats) else None,
        "nk",
    )
    await show(call, f"{emoji} {name}: kategoriyani tanlang.", kb.as_markup())


@dp.callback_query(F.data.startswith("nkc:"))
async def nk_services(call: CallbackQuery):
    _, p, idx, page = call.data.split(":")
    idx, page = int(idx), int(page)
    cats = await categories(p)
    if idx >= len(cats):
        await show(call, "Ro'yxat yangilandi, qaytadan tanlang.", back_menu())
        return
    cat = cats[idx]
    items = sorted((s for s in await platform_services(p) if s["category"] == cat), key=lambda s: s["rate"])
    chunk = items[page * PAGE:(page + 1) * PAGE]
    kb = InlineKeyboardBuilder()
    for s in chunk:
        kb.button(text=f"{fmt(sell_per_1000(s['rate']))} so'm/1000 · {s['name'][:35]}", callback_data=f"nks:{s['service']}")
    kb.adjust(1)
    add_nav(
        kb,
        f"nkc:{p}:{idx}:{page - 1}" if page > 0 else None,
        f"nkc:{p}:{idx}:{page + 1}" if (page + 1) * PAGE < len(items) else None,
        f"nkp:{p}:0",
    )
    await show(call, f"📂 {cat}\nXizmatni tanlang (narx 1000 ta uchun):", kb.as_markup())


@dp.callback_query(F.data.startswith("nks:"))
async def nk_pick(call: CallbackQuery, state: FSMContext):
    sid = int(call.data.split(":")[1])
    s = next((x for x in await get_services() if x["service"] == sid), None)
    if not s:
        await show(call, "Xizmat topilmadi, qaytadan tanlang.", back_menu())
        return
    await state.set_state(Order.quantity)
    await state.update_data(sid=sid)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Bekor qilish", callback_data="nkx")
    text = (
        f"📦 {s['name']}\n\n"
        f"💵 Narx: {fmt(sell_per_1000(s['rate']))} so'm / 1000 ta\n"
        f"🔢 Minimal: {fmt(s['min'])} · Maksimal: {fmt(s['max'])}\n"
        f"♻️ Kafolat (refill): {'bor' if s['refill'] else 'yo`q'}\n\n"
        "Kerakli miqdorni yozing:"
    )
    await show(call, text, kb.as_markup())


@dp.callback_query(F.data == "nkx")
async def nk_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(call, "Bekor qilindi.", main_menu(call.from_user.id == ADMIN_ID))


@dp.message(Order.quantity)
async def nk_quantity(msg: Message, state: FSMContext):
    data = await state.get_data()
    s = next((x for x in await get_services() if x["service"] == data.get("sid")), None)
    if not s:
        await state.clear()
        await msg.answer("Xizmat topilmadi. /start bosing.")
        return
    txt = (msg.text or "").replace(" ", "")
    if not txt.isdigit() or not (s["min"] <= int(txt) <= s["max"]):
        await msg.answer(f"Miqdor {fmt(s['min'])} dan {fmt(s['max'])} gacha butun son bo'lishi kerak. Qayta yozing:")
        return
    await state.update_data(qty=int(txt))
    await state.set_state(Order.link)
    await msg.answer("🔗 Havolani yuboring (kanal/akkaunt/post havolasi yoki @username):")


@dp.message(Order.link)
async def nk_link(msg: Message, state: FSMContext):
    link = (msg.text or "").strip()
    if len(link) < 3 or len(link) > 300 or " " in link:
        await msg.answer("Havola noto'g'ri. Probelsiz havola yoki @username yuboring:")
        return
    data = await state.get_data()
    s = next((x for x in await get_services() if x["service"] == data.get("sid")), None)
    if not s:
        await state.clear()
        await msg.answer("Xizmat topilmadi. /start bosing.")
        return
    price = calc_price(s["rate"], data["qty"])
    await state.update_data(link=link, price=price)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Tasdiqlash", callback_data="nkok")
    kb.button(text="❌ Bekor qilish", callback_data="nkx")
    kb.adjust(2)
    u = get_user(msg.from_user.id)
    await msg.answer(
        f"🧾 Buyurtmani tekshiring\n\n📦 {s['name']}\n🔗 {link}\n🔢 Miqdor: {fmt(data['qty'])}\n"
        f"💰 Narx: {fmt(price)} so'm\n💳 Balansingiz: {fmt(u['balance'])} so'm\n\n"
        "⚠️ Natija kafolatlanmaydi: platforma qoidalari tufayli obunachi/layklar kamayishi mumkin.",
        reply_markup=kb.as_markup(),
    )


@dp.callback_query(F.data == "nkok")
async def nk_confirm(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("link") or not data.get("price"):
        await state.clear()
        await show(call, "Buyurtma ma'lumoti topilmadi. Qaytadan boshlang.", main_menu())
        return
    s = next((x for x in await get_services() if x["service"] == data["sid"]), None)
    u = get_user(call.from_user.id)
    price = data["price"]
    if not s or not u:
        await state.clear()
        await show(call, "Xizmat topilmadi. Qaytadan boshlang.", main_menu())
        return
    if u["balance"] < price:
        await call.message.answer(
            f"💳 Balans yetarli emas. Kerak: {fmt(price)} so'm, sizda: {fmt(u['balance'])} so'm.\n"
            f"Hisobni to'ldirish uchun {SUPPORT} ga yozing (ID: {call.from_user.id})."
        )
        await call.answer()
        return
    change_balance(call.from_user.id, -price)
    cur = db.execute(
        "INSERT INTO nk_orders (user_id, service_id, service_name, link, quantity, price, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (call.from_user.id, s["service"], s["name"], data["link"], data["qty"], price,
         datetime.now().isoformat(timespec="seconds")),
    )
    db.commit()
    oid = cur.lastrowid
    await state.clear()
    await call.message.answer(f"✅ Buyurtma #{oid} qabul qilindi. {fmt(price)} so'm balansingizdan yechildi.")
    await call.answer()
    if auto_approve():
        result = await process_order(call.bot, oid)
        if ADMIN_ID:
            await notify(call.bot, ADMIN_ID, result)
    elif ADMIN_ID:
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Yuborish", callback_data=f"nka:{oid}")
        kb.button(text="❌ Rad etish", callback_data=f"nkr:{oid}")
        kb.adjust(2)
        mode = "\n🧪 TEST rejimi" if (test_mode() or not SMM_KEY) else ""
        await call.bot.send_message(
            ADMIN_ID,
            f"🆕 Buyurtma #{oid}\n👤 {call.from_user.full_name} ({call.from_user.id})\n"
            f"📦 {s['name']}\n🔗 {data['link']}\n🔢 {data['qty']}\n💰 {fmt(price)} so'm"
            f"\n📉 Panel narxi: ~{rub_cost(s['rate'], data['qty']):.1f} ₽{mode}",
            reply_markup=kb.as_markup(),
        )


def rub_cost(rate, qty):
    return rate / 1000 * qty


@dp.callback_query(F.data.startswith("nka:"))
async def nk_admin_ok(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Ruxsat yo'q", show_alert=True)
        return
    oid = int(call.data.split(":")[1])
    result = await process_order(call.bot, oid)
    try:
        await call.message.edit_text(call.message.text + f"\n\n{result}")
    except Exception:
        await call.message.answer(result)
    await call.answer()


@dp.callback_query(F.data.startswith("nkr:"))
async def nk_admin_no(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Ruxsat yo'q", show_alert=True)
        return
    oid = int(call.data.split(":")[1])
    o = order_row(oid)
    if not o or o["status"] != "pending":
        await call.answer("Allaqachon ko'rib chiqilgan", show_alert=True)
        return
    refund(oid, o["price"])
    set_status(oid, "canceled")
    await notify(call.bot, o["user_id"], f"❌ Buyurtma #{oid} rad etildi. Pul balansingizga qaytarildi.")
    try:
        await call.message.edit_text(call.message.text + f"\n\n❌ #{oid} rad etildi, pul qaytarildi.")
    except Exception:
        pass
    await call.answer()


# ---------- Profil, buyurtmalar, boshqalar ----------
@dp.callback_query(F.data == "profile")
async def profile(call: CallbackQuery):
    u = get_user(call.from_user.id)
    if not u:
        add_user(call.from_user.id, call.from_user.username, call.from_user.full_name)
        u = get_user(call.from_user.id)
    me = await call.bot.get_me()
    text = (
        "👤 Profil\n\n"
        f"🆔 ID: {u['id']}\n"
        f"💰 Balans: {fmt(u['balance'])} so'm\n"
        f"🛒 Bajarilgan buyurtmalar: {u['orders_count']} ta\n"
        f"📅 Ro'yxatdan o'tgan: {u['created_at'][:10]}\n\n"
        f"🔗 Referal havolangiz:\nhttps://t.me/{me.username}?start={u['id']}"
    )
    await show(call, text, back_menu())


@dp.callback_query(F.data == "orders")
async def orders(call: CallbackQuery):
    rows = db.execute(
        "SELECT * FROM nk_orders WHERE user_id=? ORDER BY id DESC LIMIT 10",
        (call.from_user.id,),
    ).fetchall()
    lines = [
        f"#{r['id']} · {r['service_name'][:30]} · {fmt(r['quantity'])} ta · {fmt(r['price'])} so'm\n   {STATUS_TEXT.get(r['status'], r['status'])}"
        for r in rows
    ]
    mrows = db.execute(
        "SELECT * FROM man_orders WHERE user_id=? ORDER BY id DESC LIMIT 10", (call.from_user.id,)
    ).fetchall()
    lines += [
        f"M{r['id']} · {r['title'][:30]} · {fmt(r['price'])} so'm\n   {MAN_STATUS.get(r['status'], r['status'])}"
        for r in mrows
    ]
    text = "🧾 Buyurtmalar\n\n" + ("\n".join(lines) if lines else "Hozircha buyurtma yo'q.")
    await show(call, text, back_menu())


class Top(StatesGroup):
    amount = State()
    chek = State()


@dp.callback_query(F.data == "topup")
async def topup(call: CallbackQuery, state: FSMContext):
    if not CARD_NUMBER:
        await show(call, f"💳 Hisobni to'ldirish uchun adminga yozing: {SUPPORT}\nID: {call.from_user.id}", back_menu())
        return
    await state.set_state(Top.amount)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Bekor qilish", callback_data="nkx")
    await show(call,
        f"💳 Hisobni to'ldirish\n\nKarta: {CARD_NUMBER}\nEgasi: {CARD_OWNER}\n\n"
        f"1) Shu kartaga pul o'tkazing\n2) O'tkazgan summani yozing (kamida {fmt(MIN_TOPUP)} so'm)",
        kb.as_markup())


@dp.message(Top.amount)
async def top_amount(msg: Message, state: FSMContext):
    t = (msg.text or "").replace(" ", "")
    if not t.isdigit() or int(t) < MIN_TOPUP:
        await msg.answer(f"Summani raqam bilan yozing (kamida {fmt(MIN_TOPUP)} so'm):")
        return
    await state.update_data(amount=int(t))
    await state.set_state(Top.chek)
    await msg.answer("📸 Endi to'lov chekining rasmini (skrinshot) yuboring:")


@dp.message(Top.chek, F.photo)
async def top_chek(msg: Message, state: FSMContext):
    data = await state.get_data()
    amount = data["amount"]
    cur = db.execute("INSERT INTO pay_requests (user_id, amount, created_at) VALUES (?,?,?)",
                     (msg.from_user.id, amount, datetime.now().isoformat(timespec="seconds")))
    db.commit()
    rid = cur.lastrowid
    await state.clear()
    await msg.answer("✅ Chek yuborildi. Admin tekshirgach balansingiz to'ldiriladi.")
    if ADMIN_ID:
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Tasdiqlash", callback_data=f"pya:{rid}")
        kb.button(text="❌ Rad etish", callback_data=f"pyr:{rid}")
        kb.adjust(2)
        await msg.bot.send_photo(ADMIN_ID, msg.photo[-1].file_id,
            caption=f"💳 To'lov so'rovi P{rid}\n👤 {msg.from_user.full_name} ({msg.from_user.id})\n💰 {fmt(amount)} so'm",
            reply_markup=kb.as_markup())


@dp.message(Top.chek)
async def top_chek_bad(msg: Message):
    await msg.answer("Iltimos, chek rasmini yuboring (rasm sifatida).")


@dp.callback_query(F.data.startswith("pya:"))
async def pay_ok(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Ruxsat yo'q", show_alert=True)
        return
    rid = int(call.data.split(":")[1])
    r = db.execute("SELECT * FROM pay_requests WHERE id=?", (rid,)).fetchone()
    if not r or r["status"] != "pending":
        await call.answer("Allaqachon ko'rib chiqilgan", show_alert=True)
        return
    db.execute("UPDATE pay_requests SET status='done' WHERE id=?", (rid,))
    db.commit()
    change_balance(r["user_id"], r["amount"])
    await notify(call.bot, r["user_id"], f"💰 Balansingiz {fmt(r['amount'])} so'mga to'ldirildi.")
    await call.message.edit_caption(caption=(call.message.caption or "") + "\n\n✅ Tasdiqlandi")
    await call.answer()


@dp.callback_query(F.data.startswith("pyr:"))
async def pay_no(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Ruxsat yo'q", show_alert=True)
        return
    rid = int(call.data.split(":")[1])
    r = db.execute("SELECT * FROM pay_requests WHERE id=?", (rid,)).fetchone()
    if not r or r["status"] != "pending":
        await call.answer("Allaqachon ko'rib chiqilgan", show_alert=True)
        return
    db.execute("UPDATE pay_requests SET status='rejected' WHERE id=?", (rid,))
    db.commit()
    await notify(call.bot, r["user_id"], f"❌ To'lov so'rovi P{rid} rad etildi. Savol bo'lsa: {SUPPORT}")
    await call.message.edit_caption(caption=(call.message.caption or "") + "\n\n❌ Rad etildi")
    await call.answer()


@dp.callback_query(F.data == "top")
async def top(call: CallbackQuery):
    rows = db.execute(
        "SELECT full_name, orders_count FROM users ORDER BY orders_count DESC LIMIT 10"
    ).fetchall()
    lines = [f"{i}. {r['full_name']} — {r['orders_count']} ta" for i, r in enumerate(rows, 1)]
    text = "🏆 Top xaridorlar\n\n" + ("\n".join(lines) if lines else "Hozircha bo'sh.")
    await show(call, text, back_menu())


@dp.callback_query(F.data == "support")
async def support(call: CallbackQuery):
    await show(call, f"🆘 Savollar uchun: {SUPPORT}", back_menu())


# ---------- Admin ----------
@dp.message(Command("addbalance"))
async def addbalance(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    try:
        _, uid, amount = msg.text.split()
        uid, amount = int(uid), int(amount)
    except ValueError:
        await msg.answer("Format: /addbalance user_id summa")
        return
    if not get_user(uid):
        await msg.answer("Foydalanuvchi topilmadi.")
        return
    change_balance(uid, amount)
    await msg.answer(f"✅ {uid} balansiga {fmt(amount)} so'm qo'shildi.")
    await notify(msg.bot, uid, f"💰 Balansingiz {fmt(amount)} so'mga to'ldirildi.")


@dp.message(Command("stats"))
async def stats(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    n = db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    o = db.execute("SELECT COUNT(*) c FROM nk_orders").fetchone()["c"]
    mode = "TEST" if (test_mode() or not SMM_KEY) else "HAQIQIY"
    await msg.answer(f"👥 Foydalanuvchilar: {n}\n📈 Nakrutka buyurtmalari: {o}\n⚙️ Rejim: {mode}\n"
                     f"💱 1 ₽ = {cur_rate()} so'm, marja {cur_margin()}%")


@dp.message(Command("smmbalance"))
async def smmbalance(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    if not SMM_KEY:
        await msg.answer("SMM_API_KEY kiritilmagan.")
        return
    try:
        res = await smm("balance")
        await msg.answer(f"Panel balansi: {res}")
    except Exception as e:
        await msg.answer(f"Xato: {e}")


# ---------- Majburiy obuna (kanallar) ----------
_sub_ok = {}


async def missing_channels(bot, uid):
    now = time.time()
    if _sub_ok.get(uid, 0) > now:
        return []
    missing = []
    for ch in get_channels():
        try:
            m = await bot.get_chat_member(ch["chat_id"], uid)
            if m.status in ("left", "kicked"):
                missing.append(ch)
        except Exception:
            logging.warning("obuna tekshirilmadi: %s (bot kanalda admin emasmi?)", ch["chat_id"])
    if not missing:
        _sub_ok[uid] = now + 30
    return missing


def sub_prompt(missing):
    kb = InlineKeyboardBuilder()
    for ch in missing:
        kb.button(text=f"📢 {ch['title'][:40]}", url=ch["link"])
    kb.button(text="✅ Tekshirish", callback_data="chk")
    kb.adjust(1)
    return "Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling, so'ng «Tekshirish» ni bosing:", kb.as_markup()


class SubMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if not user or user.id == ADMIN_ID or not get_channels():
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and event.data == "chk":
            return await handler(event, data)
        missing = await missing_channels(event.bot, user.id)
        if not missing:
            return await handler(event, data)
        if isinstance(event, Message) and (event.text or "").startswith("/start"):
            parts = event.text.split()
            ref = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() and int(parts[1]) != user.id else None
            add_user(user.id, user.username, user.full_name, ref)
        text, kb = sub_prompt(missing)
        if isinstance(event, CallbackQuery):
            await event.message.answer(text, reply_markup=kb)
            await event.answer()
        else:
            await event.answer(text, reply_markup=kb)


@dp.callback_query(F.data == "chk")
async def check_sub(call: CallbackQuery, state: FSMContext):
    _sub_ok.pop(call.from_user.id, None)
    missing = await missing_channels(call.bot, call.from_user.id)
    if missing:
        await call.answer("Hali hamma kanalga obuna bo'lmadingiz ❗", show_alert=True)
        return
    add_user(call.from_user.id, call.from_user.username, call.from_user.full_name)
    await state.clear()
    await call.message.answer("✅ Rahmat, tekshiruvdan o'tdingiz!\n\nQuyidagi menyudan keraklisini tanlang:",
                              reply_markup=main_menu(call.from_user.id == ADMIN_ID))
    await call.answer()


# ---------- Admin panel ----------
class AdminS(StatesGroup):
    add_channel = State()
    balance = State()
    find_user = State()
    set_rate = State()
    set_margin = State()
    broadcast = State()


ONLY_ADMIN = F.from_user.id == ADMIN_ID


def admin_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Statistika", callback_data="adm:stats")
    kb.button(text="📢 Kanallar", callback_data="adm:chan")
    kb.button(text="💰 Balans qo'shish/ayirish", callback_data="adm:bal")
    kb.button(text="🔎 Foydalanuvchi", callback_data="adm:find")
    kb.button(text="🧾 Kutayotgan buyurtmalar", callback_data="adm:pend")
    kb.button(text="⚙️ Sozlamalar", callback_data="adm:set")
    kb.button(text="📣 Xabar yuborish", callback_data="adm:bc")
    kb.button(text="⬅️ Asosiy menyu", callback_data="back")
    kb.adjust(2, 1, 1, 1, 1, 1, 1)
    return kb.as_markup()


def adm_back():
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ Admin panel", callback_data="adm")
    return kb.as_markup()


@dp.message(Command("admin"), ONLY_ADMIN)
async def admin_cmd(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("🛠 Admin panel", reply_markup=admin_menu())


@dp.callback_query(F.data == "adm", ONLY_ADMIN)
async def admin_home(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(call, "🛠 Admin panel", admin_menu())


@dp.callback_query(F.data == "adm:stats", ONLY_ADMIN)
async def adm_stats(call: CallbackQuery):
    n = db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    today = datetime.now().strftime("%Y-%m-%d")
    new = db.execute("SELECT COUNT(*) c FROM users WHERE created_at LIKE ?", (today + "%",)).fetchone()["c"]
    bal = db.execute("SELECT COALESCE(SUM(balance),0) s FROM users").fetchone()["s"]
    by = db.execute("SELECT status, COUNT(*) c FROM nk_orders GROUP BY status").fetchall()
    sales = db.execute("SELECT COALESCE(SUM(price - refunded),0) s FROM nk_orders "
                       "WHERE status IN ('sent','completed','partial')").fetchone()["s"]
    lines = "\n".join(f"  {STATUS_TEXT.get(r['status'], r['status'])}: {r['c']}" for r in by) or "  yo'q"
    text = (f"📊 Statistika\n\n👥 Foydalanuvchilar: {n} (bugun +{new})\n"
            f"💰 Mijozlar balansi jami: {fmt(bal)} so'm\n"
            f"📈 Sotuv (qaytarilmagan): {fmt(sales)} so'm\n🧾 Buyurtmalar:\n{lines}")
    await show(call, text, adm_back())


# --- Kanallar ---
@dp.callback_query(F.data == "adm:chan", ONLY_ADMIN)
async def adm_channels(call: CallbackQuery, state: FSMContext):
    await state.clear()
    chans = get_channels()
    kb = InlineKeyboardBuilder()
    for ch in chans:
        kb.button(text=f"🗑 {ch['title'][:40]}", callback_data=f"adm:chandel:{ch['chat_id']}")
    kb.button(text="➕ Kanal qo'shish", callback_data="adm:chanadd")
    kb.button(text="↩️ Admin panel", callback_data="adm")
    kb.adjust(1)
    text = "📢 Majburiy obuna kanallari\n\n" + (
        "\n".join(f"• {c['title']}" for c in chans) if chans else "Hozircha kanal yo'q (obuna talab qilinmaydi)."
    ) + "\n\nO'chirish uchun kanal nomini bosing."
    await show(call, text, kb.as_markup())


@dp.callback_query(F.data == "adm:chanadd", ONLY_ADMIN)
async def adm_chan_add(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminS.add_channel)
    await show(
        call,
        "Avval botni kanalga admin qilib qo'shing.\n\nKeyin kanal @username'ini yuboring (masalan @mening_kanalim).\n"
        "Yopiq kanal bo'lsa: ID va taklif havolasini probel bilan yuboring:\n-1001234567890 https://t.me/+AbCdEf",
        adm_back(),
    )


@dp.message(AdminS.add_channel, ONLY_ADMIN)
async def adm_chan_add_msg(msg: Message, state: FSMContext):
    parts = (msg.text or "").split()
    if not parts:
        await msg.answer("@username yoki ID yuboring.")
        return
    ref = parts[0]
    if ref.startswith("https://t.me/"):
        ref = "@" + ref.split("/")[-1]
    try:
        chat = await msg.bot.get_chat(int(ref) if ref.lstrip("-").isdigit() else ref)
    except Exception as e:
        await msg.answer(f"Kanal topilmadi: {e}\n@username to'g'ri va bot kanalga qo'shilganini tekshiring.")
        return
    try:
        me = await msg.bot.get_me()
        m = await msg.bot.get_chat_member(chat.id, me.id)
        ok = m.status in ("administrator", "creator")
    except Exception:
        ok = False
    if not ok:
        await msg.answer("Bot bu kanalda admin emas. Avval botni kanalga admin qiling, so'ng qayta yuboring.")
        return
    if len(parts) > 1:
        link = parts[1]
    elif chat.username:
        link = f"https://t.me/{chat.username}"
    else:
        await msg.answer("Bu yopiq kanal. ID va taklif havolasini yuboring:\n-1001234567890 https://t.me/+AbCdEf")
        return
    db.execute("INSERT OR REPLACE INTO channels (chat_id, title, link) VALUES (?,?,?)",
               (chat.id, chat.title or ref, link))
    db.commit()
    _sub_ok.clear()
    await state.clear()
    await msg.answer(f"✅ Kanal qo'shildi: {chat.title}", reply_markup=adm_back())


@dp.callback_query(F.data.startswith("adm:chandel:"), ONLY_ADMIN)
async def adm_chan_del(call: CallbackQuery):
    cid = int(call.data.split(":")[2])
    db.execute("DELETE FROM channels WHERE chat_id=?", (cid,))
    db.commit()
    _sub_ok.clear()
    await show(call, "🗑 Kanal o'chirildi.", adm_back())


# --- Balans ---
@dp.callback_query(F.data == "adm:bal", ONLY_ADMIN)
async def adm_bal(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminS.balance)
    await show(call, "Foydalanuvchi ID va summani yuboring:\n123456789 50000\n(ayirish uchun minus: 123456789 -20000)", adm_back())


@dp.message(AdminS.balance, ONLY_ADMIN)
async def adm_bal_msg(msg: Message, state: FSMContext):
    try:
        uid, amount = (int(x) for x in (msg.text or "").split())
    except ValueError:
        await msg.answer("Format: ID summa  (masalan 123456789 50000)")
        return
    if not get_user(uid):
        await msg.answer("Foydalanuvchi topilmadi.")
        return
    change_balance(uid, amount)
    await state.clear()
    u = get_user(uid)
    await msg.answer(f"✅ {uid}: {fmt(amount)} so'm. Yangi balans: {fmt(u['balance'])} so'm", reply_markup=adm_back())
    if amount > 0:
        await notify(msg.bot, uid, f"💰 Balansingiz {fmt(amount)} so'mga to'ldirildi.")


# --- Foydalanuvchi ---
@dp.callback_query(F.data == "adm:find", ONLY_ADMIN)
async def adm_find(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminS.find_user)
    await show(call, "Foydalanuvchi ID raqamini yuboring:", adm_back())


@dp.message(AdminS.find_user, ONLY_ADMIN)
async def adm_find_msg(msg: Message, state: FSMContext):
    t = (msg.text or "").strip()
    if not t.isdigit() or not get_user(int(t)):
        await msg.answer("Topilmadi. ID ni tekshirib qayta yuboring.")
        return
    u = get_user(int(t))
    cnt = db.execute("SELECT COUNT(*) c FROM nk_orders WHERE user_id=?", (u["id"],)).fetchone()["c"]
    await state.clear()
    await msg.answer(
        f"👤 {u['full_name']} (@{u['username']})\n🆔 {u['id']}\n💰 Balans: {fmt(u['balance'])} so'm\n"
        f"🛒 Bajarilgan: {u['orders_count']} ta · Jami buyurtma: {cnt}\n📅 {u['created_at']}",
        reply_markup=adm_back(),
    )


# --- Kutayotgan buyurtmalar ---
@dp.callback_query(F.data == "adm:pend", ONLY_ADMIN)
async def adm_pending(call: CallbackQuery):
    rows = db.execute("SELECT * FROM nk_orders WHERE status='pending' ORDER BY id LIMIT 10").fetchall()
    mrows = db.execute("SELECT * FROM man_orders WHERE status='pending' ORDER BY id LIMIT 10").fetchall()
    if mrows:
        await call.answer()
        for o in mrows:
            kb = InlineKeyboardBuilder()
            kb.button(text="✅ Tayyor", callback_data=f"mda:{o['id']}")
            kb.button(text="❌ Rad etish", callback_data=f"mdr:{o['id']}")
            kb.adjust(2)
            await call.message.answer(
                f"🆕 M{o['id']} · 👤 {o['user_id']}\n📦 {o['title']}\n🎯 {o['target']}\n💰 {fmt(o['price'])} so'm",
                reply_markup=kb.as_markup())
        if not rows:
            return
    if not rows and not mrows:
        await show(call, "Kutayotgan buyurtma yo'q ✅", adm_back())
        return
    if not mrows:
        await call.answer()
    for o in rows:
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Yuborish", callback_data=f"nka:{o['id']}")
        kb.button(text="❌ Rad etish", callback_data=f"nkr:{o['id']}")
        kb.adjust(2)
        await call.message.answer(
            f"🆕 #{o['id']} · 👤 {o['user_id']}\n📦 {o['service_name']}\n🔗 {o['link']}\n"
            f"🔢 {o['quantity']} · 💰 {fmt(o['price'])} so'm", reply_markup=kb.as_markup())


# --- Sozlamalar ---
def settings_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="💱 Rubl kursi", callback_data="adm:rate")
    kb.button(text="📊 Marja (%)", callback_data="adm:margin")
    kb.button(text="🧪 TEST / HAQIQIY rejim", callback_data="adm:test")
    kb.button(text="🤖 Avto-tasdiqlash", callback_data="adm:auto")
    kb.button(text="↩️ Admin panel", callback_data="adm")
    kb.adjust(2, 1, 1, 1)
    return kb.as_markup()


def settings_text():
    return (f"⚙️ Sozlamalar\n\n💱 1 ₽ = {cur_rate()} so'm\n📊 Marja: {cur_margin()}%\n"
            f"🧪 Rejim: {'TEST (haqiqiy buyurtma ketmaydi)' if test_mode() else 'HAQIQIY'}\n"
            f"🤖 Avto-tasdiqlash: {'YOQILGAN' if auto_approve() else 'o`chiq (siz tasdiqlaysiz)'}\n"
            f"🔑 SMM kalit: {'kiritilgan' if SMM_KEY else 'YO`Q (demo xizmatlar)'}")


@dp.callback_query(F.data == "adm:set", ONLY_ADMIN)
async def adm_settings(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(call, settings_text(), settings_menu())


@dp.callback_query(F.data == "adm:rate", ONLY_ADMIN)
async def adm_rate(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminS.set_rate)
    await show(call, f"Hozirgi: 1 ₽ = {cur_rate()} so'm.\nYangi kursni yozing (masalan 155):", adm_back())


@dp.message(AdminS.set_rate, ONLY_ADMIN)
async def adm_rate_msg(msg: Message, state: FSMContext):
    try:
        v = float((msg.text or "").replace(",", "."))
        assert 0 < v < 10000
    except Exception:
        await msg.answer("Son yozing (masalan 155).")
        return
    set_setting("rub_to_uzs", v)
    _cache["t"] = 0
    await state.clear()
    await msg.answer(f"✅ Kurs: 1 ₽ = {v} so'm", reply_markup=adm_back())


@dp.callback_query(F.data == "adm:margin", ONLY_ADMIN)
async def adm_margin(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminS.set_margin)
    await show(call, f"Hozirgi marja: {cur_margin()}%.\nYangi foizni yozing (masalan 30):", adm_back())


@dp.message(AdminS.set_margin, ONLY_ADMIN)
async def adm_margin_msg(msg: Message, state: FSMContext):
    try:
        v = float((msg.text or "").replace(",", ".").replace("%", ""))
        assert 0 <= v <= 1000
    except Exception:
        await msg.answer("Son yozing (masalan 30).")
        return
    set_setting("margin", v)
    await state.clear()
    await msg.answer(f"✅ Marja: {v}%", reply_markup=adm_back())


@dp.callback_query(F.data == "adm:test", ONLY_ADMIN)
async def adm_test(call: CallbackQuery):
    set_setting("test_mode", "0" if test_mode() else "1")
    await show(call, settings_text(), settings_menu())


@dp.callback_query(F.data == "adm:auto", ONLY_ADMIN)
async def adm_auto(call: CallbackQuery):
    set_setting("auto_approve", "0" if auto_approve() else "1")
    await show(call, settings_text(), settings_menu())


# --- Xabar yuborish ---
@dp.callback_query(F.data == "adm:bc", ONLY_ADMIN)
async def adm_bc(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminS.broadcast)
    await show(call, "Hamma foydalanuvchilarga yuboriladigan xabar matnini yozing:", adm_back())


@dp.message(AdminS.broadcast, ONLY_ADMIN)
async def adm_bc_msg(msg: Message, state: FSMContext):
    text = msg.text or ""
    if not text:
        await msg.answer("Matn yuboring.")
        return
    await state.update_data(bc=text)
    n = db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    kb = InlineKeyboardBuilder()
    kb.button(text=f"✅ {n} kishiga yuborish", callback_data="adm:bcok")
    kb.button(text="❌ Bekor", callback_data="adm")
    kb.adjust(1)
    await msg.answer(f"Xabar:\n\n{text}", reply_markup=kb.as_markup())


@dp.callback_query(F.data == "adm:bcok", ONLY_ADMIN)
async def adm_bc_go(call: CallbackQuery, state: FSMContext):
    text = (await state.get_data()).get("bc")
    if not text:
        await show(call, "Xabar topilmadi.", adm_back())
        return
    await state.clear()
    await call.answer("Yuborilmoqda...")
    ok = bad = 0
    for r in db.execute("SELECT id FROM users").fetchall():
        try:
            await call.bot.send_message(r["id"], text)
            ok += 1
        except Exception:
            bad += 1
        await asyncio.sleep(0.05)
    await call.message.answer(f"📣 Yuborildi: {ok}, yetib bormadi: {bad}", reply_markup=adm_back())


async def main():
    if not TOKEN:
        raise SystemExit("BOT_TOKEN yo'q")
    bot = Bot(TOKEN)
    dp.message.outer_middleware(SubMiddleware())
    dp.callback_query.outer_middleware(SubMiddleware())
    asyncio.create_task(tracker(bot))
    asyncio.create_task(num_tracker(bot))
    asyncio.create_task(mys_tracker(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
