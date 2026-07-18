import logging
import sqlite3
import asyncio
import time
import os
import re
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ============ SOZLAMALAR ============
API_TOKEN = os.getenv('BOT_TOKEN')
_ADMIN_ID_RAW = os.getenv('ADMIN_ID')
CHANNEL_ID = os.getenv('CHANNEL_ID', '@Ruhshunos_seriali7')
CHANNEL_ID2 = os.getenv('CHANNEL_ID2', '@org_Kino_finder')
CHANNELS = [CHANNEL_ID, CHANNEL_ID2]
BOT_USERNAME = os.getenv('BOT_USERNAME', 'KinoFinderUzzBOT')  # masalan: MyMovieBot (@ belgisiz)

if not API_TOKEN:
    raise RuntimeError(
        "❌ BOT_TOKEN muhit o'zgaruvchisi topilmadi! "
        "Railway'da Variables bo'limida BOT_TOKEN nomi to'g'ri yozilganini tekshiring."
    )
if not _ADMIN_ID_RAW:
    raise RuntimeError(
        "❌ ADMIN_ID muhit o'zgaruvchisi topilmadi! "
        "Railway'da Variables bo'limida ADMIN_ID nomi to'g'ri yozilganini tekshiring."
    )
try:
    ADMIN_ID = int(_ADMIN_ID_RAW)
except ValueError:
    raise RuntimeError(
        f"❌ ADMIN_ID qiymati raqam emas: '{_ADMIN_ID_RAW}'. Faqat Telegram ID raqamini kiriting."
    )
REFERRAL_REWARD_THRESHOLD = 5  # nechta do'st taklif qilsa mukofot beriladi

# Admin uchun doimiy pastki (Reply) tugmalar nomlari
ADMIN_BUTTON_TEXT = "🎬 Kino yuklash"
PREMIERE_BUTTON_TEXT = "🎬 Premyera"
BROADCAST_BUTTON_TEXT = "✉️ Xabar qoldirish"
STATS_BUTTON_TEXT = "📊 Statistika"
MEMBERS_BUTTON_TEXT = "👥 Azolar"
ACTIVE_SUBS_BUTTON_TEXT = "📡 Faol obunachilar"
LINK_BUTTON_TEXT = "🔗 Havola kiritish"
LIST_BUTTON_TEXT = "📋 Ro'yxat"
USER_CANCEL_BUTTON_TEXT = "🚫 Bekor qilish"

# Necha kun harakatsiz qolsa, foydalanuvchi "passiv" deb belgilanadi
ACTIVE_THRESHOLD_DAYS = 3

logging.basicConfig(level=logging.INFO)

bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ============ ADMIN UCHUN HOLATLAR (FSM) ============
class AdminStates(StatesGroup):
    waiting_premiere_video = State()      # premyera videosini kutmoqda
    waiting_premiere_caption = State()     # premyera uchun "Nomi | Tavsif" ni kutmoqda
    waiting_broadcast_message = State()    # xabar (matn/video/ovozli) ni kutmoqda
    waiting_broadcast_confirm = State()    # "izoh qo'shasizmi?" javobini kutmoqda
    waiting_broadcast_izoh = State()       # izoh matnini kutmoqda
    waiting_slot_video = State()          # bo'sh qolgan raqamga video kutmoqda
    waiting_ad_link = State()             # reklama havolasini kutmoqda
    waiting_ad_duration = State()         # reklama muddatini kutmoqda


# ============ BAZA ============
DB_PATH = os.getenv('DB_PATH', 'movies.db')
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()
cursor.execute('PRAGMA journal_mode=WAL')
cursor.execute('PRAGMA synchronous=NORMAL')

cursor.execute('''
CREATE TABLE IF NOT EXISTS movies (
    id INTEGER PRIMARY KEY,
    name TEXT,
    file_id TEXT,
    code TEXT DEFAULT '',
    year INTEGER DEFAULT 0,
    search_count INTEGER DEFAULT 0,
    order_num INTEGER DEFAULT 0,
    upload_number INTEGER DEFAULT 0
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS counters (
    key TEXT PRIMARY KEY,
    value INTEGER DEFAULT 0
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    referred_by INTEGER,
    points INTEGER DEFAULT 0
)
''')

# ── Azolar/faollik kuzatuvi uchun users jadvaliga yangi ustunlar
for col_name, col_def in [
    ("full_name", "TEXT DEFAULT ''"),
    ("username", "TEXT DEFAULT ''"),
    ("last_active", "TEXT DEFAULT ''"),
    ("is_active", "INTEGER DEFAULT 1"),
]:
    try:
        cursor.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_def}")
    except sqlite3.OperationalError:
        pass  # ustun allaqachon mavjud

    cursor.execute('''
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
)
''')
conn.commit()


def get_setting(key, default=None):
    cursor.execute('SELECT value FROM settings WHERE key = ?', (key,))
    row = cursor.fetchone()
    return row[0] if row else default


def set_setting(key, value):
    cursor.execute('''
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
    ''', (key, value))
    conn.commit()


cursor.execute('''
CREATE TABLE IF NOT EXISTS status_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    old_status TEXT,
    new_status TEXT,
    changed_at TEXT DEFAULT CURRENT_TIMESTAMP
)
''')
conn.commit()


cursor.execute('''
CREATE TABLE IF NOT EXISTS watchlist (
    user_id INTEGER,
    movie_id INTEGER,
    PRIMARY KEY (user_id, movie_id)
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS ratings (
    user_id INTEGER,
    movie_id INTEGER,
    score INTEGER,
    PRIMARY KEY (user_id, movie_id)
)
''')

# ── TUZATILDI: bu jadval avval umuman yaratilmagan edi, shuning uchun
# har safar kino topilganda ham bot xatoga uchrab, video yubormay qolar edi.
cursor.execute('''
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    movie_id INTEGER,
    movie_name TEXT,
    movie_code TEXT,
    watched_at TEXT DEFAULT CURRENT_TIMESTAMP
)
''')
conn.commit()

cursor.execute('''
CREATE TABLE IF NOT EXISTS ads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT,
    duration_text TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT,
    is_active INTEGER DEFAULT 1
)
''')
conn.commit()

try:
    cursor.execute("ALTER TABLE ads ADD COLUMN channel_username TEXT DEFAULT ''")
except sqlite3.OperationalError:
    pass  # ustun allaqachon mavjud
conn.commit()

cursor.execute('''
CREATE TABLE IF NOT EXISTS premieres (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    caption TEXT,
    file_id TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS premiere_sends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    premiere_id INTEGER,
    user_id INTEGER,
    message_id INTEGER
)
''')
conn.commit()

# Qidiruvni tezlashtiruvchi indekslar
cursor.execute('CREATE INDEX IF NOT EXISTS idx_movies_code ON movies(code)')
cursor.execute('CREATE INDEX IF NOT EXISTS idx_history_user ON history(user_id, watched_at)')
cursor.execute('CREATE INDEX IF NOT EXISTS idx_watchlist_user ON watchlist(user_id)')
cursor.execute('CREATE INDEX IF NOT EXISTS idx_ads_active ON ads(is_active)')
cursor.execute('CREATE INDEX IF NOT EXISTS idx_premiere_sends_premiere ON premiere_sends(premiere_id)')
conn.commit()


# ============ YORDAMCHI FUNKSIYALAR ============

def increment_counter(key):
    """Doimiy o'suvchi hisoblagich. Masalan 'movie_count'.
    Kino o'chirilsa ham bu raqam pasaymaydi - haqiqiy "nechanchi bo'lib yuklangani"ni ko'rsatadi."""
    cursor.execute('''
        INSERT INTO counters (key, value) VALUES (?, 1)
        ON CONFLICT(key) DO UPDATE SET value = value + 1
    ''', (key,))
    conn.commit()
    cursor.execute('SELECT value FROM counters WHERE key = ?', (key,))
    return cursor.fetchone()[0]


def get_next_free_movie_number():
    """Navbatdagi bo'sh (hali ishlatilmagan) kino raqamini qaytaradi.
    Hisoblagich o'chirishlar tufayli siljigan bo'lsa ham, mavjud kodlar bilan
    to'qnashib qolmasligi uchun tekshirib chiqadi."""
    while True:
        candidate = increment_counter('movie_count')
        cursor.execute('SELECT 1 FROM movies WHERE code = ?', (str(candidate),))
        if cursor.fetchone() is None:
            return candidate


async def is_subscribed_to(user_id, channel_id):
    try:
        member = await asyncio.wait_for(
            bot.get_chat_member(chat_id=channel_id, user_id=user_id),
            timeout=4
        )
        return member.status in ['member', 'administrator', 'creator']
    except asyncio.TimeoutError:
        logging.warning(f"Obunani tekshirish {channel_id} uchun vaqt tugadi (timeout)")
        return False
    except Exception as e:
        logging.warning(f"Obunani tekshirishda xato ({channel_id}): {e}")
        return False


SUBSCRIPTION_CACHE = {}  # user_id -> (natija: bool, tekshirilgan_vaqt: float)
SUBSCRIPTION_CACHE_TTL = 300  # 5 daqiqa - shu muddatda qayta so'ramaydi, bot tezroq javob beradi


