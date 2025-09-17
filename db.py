# db.py
import sqlite3
from datetime import datetime
from config import DB_FILE

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # otps table
    c.execute("""
    CREATE TABLE IF NOT EXISTS otps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        number TEXT,
        otp TEXT,
        full_msg TEXT,
        service TEXT,
        country TEXT,
        fetched_at TEXT,
        sent INTEGER DEFAULT 0,
        UNIQUE(number, otp)
    )
    """)
    # errors table
    c.execute("""
    CREATE TABLE IF NOT EXISTS errors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        err TEXT,
        created_at TEXT
    )
    """)
    # status row
    c.execute("""
    CREATE TABLE IF NOT EXISTS status (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        value TEXT
    )
    """)
    c.execute("INSERT OR IGNORE INTO status (id, value) VALUES (1, 'offline')")
    conn.commit()
    conn.close()

def save_otp(number, otp, full_msg, service, country):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute(
            "INSERT OR IGNORE INTO otps (number, otp, full_msg, service, country, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
            (number, otp, full_msg, service, country, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()
    finally:
        conn.close()

def otp_exists(number, otp):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT 1 FROM otps WHERE number=? AND otp=? LIMIT 1", (number, otp))
    r = c.fetchone()
    conn.close()
    return r is not None

def mark_sent(number, otp):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE otps SET sent=1 WHERE number=? AND otp=?", (number, otp))
    conn.commit()
    conn.close()

def clear_otps():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM otps")
    conn.commit()
    conn.close()

def count_otps():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM otps")
    n = c.fetchone()[0]
    conn.close()
    return n

def save_error(text):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO errors (err, created_at) VALUES (?, ?)", (text, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

def get_errors(limit=10):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT err, created_at FROM errors ORDER BY id DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return rows

def set_status(val):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE status SET value=? WHERE id=1", (val,))
    conn.commit()
    conn.close()

def get_status():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT value FROM status WHERE id=1")
    r = c.fetchone()
    conn.close()
    return r[0] if r else "offline"
