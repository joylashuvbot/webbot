import os
import json
import hmac
import hashlib
import logging
import re
from typing import Optional, Dict, Any, List
from urllib.parse import parse_qsl
import asyncio
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart
from dotenv import load_dotenv

load_dotenv()

# ------------------- Konfiguratsiya -------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = set(int(i.strip()) for i in os.getenv("ADMIN_ID", "").split(",") if i.strip())
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:CCvagWGwtueLwCUyfaEbkPQvSDOHoygR@caboose.proxy.rlwy.net:48751/railway")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
BOT_WEBHOOK_HOST = os.getenv("BOT_WEBHOOK_HOST", "0.0.0.0")
BOT_WEBHOOK_PORT = int(os.getenv("BOT_WEBHOOK_PORT", "8080"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ------------------- Database -------------------
db_pool: Optional[asyncpg.Pool] = None

async def init_db():
    """Bazaga ulanish va jadvallarni yaratish (yoki yangilash)"""
    global db_pool
    try:
        logger.info(f"🔗 Connecting to DB...")
        logger.info(f"🔗 DATABASE_URL starts with: {DATABASE_URL[:50]}...")

        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
        async with db_pool.acquire() as conn:
            # 1. Places jadvalini yaratish (agar yo'q bo'lsa)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS places (
                    id SERIAL PRIMARY KEY
                )
            """)

            # 2. Barcha kerakli ustunlarni tekshirish va qo'shish
            columns = await conn.fetch("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = 'places' AND table_schema = 'public'
            """)
            existing_cols = {row['column_name'] for row in columns}
            logger.info(f"📋 Mavjud ustunlar: {existing_cols}")

            required_columns = {
                'name': "TEXT NOT NULL DEFAULT ''",
                'lat': 'DOUBLE PRECISION',
                'lng': 'DOUBLE PRECISION',
                'text_user': "TEXT NOT NULL DEFAULT ''",
                'text_channel': "TEXT NOT NULL DEFAULT ''",
            }

            for col_name, col_type in required_columns.items():
                if col_name not in existing_cols:
                    logger.info(f"➕ Adding '{col_name}' column to places table...")
                    await conn.execute(f"ALTER TABLE places ADD COLUMN {col_name} {col_type}")

            # 3. Indexlarni xavfsiz yaratish
            await conn.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_indexes 
                        WHERE indexname = 'idx_places_name'
                    ) THEN
                        CREATE INDEX idx_places_name ON places(name);
                    END IF;

                    IF NOT EXISTS (
                        SELECT 1 FROM pg_indexes 
                        WHERE indexname = 'idx_places_coords'
                    ) THEN
                        CREATE INDEX idx_places_coords ON places(lat, lng);
                    END IF;
                END $$;
            """)

            # 4. Bazada ma'lumot borligini tekshirish
            count = await conn.fetchval("SELECT COUNT(*) FROM places")
            logger.info(f"✅ DB ulanish OK. 'places' jadvalida {count} ta joy bor.")

            if count == 0:
                logger.info("📥 Baza bo'sh. Initial restoranlarni qo'shish...")
                await load_initial_places(conn)
                count = await conn.fetchval("SELECT COUNT(*) FROM places")
                logger.info(f"✅ Initial restoranlar qo'shildi. Jami: {count} ta.")
            else:
                null_count = await conn.fetchval("SELECT COUNT(*) FROM places WHERE lat IS NULL OR lng IS NULL")
                with_coords = await conn.fetchval("SELECT COUNT(*) FROM places WHERE lat IS NOT NULL AND lng IS NOT NULL")
                logger.info(f"✅ Koordinatasiz joylar: {null_count}")
                logger.info(f"✅ Koordinatali joylar: {with_coords}")

    except Exception as e:
        logger.error(f"Database init error: {e}")
        raise

async def load_initial_places(conn):
    """Baza bo'sh bo'lsa, initial restoranlarni qo'shish va koordinatalarini avto-topish"""
    for place in INITIAL_PLACES:
        lat = place.get("lat")
        lng = place.get("lng")

        # Agar koordinata yo'q bo'lsa, manzilni geocode qilishga urinish
        if lat is None or lng is None:
            text = place.get("text_user") or place.get("text_channel") or ""
            addr_match = re.search(r'📍\s*([^\n]+)', text)
            address = addr_match.group(1).strip() if addr_match else place["name"]

            coords = await geocode_address(address)
            if coords:
                lat, lng = coords
                logger.info(f"📍 Auto-geocoded '{place['name']}' -> {lat}, {lng}")
            else:
                logger.warning(f"❌ Failed to geocode '{place['name']}'")

            # Nominatim: 1 soniyada 1 ta so'rov (rate limit)
            await asyncio.sleep(1.1)

        await conn.execute("""
            INSERT INTO places (name, lat, lng, text_user, text_channel)
            VALUES ($1, $2, $3, $4, $5)
        """, place["name"], lat, lng, 
            place["text_user"], place["text_channel"])