async def is_subscribed(user_id):
    """Foydalanuvchi BARCHA majburiy kanallarga (doimiy 2 ta + admin qo'shgan faol havola-kanallar)
    obuna bo'lgandagina True qaytaradi.
    Tezlik uchun: 1) barcha kanallar BIR VAQTDA tekshiriladi, 2) natija 5 daqiqaga keshlanadi."""
    cached = SUBSCRIPTION_CACHE.get(user_id)
    if cached and (time.time() - cached[1] < SUBSCRIPTION_CACHE_TTL):
        return cached[0]

    all_channels = CHANNELS + get_active_ad_channels()
    results = await asyncio.gather(*[is_subscribed_to(user_id, ch) for ch in all_channels])
    result = all(results)
    if result:
        SUBSCRIPTION_CACHE[user_id] = (result, time.time())
    else:
        SUBSCRIPTION_CACHE.pop(user_id, None)  # obuna emasligini keshlamaymiz - doim qayta tekshiriladi
    return result


LAST_ACTIVE_UPDATE = {}  # user_id -> oxirgi yozilgan vaqt (soniya) - keraksiz bazaga yozishlarni kamaytirish uchun


def ensure_user(user_id, referred_by=None, full_name=None, username=None):
    now_ts = time.time()
    last_update = LAST_ACTIVE_UPDATE.get(user_id)

    if last_update and (now_ts - last_update < 60):
        return False  # yaqinda yozilgan, bazaga qayta murojaat qilmaymiz

    now_str = time.strftime('%Y-%m-%d %H:%M:%S')
    cursor.execute('SELECT is_active FROM users WHERE user_id = ?', (user_id,))
    existing = cursor.fetchone()

    if existing is None:
        cursor.execute(
            'INSERT INTO users (user_id, referred_by, points, full_name, username, last_active, is_active) '
            'VALUES (?, ?, 0, ?, ?, ?, 1)',
            (user_id, referred_by, full_name or '', username or '', now_str)
        )
        conn.commit()
        if referred_by and referred_by != user_id:
            cursor.execute('UPDATE users SET points = points + 1 WHERE user_id = ?', (referred_by,))
            conn.commit()
            cursor.execute('SELECT points FROM users WHERE user_id = ?', (referred_by,))
            points = cursor.fetchone()[0]
            try:
                asyncio.create_task(notify_referrer(referred_by, points))
            except Exception:
                pass
        LAST_ACTIVE_UPDATE[user_id] = now_ts
        return True

    was_active = existing[0]
    cursor.execute(
        'UPDATE users SET full_name = ?, username = ?, last_active = ?, is_active = 1 WHERE user_id = ?',
        (full_name or '', username or '', now_str, user_id)
    )
    conn.commit()

    if was_active == 0:
        cursor.execute(
            "INSERT INTO status_log (user_id, old_status, new_status) VALUES (?, 'passive', 'active')",
            (user_id,)
        )
        conn.commit()

    LAST_ACTIVE_UPDATE[user_id] = now_ts
    return False


async def notify_referrer(referrer_id, points):
    try:
        text = f"🎉 Yangi do'stingiz botga qo'shildi! Sizda hozir {points} ball bor."
        if points >= REFERRAL_REWARD_THRESHOLD:
            text += f"\n🏆 Siz {REFERRAL_REWARD_THRESHOLD} ballga yetdingiz — mukofot sifatida eksklyuziv kontent ochildi!"
        await bot.send_message(referrer_id, text)
    except Exception as e:
        logging.warning(f"Referrerga xabar yuborishda xato: {e}")


def movie_caption(movie_id, name):
    cursor.execute('SELECT AVG(score), COUNT(score) FROM ratings WHERE movie_id = ?', (movie_id,))
    avg, count = cursor.fetchone()
    if avg:
        rating_text = f"\n⭐ Reyting: {avg:.1f}/5 ({count} baho)"
    else:
        rating_text = "\n⭐ Hali baholanmagan"
    # Kanal havolasi CAPTION MATNI ICHIDA - shunda video forward qilinganda ham yo'qolmaydi.
    # (Inline tugma forward paytida saqlanib qolishi kafolatlanmaydi, shu sababli matn sifatida ham qo'shildi.)
    channel_line = (
        f"\n\n📢 Kanal: https://t.me/{CHANNEL_ID[1:]}"
        f"\n📢 Kanal: https://t.me/{CHANNEL_ID2[1:]}"
    )
    return f"🎬 {name}{rating_text}{channel_line}"


def channel_button_row():
    """Har qanday videoning tagida turadigan yagona kanal tugmasi.
    Inline tugma bo'lgani uchun, video forward qilinganda ham Telegram uni saqlab qoladi."""
    return [
        InlineKeyboardButton(text="🎬 Kinolarni qidirish", url=f"https://t.me/{CHANNEL_ID2[1:]}")
    ]


def bot_link_button():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Botga o'tish", url=f"https://t.me/{BOT_USERNAME}")]
    ])


def bot_link_and_ads_keyboard():
    """'Xabar qoldirish' orqali yuborilgan xabarlar ostiga botga o'tish tugmasi bilan
    birga hozirda faol bo'lgan barcha reklama havolalarini ham qo'shadi."""
    rows = [[InlineKeyboardButton(text="🤖 Botga o'tish", url=f"https://t.me/{BOT_USERNAME}")]] + ads_keyboard_rows()
    return InlineKeyboardMarkup(inline_keyboard=rows)


def movie_keyboard(movie_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Saqlash", callback_data=f"save:{movie_id}")],
        [
            InlineKeyboardButton(text="1⭐", callback_data=f"rate:{movie_id}:1"),
            InlineKeyboardButton(text="2⭐", callback_data=f"rate:{movie_id}:2"),
            InlineKeyboardButton(text="3⭐", callback_data=f"rate:{movie_id}:3"),
            InlineKeyboardButton(text="4⭐", callback_data=f"rate:{movie_id}:4"),
            InlineKeyboardButton(text="5⭐", callback_data=f"rate:{movie_id}:5"),
        ],
        channel_button_row()
    ])


async def send_movie(chat_id, movie_row):
    movie_id, name, file_id = movie_row[0], movie_row[1], movie_row[2]
    cursor.execute('UPDATE movies SET search_count = search_count + 1 WHERE id = ?', (movie_id,))
    cursor.execute('SELECT code FROM movies WHERE id = ?', (movie_id,))
    code_row = cursor.fetchone()
    movie_code = code_row[0] if code_row else ''
    cursor.execute('''
        INSERT INTO history (user_id, movie_id, movie_name, movie_code)
        VALUES (?, ?, ?, ?)
    ''', (chat_id, movie_id, name, movie_code))
    conn.commit()
    await bot.send_video(
        chat_id,
        file_id,
        caption=movie_caption(movie_id, name),
        reply_markup=movie_keyboard(movie_id)
    )


def swap_movie(movie_id, direction):
    cursor.execute('SELECT id, order_num FROM movies ORDER BY order_num ASC, id ASC')
    movies = cursor.fetchall()
    ids = [m[0] for m in movies]
    if movie_id not in ids:
        return
    idx = ids.index(movie_id)
    swap_idx = idx + direction
    if swap_idx < 0 or swap_idx >= len(movies):
        return

    order_values = [m[1] for m in movies]
    if len(set(order_values)) < len(order_values):
        for i, (mid, _) in enumerate(movies):
            cursor.execute('UPDATE movies SET order_num = ? WHERE id = ?', (i, mid))
        conn.commit()
        cursor.execute('SELECT id, order_num FROM movies ORDER BY order_num ASC, id ASC')
        movies = cursor.fetchall()

    id1, order1 = movies[idx]
    id2, order2 = movies[swap_idx]
    cursor.execute('UPDATE movies SET order_num = ? WHERE id = ?', (order2, id1))
    cursor.execute('UPDATE movies SET order_num = ? WHERE id = ?', (order1, id2))
    conn.commit()


