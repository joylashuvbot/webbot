import os
import json
import hmac
import hashlib
import logging
import re
import secrets
from typing import Optional, Dict, Any, List
from urllib.parse import parse_qsl
import asyncio
import asyncpg
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart
from dotenv import load_dotenv
from urllib.parse import unquote

load_dotenv()

# ------------------- Konfiguratsiya -------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = set(int(i.strip()) for i in os.getenv("ADMIN_ID", "").split(",") if i.strip())
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:CCvagWGwtueLwCUyfaEbkPQvSDOHoygR@caboose.proxy.rlwy.net:48751/railway")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
BOT_WEBHOOK_HOST = os.getenv("BOT_WEBHOOK_HOST", "0.0.0.0")
BOT_WEBHOOK_PORT = int(os.getenv("BOT_WEBHOOK_PORT", "8080"))
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ------------------- Admin Token Storage -------------------
admin_tokens = set()

def generate_admin_token() -> str:
    return secrets.token_urlsafe(32)

async def check_admin_auth(data: dict) -> Optional[dict]:
    """
    Birlashtirilgan autentifikatsiya:
    1. Admin token orqali (brauzer rejimi)
    2. Telegram init_data orqali (WebApp rejimi)
    3. Brauzer rejimi (demo_mode_ prefix bilan)
    """
    init_data = data.get('init_data', '')
    admin_token = data.get('admin_token', '')

    if admin_token and admin_token in admin_tokens:
        return {
            "id": 0,
            "first_name": "Admin",
            "last_name": "",
            "username": "admin",
            "language_code": "uz",
            "is_admin": True
        }

    if init_data:
        # Telegram WebApp auth
        if not init_data.startswith('demo_mode_'):
            user = validate_telegram_data(init_data, BOT_TOKEN)
            if user:
                user_id = user.get('id')
                user['is_admin'] = user_id in ADMIN_IDS
                return user
        else:
            # Brauzer rejimi - demo_mode_ prefix bilan
            return {
                "id": 0,
                "first_name": "Browser Admin",
                "last_name": "",
                "username": "admin",
                "language_code": "uz",
                "is_admin": True
            }

    return None
# ------------------- Database -------------------
db_pool: Optional[asyncpg.Pool] = None

async def init_db():
    """Bazaga ulanish va jadvallarni yaratish"""
    global db_pool
    try:
        logger.info(f"🔗 Connecting to DB...")
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
        async with db_pool.acquire() as conn:
            # Places jadvali
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS places (id SERIAL PRIMARY KEY)
            """)
            
            # Users jadvali (TIL SAQLASH UCHUN)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    language TEXT NOT NULL DEFAULT 'uz',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            columns = await conn.fetch("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = 'places' AND table_schema = 'public'
            """)
            existing_cols = {row['column_name'] for row in columns}
            logger.info(f"📋 Places ustunlari: {existing_cols}")

            required_columns = {
                'name': "TEXT NOT NULL DEFAULT ''",
                'lat': 'DOUBLE PRECISION',
                'lng': 'DOUBLE PRECISION',
                'text_user': "TEXT NOT NULL DEFAULT ''",
                'text_channel': "TEXT NOT NULL DEFAULT ''",
                'category': "TEXT NOT NULL DEFAULT 'food'",
                'phone': "TEXT NOT NULL DEFAULT ''",
                'telegram': "TEXT NOT NULL DEFAULT ''",
                'menu_url': "TEXT NOT NULL DEFAULT ''",
                'address_name': "TEXT NOT NULL DEFAULT ''",
                'address_link': "TEXT NOT NULL DEFAULT ''",
                'details': "TEXT NOT NULL DEFAULT ''",
            }
            for col_name, col_type in required_columns.items():
                if col_name not in existing_cols:
                    await conn.execute(f"ALTER TABLE places ADD COLUMN {col_name} {col_type}")

            await conn.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'idx_places_name')
                    THEN CREATE INDEX idx_places_name ON places(name); END IF;
                    IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'idx_places_coords')
                    THEN CREATE INDEX idx_places_coords ON places(lat, lng); END IF;
                END $$;
            """)

            count = await conn.fetchval("SELECT COUNT(*) FROM places")
            logger.info(f"✅ DB OK. Places: {count} ta.")
            if count == 0:
                await load_initial_places(conn)

    except Exception as e:
        logger.error(f"Database init error: {e}")
        raise

async def load_initial_places(conn):
    for place in INITIAL_PLACES:
        # Parse text_user to extract individual fields
        place = parse_place_text(dict(place))

        lat, lng = place.get("lat"), place.get("lng")

        # First try to extract coords from address_link
        if lat is None or lng is None:
            addr_link = place.get("address_link", "")
            if addr_link:
                coords = extract_coords_from_url(addr_link)
                if coords:
                    lat, lng = coords
                    place["lat"] = lat
                    place["lng"] = lng

            # If still no coords, geocode the address name
            if lat is None or lng is None:
                addr_name = place.get("address_name", "")
                if addr_name:
                    coords = await geocode_address(addr_name)
                    if coords:
                        lat, lng = coords
                        place["lat"] = lat
                        place["lng"] = lng
                await asyncio.sleep(1.1)

        # Build text_user and text_channel from individual fields
        text_user = build_text_user(place)
        text_channel = build_text_channel(place)

        await conn.execute("""
            INSERT INTO places (name, lat, lng, text_user, text_channel, category,
                              phone, telegram, menu_url, address_name, address_link, details)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        """, place["name"], lat, lng, text_user, text_channel,
             place.get("category", "food"),
             place.get("phone", ""),
             place.get("telegram", ""),
             place.get("menu_url", ""),
             place.get("address_name", ""),
             place.get("address_link", ""),
             place.get("details", ""))

async def close_db():
    if db_pool:
        await db_pool.close()

# ------------------- User Language Helpers -------------------
async def get_user_language(user_id: int) -> str:
    if db_pool is None:
        return 'uz'
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT language FROM users WHERE user_id = $1", user_id)
            return row['language'] if row else 'uz'
    except Exception as e:
        logger.error(f"Get lang error: {e}")
        return 'uz'

async def set_user_language(user_id: int, language: str):
    if db_pool is None:
        return
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (user_id, language, updated_at)
                VALUES ($1, $2, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id) 
                DO UPDATE SET language = $2, updated_at = CURRENT_TIMESTAMP
            """, user_id, language)
    except Exception as e:
        logger.error(f"Set lang error: {e}")

# ------------------- Initial Places -------------------
# ------------------- Place Parsing & Building -------------------

def extract_address_name(text: str) -> str:
    """📍 qatoridan manzil nomini ajratib olish (linkisiz)"""
    addr_match = re.search(r'📍\s*([^\n(]+)', text)
    if addr_match:
        return addr_match.group(1).strip()
    return ''

def extract_address_link(text: str) -> str:
    """📍 qatoridagi Google Maps linkini ajratib olish"""
    link_match = re.search(r'https?://[^\s)]+', text)
    if link_match:
        return link_match.group(0).strip()
    return ''

def extract_phones(text: str) -> str:
    """📞 qator(lar)idan telefon raqamlarini ajratib olish"""
    phones = []
    for line in text.split('\n'):
        if line.strip().startswith('📞'):
            phone_match = re.search(r'[+\d][\d\s()-]+', line.strip()) 
            if phone_match:
                phones.append(phone_match.group(0).strip())
    return ', '.join(phones)

def extract_telegram(text: str) -> str:
    """📱 qator(lar)idan telegram username ni ajratib olish"""
    tgs = []
    for line in text.split('\n'):
        if line.strip().startswith('📱') and '@' in line:
            # Handle both "@username" and "https://t.me/..." formats
            usernames = re.findall(r'@([A-Za-z0-9_]+)', line.strip())
            tgs.extend([f'@{u}' for u in usernames])
    return ', '.join(tgs)

def extract_menu_url(text: str) -> str:
    """📋 qatoridan menu URL ni ajratib olish"""
    for line in text.split('\n'):
        if line.strip().startswith('📋'):
            url_match = re.search(r'https?://[^\s)]+', line.strip())
            if url_match:
                return url_match.group(0).strip()
    return ''

def extract_details(text: str) -> str:
    """Faqat tafsilot qismini ajratib olish (🏬, 🧾, 🚘, ⏰, va h.k.)"""
    detail_lines = []
    for line in text.split('\n'):
        line = line.strip()
        if not line:
            continue
        # Only keep lines that start with specific emojis (details)
        if line.startswith(('🏠', '🏬', '🚛', '🏪', '🧾', '🍜', '🚘', '🚚', '⏰', '❌', '❗️', '🅿️')):
            detail_lines.append(line)
    return '\n'.join(detail_lines)

def build_text_user(place: dict) -> str:
    """Alohida maydonlardan text_user yaratish"""
    lines = []
    name = place.get('name', '')
    addr_name = place.get('address_name', '')
    addr_link = place.get('address_link', '')
    details = place.get('details', '')
    phone = place.get('phone', '')
    telegram = place.get('telegram', '')
    menu_url = place.get('menu_url', '')
    lat = place.get('lat')
    lng = place.get('lng')

    # Header
    lines.append(f'🍽️ {name}')

    # Address with link or coordinates
    if addr_name:
        if addr_link:
            lines.append(f'📍 {addr_name} ({addr_link})')
        elif lat and lng:
            lines.append(f'📍 {addr_name} (https://www.google.com/maps?q={lat},{lng})')
        else:
            lines.append(f'📍 {addr_name}')
    elif lat and lng:
        lines.append(f'📍 https://www.google.com/maps?q={lat},{lng}')

    # Details (only the 4 summary lines)
    if details:
        lines.append(details)

    # Menu
    if menu_url:
        lines.append(f'📋 Меню ({menu_url}) (смотреть комментарии)')

    # Phone
    if phone:
        for p in phone.split(','):
            p = p.strip()
            if p:
                lines.append(f'📞 Телефон: {p}' if p == phone.split(',')[0].strip() else f'📞 {p}')

    # Telegram
    if telegram:
        for t in telegram.split(','):
            t = t.strip()
            if t:
                lines.append(f'📱 Telegram: {t}')

    return '\n'.join(lines)

def build_text_channel(place: dict) -> str:
    """Alohida maydonlardan text_channel (HTML) yaratish"""
    lines = []
    name = place.get('name', '')
    addr_name = place.get('address_name', '')
    addr_link = place.get('address_link', '')
    details = place.get('details', '')
    phone = place.get('phone', '')
    telegram = place.get('telegram', '')
    menu_url = place.get('menu_url', '')
    lat = place.get('lat')
    lng = place.get('lng')

    # Header with bold
    lines.append(f'🍽️ <b>{name}</b>')

    # Address with link or coordinates
    if addr_name:
        if addr_link:
            lines.append(f'📍 <a href="{addr_link}">{addr_name}</a>')
        elif lat and lng:
            lines.append(f'📍 <a href="https://www.google.com/maps?q={lat},{lng}">{addr_name}</a>')
        else:
            lines.append(f'📍 {addr_name}')
    elif lat and lng:
        lines.append(f'📍 <a href="https://www.google.com/maps?q={lat},{lng}">Xaritada ko\'rish</a>')

    # Details
    if details:
        lines.append(details)

    # Menu
    if menu_url:
        lines.append(f'📋 <a href="{menu_url}">Меню</a> (смотреть комментарии)')

    # Phone
    if phone:
        for p in phone.split(','):
            p = p.strip()
            if p:
                lines.append(f'📞 Телефон: {p}' if p == phone.split(',')[0].strip() else f'📞 {p}')

    # Telegram
    if telegram:
        for t in telegram.split(','):
            t = t.strip()
            if t:
                lines.append(f'📱 Telegram: {t}')

    return '\n'.join(lines)

def parse_place_text(place: dict) -> dict:
    """Mavjud text_user dan barcha maydonlarni ajratib olish"""
    text = place.get('text_user', '')
    if not text:
        return place

    # Extract address name and link from 📍 line
    addr_name = extract_address_name(text)
    addr_link = extract_address_link(text)

    # Extract phone, telegram, menu
    phone = extract_phones(text)
    telegram = extract_telegram(text)
    menu_url = extract_menu_url(text)

    # Extract details (only summary lines)
    details = extract_details(text)

    # Update place dict
    if addr_name:
        place['address_name'] = addr_name
    if addr_link:
        place['address_link'] = addr_link
    if phone:
        place['phone'] = phone
    if telegram:
        place['telegram'] = telegram
    if menu_url:
        place['menu_url'] = menu_url
    if details:
        place['details'] = details

    return place

