import asyncio
import re
from datetime import datetime, UTC
import requests
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
import html

import config
import db

# init db
db.init_db()

# aiogram bot
bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# requests session
session = requests.Session()

# worker control
_worker_task = None
_worker_running = False

# =========================================================
# Login and token retrieval logic
# =========================================================
def login_and_fetch_token():
    print("Attempting to login and fetch new session/token...")
    try:
        r = session.get(config.LOGIN_URL, timeout=15)
        r.raise_for_status()

        soup = BeautifulSoup(r.text, 'html.parser')
        token_input = soup.find('input', {'name': '_token'})
        
        if not token_input or not token_input.get('value'):
            db.save_error("Login failed: Could not find initial CSRF token.")
            print("Login failed: Could not find initial CSRF token.")
            return False

        initial_csrf_token = token_input.get('value')
        
        login_data = {
            '_token': initial_csrf_token,
            'email': config.LOGIN_EMAIL,
            'password': config.LOGIN_PASSWORD,
            'g-recaptcha-response': '',
            'submit': 'login'
        }
        
        session.headers.update(config.HEADERS)

        r_login = session.post(config.LOGIN_URL, data=login_data, timeout=15, allow_redirects=False)

        if r_login.status_code == 302 and 'location' in r_login.headers and 'portal' in r_login.headers['location']:
            print("Login successful! Now fetching new token from portal page.")
            
            r_portal = session.get(r_login.headers['location'], timeout=15)
            r_portal.raise_for_status()
            
            soup_portal = BeautifulSoup(r_portal.text, 'html.parser')
            new_token_input = soup_portal.find('input', {'name': '_token'})
            
            if not new_token_input or not new_token_input.get('value'):
                db.save_error("Login succeeded but could not get new CSRF token.")
                print("Login succeeded but could not get new CSRF token.")
                return False

            config.CSRF_TOKEN = new_token_input.get('value')
            print("Successfully updated session cookie and CSRF token.")
            return True
        else:
            db.save_error(f"POST login request failed. Status code: {r_login.status_code}. Response: {r_login.text}")
            print(f"POST login request failed. Status code: {r_login.status_code}")
            return False

    except requests.exceptions.RequestException as e:
        db.save_error(f"Login process failed with error: {e}")
        print(f"Login process failed with error: {e}")
        return False

# helpers
def mask_number(num: str) -> str:
    s = num.strip()
    if len(s) <= (config.MASK_PREFIX_LEN + config.MASK_SUFFIX_LEN):
        return s
    return s[:config.MASK_PREFIX_LEN] + "****" + s[-config.MASK_SUFFIX_LEN:]

def detect_service(text: str) -> str:
    t = (text or "").lower()
    # Use `sorted` with `len` to ensure longer words are tried first
    for k in sorted(config.SERVICES.keys(), key=len, reverse=True):
        if k in t:
            return config.SERVICES[k]
    return "Service"

def detect_country(number: str, extra_text: str = "") -> str:
    s = number.lstrip("+")
    for prefix, flagname in config.COUNTRY_FLAGS.items():
        if s.startswith(prefix):
            return flagname
    txt = (extra_text or "").upper()
    if "PERU" in txt:
        return config.COUNTRY_FLAGS.get("51", "🇵🇪 Peru")
    if "BANGLADESH" in txt or "+880" in number:
        return config.COUNTRY_FLAGS.get("880", "🇧🇩 Bangladesh")
    return "🌍 Unknown"

def extract_otps(text: str):
    """
    This function extracts OTPs from a given text.
    It has been updated to handle various OTP formats, including those
    like WhatsApp business codes.
    """
    regexes = [
        # This regex detects WhatsApp OTPs that have text or 6 digits.
        re.compile(r"wa(?:ts|tt)?app.*?code.*?:?\s*(\d{3,6}|[a-zA-Z0-9]{6,})", re.IGNORECASE),
        
        # This detects numbers separated by - or --.
        re.compile(r"(\d{3}[-–]\d{3})"), 
        
        # This detects 6-digit OTPs that are not separated.
        re.compile(r"\b(\d{6})\b"), 
        
        # This detects numbers preceded by 'code' or 'is'
        re.compile(r"(?:code|is)\s*(\d{3}[-–]?\d{3}|\d{6})", re.IGNORECASE), 
        
        # This is the fallback, it will detect any number between 4-8 digits.
        re.compile(r"\b(\d{4,8})\b"),  
    ]
    
    found_otps = []
    
    # Iterate through each regex pattern to find matches
    for r in regexes:
        matches = r.findall(text)
        if matches:
            found_otps.extend(matches)
    
    # Remove duplicates and return the found OTPs
    unique_otps = list(dict.fromkeys(found_otps))
    
    return unique_otps