def admin_list_text_and_keyboard(page=0):
    per_page = 10
    offset = page * per_page

    cursor.execute('SELECT COUNT(*) FROM movies')
    total = cursor.fetchone()[0]
    if total == 0:
        return "Bazada hali kino yo'q.", None
    total_pages = (total - 1) // per_page

    cursor.execute(
        'SELECT id, name, code FROM movies ORDER BY order_num ASC, id ASC LIMIT ? OFFSET ?',
        (per_page, offset)
    )
    movies = cursor.fetchall()

    if not movies:
        return "Bu sahifada kino yo'q.", InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ 1-sahifaga", callback_data="adm_page:0")
        ]])

    text = (
        f"🛠 Barcha kinolar ({offset + 1}–{offset + len(movies)} / {total}) "
        f"— tartiblash (⬆️/⬇️), o'chirish (🗑):"
    )
    rows = []
    for idx, (mid, name, code) in enumerate(movies, start=offset + 1):
        label = f"{idx}. {name}" + (f" [{code}]" if code else "")
        rows.append([
            InlineKeyboardButton(text="⬆️", callback_data=f"adm_up:{mid}:{page}"),
            InlineKeyboardButton(text=label, callback_data=f"show:{mid}"),
            InlineKeyboardButton(text="⬇️", callback_data=f"adm_down:{mid}:{page}"),
            InlineKeyboardButton(text="🗑", callback_data=f"adm_del:{mid}:{page}")
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"adm_page:{page - 1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"adm_page:{page + 1}"))
    if nav:
        rows.append(nav)

    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def parse_duration_to_seconds(text):
    """'2 kun', '12 soat', '1 yil', '3 oy', '45 soniya' kabi matnlarni soniyaga aylantiradi.
    Bir nechta birlikni birga ham qabul qiladi: '1 yil 2 oy 3 kun'.
    Hech narsa tushunilmasa None qaytaradi."""
    text = text.lower()
    total_seconds = 0
    found = False

    year_match = re.search(r'(\d+)\s*yil', text)
    if year_match:
        total_seconds += int(year_match.group(1)) * 31536000  # 365 kun
        found = True

    month_match = re.search(r'(\d+)\s*oy', text)
    if month_match:
        total_seconds += int(month_match.group(1)) * 2592000  # 30 kun
        found = True

    day_match = re.search(r'(\d+)\s*kun', text)
    if day_match:
        total_seconds += int(day_match.group(1)) * 86400
        found = True

    hour_match = re.search(r'(\d+)\s*soat', text)
    if hour_match:
        total_seconds += int(hour_match.group(1)) * 3600
        found = True

    minute_match = re.search(r'(\d+)\s*(daqiqa|minut)', text)
    if minute_match:
        total_seconds += int(minute_match.group(1)) * 60
        found = True

    second_match = re.search(r'(\d+)\s*(soniya|sekund)', text)
    if second_match:
        total_seconds += int(second_match.group(1))
        found = True

    if not found:
        return None
    return total_seconds


def format_remaining(expires_at_str):
    """Havola tugashiga qancha vaqt qolganini o'qiladigan matn qilib qaytaradi."""
    try:
        expires_ts = time.mktime(time.strptime(expires_at_str, '%Y-%m-%d %H:%M:%S'))
    except Exception:
        return "noma'lum"
    remaining = expires_ts - time.time()
    if remaining <= 0:
        return "muddati tugagan"

    years = int(remaining // 31536000)
    remaining -= years * 31536000
    months = int(remaining // 2592000)
    remaining -= months * 2592000
    days = int(remaining // 86400)
    remaining -= days * 86400
    hours = int(remaining // 3600)
    remaining -= hours * 3600
    minutes = int(remaining // 60)
    remaining -= minutes * 60
    seconds = int(remaining)

    parts = []
    if years:
        parts.append(f"{years} yil")
    if months:
        parts.append(f"{months} oy")
    if days and not years:
        parts.append(f"{days} kun")
    if hours and not years and not months:
        parts.append(f"{hours} soat")
    if minutes and not years and not months and not days:
        parts.append(f"{minutes} daqiqa")
    if seconds and not years and not months and not days and not hours:
        parts.append(f"{seconds} soniya")

    return " ".join(parts) if parts else "0 soniya"


def looks_like_valid_url(url):
    """Telegram tugmasi qabul qila oladigan haqiqiy havolaga o'xshaydimi - tekshiradi.
    Domen nomi va bo'sh joy yo'qligini talab qiladi."""
    if " " in url or "\n" in url:
        return False
    return bool(re.match(r'^https?://[^\s/$.?#][^\s]*\.[^\s]+', url, re.IGNORECASE))


def get_active_ads():
    cursor.execute('SELECT id, url FROM ads WHERE is_active = 1 ORDER BY id ASC')
    return cursor.fetchall()


def extract_tme_channel(url):
    """Agar havola https://t.me/kanal_nomi ko'rinishida bo'lsa, '@kanal_nomi' qaytaradi.
    Aks holda (masalan Instagram havolasi yoki shaxsiy taklif havolasi) None qaytaradi -
    bunday havolalar uchun obuna MAJBURIY TALAB sifatida tekshirilmaydi, faqat tugma sifatida ko'rsatiladi."""
    match = re.match(r'^https?://t\.me/([A-Za-z0-9_]{5,32})/?$', url.strip(), re.IGNORECASE)
    if match:
        return f"@{match.group(1)}"
    return None


def get_active_ad_channels():
    """Hozirda faol bo'lgan, MAJBURIY OBUNA sifatida tekshirilishi mumkin bo'lgan
    (ya'ni https://t.me/kanal_nomi ko'rinishidagi) havolalarning kanal nomlarini qaytaradi."""
    cursor.execute("SELECT channel_username FROM ads WHERE is_active = 1 AND channel_username != ''")
    return [r[0] for r in cursor.fetchall()]


def ads_keyboard_rows():
    """Faol reklama havolalarini tugma qatorlariga aylantiradi - admin yuborgan
    har qanday xabar ostiga qo'shish uchun."""
    return [[InlineKeyboardButton(text="Obuna bo'lish ➕", url=url)] for _, url in get_active_ads()]


def get_min_remaining_seconds():
    """Barcha faol havolalar orasida eng tez tugaydiganigacha necha soniya qolganini qaytaradi.
    Jonli hisoblagich qanchalik tez-tez yangilanishi kerakligini shu belgilaydi."""
    cursor.execute('SELECT expires_at FROM ads WHERE is_active = 1')
    rows = cursor.fetchall()
    if not rows:
        return None
    now = time.time()
    remainings = []
    for (expires_at,) in rows:
        try:
            expires_ts = time.mktime(time.strptime(expires_at, '%Y-%m-%d %H:%M:%S'))
            remainings.append(expires_ts - now)
        except Exception:
            continue
    if not remainings:
        return None
    return min(remainings)


def ads_text_block():
    """Faol reklama havolalarini matn ko'rinishida qaytaradi - shu bilan
    xabar forward qilinganda ham havola yo'qolmaydi."""
    ads = [url for _, url in get_active_ads() if looks_like_valid_url(url)]
    if not ads:
        return ""
    lines = "\n".join(ads)
    return f"\n\n{lines}"


def build_ads_list_text_and_keyboard():
    cursor.execute('SELECT id, url, expires_at FROM ads WHERE is_active = 1 ORDER BY id DESC')
    ads = cursor.fetchall()
    if not ads:
        return "📋 Hozircha faol havolalar yo'q.", None
    text = "📋 Faol havolalar ro'yxati:\n\n"
    rows = []
    for idx, (ad_id, url, expires_at) in enumerate(ads, start=1):
        remaining = format_remaining(expires_at)
        text += f"{idx}. {url}\n⏳ Qoldi: {remaining}\n\n"
        rows.append([InlineKeyboardButton(text=f"🗑 {idx}-havolani o'chirish", callback_data=f"ad_del:{ad_id}")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def main_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎲 Tasodifiy kino", callback_data="random"),
            InlineKeyboardButton(text="🔥 Top kinolar", callback_data="top")
        ],
        [
            InlineKeyboardButton(text="🎁 Do'st taklif qilish", callback_data="ref"),
            InlineKeyboardButton(text="📋 Mening ro'yxatim", callback_data="mylist")
        ],
        [
            InlineKeyboardButton(text="🕐 Tarix", callback_data="history")
        ]
    ])


def build_subscribe_keyboard():
    """/start bosilganda chiqadigan obuna tugmalari. Doimiy 2 ta kanal + admin
    'Havola kiritish' orqali qo'shgan, HALI MUDDATI TUGAMAGAN barcha havolalar.
    Havola muddati tugashi bilan bu yerdan AVTOMATIK yo'qoladi."""
    rows = [
        [InlineKeyboardButton(text="Obuna bo'lish ➕", url=f"https://t.me/{CHANNEL_ID[1:]}")],
        [InlineKeyboardButton(text="Obuna bo'lish ➕", url=f"https://t.me/{CHANNEL_ID2[1:]}")],
    ]
    for _, url in get_active_ads():
        rows.append([InlineKeyboardButton(text="Obuna bo'lish ➕", url=url)])
    rows.append([InlineKeyboardButton(text="Tekshirish🔁", callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_reply_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=PREMIERE_BUTTON_TEXT)],
            [KeyboardButton(text=ADMIN_BUTTON_TEXT), KeyboardButton(text=BROADCAST_BUTTON_TEXT)],
            [KeyboardButton(text=STATS_BUTTON_TEXT)],
            [KeyboardButton(text=MEMBERS_BUTTON_TEXT), KeyboardButton(text=ACTIVE_SUBS_BUTTON_TEXT)],
            [KeyboardButton(text=LINK_BUTTON_TEXT), KeyboardButton(text=LIST_BUTTON_TEXT)],
            [KeyboardButton(text=USER_CANCEL_BUTTON_TEXT)]
        ],
        resize_keyboard=True,
        is_persistent=True
    )


def user_reply_keyboard():
    """Oddiy foydalanuvchilar uchun doim pastda ko'rinib turadigan bekor qilish tugmasi."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=USER_CANCEL_BUTTON_TEXT)]],
        resize_keyboard=True,
        is_persistent=True
    )


def cancel_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="adm_cancel")]
    ])


async def open_admin_panel(chat_id, page=0):
    text, kb = admin_list_text_and_keyboard(page)
    await bot.send_message(chat_id, text, reply_markup=kb)


LIVE_LIST_TASKS = {}  # admin_id -> asyncio.Task (har bir adminda faqat bitta jonli hisoblagich)


async def live_list_updater(chat_id, message_id):
    """'Ro'yxat' xabarini muddat tugaguncha avtomatik yangilab turadi.
    Qolgan vaqtga qarab tezligini o'zi moslaydi - soniyalar bilan sanaladigan
    havolalar uchun ham (masalan '45 soniya') jonli ko'rinadi."""
    while True:
        min_remaining = get_min_remaining_seconds()
        if min_remaining is None:
            try:
                await bot.edit_message_text(
                    "📋 Hozircha faol havolalar yo'q.",
                    chat_id=chat_id, message_id=message_id
                )
            except Exception:
                pass
            break

        if min_remaining > 86400:
            interval = 300
        elif min_remaining > 3600:
            interval = 30
        elif min_remaining > 60:
            interval = 5
        else:
            interval = 2

        await asyncio.sleep(interval)

        text, kb = build_ads_list_text_and_keyboard()
        try:
            if text is None or kb is None:
                await bot.edit_message_text("📋 Hozircha faol havolalar yo'q.", chat_id=chat_id, message_id=message_id)
                break
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=kb)
        except Exception as e:
            if "message is not modified" not in str(e).lower():
                logging.warning(f"Jonli hisoblagichni yangilashda xato: {e}")
                break


async def get_all_user_ids():
    cursor.execute('SELECT user_id FROM users')
    return [r[0] for r in cursor.fetchall()]


async def broadcast_video_to_all(file_id, caption_text, progress_chat_id=None, progress_message_id=None, premiere_id=None):
    """Premyerani (video + tayyor caption) barchaga yuboradi.
    Agar premiere_id berilsa, har bir yuborilgan xabarning chat_id/message_id manzili
    premiere_sends jadvaliga yoziladi - shu orqali keyinchalik premyerani HAMMA
    foydalanuvchidan o'chirish (delete_message) imkoniyati paydo bo'ladi."""
    sent, failed = 0, 0
    kb = InlineKeyboardMarkup(inline_keyboard=[channel_button_row()] + ads_keyboard_rows())
    user_ids = await get_all_user_ids()
    total = len(user_ids)
    for uid in user_ids:
        try:
            msg = await bot.send_video(uid, file_id, caption=caption_text, reply_markup=kb)
            sent += 1
            if premiere_id is not None:
                cursor.execute(
                    'INSERT INTO premiere_sends (premiere_id, user_id, message_id) VALUES (?, ?, ?)',
                    (premiere_id, uid, msg.message_id)
                )
                if sent % 20 == 0:
                    conn.commit()  # har safar emas, har 20 ta yozuvda bir marta saqlaymiz
        except Exception as e:
            failed += 1
            logging.warning(f"Premyera yuborishda xato ({uid}): {e}")
        if progress_chat_id and (sent + failed) % 50 == 0:
            try:
                await bot.edit_message_text(
                    f"📤 Yuborilmoqda... {sent + failed}/{total}",
                    chat_id=progress_chat_id,
                    message_id=progress_message_id
                )
            except Exception:
                pass
        await asyncio.sleep(0.05)
    conn.commit()  # oxirida qolgan yozuvlarni ham saqlab qo'yamiz
    return sent, failed


async def broadcast_content_to_all(content, extra_caption=None, progress_chat_id=None, progress_message_id=None):
    """Xabar qoldirish uchun: matn, video yoki ovozli xabarni barchaga yuboradi."""
    sent, failed = 0, 0
    user_ids = await get_all_user_ids()
    total = len(user_ids)
    kb = bot_link_and_ads_keyboard()
    for uid in user_ids:
        try:
            if content['type'] == 'text':
                text = content['text']
                if extra_caption:
                    text = f"{text}\n\n{extra_caption}"
                await bot.send_message(uid, text, reply_markup=kb)
            elif content['type'] == 'photo':
                caption = content.get('caption') or ''
                if extra_caption:
                    caption = f"{caption}\n\n{extra_caption}" if caption else extra_caption
                await bot.send_photo(uid, content['file_id'], caption=caption or None, reply_markup=kb)
            elif content['type'] == 'video':
                caption = content.get('caption') or ''
                if extra_caption:
                    caption = f"{caption}\n\n{extra_caption}" if caption else extra_caption
                await bot.send_video(uid, content['file_id'], caption=caption or None, reply_markup=kb)
            elif content['type'] == 'voice':
                caption = content.get('caption') or ''
                if extra_caption:
                    caption = f"{caption}\n\n{extra_caption}" if caption else extra_caption
                await bot.send_voice(uid, content['file_id'], caption=caption or None, reply_markup=kb)
            sent += 1
        except Exception as e:
            failed += 1
            logging.warning(f"Xabar yuborishda xato ({uid}): {e}")
        if progress_chat_id and (sent + failed) % 50 == 0:
            try:
                await bot.edit_message_text(
                    f"📤 Yuborilmoqda... {sent + failed}/{total}",
                    chat_id=progress_chat_id,
                    message_id=progress_message_id
                )
            except Exception:
                pass
        await asyncio.sleep(0.05)
    return sent, failed


# ============ HANDLERLAR ============

@dp.message(Command("start"))
async def start(message: types.Message, command: CommandObject):
    user_id = message.from_user.id
    referred_by = None

    if command.args and command.args.startswith("REF"):
        try:
            referred_by = int(command.args.replace("REF", ""))
        except ValueError:
            referred_by = None

    ensure_user(
        user_id, referred_by,
        full_name=message.from_user.full_name,
        username=message.from_user.username
    )

    if user_id == ADMIN_ID:
        await message.answer(
            "🛠 Xush kelibsiz, Admin!\nPastdagi doimiy tugmalar orqali istalgan vaqt boshqarishingiz mumkin 👇",
            reply_markup=admin_reply_keyboard()
        )
        return

    await message.answer(
        "⚠️ Botdan to'liq foydalanish uchun quyidagi kanallarga obuna bo'ling",
        reply_markup=build_subscribe_keyboard()
    )


@dp.callback_query(F.data == "check_sub")
async def check_sub_callback(callback: types.CallbackQuery):
    if await is_subscribed(callback.from_user.id):
        await callback.message.edit_text(
            "✅ Obuna tasdiqlandi!\n\n"
            "🎥 Kino kodini yuboring",
            reply_markup=main_menu_keyboard()
        )
    else:
        await callback.answer("❌ Siz hali kanalga obuna bo'lmagansiz!", show_alert=True)


@dp.callback_query(F.data.startswith("show:"))
async def show_movie_callback(callback: types.CallbackQuery):
    movie_id = int(callback.data.split(":")[1])
    cursor.execute('SELECT id, name, file_id FROM movies WHERE id = ?', (movie_id,))
    movie = cursor.fetchone()
    if movie:
        await send_movie(callback.message.chat.id, movie)
    else:
        await callback.answer("Kino topilmadi.", show_alert=True)


@dp.callback_query(F.data == "random")
async def random_callback(callback: types.CallbackQuery):
    cursor.execute('SELECT id, name, file_id FROM movies ORDER BY RANDOM() LIMIT 1')
    movie = cursor.fetchone()
    if movie:
        await send_movie(callback.message.chat.id, movie)
    else:
        await callback.answer("Bazada kino yo'q.", show_alert=True)


@dp.callback_query(F.data == "top")
async def top_callback(callback: types.CallbackQuery):
    cursor.execute('SELECT id, name, search_count FROM movies ORDER BY search_count DESC LIMIT 10')
    movies = cursor.fetchall()
    if not movies:
        await callback.answer("Bazada kino yo'q.", show_alert=True)
        return
    keyboard = [[InlineKeyboardButton(text=f"{name} ({cnt} marta)", callback_data=f"show:{mid}")]
                for mid, name, cnt in movies]
    keyboard.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back_to_menu")])
    await callback.message.edit_text("🔥 Eng ko'p qidirilgan kinolar:",
                                      reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))


@dp.callback_query(F.data == "ref")
async def ref_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    ensure_user(user_id, full_name=callback.from_user.full_name, username=callback.from_user.username)
    cursor.execute('SELECT points FROM users WHERE user_id = ?', (user_id,))
    points = cursor.fetchone()[0]
    link = f"https://t.me/{BOT_USERNAME}?start=REF{user_id}"
    await callback.message.answer(
        f"🎁 Do'stlaringizni taklif qiling!\n\n"
        f"Sizning shaxsiy linkingiz:\n{link}\n\n"
        f"Hozirgi ballaringiz: {points}\n"
        f"{REFERRAL_REWARD_THRESHOLD} ballga yetsangiz maxsus mukofot olasiz!"
    )


@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu_callback(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "Bosh menyu — kerakli bo'limni tanlang, yoki kino kodini yozing:",
        reply_markup=main_menu_keyboard()
    )


@dp.callback_query(F.data == "mylist")
async def mylist_callback(callback: types.CallbackQuery):
    text, keyboard = build_mylist_content(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=keyboard)


@dp.message(Command("mylist"))
async def mylist_command(message: types.Message):
    text, keyboard = build_mylist_content(message.from_user.id, from_command=True)
    await message.answer(text, reply_markup=keyboard)


def build_mylist_content(user_id, from_command=False):
    cursor.execute('''
        SELECT movies.id, movies.name FROM watchlist
        JOIN movies ON watchlist.movie_id = movies.id
        WHERE watchlist.user_id = ?
    ''', (user_id,))
    movies = cursor.fetchall()
    if not movies:
        text = "📋 Ro'yxatingiz bo'sh. Kino ostidagi ⭐ Saqlash tugmasini bosing."
        keyboard = None
        if not from_command:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back_to_menu")]
            ])
        return text, keyboard

    keyboard = [[InlineKeyboardButton(text=name, callback_data=f"show:{mid}")] for mid, name in movies]
    keyboard.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back_to_menu")])
    return "📋 Sizning ro'yxatingiz:", InlineKeyboardMarkup(inline_keyboard=keyboard)


@dp.callback_query(F.data.startswith("save:"))
async def save_callback(callback: types.CallbackQuery):
    movie_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    ensure_user(user_id, full_name=callback.from_user.full_name, username=callback.from_user.username)
    try:
        cursor.execute('INSERT INTO watchlist (user_id, movie_id) VALUES (?, ?)', (user_id, movie_id))
        conn.commit()
        await callback.answer("✅ Ro'yxatga qo'shildi!")
    except sqlite3.IntegrityError:
        await callback.answer("Bu kino allaqachon ro'yxatingizda.")


@dp.callback_query(F.data.startswith("rate:"))
async def rate_callback(callback: types.CallbackQuery):
    _, movie_id, score = callback.data.split(":")
    movie_id, score = int(movie_id), int(score)
    user_id = callback.from_user.id
    ensure_user(user_id, full_name=callback.from_user.full_name, username=callback.from_user.username)

    cursor.execute('''
        INSERT INTO ratings (user_id, movie_id, score) VALUES (?, ?, ?)
        ON CONFLICT(user_id, movie_id) DO UPDATE SET score = excluded.score
    ''', (user_id, movie_id, score))
    conn.commit()

    cursor.execute('SELECT name FROM movies WHERE id = ?', (movie_id,))
    name = cursor.fetchone()[0]
    try:
        await callback.message.edit_caption(
            caption=movie_caption(movie_id, name),
            reply_markup=movie_keyboard(movie_id)
        )
    except Exception:
        pass
    await callback.answer(f"Siz {score}⭐ baho berdingiz!")


# ---------- ADMIN: umumiy tartiblash paneli ----------

@dp.callback_query(F.data == "adm_cancel")
async def adm_cancel_callback(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await callback.message.edit_text("❌ Bekor qilindi.")


@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await open_admin_panel(message.chat.id)


@dp.callback_query(F.data.startswith("adm_page:"))
async def adm_page_callback(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Ruxsat yo'q.", show_alert=True)
        return
    page = int(callback.data.split(":")[1])
    text, kb = admin_list_text_and_keyboard(page)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("adm_up:"))
async def adm_up_callback(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Ruxsat yo'q.", show_alert=True)
        return
    _, movie_id, page = callback.data.split(":")
    movie_id, page = int(movie_id), int(page)
    swap_movie(movie_id, direction=-1)
    text, kb = admin_list_text_and_keyboard(page)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer("⬆️ Ko'tarildi")


@dp.callback_query(F.data.startswith("adm_down:"))
async def adm_down_callback(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Ruxsat yo'q.", show_alert=True)
        return
    _, movie_id, page = callback.data.split(":")
    movie_id, page = int(movie_id), int(page)
    swap_movie(movie_id, direction=1)
    text, kb = admin_list_text_and_keyboard(page)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer("⬇️ Tushirildi")


@dp.callback_query(F.data.startswith("adm_del:"))
async def adm_del_callback(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Ruxsat yo'q.", show_alert=True)
        return
    _, movie_id, page = callback.data.split(":")
    movie_id, page = int(movie_id), int(page)
    cursor.execute('SELECT name FROM movies WHERE id = ?', (movie_id,))
    row = cursor.fetchone()
    if not row:
        await callback.answer("Kino topilmadi.", show_alert=True)
        return
    name = row[0]
    await callback.message.edit_text(
        f"⚠️ '{name}' kinoni rostdan ham o'chirmoqchimisiz?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Ha, o'chirish", callback_data=f"adm_del_yes:{movie_id}:{page}"),
            InlineKeyboardButton(text="❌ Yo'q", callback_data=f"adm_del_no:{page}")
        ]])
    )


@dp.callback_query(F.data.startswith("adm_del_yes:"))
async def adm_del_yes_callback(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Ruxsat yo'q.", show_alert=True)
        return
    _, movie_id, page = callback.data.split(":")
    movie_id, page = int(movie_id), int(page)

    cursor.execute('DELETE FROM movies WHERE id = ?', (movie_id,))
    conn.commit()

    cursor.execute('SELECT COUNT(*) FROM movies')
    remaining = cursor.fetchone()[0]

    if remaining == 0:
        cursor.execute("UPDATE counters SET value = 0 WHERE key = 'movie_count'")
    else:
        cursor.execute("SELECT value FROM counters WHERE key = 'movie_count'")
        row = cursor.fetchone()
        current = row[0] if row else 0
        cursor.execute("UPDATE counters SET value = ? WHERE key = 'movie_count'", (max(current - 1, 0),))
    conn.commit()

    text, kb = admin_list_text_and_keyboard(page)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer("🗑 O'chirildi, hisoblagich yangilandi")


@dp.callback_query(F.data.startswith("adm_del_no:"))
async def adm_del_no_callback(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    page = int(callback.data.split(":")[1])
    text, kb = admin_list_text_and_keyboard(page)
    await callback.message.edit_text(text, reply_markup=kb)


@dp.callback_query(F.data.startswith("fillslot:"))
async def fillslot_callback(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Ruxsat yo'q.", show_alert=True)
        return
    slot_number = int(callback.data.split(":")[1])
    await state.set_state(AdminStates.waiting_slot_video)
    await state.update_data(slot_number=slot_number)
    await callback.message.edit_text(
        f"📤 {slot_number}-raqamga yangi kino video yuklang.\n"
        f"Caption formati: <code>Nomi | Yil(ixtiyoriy)</code>",
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("leaveslot:"))
async def leaveslot_callback(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Ruxsat yo'q.", show_alert=True)
        return
    slot_number = int(callback.data.split(":")[1])
    await callback.message.edit_text(f"🗂 {slot_number}-raqam bo'sh holicha qoldirildi.")
    await callback.answer()
    text, kb = admin_list_text_and_keyboard()
    await bot.send_message(callback.message.chat.id, text, reply_markup=kb)


@dp.message(F.video, AdminStates.waiting_slot_video)
async def receive_slot_video(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    slot_number = data.get('slot_number')
    await state.clear()

    if slot_number is None:
        await message.reply("❌ Xatolik: raqam topilmadi. Qaytadan urinib ko'ring.")
        return

    caption = message.caption or ""
    parts = [p.strip() for p in caption.split("|")]
    name = parts[0] if len(parts) > 0 and parts[0] else "Noma'lum"
    try:
        year = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        year = 0

    code = str(slot_number)
    cursor.execute(
        'INSERT INTO movies (name, file_id, code, year, order_num, upload_number) VALUES (?, ?, ?, ?, ?, ?)',
        (name, message.video.file_id, code, year, slot_number, slot_number)
    )
    conn.commit()

    await message.reply(
        f"✅ {slot_number}-raqamga yangi kino qo'shildi.\nNomi: {name}\nYil: {year or '(kiritilmagan)'}"
    )


# ---------- ADMIN: doimiy pastki tugmalar ----------
# MUHIM: bu handlerlar generik F.text/F.video handlerlaridan OLDIN turishi shart.

@dp.message(F.text == ADMIN_BUTTON_TEXT)
async def admin_button_handler(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await open_admin_panel(message.chat.id)


def premiere_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Premyera yuklash", callback_data="premiere_upload")],
        [InlineKeyboardButton(text="🗑 Premyeralarni tahrirlash", callback_data="premiere_edit_list")]
    ])


@dp.message(F.text == PREMIERE_BUTTON_TEXT)
async def premiere_button_handler(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer("🎬 Nima qilmoqchisiz?", reply_markup=premiere_menu_keyboard())


@dp.callback_query(F.data == "premiere_menu")
async def premiere_menu_callback(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await callback.message.edit_text("🎬 Nima qilmoqchisiz?", reply_markup=premiere_menu_keyboard())


@dp.callback_query(F.data == "premiere_upload")
async def premiere_upload_callback(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.waiting_premiere_video)
    await callback.message.edit_text(
        "🎬 Premyera uchun qisqa kino videosini shu yerga yuboring.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="premiere_menu")]
        ])
    )


def build_premiere_list_keyboard():
    cursor.execute('SELECT id, name FROM premieres ORDER BY id DESC')
    rows = cursor.fetchall()
    if not rows:
        return (
            "🗑 Hozircha yuklangan premyeralar yo'q.",
            InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Orqaga", callback_data="premiere_menu")]])
        )
    text = "🗑 Premyeralar ro'yxati — o'chirish uchun tanlang:\n\n"
    kb_rows = []
    for idx, (pid, name) in enumerate(rows, start=1):
        text += f"{idx}. {name}\n"
        kb_rows.append([InlineKeyboardButton(text=f"🗑 {idx}. {name}", callback_data=f"premiere_del:{pid}")])
    kb_rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="premiere_menu")])
    return text, InlineKeyboardMarkup(inline_keyboard=kb_rows)


@dp.callback_query(F.data == "premiere_edit_list")
async def premiere_edit_list_callback(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    text, kb = build_premiere_list_keyboard()
    await callback.message.edit_text(text, reply_markup=kb)


@dp.callback_query(F.data.startswith("premiere_del:"))
async def premiere_del_callback(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    premiere_id = int(callback.data.split(":")[1])
    cursor.execute('SELECT name FROM premieres WHERE id = ?', (premiere_id,))
    row = cursor.fetchone()
    if not row:
        await callback.answer("Premyera topilmadi.", show_alert=True)
        return
    name = row[0]
    await callback.message.edit_text(
        f"⚠️ '{name}' premyerasini barcha foydalanuvchilardan o'chirmoqchimisiz?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Ha, o'chirish", callback_data=f"premiere_del_yes:{premiere_id}"),
            InlineKeyboardButton(text="❌ Yo'q", callback_data="premiere_edit_list")
        ]])
    )