INITIAL_PLACES = [
    {
        "name": "ARZU CHICAGO",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ ARZU CHICAGO\n"
            "📍 Mt.Prospect, IL (https://maps.app.goo.gl/RqKgmFbR7BkNcJm29?g_st=ic)\n"
            "🏬 Ресторан\n"
            "🍜 Заказы принимаются с 11:00 до 22:00\n"
            "🚚 Доставка есть\n"
            "⏰ Время работы: 11:00 - 23:00\n"
            "📋 Меню (https://t.me/myhalalmenu/111) (смотреть комментарии)\n"
            "📞 Телефон: +13127744771\n"
            "📱 Telegram: @arzu_chicago"
        ),
        "text_channel": (
            "🍽️ <b>ARZU CHICAGO</b>\n"
            "📍 Mt.Prospect, IL\n"
            "🏬 Ресторан\n"
            "🍜 Заказы принимаются с 11:00 до 22:00\n"
            "🚚 Доставка есть\n"
            "⏰ Время работы: 11:00 - 23:00\n"
            "📋 <a href=\"https://t.me/myhalalmenu/111\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +13127744771\n"
            "📱 Telegram: @arzu_chicago"
        )
    },
    {
        "name": "SHIRIN FOOD",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ SHIRIN FOOD\n"
            "📍 Tacoma, WA (https://maps.app.goo.gl/Tuz4fvHDCLExdtDa9)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 2-3 часа до доставки\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 24/7\n"
            "📋 Меню (https://t.me/myhalalmenu/112) (смотреть комментарии)\n"
            "📞 Телефон: +19296770708\n"
            "📱 Telegram: @SHIRIN_N1FOOD"
        ),
        "text_channel": (
            "🍽️ <b>SHIRIN FOOD</b>\n"
            "📍 Tacoma, WA\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 2-3 часа до доставки\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 24/7\n"
            "📋 <a href=\"https://t.me/myhalalmenu/112\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +19296770708\n"
            "📱 Telegram: @SHIRIN_N1FOOD"
        )
    },
    {
        "name": "HALAL FOOD PORTLAND",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ HALAL FOOD PORTLAND\n"
            "📍 Portland, OR (https://maps.app.goo.gl/4njBXGWtxnE1aYMfA)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Все продукты готовы, можно перед заказом купить\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 24/7\n"
            "📋 Меню (https://t.me/myhalalmenu/113) (смотреть комментарии)\n"
            "📞 Телефон: +15038889757\n"
            "📱 Telegram: @halal_Food_portland"
        ),
        "text_channel": (
            "🍽️ <b>HALAL FOOD PORTLAND</b>\n"
            "📍 Portland, OR\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Все продукты готовы, можно перед заказом купить\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 24/7\n"
            "📋 <a href=\"https://t.me/myhalalmenu/113\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +15038889757\n"
            "📱 Telegram: @halal_Food_portland"
        )
    },
    {
        "name": "BOISE HALAL FOOD",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ BOISE HALAL FOOD\n"
            "📍 Boise, ID (https://maps.app.goo.gl/g9rCnH9AhXCKkqdNA)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 4 часа до доставки\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 24/7\n"
            "📋 Меню (https://t.me/myhalalmenu/114) (смотреть комментарии)\n"
            "📞 Телефон: +19868884949\n"
            "📱 Telegram: @Sarmadiuzz"
        ),
        "text_channel": (
            "🍽️ <b>BOISE HALAL FOOD</b>\n"
            "📍 Boise, ID\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 4 часа до доставки\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 24/7\n"
            "📋 <a href=\"https://t.me/myhalalmenu/114\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +19868884949\n"
            "📱 Telegram: @Sarmadiuzz"
        )
    },
    {
        "name": "CAFE PLOV AND BORSCH",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ CAFE PLOV AND BORSCH\n"
            "📍 Salt Lake city, UT (https://maps.app.goo.gl/txZHP6PdNvzVXZ7Y8)\n"
            "🏠 Ресторан\n"
            "🧾 Все продукты готовы, можно перед заказом купить\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 08:00-10:00\n"
            "📋 Меню (https://t.me/myhalalmenu/115) (смотреть комментарии)\n"
            "📞 Телефон: +13854004300\n"
            "📱 Telegram: @Cafe_Plov_and_Borsch"
        ),
        "text_channel": (
            "🍽️ <b>CAFE PLOV AND BORSCH</b>\n"
            "📍 Salt Lake city, UT\n"
            "🏠 Ресторан\n"
            "🧾 Все продукты готовы, можно перед заказом купить\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 08:00-10:00\n"
            "📋 <a href=\"https://t.me/myhalalmenu/115\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +13854004300\n"
            "📱 Telegram: @Cafe_Plov_and_Borsch"
        )
    },
    {
        "name": "CHAYXANA AMIR",
        "lat": 38.61700400,
        "lng": -121.53797100,
        "text_user": (
            "🍽️ CHAYXANA AMIR\n"
            "📍 Sacramento, CA (https://www.google.com/maps?q=38.61700400,-121.53797100)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 2–3 часа до доставки\n"
            "⏰ Время работы: 24/7\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/116) (смотреть комментарии)\n"
            "📞 +19167506977\n"
            "📱 Telegram: @N1_Ibragim"
        ),
        "text_channel": (
            "🍽️ <b>CHAYXANA AMIR</b>\n"
            "📍 <a href=\"https://www.google.com/maps?q=38.617004,-121.537971\">Sacramento, CA</a>\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 2–3 часа до доставки\n"
            "⏰ Время работы: 24/7\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/116\">Меню  (смотреть комментарии)</a>\n"
            "📞 +19167506977\n"
            "📱 Telegram: @N1_Ibragim"
        )
    },
    {
        "name": "RA\'NO OPA KITCHEN - HALOL MILLIY UZBEK TAOMLARI",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ RA\'NO OPA KITCHEN - HALOL MILLIY UZBEK TAOMLARI\n"
            "📍 San Francisco, CA (https://goo.gl/maps/3k6yxG5WASmCoJRq7)\n"
            "🏠 Домашняя кухня\n"
            "🧾 Заказы за 3–4 ч до доставки\n"
            "⏰ Время работы 10:00–22:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/118) (смотреть комментарии)\n"
            "📞 Телефон: +15107782614\n"
            "📱 Telegram: @Gulrano2610"
        ),
        "text_channel": (
            "🍽️ <b>RA\'NO OPA KITCHEN - HALOL MILLIY UZBEK TAOMLARI</b>\n"
            "📍 San Francisco, CA\n"
            "🏠 Домашняя кухня\n"
            "🧾 Заказы за 3–4 ч до доставки\n"
            "⏰ Время работы 10:00–22:00\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/118\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +15107782614\n"
            "📱 Telegram: @Gulrano2610"
        )
    },
    {
        "name": "HAKIM HALAL FOOD",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ HAKIM HALAL FOOD\n"
            "📍 Los Angeles, CA (https://goo.gl/maps/PKenHh8xC1JjQHYp7)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 1–2 ч до доставки\n"
            "⏰ Время работы 07:00–21:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/119) (смотреть комментарии)\n"
            "📞 Телефон: +16266878844\n"
            "📱 Telegram: @Hakimhalalfood"
        ),
        "text_channel": (
            "🍽️ <b>HAKIM HALAL FOOD</b>\n"
            "📍 Los Angeles, CA\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 1–2 ч до доставки\n"
            "⏰ Время работы 07:00–21:00\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/119\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +16266878844\n"
            "📱 Telegram: @Hakimhalalfood"
        )
    },
    {
        "name": "ASTAU HALAL FOOD",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ ASTAU HALAL FOOD\n"
            "📍 Los Angeles, CA (https://goo.gl/maps/SULgMJCXojQg1o4C7)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 2–3 ч до доставки\n"
            "⏰ Время работы: 24/7\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/120) (смотреть комментарии)\n"
            "📞 Телефон: +16464642693\n"
            "📱 Telegram: @naz_amerika"
        ),
        "text_channel": (
            "🍽️ <b>ASTAU HALAL FOOD</b>\n"
            "📍 Los Angeles, CA\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 2–3 ч до доставки\n"
            "⏰ Время работы: 24/7\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/120\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +16464642693\n"
            "📱 Telegram: @naz_amerika"
        )
    },
    {
        "name": "TASTE OF SAMARKAND",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ TASTE OF SAMARKAND\n"
            "📍 Denver, CO (https://goo.gl/maps/BwLYMtwTsmXeT3fL9)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 2-3 часа до доставки\n"
            "⏰ Время работы 08:00-22:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/121) (смотреть комментарии)\n"
            "📞 Телефон: +13039601391\n"
            "📱 Telegram: @Firdavs_57"
        ),
        "text_channel": (
            "🍽️ <b>TASTE OF SAMARKAND</b>\n"
            "📍 Denver, CO\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 2-3 часа до доставки\n"
            "⏰ Время работы 08:00-22:00\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/121\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +13039601391\n"
            "📱 Telegram: @Firdavs_57"
        )
    },
    {
        "name": "AUSTIN HALAL FOOD",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ AUSTIN HALAL FOOD\n"
            "📍 Austin, TX (https://goo.gl/maps/ENhTTDrZ6SzqLU7D9)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 3-4 часа до доставки\n"
            "⏰ Время работы 08:00-22:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/122) (смотреть комментарии)\n"
            "📞 Телефон: +17739832504\n"
            "📱 Telegram: @Austin_HalalFood"
        ),
        "text_channel": (
            "🍽️ <b>AUSTIN HALAL FOOD</b>\n"
            "📍 Austin, TX\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 3-4 часа до доставки\n"
            "⏰ Время работы 08:00-22:00\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/122\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +17739832504\n"
            "📱 Telegram: @Austin_HalalFood"
        )
    },
    {
        "name": "LAZZAT UZBEK RESTAURANT",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ LAZZAT UZBEK RESTAURANT\n"
            "📍 Chicago, IL (https://goo.gl/maps/Htj3D68qwDUptqio6)\n"
            "🏬 Ресторан\n"
            "🧾 Все продукты готовы, можно перед заказом купить\n"
            "⏰ Время работы 10:00-23:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/123) (смотреть комментарии)\n"
            "📞 Телефон: +18478936069\n"
            "📱 Telegram: @asd112777\n"
            "📱 Telegram: @LAZZAT_Chicago"
        ),
        "text_channel": (
            "🍽️ <b>LAZZAT UZBEK RESTAURANT</b>\n"
            "📍 Chicago, IL\n"
            "🏬 Ресторан\n"
            "🧾 Все продукты готовы, можно перед заказом купить\n"
            "⏰ Время работы 10:00-23:00\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/123\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +18478936069\n"
            "📱 Telegram: @asd112777\n"
            "📱 Telegram: @LAZZAT_Chicago"
        )
    },
    {
        "name": "GRILL EXPRESS HALAL FOOD",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ GRILL EXPRESS HALAL FOOD\n"
            "📍 Chicago, IL (https://goo.gl/maps/vCmvpHtW4mGgzPa18)\n"
            "🚛 Фудтрак\n"
            "🧾 Все продукты готовы, можно купить сразу при заказе\n"
            "🚘 Доставка бесплатная до 3 миль\n"
            "⏰ Время работы: 10:00 – 21:00\n"
            "📋 Меню (https://t.me/myhalalmenu/124) (смотреть комментарии)\n"
            "📞 Телефон: +12243633093\n"
            "📱 Telegram: @grillexp"
        ),
        "text_channel": (
            "🍽️ <b>GRILL EXPRESS HALAL FOOD</b>\n"
            "📍 Chicago, IL\n"
            "🚛 Фудтрак\n"
            "🧾 Все продукты готовы, можно купить сразу при заказе\n"
            "🚘 Доставка бесплатная до 3 миль\n"
            "⏰ Время работы: 10:00 – 21:00\n"
            "📋 <a href=\"https://t.me/myhalalmenu/124\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +12243633093\n"
            "📱 Telegram: @grillexp"
        )
    },
    {
        "name": "CLEVELAND OHIO",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ CLEVELAND OHIO\n"
            "📍 Cleveland, OH (https://goo.gl/maps/6PRQ2KxTFp5i4bwVA)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 3-4 часа до доставки\n"
            "⏰ Время работы 08:00-23:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/125) (смотреть комментарии)\n"
            "📞 Телефон: +15055001717\n"
            "📱 Telegram: @Farrukh171"
        ),
        "text_channel": (
            "🍽️ <b>CLEVELAND OHIO</b>\n"
            "📍 Cleveland, OH\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 3-4 часа до доставки\n"
            "⏰ Время работы 08:00-23:00\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/125\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +15055001717\n"
            "📱 Telegram: @Farrukh171"
        )
    },
    {
        "name": "DAYTON VILLAGE PIZZA HALAL TURKISH MEDITERRANEAN RESTAURANT",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ DAYTON VILLAGE PIZZA HALAL TURKISH MEDITERRANEAN RESTAURANT\n"
            "📍 Dayton, OH (https://goo.gl/maps/NsTqCPRKqaAKHoeq6)\n"
            "🏬 Ресторан\n"
            "🧾 Все продукты готовы, можно перед заказом купить\n"
            "⏰ Время работы 11:00-21:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/127) (смотреть комментарии)\n"
            "📞 Телефон: +19375670775\n"
            "📱 Telegram: @Gulnaz_Makhmudova"
        ),
        "text_channel": (
            "🍽️ <b>DAYTON VILLAGE PIZZA HALAL TURKISH MEDITERRANEAN RESTAURANT</b>\n"
            "📍 Dayton, OH\n"
            "🏬 Ресторан\n"
            "🧾 Все продукты готовы, можно перед заказом купить\n"
            "⏰ Время работы 11:00-21:00\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/127\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +19375670775\n"
            "📱 Telegram: @Gulnaz_Makhmudova"
        )
    },
    {
        "name": "GREENWICH PITA AND GRILL / UZBEK FOOD",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ GREENWICH PITA AND GRILL / UZBEK FOOD\n"
            "📍 Mason, OH (https://goo.gl/maps/DuoYBNKCaMW5EvHcA)\n"
            "🏬 Ресторан\n"
            "🧾 Все продукты готовы, можно перед заказом купить\n"
            "⏰ Время работы: 24/7\n"
            "❌ Пятница выходные\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/Masonfood) (смотреть комментарии)\n"
            "📞 Телефон: +13474555588\n"
            "📱 Telegram: @Farkhod_OHIO\n"
            "📱 Telegram: @masonfood"
        ),
        "text_channel": (
            "🍽️ <b>GREENWICH PITA AND GRILL / UZBEK FOOD</b>\n"
            "📍 Mason, OH\n"
            "🏬 Ресторан\n"
            "🧾 Все продукты готовы, можно перед заказом купить\n"
            "⏰ Время работы: 24/7\n"
            "❌ Пятница выходные\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/Masonfood\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +13474555588\n"
            "📱 Telegram: @Farkhod_OHIO\n"
            "📱 Telegram: @masonfood"
        )
    },
    {
        "name": "TASHKENT FOOD",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ TASHKENT FOOD\n"
            "📍 Nashville, TN (https://goo.gl/maps/zDMXoSHYoFAwMxi99)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 2-3 часа до доставки\n"
            "⏰ Время работы 08:00-23:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/129) (смотреть комментарии)\n"
            "📞 Телефон: +16155214497\n"
            "📱 Telegram: @uzbekfood_2026"
        ),
        "text_channel": (
            "🍽️ <b>TASHKENT FOOD</b>\n"
            "📍 Nashville, TN\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 2-3 часа до доставки\n"
            "⏰ Время работы 08:00-23:00\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/129\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +16155214497\n"
            "📱 Telegram: @uzbekfood_2026"
        )
    },
    {
        "name": "F.S_FOODS",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ F.S_FOODS\n"
            "📍 Knoxville, TN (https://goo.gl/maps/p9kMG6HY2Ybq8G6J9)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 4-5 часа до доставки\n"
            "⏰ Время работы: 24/7\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/feruza_mustakimo) (смотреть комментарии)\n"
            "📞 Телефон: +12028789292\n"
            "📞 +18653690369\n"
            "📱 Telegram: @NeoSm\n"
            "📱 Telegram: @feruza_mustakimova"
        ),
        "text_channel": (
            "🍽️ <b>F.S_FOODS</b>\n"
            "📍 Knoxville, TN\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 4-5 часа до доставки\n"
            "⏰ Время работы: 24/7\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/feruza_mustakimo\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +12028789292\n"
            "📞 +18653690369\n"
            "📱 Telegram: @NeoSm\n"
            "📱 Telegram: @feruza_mustakimova"
        )
    },
    {
        "name": "ATLANTA UZBEK HALOL",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ ATLANTA UZBEK HALOL\n"
            "📍 Atlanta, GA (https://goo.gl/maps/KRomTZi2adTW1Mt88)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 4-5 часа до доставки\n"
            "⏰ Время работы 09:00 am -18:00 pm\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/131) (смотреть комментарии)\n"
            "📞 Телефон: +17708782168\n"
            "📱 Telegram: @kozim4202"
        ),
        "text_channel": (
            "🍽️ <b>ATLANTA UZBEK HALOL</b>\n"
            "📍 Atlanta, GA\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 4-5 часа до доставки\n"
            "⏰ Время работы 09:00 am -18:00 pm\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/131\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +17708782168\n"
            "📱 Telegram: @kozim4202"
        )
    },
    {
        "name": "ALONS UZBEK HALAL GRILL",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ ALONS UZBEK HALAL GRILL\n"
            "📍 New York (https://goo.gl/maps/h3qEJE4jJHR1ZUpr8)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Все продукты готовы, можно перед заказом купить\n"
            "⏰ Время работы 11:00-20:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/132) (смотреть комментарии)\n"
            "📞 Телефон: +18458342180\n"
            "📱 Telegram: @myhalal_admin"
        ),
        "text_channel": (
            "🍽️ <b>ALONS UZBEK HALAL GRILL</b>\n"
            "📍 New York\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Все продукты готовы, можно перед заказом купить\n"
            "⏰ Время работы 11:00-20:00\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/132\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +18458342180\n"
            "📱 Telegram: @myhalal_admin"
        )
    },
    {
        "name": "ВКУС ВОСТОКА ХАЛЯЛЬ",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ ВКУС ВОСТОКА ХАЛЯЛЬ\n"
            "📍 Baltimore, MD (https://goo.gl/maps/fnesEzQyGGYzY4Mn7)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 2-3 часа до доставки\n"
            "⏰ Время работы 07:00-22:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/133) (смотреть комментарии)\n"
            "📞 Телефон: +14439700986\n"
            "📱 Telegram: @solikhamuslima"
        ),
        "text_channel": (
            "🍽️ <b>ВКУС ВОСТОКА ХАЛЯЛЬ</b>\n"
            "📍 Baltimore, MD\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 2-3 часа до доставки\n"
            "⏰ Время работы 07:00-22:00\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/133\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +14439700986\n"
            "📱 Telegram: @solikhamuslima"
        )
    },
    {
        "name": "MR HALAL FOOD MARKET",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ MR HALAL FOOD MARKET\n"
            "📍 Pikesville, MD (https://maps.app.goo.gl/NMQCGPqVTjGpELrQA)\n"
            "🏪 Магазин\n"
            "⏰ Время работы: 24/7\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/MRHALOL) (смотреть комментарии)\n"
            "📞 Телефон: +14046656589\n"
            "📞 +19144343927\n"
            "📱 Telegram: @mrhalolmd\n"
            "📱 Telegram: @iX_MiR"
        ),
        "text_channel": (
            "🍽️ <b>MR HALAL FOOD MARKET</b>\n"
            "📍 Pikesville, MD\n"
            "🏪 Магазин\n"
            "⏰ Время работы: 24/7\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/MRHALOL\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +14046656589\n"
            "📞 +19144343927\n"
            "📱 Telegram: @mrhalolmd\n"
            "📱 Telegram: @iX_MiR"
        )
    },
    {
        "name": "REGISTON HALAL FOOD",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ REGISTON HALAL FOOD\n"
            "📍 Charlotte, NC (https://goo.gl/maps/uoVJ47oesXgFPfSUA)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 2 часа до доставки\n"
            "⏰ Время работы: 24/7\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/135) (смотреть комментарии)\n"
            "📞 Телефон: +17046152888\n"
            "📱 Telegram: @myhalal_admin"
        ),
        "text_channel": (
            "🍽️ <b>REGISTON HALAL FOOD</b>\n"
            "📍 Charlotte, NC\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 2 часа до доставки\n"
            "⏰ Время работы: 24/7\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/135\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +17046152888\n"
            "📱 Telegram: @myhalal_admin"
        )
    },
    {
        "name": "CRIMEAN CUISINE BY ASIE",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ CRIMEAN CUISINE BY ASIE\n"
            "📍 Jacksonville, FL (https://goo.gl/maps/55mPXuBsX7m4PJA39)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 3-4 часа до доставки\n"
            "⏰ Время работы 08:00-20:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/136) (смотреть комментарии)\n"
            "📞 Телефон: +19046093711\n"
            "📱 Telegram: @CRIMEAN_CUISINE_BY_ASIE"
        ),
        "text_channel": (
            "🍽️ <b>CRIMEAN CUISINE BY ASIE</b>\n"
            "📍 Jacksonville, FL\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 3-4 часа до доставки\n"
            "⏰ Время работы 08:00-20:00\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/136\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +19046093711\n"
            "📱 Telegram: @CRIMEAN_CUISINE_BY_ASIE"
        )
    },
    {
        "name": "JACKSONVILLE HALAL FOOD",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ JACKSONVILLE HALAL FOOD\n"
            "📍 Jacksonville, FL (https://goo.gl/maps/yRuNo9un5iMLFcmo9)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 3-4 часа до доставки\n"
            "⏰ Время работы 09:00-20:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/137) (смотреть комментарии)\n"
            "📞 Телефон: +19044768176\n"
            "📱 Telegram: @semifinished_products_jax"
        ),
        "text_channel": (
            "🍽️ <b>JACKSONVILLE HALAL FOOD</b>\n"
            "📍 Jacksonville, FL\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 3-4 часа до доставки\n"
            "⏰ Время работы 09:00-20:00\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/137\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +19044768176\n"
            "📱 Telegram: @semifinished_products_jax"
        )
    },
    {
        "name": "ORLANDO HALAL FOOD",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ ORLANDO HALAL FOOD\n"
            "📍 Orlando, FL (https://goo.gl/maps/PivWEoBKccn7eSg28)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 1-2 часа до доставки\n"
            "⏰ Время работы: 24/7\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/138) (смотреть комментарии)\n"
            "📞 Телефон: +16893454142\n"
            "📱 Telegram: @adi1lek"
        ),
        "text_channel": (
            "🍽️ <b>ORLANDO HALAL FOOD</b>\n"
            "📍 Orlando, FL\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 1-2 часа до доставки\n"
            "⏰ Время работы: 24/7\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/138\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +16893454142\n"
            "📱 Telegram: @adi1lek"
        )
    },
    {
        "name": "AISHA FOOD",
        "lat": 38.61708200,
        "lng": -121.53778900,
        "text_user": (
            "🍽️ AISHA FOOD\n"
            "📍 Sacramento, CA (https://www.google.com/maps?q=38.61708200,-121.53778900)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 4–5 часов до доставки\n"
            "⏰ Время работы: 11:00 – 23:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/139) (смотреть комментарии)\n"
            "📞 Телефон: +19163362736\n"
            "📱 Telegram: @floral79766"
        ),
        "text_channel": (
            "🍽️ <b>AISHA FOOD</b>\n"
            "📍 <a href=\"https://www.google.com/maps?q=38.617082,-121.537789\">Sacramento, CA</a>\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 4–5 часов до доставки\n"
            "⏰ Время работы: 11:00 – 23:00\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/139\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +19163362736\n"
            "📱 Telegram: @floral79766"
        )
    },
    {
        "name": "UMAR UZBEK NATIONAL FOOD",
        "lat": 38.61700400,
        "lng": -121.53797100,
        "text_user": (
            "🍽️ UMAR UZBEK NATIONAL FOOD\n"
            "📍 Sacramento, CA (https://www.google.com/maps?q=38.61700400,-121.53797100)\n"
            "🏠 Домашняя кухня\n"
            "🧾 Заказы принимаются за 4–5 часов до доставки\n"
            "⏰ Время работы: 10:00 – 20:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/140) (смотреть комментарии)\n"
            "📞 Телефон: +19165333778\n"
            "📱 Telegram: @UMARFOODCILE"
        ),
        "text_channel": (
            "🍽️ <b>UMAR UZBEK NATIONAL FOOD</b>\n"
            "📍 <a href=\"https://www.google.com/maps?q=38.617004,-121.537971\">Sacramento, CA</a>\n"
            "🏠 Домашняя кухня\n"
            "🧾 Заказы принимаются за 4–5 часов до доставки\n"
            "⏰ Время работы: 10:00 – 20:00\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/140\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +19165333778\n"
            "📱 Telegram: @UMARFOODCILE"
        )
    },
    {
        "name": "ASIA HALAL FOOD",
        "lat": 47.24476600,
        "lng": -122.38548700,
        "text_user": (
            "🍽️ ASIA HALAL FOOD\n"
            "📍 Tacoma, WA (https://www.google.com/maps?q=47.24476600,-122.38548700)\n"
            "🏠 Домашняя кухня\n"
            "🧾 Все продукты готовы, можно купить сразу при заказе\n"
            "⏰ Время работы: 08:00-22:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/141) (смотреть комментарии)\n"
            "📞 Телефон: +18782294148\n"
            "📞 +18782294149\n"
            "📱 Telegram: @AsiaHalalFood"
        ),
        "text_channel": (
            "🍽️ <b>ASIA HALAL FOOD</b>\n"
            "📍 <a href=\"https://www.google.com/maps?q=47.244766,-122.385487\">Tacoma, WA</a>\n"
            "🏠 Домашняя кухня\n"
            "🧾 Все продукты готовы, можно купить сразу при заказе\n"
            "⏰ Время работы: 08:00-22:00\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/141\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +18782294148\n"
            "📞 +18782294149\n"
            "📱 Telegram: @AsiaHalalFood"
        )
    },
    {
        "name": "UZBEK HALOL FOOD",
        "lat": 47.24476600,
        "lng": -122.38548700,
        "text_user": (
            "🍽️ UZBEK HALOL FOOD\n"
            "📍 Tacoma, WA (https://www.google.com/maps?q=47.24476600,-122.38548700)\n"
            "🚛 Food truck\n"
            "🧾 Все продукты готовы, можно купить сразу при заказе\n"
            "⏰ Время работы: 08:00 – 22:00\n"
            "🚘 Доставка бесплатно\n"
            "📋 Меню (https://t.me/myhalalmenu/142) (смотреть комментарии)\n"
            "📞 Телефон: +13609306392\n"
            "📞 +12534485190\n"
            "📱 Telegram: @SabinaBekzodSafiya"
        ),
        "text_channel": (
            "🍽️ <b>UZBEK HALOL FOOD</b>\n"
            "📍 <a href=\"https://www.google.com/maps?q=47.244766,-122.385487\">Tacoma, WA</a>\n"
            "🚛 Food truck\n"
            "🧾 Все продукты готовы, можно купить сразу при заказе\n"
            "⏰ Время работы: 08:00 – 22:00\n"
            "🚘 Доставка бесплатно\n"
            "📋 <a href=\"https://t.me/myhalalmenu/142\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +13609306392\n"
            "📞 +12534485190\n"
            "📱 Telegram: @SabinaBekzodSafiya"
        )
    },
    {
        "name": "CARAVAN RESTAURANT – 2",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ CARAVAN RESTAURANT – 2\n"
            "📍 Seattle, WA (https://maps.app.goo.gl/RiKVT3aQoJbWZ3xg8)\n"
            "🏬 Ресторан\n"
            "🧾 Все продукты готовы, можно купить сразу при заказе\n"
            "⏰ Время работы: 11:00 – 22:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/143) (смотреть комментарии)\n"
            "📞 Телефон: +12064501323\n"
            "📱 Telegram: @caravanseattle"
        ),
        "text_channel": (
            "🍽️ <b>CARAVAN RESTAURANT – 2</b>\n"
            "📍 Seattle, WA\n"
            "🏬 Ресторан\n"
            "🧾 Все продукты готовы, можно купить сразу при заказе\n"
            "⏰ Время работы: 11:00 – 22:00\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/143\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +12064501323\n"
            "📱 Telegram: @caravanseattle"
        )
    },
    {
        "name": "CHAYHANA №1",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ CHAYHANA №1\n"
            "📍 Cincinnati, OH (https://maps.app.goo.gl/oN7C6yWch5y23sMdA)\n"
            "🏬 Ресторан\n"
            "🧾 Все блюда готовы, можно заказать заранее и забрать\n"
            "⏰ Время работы: 24/7\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/144) (смотреть комментарии)\n"
            "📞 Телефон: +15137550596\n"
            "📱 Telegram: @DS_EXPRESS"
        ),
        "text_channel": (
            "🍽️ <b>CHAYHANA №1</b>\n"
            "📍 Cincinnati, OH\n"
            "🏬 Ресторан\n"
            "🧾 Все блюда готовы, можно заказать заранее и забрать\n"
            "⏰ Время работы: 24/7\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/144\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +15137550596\n"
            "📱 Telegram: @DS_EXPRESS"
        )
    },
    {
        "name": "SHEF MOM – CAKE – SUSHI",
        "lat": 39.38454100,
        "lng": -84.34233300,
        "text_user": (
            "🍽️ SHEF MOM – CAKE – SUSHI\n"
            "📍 Cincinnati, OH (https://www.google.com/maps?q=39.38454100,-84.34233300)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 5 часов до доставки\n"
            "⏰ Время работы: 10:00–22:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/145) (смотреть комментарии)\n"
            "📞 Телефон: +14704000770\n"
            "📱 Telegram:"
        ),
        "text_channel": (
            "🍽️ <b>SHEF MOM – CAKE – SUSHI</b>\n"
            "📍 <a href=\"https://www.google.com/maps?q=39.384541,-84.342333\">Cincinnati, OH</a>\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 5 часов до доставки\n"
            "⏰ Время работы: 10:00–22:00\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/145\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +14704000770\n"
            "📱 Telegram:"
        )
    },
    {
        "name": "Таджикско-узбекская Национальная кухня",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ Таджикско-узбекская Национальная кухня\n"
            "📍 Omaha, NE (https://maps.app.goo.gl/JJinSW71AMcbyXTEA)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 2–3 часа до доставки\n"
            "⏰ Время работы: 05:00-22:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/146) (смотреть комментарии)\n"
            "📞 Телефон: +14026168772\n"
            "📱 Telegram: @DCOMAHAFOOD"
        ),
        "text_channel": (
            "🍽️ <b>Таджикско-узбекская Национальная кухня</b>\n"
            "📍 Omaha, NE\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 2–3 часа до доставки\n"
            "⏰ Время работы: 05:00-22:00\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/146\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +14026168772\n"
            "📱 Telegram: @DCOMAHAFOOD"
        )
    },
    {
        "name": "ZARINA FOOD UYGʻUR OSHXONASI",
        "lat": 40.28957100,
        "lng": -76.88458100,
        "text_user": (
            "🍽️ ZARINA FOOD UYGʻUR OSHXONASI\n"
            "📍 Harrisburg, PA  (https://www.google.com/maps?q=40.28957100,-76.88458100)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 2–3 часа до доставки\n"
            "⏰ Время работы: 08:00 – 18:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/147) (смотреть комментарии)\n"
            "📞 Телефон: +17175626326\n"
            "📱 Telegram: @Zarina_halal_food"
        ),
        "text_channel": (
            "🍽️ <b>ZARINA FOOD UYGʻUR OSHXONASI</b>\n"
            "📍 <a href=\"https://www.google.com/maps?q=40.289571,-76.884581\">Harrisburg, PA</a>\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 2–3 часа до доставки\n"
            "⏰ Время работы: 08:00 – 18:00\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/147\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +17175626326\n"
            "📱 Telegram: @Zarina_halal_food"
        )
    },
    {
        "name": "PIZZA BARI",
        "lat": 40.44370500,
        "lng": -79.99612500,
        "text_user": (
            "🍽️ PIZZA BARI\n"
            "📍 Pittsburgh, PA (https://www.google.com/maps?q=40.44370500,-79.99612500)\n"
            "🏠 Кафе\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ Время работы: 10:00 – 02:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/148) (смотреть комментарии)\n"
            "📞 Телефон: +14124020444\n"
            "📞 +14126090714\n"
            "📱 Telegram: @Odil_lfc"
        ),
        "text_channel": (
            "🍽️ <b>PIZZA BARI</b>\n"
            "📍 <a href=\"https://www.google.com/maps?q=40.443705,-79.996125\">Pittsburgh, PA</a>\n"
            "🏠 Кафе\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ Время работы: 10:00 – 02:00\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/148\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +14124020444\n"
            "📞 +14126090714\n"
            "📱 Telegram: @Odil_lfc"
        )
    },
    {
        "name": "MUSOJON",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ MUSOJON\n"
            "📍 Phoenix, AZ (https://maps.app.goo.gl/nKS2adwvmkwss3XP8)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ Время работы: 05:00 – 22:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/149) (смотреть комментарии)\n"
            "📞 Телефон: +16028201597\n"
            "📱 Telegram: @ibrohim_Musojon"
        ),
        "text_channel": (
            "🍽️ <b>MUSOJON</b>\n"
            "📍 Phoenix, AZ\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ Время работы: 05:00 – 22:00\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/149\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +16028201597\n"
            "📱 Telegram: @ibrohim_Musojon"
        )
    },
    {
        "name": "ARIZONA HALAL FOOD",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ ARIZONA HALAL FOOD\n"
            "📍 Phoenix, AZ (https://maps.app.goo.gl/dtZverCcRF5TTVc6A)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 4–5 часов до доставки\n"
            "⏰ Время работы: 09:00 – 21:00\n"
            "🚘 Доставка договорная\n"
            "📋 Меню (https://t.me/myhalalmenu/150) (смотреть комментарии)\n"
            "📞 Телефон: +16238062332\n"
            "📞 +14806343188\n"
            "📱 Telegram:"
        ),
        "text_channel": (
            "🍽️ <b>ARIZONA HALAL FOOD</b>\n"
            "📍 Phoenix, AZ\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 4–5 часов до доставки\n"
            "⏰ Время работы: 09:00 – 21:00\n"
            "🚘 Доставка договорная\n"
            "📋 <a href=\"https://t.me/myhalalmenu/150\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +16238062332\n"
            "📞 +14806343188\n"
            "📱 Telegram:"
        )
    },
    {
        "name": "TOSHKENT MILLIY TAOMLARI",
        "lat": 33.50457600,
        "lng": -112.44414100,
        "text_user": (
            "🍽️ TOSHKENT MILLIY TAOMLARI\n"
            "📍 Phoenix, AZ (https://www.google.com/maps?q=33.50457600,-112.44414100)  (https://www.google.com/maps?q=33.49340800,-112.33416100)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Все продукты готовы, можно купить сразу при заказе\n"
            "⏰ Время работы: 07:00 – 21:00\n"
            "❗️ В пятницу выходные\n"
            "🚚 Можно приехать с трак трейлером\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/151) (смотреть комментарии)\n"
            "📞 Телефон: +16023489938\n"
            "📱 Telegram: @Samo_SR"
        ),
        "text_channel": (
            "🍽️ <b>TOSHKENT MILLIY TAOMLARI</b>\n"
            "📍 <a href=\"https://www.google.com/maps?q=33.504576,-112.444141\">Phoenix, AZ</a>\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Все продукты готовы, можно купить сразу при заказе\n"
            "⏰ Время работы: 07:00 – 21:00\n"
            "❗️ В пятницу выходные\n"
            "🚚 Можно приехать с трак трейлером\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/151\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +16023489938\n"
            "📱 Telegram: @Samo_SR"
        )
    },
    {
        "name": "ALI\'S KITCHEN",
        "lat": 33.46092400,
        "lng": -112.25515400,
        "text_user": (
            "🍽️ ALI\'S KITCHEN\n"
            "📍 Phoenix, AZ   (https://www.google.com/maps?q=33.46092400,-112.25515400)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Все продукты готовы, можно купить сразу при заказе\n"
            "⏰ Время работы: 09:00 – 00:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/152) (смотреть комментарии)\n"
            "📞 Телефон: +16026997010\n"
            "📱 Telegram:"
        ),
        "text_channel": (
            "🍽️ <b>ALI\'S KITCHEN</b>\n"
            "📍 <a href=\"https://www.google.com/maps?q=33.460924,-112.255154\">Phoenix, AZ</a>\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Все продукты готовы, можно купить сразу при заказе\n"
            "⏰ Время работы: 09:00 – 00:00\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/152\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +16026997010\n"
            "📱 Telegram:"
        )
    },
    {
        "name": "UZBEK HALAL FOODS",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ UZBEK HALAL FOODS\n"
            "📍 Memphis, TN (https://maps.app.goo.gl/DxTwbfJaypEZvf647?g_st=atmID18) (Arkansas border)\n"
            "🚛 Фудтрак\n"
            "🧾 Все продукты готовы, можно купить сразу при заказе\n"
            "⏰ Время работы: 09:00 – 23:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/153) (смотреть комментарии)\n"
            "📞 Телефон: +15126693163\n"
            "📱 Telegram: @JJuraev_707"
        ),
        "text_channel": (
            "🍽️ <b>UZBEK HALAL FOODS</b>\n"
            "📍 Memphis, TN (Arkansas border)\n"
            "🚛 Фудтрак\n"
            "🧾 Все продукты готовы, можно купить сразу при заказе\n"
            "⏰ Время работы: 09:00 – 23:00\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/153\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +15126693163\n"
            "📱 Telegram: @JJuraev_707"
        )
    },
    {
        "name": "MADI FOOD (Uygʻurcha taomlar)",
        "lat": 28.03012900,
        "lng": -82.45883800,
        "text_user": (
            "🍽️ MADI FOOD (Uygʻurcha taomlar)\n"
            "📍 Tampa, FL (https://www.google.com/maps?q=28.03012900,-82.45883800)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 3–4 часа до доставки\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 10:00 – 22:00\n"
            "📋 Меню (https://t.me/myhalalmenu/154) (смотреть комментарии)\n"
            "📞 Телефон: +17178058368\n"
            "📱 Telegram: @madimadi04"
        ),
        "text_channel": (
            "🍽️ <b>MADI FOOD (Uygʻurcha taomlar)</b>\n"
            "📍 <a href=\"https://www.google.com/maps?q=28.030129,-82.458838\">Tampa, FL</a>\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 3–4 часа до доставки\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 10:00 – 22:00\n"
            "📋 <a href=\"https://t.me/myhalalmenu/154\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +17178058368\n"
            "📱 Telegram: @madimadi04"
        )
    },
    {
        "name": "CHAYHANA ORLANDO",
        "lat": 28.66596900,
        "lng": -81.41681300,
        "text_user": (
            "🍽️ CHAYHANA ORLANDO\n"
            "📍 Orlando, FL (https://www.google.com/maps?q=28.66596900,-81.41681300)\n"
            "🏬 Ресторан\n"
            "🧾 Все продукты готовы, можно купить сразу при заказе\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 24/7\n"
            "📋 Меню (https://t.me/myhalalmenu/155) (смотреть комментарии)\n"
            "📞 Телефон: +13213676807\n"
            "📞 +13214220143\n"
            "📱 Telegram: @chayhanaOrlando"
        ),
        "text_channel": (
            "🍽️ <b>CHAYHANA ORLANDO</b>\n"
            "📍 <a href=\"https://www.google.com/maps?q=28.665969,-81.416813\">Orlando, FL</a>\n"
            "🏬 Ресторан\n"
            "🧾 Все продукты готовы, можно купить сразу при заказе\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 24/7\n"
            "📋 <a href=\"https://t.me/myhalalmenu/155\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +13213676807\n"
            "📞 +13214220143\n"
            "📱 Telegram: @chayhanaOrlando"
        )
    },
    {
        "name": "CARAVAN RESTAURANT",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ CARAVAN RESTAURANT\n"
            "📍 Chicago, IL  (https://maps.app.goo.gl/gj72DoxeAVhTFgsy5?g_st=atm)\n"
            "🏬 Ресторан\n"
            "🧾 Все продукты готовы, можно купить сразу при заказе\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 10:00 – 22:00\n"
            "📋 Меню (https://t.me/myhalalmenu/156) (смотреть комментарии)\n"
            "📞 Телефон: +17733673258\n"
            "📱 Telegram: https://t.me/+ymTVa5mIxjphZTcx"
        ),
        "text_channel": (
            "🍽️ <b>CARAVAN RESTAURANT</b>\n"
            "📍 Chicago, IL\n"
            "🏬 Ресторан\n"
            "🧾 Все продукты готовы, можно купить сразу при заказе\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 10:00 – 22:00\n"
            "📋 <a href=\"https://t.me/myhalalmenu/156\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +17733673258\n"
            "📱 Telegram: https://t.me/+ymTVa5mIxjphZTcx"
        )
    },
    {
        "name": "TAKU FOOD",
        "lat": 41.98429200,
        "lng": -87.69751100,
        "text_user": (
            "🍽️ TAKU FOOD\n"
            "📍 Chicago, IL (https://www.google.com/maps?q=41.98429200,-87.69751100)\n"
            "🏬 Ресторан\n"
            "🧾 Все продукты готовы, можно купить сразу при заказе\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 08:00 – 23:00\n"
            "📋 Меню (https://t.me/myhalalmenu/189) (смотреть комментарии)\n"
            "📞 Телефон: +12247600211\n"
            "📞 +17736812626\n"
            "📱 Telegram: @takufood"
        ),
        "text_channel": (
            "🍽️ <b>TAKU FOOD</b>\n"
            "📍 <a href=\"https://www.google.com/maps?q=41.984292,-87.697511\">Chicago, IL</a>\n"
            "🏬 Ресторан\n"
            "🧾 Все продукты готовы, можно купить сразу при заказе\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 08:00 – 23:00\n"
            "📋 <a href=\"https://t.me/myhalalmenu/189\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +12247600211\n"
            "📞 +17736812626\n"
            "📱 Telegram: @takufood"
        )
    },
    {
        "name": "KAZAN KEBAB",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ KAZAN KEBAB\n"
            "📍 Chicago, IL  (https://maps.app.goo.gl/udKURdbEZi35C4Z46)\n"
            "🏬 Ресторан\n"
            "🧾 Все продукты готовы, можно перед заказом купить\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 24/7\n"
            "📋 Меню (https://t.me/myhalalmenu/190) (смотреть комментарии)\n"
            "📞 Телефон: +15517869980\n"
            "📱 Telegram: @Ali071188"
        ),
        "text_channel": (
            "🍽️ <b>KAZAN KEBAB</b>\n"
            "📍 Chicago, IL\n"
            "🏬 Ресторан\n"
            "🧾 Все продукты готовы, можно перед заказом купить\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 24/7\n"
            "📋 <a href=\"https://t.me/myhalalmenu/190\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +15517869980\n"
            "📱 Telegram: @Ali071188"
        )
    },
    {
        "name": "MAKSAT FOOD TRUCK",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ MAKSAT FOOD TRUCK\n"
            "📍 Portland, OR (https://maps.app.goo.gl/3pndGgSNVU2iy16j8)\n"
            "🚛 Фудтрак\n"
            "🧾 Все продукты готовы, можно купить сразу при заказе\n"
            "🚘 Доставка бесплатная\n"
            "⏰ Время работы: 10:00 – 23:00\n"
            "📋 Меню (https://t.me/myhalalmenu/191) (смотреть комментарии)\n"
            "📞 Телефон: +13602108483\n"
            "📱 Telegram: @Maksat_Food_Portland"
        ),
        "text_channel": (
            "🍽️ <b>MAKSAT FOOD TRUCK</b>\n"
            "📍 Portland, OR\n"
            "🚛 Фудтрак\n"
            "🧾 Все продукты готовы, можно купить сразу при заказе\n"
            "🚘 Доставка бесплатная\n"
            "⏰ Время работы: 10:00 – 23:00\n"
            "📋 <a href=\"https://t.me/myhalalmenu/191\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +13602108483\n"
            "📱 Telegram: @Maksat_Food_Portland"
        )
    },
    {
        "name": "NAVAT PDX",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ NAVAT PDX\n"
            "📍 Portland, OR (https://maps.app.goo.gl/gyk4Sr2wp7KWA4EB8)  (https://www.google.com/maps?q=45.54936400,-122.66185700)\n"
            "🚛 Фудтрак\n"
            "🧾 Все продукты готовы, можно купить сразу при заказе\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 11:00 – 22:00\n"
            "📋 Меню (https://t.me/myhalalmenu/192) (смотреть комментарии)\n"
            "📞 Телефон: +15033428099\n"
            "📞 +14254282011\n"
            "📞 +17253774764\n"
            "📱 Telegram: @daniiarsariev"
        ),
        "text_channel": (
            "🍽️ <b>NAVAT PDX</b>\n"
            "📍 Portland, OR\n"
            "🚛 Фудтрак\n"
            "🧾 Все продукты готовы, можно купить сразу при заказе\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 11:00 – 22:00\n"
            "📋 <a href=\"https://t.me/myhalalmenu/192\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +15033428099\n"
            "📞 +14254282011\n"
            "📞 +17253774764\n"
            "📱 Telegram: @daniiarsariev"
        )
    },
    {
        "name": "OSH RESTAURANT AND GRILL",
        "lat": 36.11125400,
        "lng": -86.74126300,
        "text_user": (
            "🍽️ OSH RESTAURANT AND GRILL\n"
            "📍 Nashville, TN (https://www.google.com/maps?q=36.11125400,-86.74126300)\n"
            "🏬 Ресторан\n"
            "🧾 Заказы принимаются до 21:00\n"
            "🚘 Доставка: с 10:00 до 02:00\n"
            "⏰ Время работы:\n"
            "— Втрн–Вскр: 11:00 – 21:00\n"
            "— Понедельник: выходной\n"
            "📋 Меню (https://t.me/myhalalmenu/193) (смотреть комментарии)\n"
            "📞 Телефон: +16159684444\n"
            "📞 +16157102288\n"
            "📞 +16157129985\n"
            "📱 Telegram: @BA7007"
        ),
        "text_channel": (
            "🍽️ <b>OSH RESTAURANT AND GRILL</b>\n"
            "📍 <a href=\"https://www.google.com/maps?q=36.111254,-86.741263\">Nashville, TN</a>\n"
            "🏬 Ресторан\n"
            "🧾 Заказы принимаются до 21:00\n"
            "🚘 Доставка: с 10:00 до 02:00\n"
            "⏰ Время работы:\n"
            "— Втрн–Вскр: 11:00 – 21:00\n"
            "— Понедельник: выходной\n"
            "📋 <a href=\"https://t.me/myhalalmenu/193\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +16159684444\n"
            "📞 +16157102288\n"
            "📞 +16157129985\n"
            "📱 Telegram: @BA7007"
        )
    },
    {
        "name": "BROOKLYN PIZZA",
        "lat": 36.11934500,
        "lng": -86.74898100,
        "text_user": (
            "🍽️ BROOKLYN PIZZA\n"
            "📍 Nashville, TN  (https://www.google.com/maps?q=36.11934500,-86.74898100)\n"
            "🏬 Ресторан/Кафе\n"
            "🧾 Все продукты готовы, можно купить сразу при заказе\n"
            "🚘 Доставка: 24/7 — $1 за каждую милю\n"
            "⏰ Время работы: 10:00 – 22:00\n"
            "📋 Меню (https://t.me/myhalalmenu/194) (смотреть комментарии)\n"
            "📞 Телефон: +16159552222\n"
            "📞 +16159257070\n"
            "📱 Telegram: @Brooklyncafe"
        ),
        "text_channel": (
            "🍽️ <b>BROOKLYN PIZZA</b>\n"
            "📍 <a href=\"https://www.google.com/maps?q=36.119345,-86.748981\">Nashville, TN</a>\n"
            "🏬 Ресторан/Кафе\n"
            "🧾 Все продукты готовы, можно купить сразу при заказе\n"
            "🚘 Доставка: 24/7 — $1 за каждую милю\n"
            "⏰ Время работы: 10:00 – 22:00\n"
            "📋 <a href=\"https://t.me/myhalalmenu/194\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +16159552222\n"
            "📞 +16159257070\n"
            "📱 Telegram: @Brooklyncafe"
        )
    },
    {
        "name": "KAMOLA OSHXONASI",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ KAMOLA OSHXONASI\n"
            "📍 Knoxville, TN  (https://maps.app.goo.gl/hnBtmygu2Q1aMrZg6)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 4–5 часов до доставки\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 24/7\n"
            "📋 Меню (https://t.me/myhalalmenu/195) (смотреть комментарии)\n"
            "📞 Телефон: +18654408193\n"
            "📞 +18654100845\n"
            "📞 +18653205784\n"
            "📱 Telegram: @komolaoshhonasi\n"
            "📱 Telegram: @Kaamollaa\n"
            "📱 Telegram: @Abdumannopovv01"
        ),
        "text_channel": (
            "🍽️ <b>KAMOLA OSHXONASI</b>\n"
            "📍 Knoxville, TN\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 4–5 часов до доставки\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 24/7\n"
            "📋 <a href=\"https://t.me/myhalalmenu/195\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +18654408193\n"
            "📞 +18654100845\n"
            "📞 +18653205784\n"
            "📱 Telegram: @komolaoshhonasi\n"
            "📱 Telegram: @Kaamollaa\n"
            "📱 Telegram: @Abdumannopovv01"
        )
    },
    {
        "name": "UZBEGIM RESTAURANT",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ UZBEGIM RESTAURANT\n"
            "📍 Nashville, TN (https://maps.app.goo.gl/LRbhfiiNhxRpGVq39)\n"
            "🏬 Кафе\n"
            "🧾 Все продукты готовы, можно купить сразу при заказе\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 10:00 - 00:00\n"
            "📋 Меню (https://t.me/myhalalmenu/196) (смотреть комментарии)\n"
            "📞 Телефон: +13476138691\n"
            "📱 Telegram: @uzbegimhalalrestaurant"
        ),
        "text_channel": (
            "🍽️ <b>UZBEGIM RESTAURANT</b>\n"
            "📍 Nashville, TN\n"
            "🏬 Кафе\n"
            "🧾 Все продукты готовы, можно купить сразу при заказе\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 10:00 - 00:00\n"
            "📋 <a href=\"https://t.me/myhalalmenu/196\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +13476138691\n"
            "📱 Telegram: @uzbegimhalalrestaurant"
        )
    },
    {
        "name": "BARAKAT HALAL FOOD",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ BARAKAT HALAL FOOD\n"
            "📍 Houston, TX (https://maps.app.goo.gl/gPgMgtmktAqWmpPq6)\n"
            "🚛 Фудтрак\n"
            "🧾 Все продукты готовы, можно купить сразу при заказе\n"
            "⏰ Время работы: 11:00-00:00\n"
            "🚘 Доставка 24/7\n"
            "📋 Меню (https://t.me/myhalalmenu/197) (смотреть комментарии)\n"
            "📞 Телефон: +13463772939\n"
            "📱 Telegram: @Ehsonjon01"
        ),
        "text_channel": (
            "🍽️ <b>BARAKAT HALAL FOOD</b>\n"
            "📍 Houston, TX\n"
            "🚛 Фудтрак\n"
            "🧾 Все продукты готовы, можно купить сразу при заказе\n"
            "⏰ Время работы: 11:00-00:00\n"
            "🚘 Доставка 24/7\n"
            "📋 <a href=\"https://t.me/myhalalmenu/197\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +13463772939\n"
            "📱 Telegram: @Ehsonjon01"
        )
    },
    {
        "name": "DIYAR HOUSTON FOOD",
        "lat": 29.77985100,
        "lng": -95.88196500,
        "text_user": (
            "🍽️ DIYAR HOUSTON FOOD\n"
            "📍 Houston, TX   (https://www.google.com/maps?q=29.77985100,-95.88196500)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 4–5 часов до доставки\n"
            "⏰ Время работы: 09:30 – 23:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/198) (смотреть комментарии)\n"
            "📞 Телефон: +13462740363\n"
            "📱 Telegram: @DiyarFood"
        ),
        "text_channel": (
            "🍽️ <b>DIYAR HOUSTON FOOD</b>\n"
            "📍 <a href=\"https://www.google.com/maps?q=29.779851,-95.881965\">Houston, TX</a>\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 4–5 часов до доставки\n"
            "⏰ Время работы: 09:30 – 23:00\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/198\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +13462740363\n"
            "📱 Telegram: @DiyarFood"
        )
    },
    {
        "name": "CARAVAN HOUSE",
        "lat": 41.04526200,
        "lng": -81.58033400,
        "text_user": (
            "🍽️ CARAVAN HOUSE\n"
            "📍 Akron, OH (https://www.google.com/maps?q=41.04526200,-81.58033400)\n"
            "🏬 Ресторан рядом с AMAZON\n"
            "🧾 Все продукты готовы, можно купить сразу при заказе\n"
            "⏰ Время работы: 10:00–22:00\n"
            "🚘 Доставка есть\n"
            "🅿️ Парковка для трейлеров\n"
            "📋 Меню (https://t.me/myhalalmenu/199) (смотреть комментарии)\n"
            "📞 Телефон: +14405755555\n"
            "📞 +12344020202\n"
            "📱 Telegram: @caravanhouse\n"
            "📱 Telegram: @dubaivali"
        ),
        "text_channel": (
            "🍽️ <b>CARAVAN HOUSE</b>\n"
            "📍 <a href=\"https://www.google.com/maps?q=41.045262,-81.580334\">Akron, OH</a>\n"
            "🏬 Ресторан рядом с AMAZON\n"
            "🧾 Все продукты готовы, можно купить сразу при заказе\n"
            "⏰ Время работы: 10:00–22:00\n"
            "🚘 Доставка есть\n"
            "🅿️ Парковка для трейлеров\n"
            "📋 <a href=\"https://t.me/myhalalmenu/199\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +14405755555\n"
            "📞 +12344020202\n"
            "📱 Telegram: @caravanhouse\n"
            "📱 Telegram: @dubaivali"
        )
    },
    {
        "name": "MAZALI CHARLOTTE OSHXONASI",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ MAZALI CHARLOTTE OSHXONASI\n"
            "📍 Charlotte, NC (https://maps.app.goo.gl/daeWzMNnnNnCUcTM6)\n"
            "🏬 Ресторан\n"
            "🧾 Заказы принимаются за 3–4 часа до доставки\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы:\n"
            "Пн–Пт: 11:00–20:00\n"
            "Сб–Вс: выходной\n"
            "📋 Меню (https://t.me/myhalalmenu/200) (смотреть комментарии)\n"
            "📞 Телефон: +13477856222\n"
            "📞 +13476666930\n"
            "📱 Telegram: @Mazali_Charlotte"
        ),
        "text_channel": (
            "🍽️ <b>MAZALI CHARLOTTE OSHXONASI</b>\n"
            "📍 Charlotte, NC\n"
            "🏬 Ресторан\n"
            "🧾 Заказы принимаются за 3–4 часа до доставки\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы:\n"
            "Пн–Пт: 11:00–20:00\n"
            "Сб–Вс: выходной\n"
            "📋 <a href=\"https://t.me/myhalalmenu/200\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +13477856222\n"
            "📞 +13476666930\n"
            "📱 Telegram: @Mazali_Charlotte"
        )
    },
    {
        "name": "N.N.D FOOD",
        "lat": 35.25497600,
        "lng": -80.97975000,
        "text_user": (
            "🍽️ N.N.D FOOD\n"
            "📍 Charlotte, NC   (https://www.google.com/maps?q=35.25497600,-80.97975000)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 2–3 часа до доставки\n"
            "⏰ Время работы: 24/7\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/201) (смотреть комментарии)\n"
            "📞 Телефон: +17045764025\n"
            "📞 +17046191145\n"
            "📞 +19802393354\n"
            "📱 Telegram: @nadi84food"
        ),
        "text_channel": (
            "🍽️ <b>N.N.D FOOD</b>\n"
            "📍 <a href=\"https://www.google.com/maps?q=35.254976,-80.97975\">Charlotte, NC</a>\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 2–3 часа до доставки\n"
            "⏰ Время работы: 24/7\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/201\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +17045764025\n"
            "📞 +17046191145\n"
            "📞 +19802393354\n"
            "📱 Telegram: @nadi84food"
        )
    },
    {
        "name": "AFSONA",
        "lat": 40.63575300,
        "lng": -73.97448900,
        "text_user": (
            "🍽️ AFSONA\n"
            "📍 Brooklyn, NY  (https://www.google.com/maps?q=40.63575300,-73.97448900)\n"
            "🏬 Ресторан\n"
            "🧾 Заказы принимаются заранее, еду можно забирать с собой\n"
            "⏰ Время работы: 10:00–23:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/202) (смотреть комментарии)\n"
            "📞 Телефон: +19294002252\n"
            "📞 +19296224444\n"
            "📞 +17186333006\n"
            "📱 Telegram: @urgutafsona1\n"
            "📱 Telegram: @afsona_admin"
        ),
        "text_channel": (
            "🍽️ <b>AFSONA</b>\n"
            "📍 <a href=\"https://www.google.com/maps?q=40.635753,-73.974489\">Brooklyn, NY</a>\n"
            "🏬 Ресторан\n"
            "🧾 Заказы принимаются заранее, еду можно забирать с собой\n"
            "⏰ Время работы: 10:00–23:00\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/202\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +19294002252\n"
            "📞 +19296224444\n"
            "📞 +17186333006\n"
            "📱 Telegram: @urgutafsona1\n"
            "📱 Telegram: @afsona_admin"
        )
    },
    {
        "name": "TASHKENT CUISINE",
        "lat": 40.44291300,
        "lng": -80.08243800,
        "text_user": (
            "🍽️ TASHKENT CUISINE\n"
            "📍 Pittsburgh, PA   (https://www.google.com/maps?q=40.44291300,-80.08243800)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 2–3 часа до доставки\n"
            "⏰ Время работы: 10:00 – 22:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/203) (смотреть комментарии)\n"
            "📞 Телефон: +14125190156\n"
            "📱 Telegram:"
        ),
        "text_channel": (
            "🍽️ <b>TASHKENT CUISINE</b>\n"
            "📍 <a href=\"https://www.google.com/maps?q=40.442913,-80.082438\">Pittsburgh, PA</a>\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 2–3 часа до доставки\n"
            "⏰ Время работы: 10:00 – 22:00\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/203\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +14125190156\n"
            "📱 Telegram:"
        )
    },
    {
        "name": "SILK ROAD (UZBEK - KAZAKH kitchen)",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ SILK ROAD (UZBEK - KAZAKH kitchen)\n"
            "📍 San Bernardino CA (https://maps.app.goo.gl/TEuoZoLezN8ZxmZg7)\n"
            "🚛 Фудтрак\n"
            "🧾 Заказы принимаются заранее\n"
            "⏰ Время работы: 08:00 – 23:00\n"
            "🚘 Доставка до 50 миль\n"
            "📋 Меню (https://t.me/myhalalmenu/204) (смотреть комментарии)\n"
            "📞 Телефон: +18722221736\n"
            "📱 Telegram: @silk_Road717\n"
            "📱 Telegram: @az_xxx_az"
        ),
        "text_channel": (
            "🍽️ <b>SILK ROAD (UZBEK - KAZAKH kitchen)</b>\n"
            "📍 San Bernardino CA\n"
            "🚛 Фудтрак\n"
            "🧾 Заказы принимаются заранее\n"
            "⏰ Время работы: 08:00 – 23:00\n"
            "🚘 Доставка до 50 миль\n"
            "📋 <a href=\"https://t.me/myhalalmenu/204\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +18722221736\n"
            "📱 Telegram: @silk_Road717\n"
            "📱 Telegram: @az_xxx_az"
        )
    },
    {
        "name": "UZBEK FOOD MINNESOTA",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ UZBEK FOOD MINNESOTA\n"
            "📍 Minneapolis, MN (https://goo.gl/maps/rhZpqnrhN1tJMiQMA)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 4–5 часов до доставки\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 08:00 – 22:00\n"
            "📋 Меню (https://t.me/myhalalmenu/206) (смотреть комментарии)\n"
            "📞 Телефон: +16513525551\n"
            "📱 Telegram: @Manzura_Burkhan"
        ),
        "text_channel": (
            "🍽️ <b>UZBEK FOOD MINNESOTA</b>\n"
            "📍 Minneapolis, MN\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 4–5 часов до доставки\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 08:00 – 22:00\n"
            "📋 <a href=\"https://t.me/myhalalmenu/206\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +16513525551\n"
            "📱 Telegram: @Manzura_Burkhan"
        )
    },
    {
        "name": "ОАЗИС ДЛЯ ТРАКЕРОВ",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ ОАЗИС ДЛЯ ТРАКЕРОВ\n"
            "📍 Dallas, TX\n"
            "🏠 Домашняя кухня на вынос и на доставку\n"
            "🧾 Заказ за 3–4 часа до получения\n"
            "🚘 Доставка свежей домашней еды прямо к вашей парковке\n"
            "⏰ Время работы: 24/7\n"
            "🌐 Меню (https://t.me/myhalalmenu/207) (смотреть комментарии)\n"
            "📞 Телефон: +13478881927\n"
            "📱 Telegram: @Ianaktx"
        ),
        "text_channel": (
            "🍽️ <b>ОАЗИС ДЛЯ ТРАКЕРОВ</b>\n"
            "📍 Dallas, TX\n"
            "🏠 Домашняя кухня на вынос и на доставку\n"
            "🧾 Заказ за 3–4 часа до получения\n"
            "🚘 Доставка свежей домашней еды прямо к вашей парковке\n"
            "⏰ Время работы: 24/7\n"
            "🌐 Меню (https://t.me/myhalalmenu/207) (смотреть комментарии)\n"
            "📞 Телефон: +13478881927\n"
            "📱 Telegram: @Ianaktx"
        )
    },
    {
        "name": "GOLDEN BY NUSAYBA",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ GOLDEN BY NUSAYBA\n"
            "📍 New Jersey, Lakewood (https://maps.app.goo.gl/N58gFq6UrewBrBWm7)\n"
            "🏠 Домашняя кухня\n"
            "🧾 Заказы для тракистов/траков готовятся за день заранее\n"
            "⏰ Время работы: 08:00 – 00:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/GoldenByNusaybaNJ) (смотреть комментарии)\n"
            "📞 Телефон: +13478137000\n"
            "📱 Telegram: @golden_by_nusayba\n"
            "📱 Instagram: @golden_by_nusayba_nj (https://www.instagram.com/golden_by_nusayba_nj)"
        ),
        "text_channel": (
            "🍽️ <b>GOLDEN BY NUSAYBA</b>\n"
            "📍 New Jersey, Lakewood\n"
            "🏠 Домашняя кухня\n"
            "🧾 Заказы для тракистов/траков готовятся за день заранее\n"
            "⏰ Время работы: 08:00 – 00:00\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/GoldenByNusaybaNJ\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +13478137000\n"
            "📱 Telegram: @golden_by_nusayba\n"
            "📱 Instagram: @golden_by_nusayba_nj (https://www.instagram.com/golden_by_nusayba_nj)"
        )
    },
    {
        "name": "UZBEKISTAN RESTAURANT",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ UZBEKISTAN RESTAURANT\n"
            "📍 Cincinnati OH (https://maps.app.goo.gl/28d42BXtNPUZ9D7GA?g_st=it)\n"
            "🏬 Ресторан/Кафе\n"
            "🧾 Заказы принимаются за 3–4 часа до доставки\n"
            "🚘 Доставка: 24/7\n"
            "⏰ Время работы: 10:00 - 23:00\n"
            "📋 Меню (https://t.me/myhalalmenu/209) (смотреть комментарии)\n"
            "📞 Телефон: +12674230301\n"
            "📱 Telegram:"
        ),
        "text_channel": (
            "🍽️ <b>UZBEKISTAN RESTAURANT</b>\n"
            "📍 Cincinnati OH\n"
            "🏬 Ресторан/Кафе\n"
            "🧾 Заказы принимаются за 3–4 часа до доставки\n"
            "🚘 Доставка: 24/7\n"
            "⏰ Время работы: 10:00 - 23:00\n"
            "📋 <a href=\"https://t.me/myhalalmenu/209\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +12674230301\n"
            "📱 Telegram:"
        )
    },
    {
        "name": "RAIANA HALAL FOOD",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ RAIANA HALAL FOOD\n"
            "📍 Sacramento, CA (https://maps.app.goo.gl/bgCVHfHMcR3hfdzx5)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 2–3 часа до доставки\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 24/7\n"
            "📋 Меню (https://t.me/myhalalmenu/210) (смотреть комментарии)\n"
            "📞 Телефон: +17732567187\n"
            "📞 +17732568893\n"
            "📱 Telegram: @Raiana_halal_food\n"
            "📱 Telegram: @Bakulya1986 (https://t.me/myhalal_food)"
        ),
        "text_channel": (
            "🍽️ <b>RAIANA HALAL FOOD</b>\n"
            "📍 Sacramento, CA\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 2–3 часа до доставки\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 24/7\n"
            "📋 <a href=\"https://t.me/myhalalmenu/210\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +17732567187\n"
            "📞 +17732568893\n"
            "📱 Telegram: @Raiana_halal_food\n"
            "📱 Telegram: @Bakulya1986 (https://t.me/myhalal_food)"
        )
    },
    {
        "name": "HALAL JASMIN KITCHEN",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ HALAL JASMIN KITCHEN\n"
            "📍 Kansas (https://maps.app.goo.gl/MTc7JWSzKxafXtH27)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 1.5–2 часа до доставки\n"
            "🚘 Бесплатная доставка по Kansas City\n"
            "⏰ Время работы: 09:00-00:00\n"
            "📋 Меню (https://t.me/myhalalmenu/211) (смотреть комментарии)\n"
            "📞 Телефон: +18162991870\n"
            "📱 Telegram: @Rozazhasmin"
        ),
        "text_channel": (
            "🍽️ <b>HALAL JASMIN KITCHEN</b>\n"
            "📍 Kansas\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 1.5–2 часа до доставки\n"
            "🚘 Бесплатная доставка по Kansas City\n"
            "⏰ Время работы: 09:00-00:00\n"
            "📋 <a href=\"https://t.me/myhalalmenu/211\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +18162991870\n"
            "📱 Telegram: @Rozazhasmin"
        )
    },
    {
        "name": "ATLAS KITCHEN",
        "lat": 38.85842400,
        "lng": -94.81290200,
        "text_user": (
            "🍽 ATLAS KITCHEN\n"
            "📍 Kansas City, KS/MO  (https://www.google.com/maps?q=38.85842400,-94.81290200)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 4–5 часов до доставки\n"
            "⏰ Время работы: 15:00 – 22:00\n"
            "🚘 Доставка: Договорная\n"
            "📋 Меню (https://t.me/myhalalmenu/212) (смотреть комментарии)\n"
            "📞 Телефон: +19134869109\n"
            "📞 +19899544770\n"
            "📱 Telegram: @Sabru_jamil1\n"
            "📱 Telegram: @Bek_KC"
        ),
        "text_channel": (
            "🍽️ <b>ATLAS KITCHEN</b>\n"
            "📍 <a href=\"https://www.google.com/maps?q=38.858424,-94.812902\">Kansas City, KS/MO</a>\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 4–5 часов до доставки\n"
            "⏰ Время работы: 15:00 – 22:00\n"
            "🚘 Доставка: Договорная\n"
            "📋 <a href=\"https://t.me/myhalalmenu/212\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +19134869109\n"
            "📞 +19899544770\n"
            "📱 Telegram: @Sabru_jamil1\n"
            "📱 Telegram: @Bek_KC"
        )
    },
    {
        "name": "HALAL FOOD MICHIGAN",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ HALAL FOOD MICHIGAN\n"
            "📍 Detroit MI (https://goo.gl/maps/3AgLsE9x4kPKLaFD9)\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 1/2 часа до доставки\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 07:00 - 11:00\n"
            "📋 Меню (https://t.me/myhalalmenu/213) (смотреть комментарии)\n"
            "📞 Телефон: +14153190954\n"
            "📞 +12489159760\n"
            "📱 Telegram: @Halal_food_Michigan"
        ),
        "text_channel": (
            "🍽️ <b>HALAL FOOD MICHIGAN</b>\n"
            "📍 Detroit MI\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы принимаются за 1/2 часа до доставки\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 07:00 - 11:00\n"
            "📋 <a href=\"https://t.me/myhalalmenu/213\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +14153190954\n"
            "📞 +12489159760\n"
            "📱 Telegram: @Halal_food_Michigan"
        )
    },
    {
        "name": "ISLOM HALAL FOOD",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ ISLOM HALAL FOOD\n"
            "📍 Nashville TN (https://maps.app.goo.gl/wNxhZDShHtEDFo5j9)\n"
            "🏠 Домашняя кухня на вынос и на доставку\n"
            "🧾 Время приготовления зависит от блюда\n"
            "⏰ Время работы: 24/7\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/214) (смотреть комментарии)\n"
            "📞 Телефон: +12159296717\n"
            "📞 +12159296707\n"
            "📞 +18352059595\n"
            "📱 Telegram: @islom_halol_food"
        ),
        "text_channel": (
            "🍽️ <b>ISLOM HALAL FOOD</b>\n"
            "📍 Nashville TN\n"
            "🏠 Домашняя кухня на вынос и на доставку\n"
            "🧾 Время приготовления зависит от блюда\n"
            "⏰ Время работы: 24/7\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/214\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +12159296717\n"
            "📞 +12159296707\n"
            "📞 +18352059595\n"
            "📱 Telegram: @islom_halol_food"
        )
    },
    {
        "name": "ROAD HOUSE",
        "lat": 37.40148600,
        "lng": -77.70871800,
        "text_user": (
            "🍽️ ROAD HOUSE\n"
            "📍 Richmond VA (https://www.google.com/maps?q=37.40148600,-77.70871800)\n"
            "🏠 Домашняя кухня\n"
            "🧾 Заказы принимаются за 3-4 часа до доставки\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 08:00 - 21:00\n"
            "📋 Меню (https://t.me/myhalalmenu/215) (смотреть комментарии)\n"
            "📞 Телефон: +18044713632\n"
            "📱 Telegram: @roadhouse_food"
        ),
        "text_channel": (
            "🍽️ <b>ROAD HOUSE</b>\n"
            "📍 <a href=\"https://www.google.com/maps?q=37.401486,-77.708718\">Richmond VA</a>\n"
            "🏠 Домашняя кухня\n"
            "🧾 Заказы принимаются за 3-4 часа до доставки\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 08:00 - 21:00\n"
            "📋 <a href=\"https://t.me/myhalalmenu/215\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +18044713632\n"
            "📱 Telegram: @roadhouse_food"
        )
    },
    {
        "name": "LAZIZ KITCHEN",
        "lat": 39.79012190,
        "lng": -104.90447310,
        "text_user": (
            "🍽️ LAZIZ KITCHEN\n"
            "📍 Denver, CO (https://www.google.com/maps?q=39.7901219,-104.9044731)\n"
            "🏠 Домашняя кухня\n"
            "🧾 Заказы принимаются за 2-3 часа до доставки\n"
            "🚘 Доставка до 100 миль\n"
            "⏰ Время работы: 09:00-23:00\n"
            "📋 Меню (https://t.me/myhalalmenu/217) (смотреть комментарии)\n"
            "📞 Телефон: +13035230449\n"
            "📱 Telegram: @Lazizakang"
        ),
        "text_channel": (
            "🍽️ <b>LAZIZ KITCHEN</b>\n"
            "📍 <a href=\"https://www.google.com/maps?q=39.7901219,-104.9044731\">Denver, CO</a>\n"
            "🏠 Домашняя кухня\n"
            "🧾 Заказы принимаются за 2-3 часа до доставки\n"
            "🚘 Доставка до 100 миль\n"
            "⏰ Время работы: 09:00-23:00\n"
            "📋 <a href=\"https://t.me/myhalalmenu/217\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +13035230449\n"
            "📱 Telegram: @Lazizakang"
        )
    },
    {
        "name": "KAZAN KEBAB (2)",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ KAZAN KEBAB (2)\n"
            "📍 Naperville, IL (https://maps.app.goo.gl/KG51Ru5ED5qskgCH9?g_st=atm)\n"
            "🚛 Фудтрак\n"
            "🧾 Все продукты готовы, можно перед заказом купить\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 24/7\n"
            "📋 Меню (https://t.me/myhalalmenu/218) (смотреть комментарии)\n"
            "📞 Телефон: +15517869980\n"
            "📱 Telegram: @Ali071188"
        ),
        "text_channel": (
            "🍽️ <b>KAZAN KEBAB (2)</b>\n"
            "📍 Naperville, IL\n"
            "🚛 Фудтрак\n"
            "🧾 Все продукты готовы, можно перед заказом купить\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 24/7\n"
            "📋 <a href=\"https://t.me/myhalalmenu/218\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +15517869980\n"
            "📱 Telegram: @Ali071188"
        )
    },
    {
        "name": "CHICAGO\'S BEST HALAL FOOD",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ CHICAGO\'S BEST HALAL FOOD\n"
            "📍 Chicago, IL (https://maps.app.goo.gl/UhR5Kp1mMQSvfnge8)\n"
            "🏠 Кухня на вынос из дома\n"
            "🧾 Заказы принимаются за 2 часа до доставки\n"
            "⏰ Время работы: 24/7\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/219) (смотреть комментарии)\n"
            "📞 Телефон: +12248449935\n"
            "📱 Telegram: @Mukhae_Akhmedova"
        ),
        "text_channel": (
            "🍽️ <b>CHICAGO\'S BEST HALAL FOOD</b>\n"
            "📍 Chicago, IL\n"
            "🏠 Кухня на вынос из дома\n"
            "🧾 Заказы принимаются за 2 часа до доставки\n"
            "⏰ Время работы: 24/7\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/219\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +12248449935\n"
            "📱 Telegram: @Mukhae_Akhmedova"
        )
    },
    {
        "name": "ANJIR UZBEK HALAL CUISINE",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ ANJIR UZBEK HALAL CUISINE\n"
            "📍 Chicago IL (https://maps.app.goo.gl/bhZmxu92nFs8CoaA7)\n"
            "🏬 Ресторан\n"
            "🧾 Все продукты готовы, можно перед заказом купить\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 11:00 - 23:00\n"
            "📋 Меню (https://www.anjir.restaurant/)\n"
            "📞 Телефон: +16305419004\n"
            "📱 Telegram: @Anjir_Halal_Restaurant"
        ),
        "text_channel": (
            "🍽️ <b>ANJIR UZBEK HALAL CUISINE</b>\n"
            "📍 Chicago IL\n"
            "🏬 Ресторан\n"
            "🧾 Все продукты готовы, можно перед заказом купить\n"
            "🚘 Доставка есть\n"
            "⏰ Время работы: 11:00 - 23:00\n"
            "📋 <a href=\"https://www.anjir.restaurant/\">Меню</a>\n"
            "📞 Телефон: +16305419004\n"
            "📱 Telegram: @Anjir_Halal_Restaurant"
        )
    },
    {
        "name": "HOUSE OF SHISHKEBABS",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ HOUSE OF SHISHKEBABS\n"
            "📍 Pittsburgh, PA (https://maps.app.goo.gl/sAscfUPLJfWdry5z7?g_st=ic)\n"
            "🏬 Ресторан\n"
            "🧾 Все продукты готовы, можно перед заказом купить\n"
            "⏰ Время работы 11:00-23:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/rivierapizza)\n"
            "📞 Телефон: +14129181536\n"
            "📱 Telegram: @DilmurodS"
        ),
        "text_channel": (
            "🍽️ <b>HOUSE OF SHISHKEBABS</b>\n"
            "📍 Pittsburgh, PA\n"
            "🏬 Ресторан\n"
            "🧾 Все продукты готовы, можно перед заказом купить\n"
            "⏰ Время работы 11:00-23:00\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/rivierapizza\">Меню</a>\n"
            "📞 Телефон: +14129181536\n"
            "📱 Telegram: @DilmurodS"
        )
    },
    {
        "name": "TARU KITCHEN",
        "lat": None,
        "lng": None,
        "text_user": (
            "🍽️ TARU KITCHEN\n"
            "📍 Illinois, IL\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Готовая продукция, можно купить сразу\n"
            "⏰ Время работы: 24/7\n"
            "🚘 Доставка есть\n"
            "📋 Меню (https://t.me/myhalalmenu/227) (смотреть комментарии)\n"
            "📞 Телефон: +19293181190\n"
            "📞 +19293010440\n"
            "📱 Telegram: @taru_us"
        ),
        "text_channel": (
            "🍽️ <b>TARU KITCHEN</b>\n"
            "📍 Illinois, IL\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Готовая продукция, можно купить сразу\n"
            "⏰ Время работы: 24/7\n"
            "🚘 Доставка есть\n"
            "📋 <a href=\"https://t.me/myhalalmenu/227\">Меню  (смотреть комментарии)</a>\n"
            "📞 Телефон: +19293181190\n"
            "📞 +19293010440\n"
            "📱 Telegram: @taru_us"
        )
    }
]