# parsing helpers
def parse_ranges(html_text: str):
    soup = BeautifulSoup(html_text, "html.parser")
    ranges = []
    for opt in soup.select("select#range option"):
        val = opt.get_text(strip=True)
        if val:
            ranges.append(val)
    if not ranges:
        for m in re.finditer(r"([A-Z][A-Z\s]{2,}\s+\d{2,6})", html_text):
            ranges.append(m.group(1).strip())
    return list(dict.fromkeys(ranges))

def parse_numbers(html_text: str):
    soup = BeautifulSoup(html_text, "html.parser")
    nums = []
    for tr in soup.select("table tr"):
        tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        for txt in tds:
            m = re.search(r"(\+?\d{6,15})", txt)
            if m:
                nums.append(m.group(1))
                break
    if not nums:
        for m in re.finditer(r"(\+?\d{6,15})", html_text):
            nums.append(m.group(1))
    return list(dict.fromkeys(nums))

def parse_messages_with_timestamps(html_text: str):
    soup = BeautifulSoup(html_text, "html.parser")
    msgs = []
    for tr in soup.select("table tbody tr"):
        tds = tr.find_all("td")
        if len(tds) >= 3:
            timestamp_str = tds[0].get_text(strip=True)
            full_msg = tds[2].get_text(strip=True)
            if timestamp_str and full_msg:
                try:
                    fetched_at = timestamp_str
                except ValueError:
                    fetched_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
                msgs.append({"message": full_msg, "fetched_at": fetched_at})
    if not msgs:
        for m in re.finditer(r"([A-Za-z0-9\W\s]{10,})", html_text):
            t = m.group(1).strip()
            if re.search(r"\d{4,8}", t):
                msgs.append({"message": t, "fetched_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")})
    return msgs

def fetch_once():
    entries = []
    try:
        r = session.post(config.GET_SMS_URL, data={"_token": config.CSRF_TOKEN, "from": datetime.now(UTC).date().isoformat(), "to": datetime.now(UTC).date().isoformat()}, timeout=20)
        
        if r.status_code == 419 or r.status_code == 403 or r.status_code == 401:
            db.save_error(f"GET_SMS status {r.status_code} - session/token expired. Attempting to relogin...")
            if login_and_fetch_token():
                print("Relogin successful. Retrying.")
                r = session.post(config.GET_SMS_URL, data={"_token": config.CSRF_TOKEN, "from": datetime.now(UTC).date().isoformat(), "to": datetime.now(UTC).date().isoformat()}, timeout=20)
            else:
                db.save_error("Relogin failed. Skipping this cycle.")
                return entries

        if r.status_code != 200:
            db.save_error(f"GET_SMS status {r.status_code}")
            return entries

        ranges = parse_ranges(r.text)
        if not ranges:
            try:
                j = r.json()
                if isinstance(j, list):
                    ranges = [str(x) for x in j]
            except Exception:
                pass
        
        if not ranges:
            ranges = [""]

        for rng in ranges:
            r2 = session.post(config.GET_NUMBER_URL, data={"_token": config.CSRF_TOKEN, "start": datetime.now(UTC).date().isoformat(), "end": datetime.now(UTC).date().isoformat(), "range": rng}, timeout=20)
            if r2.status_code != 200:
                db.save_error(f"GET_NUMBER failed for range={rng} status={r2.status_code}")
                continue
            numbers = parse_numbers(r2.text)
            if not numbers:
                try:
                    j2 = r2.json()
                    if isinstance(j2, list):
                        for item in j2:
                            if isinstance(item, dict):
                                num = item.get("Number") or item.get("number") or item.get("msisdn")
                                if num:
                                    numbers.append(str(num))
                except Exception:
                    pass

            for number in numbers:
                r3 = session.post(config.GET_OTP_URL, data={"_token": config.CSRF_TOKEN, "start": datetime.now(UTC).date().isoformat(), "Number": number, "Range": rng}, timeout=20)
                if r3.status_code != 200:
                    db.save_error(f"GET_OTP failed number={number} range={rng} status={r3.status_code}")
                    continue
                msgs_and_times = parse_messages_with_timestamps(r3.text)
                if not msgs_and_times:
                    try:
                        j3 = r3.json()
                        if isinstance(j3, list):
                            for it in j3:
                                if isinstance(it, dict):
                                    text = it.get("sms") or it.get("message") or it.get("full")
                                    if text:
                                        msgs_and_times.append({"message": text, "fetched_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")})
                    except Exception:
                        pass
                for item in msgs_and_times:
                    m = item['message']
                    fetched_at = item['fetched_at']
                    otps = extract_otps(m)
                    if not otps:
                        continue
                    for otp in otps:
                        service = detect_service(m)
                        country = detect_country(number, rng)
                        entries.append({
                            "number": number,
                            "otp": otp,
                            "full_msg": m,
                            "service": service,
                            "country": country,
                            "range": rng,
                            "fetched_at": fetched_at
                        })
    except Exception as e:
        db.save_error(f"fetch_once exception: {e}")
    return entries