@dp.callback_query(F.data.startswith("premiere_del_yes:"))
async def premiere_del_yes_callback(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    premiere_id = int(callback.data.split(":")[1])

    cursor.execute('SELECT user_id, message_id FROM premiere_sends WHERE premiere_id = ?', (premiere_id,))
    sends = cursor.fetchall()

    deleted, failed = 0, 0
    for uid, mid in sends:
        try:
            await bot.delete_message(chat_id=uid, message_id=mid)
            deleted += 1
        except Exception as e:
            failed += 1
            logging.warning(f"Premyera xabarini o'chirishda xato ({uid}): {e}")

    cursor.execute('DELETE FROM premiere_sends WHERE premiere_id = ?', (premiere_id,))
    cursor.execute('DELETE FROM premieres WHERE id = ?', (premiere_id,))
    conn.commit()

    await callback.answer(f"✅ {deleted} ta xabardan o'chirildi, {failed} ta muvaffaqiyatsiz.")
    text, kb = build_premiere_list_keyboard()
    await callback.message.edit_text(text, reply_markup=kb)


@dp.message(F.text == STATS_BUTTON_TEXT)
async def stats_button_handler(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    cursor.execute('SELECT COUNT(*) FROM movies')
    movie_count = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM users')
    user_count = cursor.fetchone()[0]
    cursor.execute('SELECT SUM(search_count) FROM movies')
    total_searches = cursor.fetchone()[0] or 0
    cursor.execute('SELECT user_id, points FROM users ORDER BY points DESC LIMIT 5')
    top_referrers = cursor.fetchall()

    text = (
        "📊 Bot statistikasi:\n\n"
        f"🎬 Jami kinolar: {movie_count}\n"
        f"👥 Jami foydalanuvchilar: {user_count}\n"
        f"🔍 Jami qidiruvlar: {total_searches}\n"
    )
    if top_referrers and top_referrers[0][1] > 0:
        text += "\n🏆 Eng faol referrerlar:\n"
        for i, (uid, pts) in enumerate(top_referrers, start=1):
            if pts > 0:
                text += f"{i}. ID {uid} — {pts} ball\n"

    await message.answer(text)


def build_members_keyboard(page=0):
    per_page = 15
    offset = page * per_page
    cursor.execute('''
        SELECT user_id, full_name, username FROM users
        ORDER BY user_id DESC
        LIMIT ? OFFSET ?
    ''', (per_page, offset))
    rows = cursor.fetchall()

    cursor.execute('SELECT COUNT(*) FROM users')
    total = cursor.fetchone()[0]
    total_pages = (total - 1) // per_page if total else 0

    if not rows:
        return "👥 Hali azolar yo'q.", None

    text = f"👥 Azolar ({page * per_page + 1}–{page * per_page + len(rows)} / {total}):\n\n"
    for i, (uid, name, username) in enumerate(rows, start=page * per_page + 1):
        uname = f"@{username}" if username else "(username yo'q)"
        text += f"{i}. {name or 'Nomasum'} — {uname} — ID: {uid}\n"

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"members_page:{page - 1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"members_page:{page + 1}"))
    keyboard = InlineKeyboardMarkup(inline_keyboard=[nav]) if nav else None
    return text, keyboard


@dp.message(F.text == MEMBERS_BUTTON_TEXT)
async def members_button_handler(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    text, kb = build_members_keyboard()
    await message.answer(text, reply_markup=kb)


@dp.callback_query(F.data.startswith("members_page:"))
async def members_page_callback(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Ruxsat yo'q.", show_alert=True)
        return
    page = int(callback.data.split(":")[1])
    text, kb = build_members_keyboard(page)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


def build_active_subs_text():
    cursor.execute('''
        SELECT users.user_id, users.full_name, users.username, users.is_active,
               COUNT(history.id) as watch_count
        FROM users
        LEFT JOIN history ON history.user_id = users.user_id
        GROUP BY users.user_id
        ORDER BY watch_count DESC
        LIMIT 20
    ''')
    rows = cursor.fetchall()

    if not rows:
        return "📡 Hali foydalanuvchilar yo'q."

    text = "📡 Eng faol obunachilar (🟢 faol / 🔴 passiv):\n\n"
    for i, (uid, name, username, is_active, watch_count) in enumerate(rows, start=1):
        dot = "🟢" if is_active else "🔴"
        uname = f"@{username}" if username else ""
        text += f"{i}. {dot} {name or 'Nomasum'} {uname} — {watch_count} marta ko'rgan\n"

    cursor.execute("SELECT COUNT(*) FROM status_log WHERE new_status = 'passive'")
    passive_transitions = cursor.fetchone()[0]
    text += f"\nℹ️ Jami {passive_transitions} marta foydalanuvchi faoldan passivga o'tgan."

    return text


@dp.message(F.text == ACTIVE_SUBS_BUTTON_TEXT)
async def active_subs_button_handler(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(build_active_subs_text())


@dp.message(F.text == BROADCAST_BUTTON_TEXT)
async def broadcast_button_handler(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.waiting_broadcast_message)
    await message.answer(
        "✉️ Xabar jo'nating (matn, video yoki ovozli xabar bo'lishi mumkin).",
        reply_markup=cancel_keyboard()
    )


@dp.message(F.text == LINK_BUTTON_TEXT)
async def link_button_handler(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.waiting_ad_link)
    await message.answer("🔗 Havola kiriting:", reply_markup=cancel_keyboard())


@dp.message(F.text == LIST_BUTTON_TEXT)
async def list_button_handler(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    old_task = LIVE_LIST_TASKS.pop(message.from_user.id, None)
    if old_task:
        old_task.cancel()

    text, kb = build_ads_list_text_and_keyboard()
    sent = await message.answer(text, reply_markup=kb)

    if text != "📋 Hozircha faol havolalar yo'q.":
        task = asyncio.create_task(live_list_updater(sent.chat.id, sent.message_id))
        LIVE_LIST_TASKS[message.from_user.id] = task


@dp.callback_query(F.data.startswith("ad_del:"))
async def ad_del_callback(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Ruxsat yo'q.", show_alert=True)
        return
    ad_id = int(callback.data.split(":")[1])
    cursor.execute('SELECT url FROM ads WHERE id = ?', (ad_id,))
    row = cursor.fetchone()
    if not row:
        await callback.answer("Havola topilmadi.", show_alert=True)
        return
    url = row[0]
    await callback.message.edit_text(
        f"⚠️ Ushbu havolani rostdan ham o'chirmoqchimisiz?\n\n🔗 {url}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Ha, o'chirish", callback_data=f"ad_del_yes:{ad_id}"),
            InlineKeyboardButton(text="❌ Yo'q", callback_data="ad_del_no")
        ]])
    )


@dp.callback_query(F.data.startswith("ad_del_yes:"))
async def ad_del_yes_callback(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Ruxsat yo'q.", show_alert=True)
        return
    ad_id = int(callback.data.split(":")[1])
    cursor.execute('UPDATE ads SET is_active = 0 WHERE id = ?', (ad_id,))
    conn.commit()
    SUBSCRIPTION_CACHE.clear()

    old_task = LIVE_LIST_TASKS.pop(callback.from_user.id, None)
    if old_task:
        old_task.cancel()

    text, kb = build_ads_list_text_and_keyboard()
    await callback.message.edit_text(text, reply_markup=kb)

    if text != "📋 Hozircha faol havolalar yo'q." and kb is not None:
        task = asyncio.create_task(live_list_updater(callback.message.chat.id, callback.message.message_id))
        LIVE_LIST_TASKS[callback.from_user.id] = task

    await callback.answer("🗑 O'chirildi")


@dp.callback_query(F.data == "ad_del_no")
async def ad_del_no_callback(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    text, kb = build_ads_list_text_and_keyboard()
    await callback.message.edit_text(text, reply_markup=kb)


# ---------- Har bir foydalanuvchi uchun /cancel (bekor qilish) ----------
# MUHIM: bu handlerlar pastdagi barcha FSM (holat) handlerlaridan OLDIN turishi shart,
# aks holda /cancel matni o'sha holat handleri tomonidan "yutib yuborilishi" mumkin.

@dp.message(Command("cancel"))
async def cancel_command(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Bekor qilindi.")


@dp.message(F.text == USER_CANCEL_BUTTON_TEXT)
async def cancel_button_handler(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Bekor qilindi.")


# ---------- ADMIN: Premyera oqimi (video -> "Nomi | Tavsif" -> barchaga avtomatik yuborish) ----------
# DIQQAT: premyera hech qanday kod bilan sanalmaydi va bazaga saqlanmaydi -
# u faqat bir martalik xabar sifatida barcha foydalanuvchilarga yuboriladi.

@dp.message(F.video, AdminStates.waiting_premiere_video)
async def receive_premiere_video(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.update_data(premiere_file_id=message.video.file_id)
    await state.set_state(AdminStates.waiting_premiere_caption)
    await message.answer(
        "✅ Video qabul qilindi.\n\n"
        "Endi kino nomi va tavsifini shu formatda yuboring:\n"
        "<code>Nomi | Tavsif matni</code>\n\n"
        "Masalan: <code>John Wick 4 | Zo'r kino, albatta tomosha qiling!</code>",
        parse_mode="HTML",
        reply_markup=cancel_keyboard()
    )


@dp.message(F.text, AdminStates.waiting_premiere_caption)
async def receive_premiere_caption(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    file_id = data.get("premiere_file_id")
    await state.clear()

    if not file_id:
        await message.answer("❌ Xatolik: video topilmadi. Qaytadan 🎬 Premyera tugmasini bosing.")
        return

    parts = [p.strip() for p in message.text.split("|")]
    name = parts[0] if parts and parts[0] else "Noma'lum"
    izoh = parts[1] if len(parts) > 1 else ""

    full_caption = f"🎬 Yangi premyera!\n\n{name}"
    if izoh:
        full_caption += f"\n\n{izoh}"
    full_caption += f"\n\n📢 Kanal: https://t.me/{CHANNEL_ID[1:]}"
    full_caption += f"\n📢 Kanal: https://t.me/{CHANNEL_ID2[1:]}"

    cursor.execute(
        'INSERT INTO premieres (name, caption, file_id) VALUES (?, ?, ?)',
        (name, full_caption, file_id)
    )
    conn.commit()
    premiere_id = cursor.lastrowid

    status_msg = await message.answer("📤 Premyera barchaga yuborilmoqda, biroz kuting...")
    sent, failed = await broadcast_video_to_all(
        file_id, full_caption,
        progress_chat_id=status_msg.chat.id,
        progress_message_id=status_msg.message_id,
        premiere_id=premiere_id
    )

    await message.answer(
        f"✅ Premyera barcha foydalanuvchilarga yuborildi.\n"
        f"{sent} foydalanuvchiga yuborildi.\n⚠️ {failed} ta xatolik.\n\n"
        f"ℹ️ Bu premyerani keyin ham 🎬 Premyera → 🗑 Premyeralarni tahrirlash orqali o'chirishingiz mumkin."
    )


# ---------- ADMIN: Xabar qoldirish oqimi ----------

@dp.message(AdminStates.waiting_broadcast_message)
async def receive_broadcast_content(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    content = None
    if message.photo:
        content = {'type': 'photo', 'file_id': message.photo[-1].file_id, 'caption': message.caption or ''}
    elif message.video:
        content = {'type': 'video', 'file_id': message.video.file_id, 'caption': message.caption or ''}
    elif message.voice:
        content = {'type': 'voice', 'file_id': message.voice.file_id, 'caption': message.caption or ''}
    elif message.text:
        content = {'type': 'text', 'text': message.text}

    if not content:
        await message.answer("⚠️ Bu turdagi xabarni qo'llab-quvvatlamaymiz. Matn, video yoki ovozli xabar yuboring.")
        return

    await state.update_data(broadcast_content=content)
    await state.set_state(AdminStates.waiting_broadcast_confirm)
    await message.answer(
        "❓ Bu xabarga qo'shimcha izoh qo'shmoqchimisiz?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Ha", callback_data="bcast_yes"),
            InlineKeyboardButton(text="❌ Yo'q", callback_data="bcast_no")
        ]])
    )


@dp.callback_query(F.data == "bcast_no", AdminStates.waiting_broadcast_confirm)
async def bcast_no_callback(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    content = data.get('broadcast_content')
    await state.clear()
    await callback.message.edit_text("📤 Xabar barchaga yuborilmoqda, biroz kuting...")
    sent, failed = await broadcast_content_to_all(
        content,
        progress_chat_id=callback.message.chat.id,
        progress_message_id=callback.message.message_id
    )
    await callback.message.answer(f"✅ Xabar {sent} foydalanuvchiga yuborildi.\n⚠️ {failed} ta xatolik.")


@dp.callback_query(F.data == "bcast_yes", AdminStates.waiting_broadcast_confirm)
async def bcast_yes_callback(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.waiting_broadcast_izoh)
    await callback.message.edit_text("✍️ Izohni yozing:", reply_markup=cancel_keyboard())


@dp.message(F.text, AdminStates.waiting_broadcast_izoh)
async def receive_broadcast_izoh(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    content = data.get('broadcast_content')
    izoh = message.text
    await state.clear()
    status_msg = await message.answer("📤 Xabar barchaga yuborilmoqda, biroz kuting...")
    sent, failed = await broadcast_content_to_all(
        content, extra_caption=izoh,
        progress_chat_id=status_msg.chat.id,
        progress_message_id=status_msg.message_id
    )
    await message.answer(f"✅ Xabar {sent} foydalanuvchiga yuborildi.\n⚠️ {failed} ta xatolik.")


# ---------- ADMIN: Reklama havolasi qo'shish oqimi ----------

@dp.message(F.text, AdminStates.waiting_ad_link)
async def receive_ad_link(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    url = message.text.strip()

    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url

    if not looks_like_valid_url(url):
        await message.answer(
            "⚠️ Bu havola noto'g'ri ko'rinishda (bo'sh joy bor yoki domen nomi yo'q).\n\n"
            "To'g'ri misol: <code>https://t.me/kanalim</code> yoki <code>instagram.com/sahifam</code>\n\n"
            "Qaytadan havolani yuboring:",
            parse_mode="HTML",
            reply_markup=cancel_keyboard()
        )
        return

    await state.update_data(ad_url=url)
    await state.set_state(AdminStates.waiting_ad_duration)
    await message.answer(
        f"🔗 Havola shunday saqlanadi: {url}\n\n"
        "⏳ Qancha muddat amalda tursin?\n\n"
        "Quyidagi birliklardan birini yoki bir nechtasini birga kiriting:\n"
        "<b>yil, oy, kun, soat, daqiqa, soniya</b>\n\n"
        "📌 Masalan:\n"
        "<code>1 yil</code>\n"
        "<code>3 oy</code>\n"
        "<code>2 kun 5 soat</code>\n"
        "<code>30 daqiqa</code>\n"
        "<code>45 soniya</code>\n"
        "<code>1 yil 2 oy 10 kun</code>",
        parse_mode="HTML",
        reply_markup=cancel_keyboard()
    )


@dp.message(F.text, AdminStates.waiting_ad_duration)
async def receive_ad_duration(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    url = data.get('ad_url')
    if not url:
        await state.clear()
        await message.answer("❌ Xatolik: havola topilmadi. Qaytadan 🔗 Havola kiritish tugmasini bosing.")
        return

    seconds = parse_duration_to_seconds(message.text)
    if seconds is None:
        await message.answer(
            "⚠️ Muddatni tushunmadim.\n\n"
            "Quyidagi birliklardan foydalaning: <b>yil, oy, kun, soat, daqiqa, soniya</b>\n"
            "Masalan: <code>1 yil</code>, <code>2 kun 5 soat</code>, <code>45 soniya</code>",
            parse_mode="HTML"
        )
        return

    await state.clear()
    expires_at = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time() + seconds))
    channel_username = extract_tme_channel(url)

    cursor.execute(
        'INSERT INTO ads (url, duration_text, expires_at, is_active, channel_username) VALUES (?, ?, ?, 1, ?)',
        (url, message.text.strip(), expires_at, channel_username or '')
    )
    conn.commit()

    if channel_username:
        SUBSCRIPTION_CACHE.clear()  # yangi majburiy kanal qo'shildi, eski "obuna bor" keshi endi noto'g'ri bo'lishi mumkin
        subscribe_note = (
            f"✅ Bu Telegram kanal havolasi deb tanildi ({channel_username}).\n"
            f"👉 /start bosgan barcha foydalanuvchilar shu kanalga ham OBUNA BO'LISHI SHART bo'ladi, "
            f"muddat tugaguncha.\n\n"
            f"⚠️ ESLATMA: bot shu kanalga ADMIN sifatida qo'shilgan bo'lishi shart, aks holda "
            f"obunani tekshira olmaydi!"
        )
    else:
        subscribe_note = (
            "ℹ️ Bu Telegram kanal havolasi ko'rinishida emas (masalan https://t.me/kanal_nomi emas), "
            "shuning uchun majburiy obuna sifatida TEKSHIRILMAYDI - faqat tugma sifatida ko'rsatiladi."
        )

    await message.answer(
        f"✅ Havola qo'shildi!\n\n"
        f"🔗 {url}\n"
        f"⏳ Muddat: {message.text.strip()}\n"
        f"📅 Tugash vaqti: {expires_at}\n\n"
        f"Bu havola shu muddat davomida yuboradigan barcha xabarlaringiz ostiga avtomatik qo'shiladi, "
        f"HAMDA /start tugmasidan keyingi obuna ro'yxatiga ham qo'shiladi.\n\n"
        f"{subscribe_note}"
    )

    # ---------- ADMIN: Banner (kirish rasmi) sozlash ----------
# Admin botga rasm yuborib, caption'ni "banner:" so'zi bilan boshlasa,
# shu rasm va matn /start bosgan HAR BIR yangi/obuna bo'lmagan foydalanuvchiga
# avtomatik ko'rsatiladi. Qayta yuborilsa - eskisi almashtiriladi.
@dp.message(F.photo)
async def set_welcome_banner(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    caption = message.caption or ""
    if not caption.lower().startswith("banner:"):
        return  # oddiy rasm - e'tibor bermaymiz, boshqa hech narsa qilmaymiz

    text = caption[len("banner:"):].strip()
    file_id = message.photo[-1].file_id

    set_setting('welcome_photo_id', file_id)
    set_setting('welcome_caption', text)

    await message.reply(
        f"✅ Banner saqlandi! Endi /start bosgan barcha foydalanuvchilarga shu ko'rinadi:\n\n"
        f"🖼 Rasm: qabul qilindi\n"
        f"📝 Matn:\n{text}"
    )


# ---------- Kino qo'shish (admin) ----------
# Caption formati -> "Nomi | Yil(ixtiyoriy)"
# Masalan: John Wick 4 | 2023
# Kino kodi endi oddiy tartib raqami: 1, 2, 3... - bot o'zi avtomatik beradi.
@dp.message(F.video)
async def add_movie(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.reply("❌ Siz admin emassiz!")
        return

    caption = message.caption or ""
    parts = [p.strip() for p in caption.split("|")]
    name = parts[0] if len(parts) > 0 and parts[0] else "Noma'lum"
    try:
        year = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        year = 0

    movie_number = get_next_free_movie_number()
    code = str(movie_number)
    order_num = movie_number

    cursor.execute(
        'INSERT INTO movies (name, file_id, code, year, order_num, upload_number) VALUES (?, ?, ?, ?, ?, ?)',
        (name, message.video.file_id, code, year, order_num, movie_number)
    )
    conn.commit()

    await message.reply(
        f"✅ {movie_number}-tartib raqamida kinolar qatoriga qo'shildi va '{code}' kodi ostida qidiriladi.\n\n"
        f"Nomi: {name}\nYil: {year or '(kiritilmagan)'}"
    )


def build_history_keyboard(user_id, page=0):
    per_page = 10
    offset = page * per_page
    cursor.execute('''
        SELECT movie_name, movie_code FROM history
        WHERE user_id = ?
        ORDER BY watched_at DESC
        LIMIT ? OFFSET ?
    ''', (user_id, per_page, offset))
    rows = cursor.fetchall()

    cursor.execute('SELECT COUNT(*) FROM history WHERE user_id = ?', (user_id,))
    total = cursor.fetchone()[0]
    total_pages = (total - 1) // per_page if total else 0

    if not rows:
        text = "📭 Siz hali hech qanday kino ko'rmadingiz."
        keyboard = [[InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back_to_menu")]]
        return text, InlineKeyboardMarkup(inline_keyboard=keyboard)

    text = f"🎬 Kino tarixingiz ({page * per_page + 1}–{page * per_page + len(rows)}):\n\n"
    for i, (name, code) in enumerate(rows, start=page * per_page + 1):
        text += f"{i}. [{code}] {name}\n"

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️", callback_data=f"hist_page:{page - 1}"))
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="➡️", callback_data=f"hist_page:{page + 1}"))

    keyboard = []
    if nav_buttons:
        keyboard.append(nav_buttons)
    keyboard.append([
        InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back_to_menu"),
        InlineKeyboardButton(text="🗑 Tozalash", callback_data="hist_clear_confirm")
    ])

    return text, InlineKeyboardMarkup(inline_keyboard=keyboard)


@dp.message(Command("history"))
async def history_command(message: types.Message):
    text, kb = build_history_keyboard(message.from_user.id)
    if not text:
        await message.answer("📭 Siz hali hech qanday kino ko'rmadingiz.")
        return
    await message.answer(text, reply_markup=kb)


@dp.callback_query(F.data.startswith("hist_page:"))
async def hist_page_callback(callback: types.CallbackQuery):
    page = int(callback.data.split(":")[1])
    text, kb = build_history_keyboard(callback.from_user.id, page)
    if not text:
        await callback.answer("Ma'lumot topilmadi.", show_alert=True)
        return
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "history")
async def history_callback(callback: types.CallbackQuery):
    text, kb = build_history_keyboard(callback.from_user.id)
    if not text:
        await callback.answer("📭 Siz hali hech qanday kino ko'rmadingiz.", show_alert=True)
        return
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "hist_clear_confirm")
async def hist_clear_confirm_callback(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "⚠️ Tarixni butunlay tozalamoqchimisiz? Bu amalni orqaga qaytarib bo'lmaydi.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Ha, tozalash", callback_data="hist_clear_yes"),
            InlineKeyboardButton(text="❌ Yo'q", callback_data="history")
        ]])
    )
    await callback.answer()


@dp.callback_query(F.data == "hist_clear_yes")
async def hist_clear_yes_callback(callback: types.CallbackQuery):
    cursor.execute('DELETE FROM history WHERE user_id = ?', (callback.from_user.id,))
    conn.commit()
    await callback.message.edit_text(
        "🗑 Tarix tozalandi.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back_to_menu")
        ]])
    )
    await callback.answer("Tozalandi ✅")