# ------------------- Telegram InitData Validation -------------------
def validate_telegram_data(init_data: str, bot_token: str) -> Optional[dict]:
    try:
        if not init_data:
            return None
        if init_data.startswith('demo_mode_'):
            return {"id": 0, "first_name": "Demo", "last_name": "User", "username": "demo", "language_code": "uz"}
        parsed_data = dict(parse_qsl(init_data))
        received_hash = parsed_data.pop('hash', None)
        if not received_hash:
            return None
        data_check_string = '\n'.join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        secret_key = hmac.new(key=b"WebAppData", msg=bot_token.encode(), digestmod=hashlib.sha256).digest()
        calculated_hash = hmac.new(key=secret_key, msg=data_check_string.encode(), digestmod=hashlib.sha256).hexdigest()
        if calculated_hash != received_hash:
            return None
        user = json.loads(parsed_data.get('user', '{}'))
        return user
    except Exception as e:
        logger.error(f"Validation error: {e}")
        return None

# ------------------- Geocoding -------------------

def extract_coords_from_url(url: str) -> Optional[tuple]:
    """Google Maps URL dan lat/lng ni ajratib olish - kengaytirilgan"""
    if not url:
        return None

    patterns = [
        # @lat,lng format (eng keng tarqalgan)
        r'@(-?\d+\.\d+),(-?\d+\.\d+)',
        # ?q=lat,lng format
        r'[?&]q=(-?\d+\.\d+),(-?\d+\.\d+)',
        # ll=lat,lng format
        r'll=(-?\d+\.\d+),(-?\d+\.\d+)',
        # /place/.../@lat,lng
        r'/place/[^/]+/@(-?\d+\.\d+),(-?\d+\.\d+)',
        # /maps?q=lat,lng
        r'maps\?.*q=(-?\d+\.\d+),(-?\d+\.\d+)',
        # /maps/place/.../@lat,lng
        r'/maps/place/[^/]+/@(-?\d+\.\d+),(-?\d+\.\d+)',
        # data=!4m2!3m1!1s0x... (Google Maps place ID format - skip)
        # But try to find coordinates anywhere in the URL
        r'(-?\d{2,3}\.\d{4,}),(-?\d{2,3}\.\d{4,})',
    ]

    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            lat = float(match.group(1))
            lng = float(match.group(2))
            # Validate coordinates
            if -90 <= lat <= 90 and -180 <= lng <= 180:
                logger.info(f"Coords extracted: lat={lat}, lng={lng}")
                return lat, lng

    return None