# Message forwarding function
async def forward_entry(e):
    num_display = mask_number(e["number"])
    
    # Use BeautifulSoup to extract text within tag
    # Find the <p> tag and extract the text inside it
    full_msg_soup = BeautifulSoup(e.get('full_msg'), 'html.parser')
    
    # Try to find the <p> tag where the actual message is
    message_content_tag = full_msg_soup.find('p', {'class': 'mb-0'})
    if message_content_tag:
        # If tag is found, extract just the text
        cleaned_full_msg = message_content_tag.get_text(strip=True)
    else:
        # If not found, try to find any text
        cleaned_full_msg = full_msg_soup.get_text(strip=True)
    
    # If text extraction fails, return the original HTML
    if not cleaned_full_msg:
        cleaned_full_msg = e.get('full_msg')

    escaped_full_msg = html.escape(cleaned_full_msg)

    # New message format with quote + code style
    text = (
        f"<b>🔔 NEW OTP DETECTED</b> 🆕\n\n"
        f"⏰ <b>Time</b>: <blockquote><code>{e.get('fetched_at')}</code></blockquote>\n"
        f"🌍 <b>Country</b>: <blockquote><code>{e.get('country')}</code></blockquote>\n"
        f"⚙️ <b>Service</b>: <blockquote><code>{e.get('service')}</code></blockquote>\n"
        f"☎️ <b>Number</b>: <blockquote><code>{num_display}</code></blockquote>\n"
        f"🔑 <b>OTP</b>: <blockquote><code>{e.get('otp')}</code></blockquote>\n\n"
        f"📩 <b>Full Message</b>:\n<blockquote><code>{escaped_full_msg}</code></blockquote>\n\n"
        "Powered by Aꪝ 0x┋TEAM ✇"
    )
    
    # New buttons as requested
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="Channel", url=config.CHANNEL_LINK),
         types.InlineKeyboardButton(text="Developer", url="https://t.me/BashOnChain")]
    ])
    
    try:
        await bot.send_message(config.GROUP_ID, text, reply_markup=kb)
    except Exception as exc:
        db.save_error(f"Failed to forward message to group: {exc}")
        try:
            await bot.send_message(config.ADMIN_ID, f"Failed to forward message: {exc}")
        except Exception:
            pass

# worker
async def worker():
    db.set_status("online")
    await bot.send_message(config.ADMIN_ID, "✅ Worker started.")
    global _worker_running
    _worker_running = True
    while _worker_running:
        entries = fetch_once()
        for e in entries:
            if not db.otp_exists(e["number"], e["otp"]):
                db.save_otp(e["number"], e["otp"], e["full_msg"], e["service"], e["country"])
                await forward_entry(e)
        await asyncio.sleep(config.FETCH_INTERVAL)
    db.set_status("offline")
    await bot.send_message(config.ADMIN_ID, "🛑 Worker stopped.")

def stop_worker_task():
    global _worker_running, _worker_task
    if not _worker_running:
        return
    _worker_running = False
    if _worker_task and not _worker_task.done():
        _worker_task.cancel()