@dp.message(F.text)
async def search_movie(message: types.Message):
    if not await is_subscribed(message.from_user.id):
        await message.answer("⚠️ Botdan foydalanish uchun kanalga obuna bo'ling!")
        return

    ensure_user(
        message.from_user.id,
        full_name=message.from_user.full_name,
        username=message.from_user.username
    )
    query_text = message.text.strip()

    if query_text.isdigit():
        normalized_code = str(int(query_text))
        cursor.execute("SELECT id, name, file_id FROM movies WHERE code = ?", (normalized_code,))
        result = cursor.fetchone()
        if result:
            await send_movie(message.chat.id, result)
            return

        num = int(query_text)
        cursor.execute("SELECT value FROM counters WHERE key = 'movie_count'")
        row = cursor.fetchone()
        max_num = row[0] if row else 0
        if 1 <= num <= max_num:
            await message.answer(f"❗ {num}-raqam ostida kino mavjud emas.")
            return

    await message.answer(
        "🔍 Bunday kodli kino topilmadi.\n"
        "Kodni tekshirib qayta yuboring yoki quyidagi menyudan foydalaning:",
        reply_markup=main_menu_keyboard()
    )


@dp.errors()
async def global_error_handler(event: types.ErrorEvent):
    logging.error(f"Kutilmagan xato: {event.exception}", exc_info=True)