async def resolve_shortened_maps_link(url: str) -> Optional[tuple]:
    """Qisqartilgan Google Maps linkini kengaytirib koordinatalarni olish"""
    if not url:
        return None

    # URL ni tozalash (bo'sh joylar va keraksiz belgilarni olib tashlash)
    url = url.strip()

    # Avval to'liq URL dan koordinatalarni olish
    coords = extract_coords_from_url(url)
    if coords:
        return coords

    # Qisqa/Google linklarni kengaytirish
    maps_domains = ['goo.gl', 'maps.app.goo.gl', 'maps.google.com', 'google.com/maps']
    is_maps = any(domain in url.lower() for domain in maps_domains)

    if not is_maps:
        return None

    try:
        import aiohttp

        # Usul 1: HEAD so'rov bilan redirect URL ni olish (eng tez)
        head_headers = {
            'User-Agent': 'Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36',
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.head(
                    url,
                    allow_redirects=True,
                    max_redirects=15,
                    timeout=aiohttp.ClientTimeout(total=15),
                    headers=head_headers,
                ) as resp:
                    final_url = str(resp.url)
                    logger.info(f"[HEAD] Final URL: {final_url}")
                    coords = extract_coords_from_url(final_url)
                    if coords:
                        logger.info(f"[HEAD] Coords: {coords}")
                        return coords
        except Exception as e:
            logger.warning(f"[HEAD] Xato: {e}")

        # Usul 2: GET so'rov - redirect ni kuzatish, Location header orqali
        get_headers = {
            'User-Agent': 'Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
        }
        async with aiohttp.ClientSession(headers=get_headers) as session:
            # Redirect'siz so'rov - Location header ni o'qish
            try:
                async with session.get(
                    url,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    location = resp.headers.get('Location', '')
                    logger.info(f"[GET no-redirect] Status: {resp.status}, Location: {location}")
                    if location:
                        coords = extract_coords_from_url(location)
                        if coords:
                            logger.info(f"[Location header] Coords: {coords}")
                            return coords
            except Exception as e:
                logger.warning(f"[GET no-redirect] Xato: {e}")

            # Redirect bilan to'liq so'rov
            try:
                async with session.get(
                    url,
                    allow_redirects=True,
                    max_redirects=15,
                    timeout=aiohttp.ClientTimeout(total=25),
                ) as resp:
                    final_url = str(resp.url)
                    logger.info(f"[GET redirect] Final URL: {final_url}")

                    coords = extract_coords_from_url(final_url)
                    if coords:
                        logger.info(f"[GET redirect URL] Coords: {coords}")
                        return coords

                    # HTML content dan qidirish
                    if resp.status == 200:
                        text = await resp.text(errors='replace')
                        html_patterns = [
                            r'@(-?\d+\.\d+),(-?\d+\.\d+)',
                            r'!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)',
                            r'"lat":(-?\d+\.\d+),"lng":(-?\d+\.\d+)',
                            r'"latitude":(-?\d+\.\d+),"longitude":(-?\d+\.\d+)',
                            r'center=(-?\d+\.\d+),(-?\d+\.\d+)',
                            r'\[null,null,(-?\d+\.\d+),(-?\d+\.\d+)\]',
                            r'data=.*?!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)',
                            r'APP_INITIALIZATION_STATE.*?(-?\d{2,3}\.\d{4,}),(-?\d{2,3}\.\d{4,})',
                        ]
                        for pattern in html_patterns:
                            match = re.search(pattern, text)
                            if match:
                                lat = float(match.group(1))
                                lng = float(match.group(2))
                                if -90 <= lat <= 90 and -180 <= lng <= 180:
                                    logger.info(f"[HTML] Coords ({pattern[:20]}): {lat},{lng}")
                                    return lat, lng

                        logger.warning(f"[HTML] Koordinat topilmadi. HTML uzunligi: {len(text)}")
                    else:
                        logger.warning(f"[GET] HTTP {resp.status}")
            except Exception as e:
                logger.error(f"[GET redirect] Xato: {e}")

    except Exception as e:
        logger.error(f"resolve_shortened_maps_link: {type(e).__name__}: {e}")

    return None

async def geocode_address(address: str) -> Optional[tuple]:
    import aiohttp
    clean = re.sub(r'https?://\S+', '', address)
    clean = re.sub(r'[📍🍽️🏠🏬🚛🏪📞📱⏰🚘📋🧾❌❗️🅿️🌐—()]', '', clean).strip()
    if len(clean) < 3:
        clean = address.strip()[:50]
    try:
        url = "https://nominatim.openstreetmap.org/search"
        params = {"q": clean, "format": "json", "limit": 1}
        headers = {"User-Agent": "MyFoodMap/1.0"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data:
                        return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:
        logger.error(f"Geocoding error: {e}")
    return None

def parse_place_from_db(row) -> dict:
    r = dict(row)
    text = r.get('text_user') or r.get('text_channel') or ''

    # Use stored individual fields if available, otherwise parse from text
    phone = r.get('phone') or None
    telegram = r.get('telegram') or None
    menu_url = r.get('menu_url') or None
    address_name = r.get('address_name') or ''
    address_link = r.get('address_link') or ''
    details = r.get('details') or ''

    # Fallback: parse from text if DB fields are empty
    if not phone:
        for pattern in [r'📞\s*Телефон:\s*([+\d\s\-\(\)–]+)', r'📞\s*Telefon:\s*([+\d\s\-\(\)–]+)', r'📞\s*([+\d\s\-\(\)–]+)']:
            match = re.search(pattern, text)
            if match:
                phone = match.group(1).strip()
                break

    if not telegram:
        tg_match = re.search(r'📱\s*(?:Telegram:\s*)?(@[\w\d_]+)', text)
        telegram = tg_match.group(1) if tg_match else None

    if not menu_url:
        try:
            for pattern in [
                r'📋.*?\(?(https?://[^\s\)\n]+)',
                r'📋\s*<a\s+href=["\']([^"\'\)\n]+)["\']',
                r'(https?://[^\s\n]+(?:menu|menyu)[^\s\n]*)',
            ]:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    menu_url = match.group(1).strip().rstrip(')')
                    break
        except Exception:
            pass

    if not address_name:
        addr_match = re.search(r'📍\s*([^\n(]+)', text)
        address_name = addr_match.group(1).strip() if addr_match else r.get('name', 'Unknown')

    if not address_link:
        link_match = re.search(r'https?://[^\s)]+', text)
        address_link = link_match.group(0).strip() if link_match else ''

    if not details:
        details = extract_details(text) if text else ''

    time_match = re.search(r'⏰\s*([^\n]+)', text)
    work_time = time_match.group(1).strip() if time_match else None

    delivery = any(w in text.lower() for w in ['доставка', 'yetkazib', 'delivery', 'доставка есть', 'yetkazib berish'])

    return {
        "id": r.get('id'), "name": r.get('name', 'Unknown'), "description": text[:300] if text else '',
        "category": "food", "lat": r.get('lat'), "lng": r.get('lng'),
        "city": address_name, "address": address_name, "phone": phone, "telegram": telegram,
        "menu_url": menu_url, "work_time": work_time, "delivery": delivery,
        "text_user": text, "text_channel": r.get('text_channel', ''),
        "address_name": address_name, "address_link": address_link,
        "details": details, "maps_url": address_link
    }

# ------------------- API Handlers -------------------
async def get_places(request: web.Request) -> web.Response:
    try:
        search = request.query.get('search', '')
        if db_pool is None:
            return web.json_response({"success": False, "error": "Database not connected"}, status=500)
        async with db_pool.acquire() as conn:
            params, conditions = [], ["lat IS NOT NULL", "lng IS NOT NULL"]
            if search:
                conditions.append(f"LOWER(name) LIKE ${len(params)+1}")
                params.append(f"%{search.lower()}%")
            rows = await conn.fetch(f"SELECT * FROM places WHERE {' AND '.join(conditions)} ORDER BY id", *params)
            return web.json_response({"success": True, "data": [parse_place_from_db(r) for r in rows]})
    except Exception as e:
        logger.error(f"Get places error: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def get_all_places_list(request: web.Request) -> web.Response:
    try:
        search = request.query.get('search', '')
        if db_pool is None:
            return web.json_response({"success": False, "error": "Database not connected"}, status=500)
        async with db_pool.acquire() as conn:
            params, conditions = [], []
            if search:
                conditions.append(f"LOWER(name) LIKE ${len(params)+1}")
                params.append(f"%{search.lower()}%")
            where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            rows = await conn.fetch(f"SELECT * FROM places {where_clause} ORDER BY id", *params)
            return web.json_response({"success": True, "data": [parse_place_from_db(r) for r in rows]})
    except Exception as e:
        logger.error(f"Get all places error: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def get_nearby(request: web.Request) -> web.Response:
    try:
        lat, lng = float(request.query.get('lat', 0)), float(request.query.get('lng', 0))
        radius = float(request.query.get('radius', 50))
        if db_pool is None:
            return web.json_response({"success": False, "error": "Database not connected"}, status=500)
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT *, (6371 * acos(cos(radians($1)) * cos(radians(lat)) * cos(radians(lng) - radians($2)) + sin(radians($1)) * sin(radians(lat)))) AS distance
                FROM places WHERE lat IS NOT NULL AND lng IS NOT NULL
                HAVING distance <= $3 ORDER BY distance
            """, lat, lng, radius)
            places = []
            for row in rows:
                p = parse_place_from_db(row)
                p["distance"] = round(row['distance'], 1)
                places.append(p)
            return web.json_response({"success": True, "data": places})
    except Exception as e:
        logger.error(f"Get nearby error: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def admin_login(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        password = data.get('password', '')
        if not ADMIN_PASSWORD:
            return web.json_response({"success": False, "error": "Admin password not configured"}, status=500)
        if password != ADMIN_PASSWORD:
            return web.json_response({"success": False, "error": "Invalid password"}, status=401)
        token = generate_admin_token()
        admin_tokens.add(token)
        logger.info("🔐 Admin logged in via password")
        return web.json_response({
            "success": True, "token": token,
            "user": {"id": 0, "name": "Admin", "username": "admin", "language": "uz", "is_admin": True}
        })
    except Exception as e:
        logger.error(f"Admin login error: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def validate_user(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        user = await check_admin_auth(data)
        if not user:
            return web.json_response({"success": False, "error": "Invalid data"}, status=403)

        user_id = user.get('id')
        db_lang = await get_user_language(user_id) if user_id else user.get('language_code', 'uz')

        return web.json_response({
            "success": True,
            "user": {
                "id": user_id,
                "name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user.get('username', 'User'),
                "username": user.get('username'),
                "language": db_lang,
                "is_admin": user.get('is_admin', False)
            }
        })
    except Exception as e:
        logger.error(f"Validate error: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def update_user_language(request: web.Request) -> web.Response:
    """WebApp dan til o'zgarishini saqlash"""
    try:
        data = await request.json()
        user = await check_admin_auth(data)
        if not user:
            return web.json_response({"success": False, "error": "Unauthorized"}, status=403)

        user_id = user.get('id')
        if not user_id:
            return web.json_response({"success": False, "error": "Invalid user"}, status=400)

        language = data.get('language', 'uz')
        if language not in ('uz', 'ru', 'en'):
            return web.json_response({"success": False, "error": "Invalid language"}, status=400)

        await set_user_language(user_id, language)
        return web.json_response({"success": True, "language": language})
    except Exception as e:
        logger.error(f"Update language error: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def create_place(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        user = await check_admin_auth(data)
        if not user or not user.get('is_admin'):
            return web.json_response({"success": False, "error": "Unauthorized"}, status=403)

        place = data.get('place', {})

        # Extract individual fields
        lat = place.get('lat')
        lng = place.get('lng')
        address_name = place.get('address_name', place.get('address', ''))
        address_link = place.get('address_link', place.get('maps_url', ''))
        phone = place.get('phone', '')
        telegram = place.get('telegram', '')
        menu_url = place.get('menu_url', '')
        details = place.get('details', place.get('text_user', ''))

        # Auto-extract coords from address_link if not provided
        if (not lat or not lng) and address_link:
            coords = extract_coords_from_url(address_link)
            if coords:
                lat, lng = coords

        # Fallback: geocode by address name
        if (not lat or not lng) and address_name:
            coords = await geocode_address(address_name)
            if coords:
                lat, lng = coords

        # Build text_user and text_channel from individual fields
        build_data = {
            'name': place.get('name', ''),
            'address_name': address_name,
            'address_link': address_link,
            'phone': phone,
            'telegram': telegram,
            'menu_url': menu_url,
            'details': details,
            'lat': lat,
            'lng': lng,
            'category': place.get('category', 'food'),
        }
        text_user = build_text_user(build_data)
        text_channel = build_text_channel(build_data)

        if db_pool is None:
            return web.json_response({"success": False, "error": "Database not connected"}, status=500)

        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO places (name, lat, lng, text_user, text_channel, category,
                                  phone, telegram, menu_url, address_name, address_link, details)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12) RETURNING id
            """, place.get('name'), lat, lng, text_user, text_channel,
                 place.get('category', 'food'),
                 phone, telegram, menu_url, address_name, address_link, details)
            return web.json_response({"success": True, "id": row['id'], "lat": lat, "lng": lng})
    except Exception as e:
        logger.error(f"Create place error: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def update_place(request: web.Request) -> web.Response:
    try:
        place_id = int(request.match_info['id'])
        data = await request.json()
        user = await check_admin_auth(data)
        if not user or not user.get('is_admin'):
            return web.json_response({"success": False, "error": "Unauthorized"}, status=403)

        place = data.get('place', {})

        # Extract individual fields
        lat = place.get('lat')
        lng = place.get('lng')
        address_name = place.get('address_name', place.get('address', ''))
        address_link = place.get('address_link', place.get('maps_url', ''))
        phone = place.get('phone', '')
        telegram = place.get('telegram', '')
        menu_url = place.get('menu_url', '')
        details = place.get('details', place.get('text_user', ''))

        # Auto-extract coords from address_link if coords missing
        if (not lat or not lng) and address_link:
            coords = extract_coords_from_url(address_link)
            if coords:
                lat, lng = coords

        # Build text_user and text_channel from individual fields
        build_data = {
            'name': place.get('name', ''),
            'address_name': address_name,
            'address_link': address_link,
            'phone': phone,
            'telegram': telegram,
            'menu_url': menu_url,
            'details': details,
            'lat': lat,
            'lng': lng,
            'category': place.get('category', 'food'),
        }
        text_user = build_text_user(build_data)
        text_channel = build_text_channel(build_data)

        if db_pool is None:
            return web.json_response({"success": False, "error": "Database not connected"}, status=500)

        async with db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE places SET name=$1, lat=$2, lng=$3, text_user=$4, text_channel=$5, category=$6,
                                  phone=$7, telegram=$8, menu_url=$9, address_name=$10, address_link=$11, details=$12
                WHERE id=$13
            """, place.get('name'), lat, lng, text_user, text_channel,
                 place.get('category', 'food'),
                 phone, telegram, menu_url, address_name, address_link, details, place_id)
            return web.json_response({"success": True, "lat": lat, "lng": lng})
    except Exception as e:
        logger.error(f"Update place error: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def delete_place(request: web.Request) -> web.Response:
    try:
        place_id = int(request.match_info['id'])
        data = await request.json()
        user = await check_admin_auth(data)
        if not user or not user.get('is_admin'):
            return web.json_response({"success": False, "error": "Unauthorized"}, status=403)

        if db_pool is None:
            return web.json_response({"success": False, "error": "Database not connected"}, status=500)

        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM places WHERE id = $1", place_id)
            return web.json_response({"success": True})
    except Exception as e:
        logger.error(f"Delete place error: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def geocode_place(request: web.Request) -> web.Response:
    try:
        place_id = int(request.match_info['id'])
        data = await request.json()
        user = await check_admin_auth(data)
        if not user or not user.get('is_admin'):
            return web.json_response({"success": False, "error": "Unauthorized"}, status=403)

        if db_pool is None:
            return web.json_response({"success": False, "error": "Database not connected"}, status=500)

        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT id, name, text_user, text_channel, address_name, address_link FROM places WHERE id = $1", place_id)
            if not row:
                return web.json_response({"success": False, "error": "Place not found"}, status=404)

            # First try to extract coords from address_link
            coords = None
            addr_link = row.get('address_link') or ''
            if addr_link:
                coords = extract_coords_from_url(addr_link)

            # Fallback: parse from text_user
            if not coords:
                text = row['text_user'] or row['text_channel'] or ''
                link_match = re.search(r'https?://[^\s)]+', text)
                if link_match:
                    coords = extract_coords_from_url(link_match.group(0))

            # Last resort: geocode by address name
            if not coords:
                addr_name = row.get('address_name') or row['name']
                if not addr_name:
                    text = row['text_user'] or row['text_channel'] or ''
                    addr_match = re.search(r'📍\s*([^\n(]+)', text)
                    addr_name = addr_match.group(1).strip() if addr_match else row['name']
                coords = await geocode_address(addr_name)

            if coords:
                await conn.execute("UPDATE places SET lat = $1, lng = $2 WHERE id = $3", coords[0], coords[1], place_id)
                return web.json_response({"success": True, "lat": coords[0], "lng": coords[1]})
            return web.json_response({"success": False, "error": "Could not geocode address"})
    except Exception as e:
        logger.error(f"Geocode place error: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def geocode_all_places(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        user = await check_admin_auth(data)
        if not user or not user.get('is_admin'):
            return web.json_response({"success": False, "error": "Unauthorized"}, status=403)

        if db_pool is None:
            return web.json_response({"success": False, "error": "Database not connected"}, status=500)

        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT id, name, text_user, text_channel, address_name, address_link FROM places WHERE lat IS NULL OR lng IS NULL")
            updated, failed = 0, 0
            for row in rows:
                coords = None

                # First try to extract coords from address_link
                addr_link = row.get('address_link') or ''
                if addr_link:
                    coords = extract_coords_from_url(addr_link)

                # Fallback: parse from text_user
                if not coords:
                    text = row['text_user'] or row['text_channel'] or ''
                    link_match = re.search(r'https?://[^\s)]+', text)
                    if link_match:
                        coords = extract_coords_from_url(link_match.group(0))

                # Last resort: geocode by address name
                if not coords:
                    addr_name = row.get('address_name') or row['name']
                    if not addr_name:
                        text = row['text_user'] or row['text_channel'] or ''
                        addr_match = re.search(r'📍\s*([^\n(]+)', text)
                        addr_name = addr_match.group(1).strip() if addr_match else row['name']
                    coords = await geocode_address(addr_name)
                    await asyncio.sleep(1.1)

                if coords:
                    await conn.execute("UPDATE places SET lat = $1, lng = $2 WHERE id = $3", coords[0], coords[1], row['id'])
                    updated += 1
                else:
                    failed += 1
            return web.json_response({"success": True, "updated": updated, "failed": failed})
    except Exception as e:
        logger.error(f"Geocode all error: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def resolve_maps_link(request: web.Request) -> web.Response:
    """Qisqartilgan yoki to'liq Google Maps linkdan koordinatalarni olib olish"""
    try:
        data = await request.json()
        
        # Admin autentifikatsiya tekshirish
        user = await check_admin_auth(data)
        if not user or not user.get('is_admin'):
            return web.json_response({"success": False, "error": "Unauthorized"}, status=403)
        
        url = data.get('url', '').strip()
        if not url:
            return web.json_response({"success": False, "error": "URL required"}, status=400)

        # URL ni to'liq tozalash: bo'sh joylar, tab, yangi qator
        url = url.strip().rstrip('  \t\n\r')
        
        logger.info(f"resolve_maps_link: URL={repr(url)}")
        
        # 1. To'g'ridan-to'g'ri URL tahlil
        coords = extract_coords_from_url(url)
        if coords:
            logger.info(f"Coords from direct parse: {coords}")
            return web.json_response({"success": True, "lat": coords[0], "lng": coords[1], "method": "direct"})
        
        # 2. Qisqartilgan linkni kengaytirish
        logger.info(f"Direct parse failed, trying redirect resolve...")
        coords = await resolve_shortened_maps_link(url)
        if coords:
            logger.info(f"Coords from redirect: {coords}")
            return web.json_response({"success": True, "lat": coords[0], "lng": coords[1], "method": "redirect"})
        
        logger.warning(f"All methods failed for URL: {url}")
        return web.json_response({
            "success": False, 
            "error": "Koordinatalar topilmadi. Manzil nomini kiriting yoki koordinatni qo'lda kiriting.",
            "url": url
        }, status=400)
        
    except Exception as e:
        logger.error(f"resolve_maps_link exception: {type(e).__name__}: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def debug_db(request: web.Request) -> web.Response:
    try:
        if db_pool is None:
            return web.json_response({"success": False, "error": "Database pool not initialized"}, status=500)
        async with db_pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM places")
            null_coords = await conn.fetchval("SELECT COUNT(*) FROM places WHERE lat IS NULL OR lng IS NULL")
            with_coords = await conn.fetchval("SELECT COUNT(*) FROM places WHERE lat IS NOT NULL AND lng IS NOT NULL")
            sample = await conn.fetch("SELECT id, name, lat, lng, text_user FROM places LIMIT 5")
            return web.json_response({
                "success": True, "total_places": total, "null_coordinates": null_coords,
                "with_coordinates": with_coords, "sample": [dict(r) for r in sample]
            })
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def debug_raw_places(request: web.Request) -> web.Response:
    try:
        if db_pool is None:
            return web.json_response({"success": False, "error": "Database pool not initialized"}, status=500)
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT id, name, lat, lng, text_user FROM places ORDER BY id")
            return web.json_response({"success": True, "count": len(rows), "places": [dict(r) for r in rows]})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)

# ------------------- CORS Middleware -------------------
@web.middleware
async def cors_middleware(request, handler):
    if request.method == 'OPTIONS':
        return web.Response(headers={
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type, Authorization',
        })
    response = await handler(request)
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response

# ------------------- Telegram Bot -------------------
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

async def show_main_menu(message: types.Message, user_id: int, lang: str, is_admin: bool):
    """Asosiy menyuni tilga qarab ko'rsatish"""
    texts = {
        'uz': {
            'welcome': "🗺 <b>My Food Map</b>\n\nXaritadan restoran va xizmatlarni toping!",
            'admin': "\n\n👨‍💼 <b>Admin rejimi</b> faol.",
            'map_btn': "🗺 Xaritani ochish",
            'admin_btn': "👨‍💼 Admin Panel"
        },
        'ru': {
            'welcome': "🗺 <b>My Food Map</b>\n\nНайдите рестораны и услуги на карте!",
            'admin': "\n\n👨‍💼 <b>Режим администратора</b> активен.",
            'map_btn': "🗺 Открыть карту",
            'admin_btn': "👨‍💼 Админ панель"
        },
        'en': {
            'welcome': "🗺 <b>My Food Map</b>\n\nFind restaurants and services on the map!",
            'admin': "\n\n👨‍💼 <b>Admin mode</b> active.",
            'map_btn': "🗺 Open Map",
            'admin_btn': "👨‍💼 Admin Panel"
        }
    }
    t = texts.get(lang, texts['uz'])
    text = t['welcome']
    if is_admin:
        text += t['admin']

    web_app_url = WEBAPP_URL
    if web_app_url.endswith('index.html'):
        admin_url = web_app_url.replace('index.html', 'admin.html')
    elif web_app_url.endswith('/'):
        admin_url = web_app_url + 'admin.html'
    else:
        admin_url = web_app_url + '/admin.html'

    web_app = types.WebAppInfo(url=web_app_url)
    admin_web_app = types.WebAppInfo(url=admin_url)

    keyboard = []
    if is_admin:
        keyboard.append([types.InlineKeyboardButton(text=t['admin_btn'], web_app=admin_web_app)])
    keyboard.append([types.InlineKeyboardButton(text=t['map_btn'], web_app=web_app)])

    markup = types.InlineKeyboardMarkup(inline_keyboard=keyboard)
    await message.answer(text, reply_markup=markup)

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    is_admin = user_id in ADMIN_IDS
    
    # Foydalanuvchi bazada bormi?
    user_exists = False
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("SELECT 1 FROM users WHERE user_id = $1", user_id)
                user_exists = row is not None
        except Exception as e:
            logger.error(f"Check user exists error: {e}")
    
    if not user_exists:
        # TIL TANLASH (birinch marta)
        tg_lang = message.from_user.language_code or 'uz'
        prompts = {
            'uz': "🌐 Tilni tanlang:",
            'ru': "🌐 Выберите язык:",
            'en': "🌐 Select language:"
        }
        prompt = prompts.get(tg_lang, prompts['uz'])
        
        markup = types.InlineKeyboardMarkup(inline_keyboard=[
            [
                types.InlineKeyboardButton(text="🇺🇿 O'zbekcha", callback_data="lang:uz"),
                types.InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang:ru"),
                types.InlineKeyboardButton(text="🇬🇧 English", callback_data="lang:en")
            ]
        ])
        await message.answer(prompt, reply_markup=markup)
        return
    
    # Saqlangan tilni olish va menyuni ko'rsatish
    lang = await get_user_language(user_id)
    await show_main_menu(message, user_id, lang, is_admin)

@dp.callback_query(lambda c: c.data.startswith('lang:'))
async def process_language(callback_query: types.CallbackQuery):
    lang = callback_query.data.split(':')[1]
    user_id = callback_query.from_user.id
    is_admin = user_id in ADMIN_IDS
    
    await set_user_language(user_id, lang)
    
    answers = {
        'uz': "✅ O'zbek tili tanlandi",
        'ru': "✅ Выбран русский язык",
        'en': "✅ English selected"
    }
    await callback_query.answer(answers.get(lang, answers['uz']))
    await callback_query.message.delete()
    
    # Asosiy menyuni tanlangan tilda ko'rsatish
    await show_main_menu(callback_query.message, user_id, lang, is_admin)

async def health_check(request: web.Request) -> web.Response:
    try:
        if db_pool is None:
            return web.json_response({"success": False, "error": "Database pool not initialized"}, status=500)
        async with db_pool.acquire() as conn:
            cols = await conn.fetch("SELECT column_name FROM information_schema.columns WHERE table_name = 'places'")
            count = await conn.fetchval("SELECT COUNT(*) FROM places")
            return web.json_response({
                "success": True, "db_connected": True, "places_count": count,
                "columns": [r['column_name'] for r in cols]
            })
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)

# ------------------- Main -------------------
async def init_app():
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get('/api/places', get_places)
    app.router.add_get('/api/places/all', get_all_places_list)
    app.router.add_get('/api/nearby', get_nearby)
    app.router.add_post('/api/validate', validate_user)
    app.router.add_post('/api/admin/login', admin_login)
    app.router.add_post('/api/user/language', update_user_language)  # TIL YANGILASH
    app.router.add_post('/api/places', create_place)
    app.router.add_post('/api/places/{id}/geocode', geocode_place)
    app.router.add_post('/api/admin/geocode-all', geocode_all_places)
    app.router.add_post('/api/admin/resolve-link', resolve_maps_link)  # LINK KENGAYTIRISH
    app.router.add_put('/api/places/{id}', update_place)
    app.router.add_delete('/api/places/{id}', delete_place)
    app.router.add_get('/api/debug/db', debug_db)
    app.router.add_get('/api/debug/raw', debug_raw_places)
    app.router.add_get('/api/health', health_check)
    return app

async def main():
    await init_db()
    runner = web.AppRunner(await init_app())
    await runner.setup()
    site = web.TCPSite(runner, BOT_WEBHOOK_HOST, BOT_WEBHOOK_PORT)
    logger.info(f"🚀 Server started on http://{BOT_WEBHOOK_HOST}:{BOT_WEBHOOK_PORT}")
    await asyncio.gather(site.start(), dp.start_polling(bot, skip_updates=True))

if __name__ == "__main__":
    asyncio.run(main())