async def close_db():
    if db_pool:
        await db_pool.close()

# ------------------- Initial Places (Restoranlar ro'yxati) -------------------
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
    }
]

# ------------------- Telegram InitData Validation -------------------
def validate_telegram_data(init_data: str, bot_token: str) -> Optional[dict]:
    """Telegram WebApp init_data ni tekshirish"""
    try:
        if not init_data:
            return None

        # Demo rejim tekshiruvi - agar init_data demo_mode bilan boshlansa
        if init_data.startswith('demo_mode_'):
            logger.info("🎮 Demo mode detected, skipping hash validation")
            return {
                "id": 0,
                "first_name": "Demo",
                "last_name": "User",
                "username": "demo",
                "language_code": "uz"
            }

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

async def geocode_address(address: str) -> Optional[tuple]:
    """OpenStreetMap Nominatim orqali manzilni koordinataga o'girish"""
    import aiohttp

    # Manzildan URL, emoji va ortiqcha belgilarni tozalash
    clean = re.sub(r'https?://\S+', '', address)
    clean = re.sub(r'[📍🍽️🏠🏬🚛🏪📞📱⏰🚘📋🧾❌❗️🅿️🌐—()]', '', clean)
    clean = clean.strip()

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
                    if data and len(data) > 0:
                        return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:
        logger.error(f"Geocoding error for '{clean}': {e}")
    return None

def parse_place_from_db(row) -> dict:
    """
    Food bot bazasidagi row ni WebApp formatiga o'tkazish.
    """
    r = dict(row)
    text = r.get('text_user') or r.get('text_channel') or ''

    # Telefon raqamini ajratib olish
    phone = None
    phone_patterns = [
        r'📞\s*Телефон:\s*([+\d\s\-\(\)–]+)',
        r'📞\s*Telefon:\s*([+\d\s\-\(\)–]+)',
        r'📞\s*([+\d\s\-\(\)–]+)',
    ]
    for pattern in phone_patterns:
        match = re.search(pattern, text)
        if match:
            phone = match.group(1).strip()
            break

    # Telegramni ajratib olish
    tg_match = re.search(r'📱\s*(?:Telegram:\s*)?(@[\w\d_]+)', text)
    telegram = tg_match.group(1) if tg_match else None

    # Menyu havolasini ajratib olish
    menu_url = None
    menu_patterns = [
        r'📋.*?\(?(https?://[^\s\)\n]+)',
        r'📋\s*<a\s+href=["\'][^"\'\)\n]+["\']',
        r'https?://[^\s\n]+(?:menu|menyu)[^\s\n]*',
    ]
    for pattern in menu_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            menu_url = match.group(1).strip().rstrip(')')
            break

    # Manzilni ajratib olish
    addr_match = re.search(r'📍\s*([^\n]+)', text)
    address = addr_match.group(1).strip() if addr_match else r.get('name', 'Unknown')

    # Ish vaqtini ajratib olish
    time_match = re.search(r'⏰\s*([^\n]+)', text)
    work_time = time_match.group(1).strip() if time_match else None

    # Yetkazib berish bormi?
    delivery_keywords = ['доставка', 'yetkazib', 'delivery', 'доставка есть', 'yetkazib berish']
    delivery = any(word in text.lower() for word in delivery_keywords)

    return {
        "id": r.get('id'),
        "name": r.get('name', 'Unknown'),
        "description": text[:300] if text else '',
        "category": "food",
        "lat": r.get('lat'),
        "lng": r.get('lng'),
        "city": address,
        "address": address,
        "phone": phone,
        "telegram": telegram,
        "menu_url": menu_url,
        "work_time": work_time,
        "delivery": delivery
    }