# commands
@dp.message(F.text == "/start")
async def cmd_start(m: types.Message):
    if m.from_user.id != config.ADMIN_ID:
        await m.answer("⛔ You don't have permission.")
        return
    st = db.get_status()
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="▶️ Start", callback_data="start_worker"),
         types.InlineKeyboardButton(text="⏸ Stop", callback_data="stop_worker")],
        [types.InlineKeyboardButton(text="🧹 Clear DB", callback_data="clear_db"),
         types.InlineKeyboardButton(text="❗ Errors", callback_data="show_errors")],
        [types.InlineKeyboardButton(text="🔄 Relogin", callback_data="relogin")]
    ])
    await m.answer(f"⚙️ <b>OTP Receiver</b>\nStatus: <b>{st}</b>\nStored OTPs: <b>{db.count_otps()}</b>", reply_markup=kb)

@dp.callback_query()
async def cb(q: types.CallbackQuery):
    if q.from_user.id != config.ADMIN_ID:
        await q.answer("⛔ No permission", show_alert=True)
        return
    if q.data == "start_worker":
        global _worker_task
        if _worker_task is None or _worker_task.done():
            _worker_task = asyncio.create_task(worker())
            await q.message.answer("✅ Worker started.")
        else:
            await q.message.answer("ℹ️ Worker is already running.")
        await q.answer()
    elif q.data == "stop_worker":
        stop_worker_task()
        await q.message.answer("🛑 Worker stopping...")
        await q.answer()
    elif q.data == "clear_db":
        db.clear_otps()
        await q.message.answer("🗑 OTP DB cleared.")
        await q.answer()
    elif q.data == "show_errors":
        rows = db.get_errors(10)
        if not rows:
            await q.message.answer("✅ No errors logged.")
        else:
            text = "\n\n".join([f"{r[1]} — {r[0]}" for r in rows])
            await q.message.answer(f"<b>Recent Errors</b>:\n\n{text}")
        await q.answer()
    elif q.data == "relogin":
        if login_and_fetch_token():
            await q.message.answer("✅ Manual relogin successful!")
        else:
            await q.message.answer("❌ Manual relogin failed! Check logs.")
        await q.answer()

@dp.message(F.text == "/on")
async def cmd_on(m: types.Message):
    if m.from_user.id != config.ADMIN_ID:
        await m.answer("⛔ You don't have permission.")
        return
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(worker())
        await m.answer("✅ Worker started.")
    else:
        await m.answer("ℹ️ Worker is already running.")

@dp.message(F.text == "/off")
async def cmd_off(m: types.Message):
    if m.from_user.id != config.ADMIN_ID:
        await m.answer("⛔ You don't have permission.")
        return
    stop_worker_task()
    await m.answer("🛑 Worker stopping...")

@dp.message(F.text == "/status")
async def cmd_status(m: types.Message):
    if m.from_user.id != config.ADMIN_ID:
        await m.answer("⛔ You don't have permission.")
        return
    await m.answer(f"📡 Status: <b>{db.get_status()}</b>\n📥 Stored OTPs: <b>{db.count_otps()}</b>")

@dp.message(F.text == "/check")
async def cmd_check(m: types.Message):
    if m.from_user.id != config.ADMIN_ID:
        await m.answer("⛔ You don't have permission.")
        return
    await m.answer(f"Stored OTPs: <b>{db.count_otps()}</b>")

@dp.message(F.text == "/clear")
async def cmd_clear(m: types.Message):
    if m.from_user.id != config.ADMIN_ID:
        await m.answer("⛔ You don't have permission.")
        return
    db.clear_otps()
    await m.answer("🗑 OTP DB cleared.")

@dp.message(F.text == "/errors")
async def cmd_errors(m: types.Message):
    if m.from_user.id != config.ADMIN_ID:
        await m.answer("⛔ You don't have permission.")
        return
    rows = db.get_errors(20)
    if not rows:
        await m.answer("✅ No errors logged.")
    else:
        text = "\n\n".join([f"{r[1]} — {r[0]}" for r in rows])
        await m.answer(f"<b>Recent Errors</b>:\n\n{text}")

async def on_startup():
    print("Attempting to login and fetch new session/token at startup.")
    if login_and_fetch_token():
        print("Initial login successful.")
    else:
        print("Initial login failed. Bot may not function properly.")
        db.save_error("Initial login failed. Bot may not function properly.")

    if db.get_status() == "online":
        global _worker_task
        _worker_task = asyncio.create_task(worker())

if __name__ == "__main__":
    try:
        import logging
        logging.basicConfig(level=logging.INFO)
        dp.startup.register(on_startup)
        dp.run_polling(bot)
    except KeyboardInterrupt:
        print("Exiting...")
