import sqlite3

conn = sqlite3.connect('movies.db')
cursor = conn.cursor()

# --- movies jadvaliga yangi ustunlar ---
columns_to_add = [
    ("year", "INTEGER DEFAULT 0"),
    ("search_count", "INTEGER DEFAULT 0"),
    ("order_num", "INTEGER DEFAULT 0"),
    ("code", "TEXT DEFAULT ''"),
    ("upload_number", "INTEGER DEFAULT 0"),  # YANGI: "N-kino" raqami
]

for col_name, col_type in columns_to_add:
    try:
        cursor.execute(f"ALTER TABLE movies ADD COLUMN {col_name} {col_type}")
        print(f"✅ movies.{col_name} ustuni qo'shildi")
    except sqlite3.OperationalError as e:
        print(f"⚠️ movies.{col_name}: {e} (ehtimol allaqachon mavjud)")

# --- yangi jadvallar ---
cursor.execute('''
CREATE TABLE IF NOT EXISTS premieres (
    id INTEGER PRIMARY KEY,
    name TEXT,
    file_id TEXT,
    caption TEXT,
    upload_number INTEGER DEFAULT 0
)
''')
print("✅ premieres jadvali tayyor")

try:
    cursor.execute("ALTER TABLE premieres ADD COLUMN code TEXT DEFAULT ''")
    print("✅ premieres.code ustuni qo'shildi")
except sqlite3.OperationalError as e:
    print(f"⚠️ premieres.code: {e} (ehtimol allaqachon mavjud)")

cursor.execute('''
CREATE TABLE IF NOT EXISTS counters (
    key TEXT PRIMARY KEY,
    value INTEGER DEFAULT 0
)
''')
print("✅ counters jadvali tayyor")

# --- eski kinolar uchun upload_number ni to'ldirish (agar hali 0 bo'lsa) ---
cursor.execute("SELECT id FROM movies WHERE upload_number = 0 OR upload_number IS NULL ORDER BY id ASC")
old_movies = cursor.fetchall()
if old_movies:
    cursor.execute("SELECT value FROM counters WHERE key = 'movie_count'")
    row = cursor.fetchone()
    current = row[0] if row else 0
    for (movie_id,) in old_movies:
        current += 1
        cursor.execute("UPDATE movies SET upload_number = ? WHERE id = ?", (current, movie_id))
        # Agar bu kinoning kodi hali bo'sh bo'lsa, avtomatik "001" formatida beramiz
        cursor.execute("SELECT code FROM movies WHERE id = ?", (movie_id,))
        existing_code = cursor.fetchone()[0]
        if not existing_code:
            cursor.execute("UPDATE movies SET code = ? WHERE id = ?", (str(current).zfill(3), movie_id))
    cursor.execute('''
        INSERT INTO counters (key, value) VALUES ('movie_count', ?)
        ON CONFLICT(key) DO UPDATE SET value = ?
    ''', (current, current))
    print(f"✅ {len(old_movies)} ta eski kinoga upload_number berildi, hisoblagich {current} ga o'rnatildi")

try:
    cursor.execute("ALTER TABLE movies DROP COLUMN genre")
    print("✅ movies.genre ustuni o'chirildi")
except sqlite3.OperationalError as e:
    print(f"⚠️ genre ustunini o'chirishda xato: {e}")

cursor.execute('''
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    movie_id INTEGER,
    movie_name TEXT,
    movie_code TEXT,
    watched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
''')
print("✅ history jadvali tayyor")

conn.commit()
conn.close()