async def check_inactive_users():
    """Har soatda ishga tushadi: uzoq vaqt faollik ko'rsatmagan foydalanuvchilarni
    'passiv' deb belgilaydi va bu o'tishni status_log jadvaliga yozib qo'yadi."""
    while True:
        try:
            cutoff = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time() - ACTIVE_THRESHOLD_DAYS * 86400))
            cursor.execute(
                "SELECT user_id FROM users WHERE is_active = 1 AND (last_active = '' OR last_active < ?)",
                (cutoff,)
            )
            to_deactivate = [r[0] for r in cursor.fetchall()]
            for uid in to_deactivate:
                cursor.execute("UPDATE users SET is_active = 0 WHERE user_id = ?", (uid,))
                cursor.execute(
                    "INSERT INTO status_log (user_id, old_status, new_status) VALUES (?, 'active', 'passive')",
                    (uid,)
                )
            if to_deactivate:
                conn.commit()
                logging.info(f"{len(to_deactivate)} ta foydalanuvchi passivga o'tkazildi")
        except Exception as e:
            logging.warning(f"Faollikni tekshirishda xato: {e}")
        await asyncio.sleep(3600)


async def check_expired_ads():
    """Har daqiqada ishga tushadi: muddati tugagan reklama havolalarini
    avtomatik faolsizlantiradi va admin'ga xabar beradi."""
    while True:
        try:
            now_str = time.strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute(
                "SELECT id, url FROM ads WHERE is_active = 1 AND expires_at <= ?",
                (now_str,)
            )
            expired = cursor.fetchall()
            for ad_id, url in expired:
                cursor.execute("UPDATE ads SET is_active = 0 WHERE id = ?", (ad_id,))
            if expired:
                conn.commit()
                SUBSCRIPTION_CACHE.clear()  # yangi shart tufayli eskirgan "obuna bor" keshini tozalaymiz
                for ad_id, url in expired:
                    try:
                        await bot.send_message(
                            ADMIN_ID,
                            f"⏰ <b>Havola muddati tugadi</b>\n\n"
                            f"🔗 {url}\n\n"
                            f"Belgilangan muddati tugagani sababli ro'yxatdan va amaldan avtomatik olib tashlandi.",
                            parse_mode="HTML"
                        )
                    except Exception as e:
                        logging.warning(f"Admin'ga havola tugashi haqida xabar berishda xato: {e}")
        except Exception as e:
            logging.warning(f"Havolalar muddatini tekshirishda xato: {e}")
        await asyncio.sleep(60)


async def main():
    asyncio.create_task(check_inactive_users())
    asyncio.create_task(check_expired_ads())
    await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())