# ------------------- API Handlers -------------------
async def get_places(request: web.Request) -> web.Response:
    """Barcha joylarni olish — faqat to'liq koordinatali joylarni"""
    try:
        search = request.query.get('search', '')

        # Check if db_pool is initialized
        if db_pool is None:
            logger.error("❌ Database pool is not initialized!")
            return web.json_response({"success": False, "error": "Database not connected"}, status=500)

        async with db_pool.acquire() as conn:
            params = []
            conditions = ["lat IS NOT NULL", "lng IS NOT NULL"]

            if search:
                conditions.append(f"LOWER(name) LIKE ${len(params)+1}")
                params.append(f"%{search.lower()}%")

            query = f"SELECT * FROM places WHERE {' AND '.join(conditions)} ORDER BY id"
            rows = await conn.fetch(query, *params)
            places = [parse_place_from_db(row) for row in rows]

            logger.info(f"✅ API /places: {len(places)} places returned")
            return web.json_response({"success": True, "data": places})
    except Exception as e:
        logger.error(f"Get places error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def get_all_places_list(request: web.Request) -> web.Response:
    """Barcha joylarni ro'yxat ko'rinishida olish (koordinatasizlar ham)"""
    try:
        search = request.query.get('search', '')

        if db_pool is None:
            return web.json_response({"success": False, "error": "Database not connected"}, status=500)

        async with db_pool.acquire() as conn:
            params = []
            conditions = []
            if search:
                conditions.append(f"LOWER(name) LIKE ${len(params)+1}")
                params.append(f"%{search.lower()}%")

            where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            rows = await conn.fetch(f"SELECT * FROM places {where_clause} ORDER BY id", *params)
            places = [parse_place_from_db(row) for row in rows]
            return web.json_response({"success": True, "data": places})
    except Exception as e:
        logger.error(f"Get all places error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def get_nearby(request: web.Request) -> web.Response:
    """Yaqin atrofdagi joylarni olish"""
    try:
        lat = float(request.query.get('lat', 0))
        lng = float(request.query.get('lng', 0))
        radius = float(request.query.get('radius', 50))

        if db_pool is None:
            return web.json_response({"success": False, "error": "Database not connected"}, status=500)

        async with db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM (
                    SELECT *, (
                        6371 * acos(
                            cos(radians($1)) * cos(radians(lat)) *
                            cos(radians(lng) - radians($2)) +
                            sin(radians($1)) * sin(radians(lat))
                        )
                    ) AS distance
                    FROM places
                    WHERE lat IS NOT NULL AND lng IS NOT NULL
                ) sub
                WHERE distance <= $3
                ORDER BY distance
            """, lat, lng, radius)

            places = []
            for row in rows:
                place = parse_place_from_db(row)
                place["distance"] = round(row['distance'], 1)
                places.append(place)

            return web.json_response({"success": True, "data": places})
    except Exception as e:
        logger.error(f"Get nearby error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def validate_user(request: web.Request) -> web.Response:
    """Foydalanuvchi ma'lumotlarini tekshirish"""
    try:
        data = await request.json()
        init_data = data.get('init_data', '')

        user = validate_telegram_data(init_data, BOT_TOKEN)
        if not user:
            return web.json_response({"success": False, "error": "Invalid data"}, status=403)

        user_id = user.get('id')
        is_admin = user_id in ADMIN_IDS

        return web.json_response({
            "success": True,
            "user": {
                "id": user_id,
                "name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip(),
                "username": user.get('username'),
                "language": user.get('language_code', 'en'),
                "is_admin": is_admin
            }
        })
    except Exception as e:
        logger.error(f"Validate error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def create_place(request: web.Request) -> web.Response:
    """Yangi joy qo'shish (faqat admin)"""
    try:
        data = await request.json()
        init_data = data.get('init_data', '')

        user = validate_telegram_data(init_data, BOT_TOKEN)
        if not user or user.get('id') not in ADMIN_IDS:
            return web.json_response({"success": False, "error": "Unauthorized"}, status=403)

        place = data.get('place', {})

        # Agar koordinatalar berilmagan bo'lsa, manzilni geocode qilishga urinish
        lat = place.get('lat')
        lng = place.get('lng')
        address = place.get('address', '')

        if (not lat or not lng) and address:
            coords = await geocode_address(address)
            if coords:
                lat, lng = coords
                logger.info(f"📍 Geocoded '{address}' -> {lat}, {lng}")

        if db_pool is None:
            return web.json_response({"success": False, "error": "Database not connected"}, status=500)

        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO places (name, lat, lng, text_user, text_channel)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
            """, 
                place.get('name'),
                lat,
                lng,
                place.get('text_user', ''),
                place.get('text_channel', '')
            )

            return web.json_response({"success": True, "id": row['id'], "lat": lat, "lng": lng})
    except Exception as e:
        logger.error(f"Create place error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def update_place(request: web.Request) -> web.Response:
    """Joyni yangilash"""
    try:
        place_id = int(request.match_info['id'])
        data = await request.json()
        init_data = data.get('init_data', '')

        user = validate_telegram_data(init_data, BOT_TOKEN)
        if not user or user.get('id') not in ADMIN_IDS:
            return web.json_response({"success": False, "error": "Unauthorized"}, status=403)

        place = data.get('place', {})

        if db_pool is None:
            return web.json_response({"success": False, "error": "Database not connected"}, status=500)

        async with db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE places SET
                    name = $1, lat = $2, lng = $3, 
                    text_user = $4, text_channel = $5
                WHERE id = $6
            """,
                place.get('name'), place.get('lat'), place.get('lng'),
                place.get('text_user', ''), place.get('text_channel', ''),
                place_id
            )
            return web.json_response({"success": True})
    except Exception as e:
        logger.error(f"Update place error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def delete_place(request: web.Request) -> web.Response:
    """Joyni o'chirish"""
    try:
        place_id = int(request.match_info['id'])
        data = await request.json()
        init_data = data.get('init_data', '')

        user = validate_telegram_data(init_data, BOT_TOKEN)
        if not user or user.get('id') not in ADMIN_IDS:
            return web.json_response({"success": False, "error": "Unauthorized"}, status=403)

        if db_pool is None:
            return web.json_response({"success": False, "error": "Database not connected"}, status=500)

        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM places WHERE id = $1", place_id)
            return web.json_response({"success": True})
    except Exception as e:
        logger.error(f"Delete place error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def geocode_place(request: web.Request) -> web.Response:
    """Mavjud joyni manzili bo'yicha geocode qilish (admin)"""
    try:
        place_id = int(request.match_info['id'])
        data = await request.json()
        init_data = data.get('init_data', '')

        user = validate_telegram_data(init_data, BOT_TOKEN)
        if not user or user.get('id') not in ADMIN_IDS:
            return web.json_response({"success": False, "error": "Unauthorized"}, status=403)

        if db_pool is None:
            return web.json_response({"success": False, "error": "Database not connected"}, status=500)

        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT id, name, text_user, text_channel FROM places WHERE id = $1", place_id)
            if not row:
                return web.json_response({"success": False, "error": "Place not found"}, status=404)

            text = row['text_user'] or row['text_channel'] or ''
            addr_match = re.search(r'📍\s*([^\n]+)', text)
            address = addr_match.group(1).strip() if addr_match else row['name']

            coords = await geocode_address(address)
            if coords:
                await conn.execute("UPDATE places SET lat = $1, lng = $2 WHERE id = $3", coords[0], coords[1], place_id)
                return web.json_response({"success": True, "lat": coords[0], "lng": coords[1]})
            else:
                return web.json_response({"success": False, "error": "Could not geocode address"})
    except Exception as e:
        logger.error(f"Geocode place error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def geocode_all_places(request: web.Request) -> web.Response:
    """Barcha koordinatasiz joylarni avtomatik geocode qilish (admin)"""
    try:
        data = await request.json()
        init_data = data.get('init_data', '')

        user = validate_telegram_data(init_data, BOT_TOKEN)
        if not user or user.get('id') not in ADMIN_IDS:
            return web.json_response({"success": False, "error": "Unauthorized"}, status=403)

        if db_pool is None:
            return web.json_response({"success": False, "error": "Database not connected"}, status=500)

        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT id, name, text_user, text_channel FROM places WHERE lat IS NULL OR lng IS NULL")

            updated = 0
            failed = 0

            for row in rows:
                text = row['text_user'] or row['text_channel'] or ''
                addr_match = re.search(r'📍\s*([^\n]+)', text)
                address = addr_match.group(1).strip() if addr_match else row['name']

                coords = await geocode_address(address)
                if coords:
                    await conn.execute("UPDATE places SET lat = $1, lng = $2 WHERE id = $3", coords[0], coords[1], row['id'])
                    updated += 1
                    logger.info(f"✅ Geocoded place {row['id']} '{row['name']}' -> {coords}")
                else:
                    failed += 1
                    logger.warning(f"❌ Failed to geocode place {row['id']} '{row['name']}' with address '{address}'")

                # Nominatim rate limit: max 1 request per second
                import asyncio
                await asyncio.sleep(1.1)

            return web.json_response({"success": True, "updated": updated, "failed": failed})
    except Exception as e:
        logger.error(f"Geocode all error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return web.json_response({"success": False, "error": str(e)}, status=500)

# ------------------- DEBUG Endpoints -------------------
async def debug_db(request: web.Request) -> web.Response:
    """Debug: bazadagi ma'lumotlar haqida umumiy ma'lumot"""
    try:
        if db_pool is None:
            return web.json_response({"success": False, "error": "Database pool not initialized"}, status=500)

        async with db_pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM places")
            null_coords = await conn.fetchval("SELECT COUNT(*) FROM places WHERE lat IS NULL OR lng IS NULL")
            with_coords = await conn.fetchval("SELECT COUNT(*) FROM places WHERE lat IS NOT NULL AND lng IS NOT NULL")
            sample = await conn.fetch("SELECT id, name, lat, lng, text_user FROM places LIMIT 5")

            return web.json_response({
                "success": True,
                "total_places": total,
                "null_coordinates": null_coords,
                "with_coordinates": with_coords,
                "sample": [dict(r) for r in sample]
            })
    except Exception as e:
        import traceback
        logger.error(traceback.format_exc())
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def debug_raw_places(request: web.Request) -> web.Response:
    """Debug: barcha joylarni cheklamasdan olish"""
    try:
        if db_pool is None:
            return web.json_response({"success": False, "error": "Database pool not initialized"}, status=500)

        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT id, name, lat, lng, text_user FROM places ORDER BY id")
            return web.json_response({
                "success": True,
                "count": len(rows),
                "places": [dict(r) for r in rows]
            })
    except Exception as e:
        import traceback
        logger.error(traceback.format_exc())
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

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    is_admin = user_id in ADMIN_IDS

    text = "🗺 <b>My Food Map</b>\n\nXaritadan restoran va xizmatlarni toping!"
    if is_admin:
        text += "\n\n👨‍💼 <b>Admin rejimi</b> faol."

    web_app = types.WebAppInfo(url=WEBAPP_URL)

    markup = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(
            text="🗺 Xaritani ochish",
            web_app=web_app
        )]
    ])

    await message.answer(text, reply_markup=markup)

async def health_check(request: web.Request) -> web.Response:
    """Tekshirish: baza ulanishi va jadval holati"""
    try:
        if db_pool is None:
            return web.json_response({
                "success": False,
                "error": "Database pool not initialized"
            }, status=500)

        async with db_pool.acquire() as conn:
            cols = await conn.fetch("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = 'places'
            """)
            count = await conn.fetchval("SELECT COUNT(*) FROM places")
            return web.json_response({
                "success": True,
                "db_connected": True,
                "places_count": count,
                "columns": [r['column_name'] for r in cols]
            })
    except Exception as e:
        import traceback
        logger.error(traceback.format_exc())
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)

# ------------------- Main -------------------
async def init_app():
    app = web.Application(middlewares=[cors_middleware])

    app.router.add_get('/api/places', get_places)
    app.router.add_get('/api/places/all', get_all_places_list)
    app.router.add_get('/api/nearby', get_nearby)
    app.router.add_post('/api/validate', validate_user)
    app.router.add_post('/api/places', create_place)
    app.router.add_post('/api/places/{id}/geocode', geocode_place)
    app.router.add_post('/api/admin/geocode-all', geocode_all_places)
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

    import asyncio
    await asyncio.gather(
        site.start(),
        dp.start_polling(bot, skip_updates=True)
    )

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
