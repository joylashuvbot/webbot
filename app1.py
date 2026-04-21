import os
import json
import hmac
import hashlib
import logging
import re
from typing import Optional, Dict, Any, List
from urllib.parse import parse_qsl

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
DATABASE_URL = os.getenv("DATABASE_URL")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
BOT_WEBHOOK_HOST = os.getenv("BOT_WEBHOOK_HOST", "0.0.0.0")
BOT_WEBHOOK_PORT = int(os.getenv("BOT_WEBHOOK_PORT", "8080"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ------------------- Database -------------------
db_pool: Optional[asyncpg.Pool] = None

async def init_db():
    """Bazaga ulanish va jadval mavjudligini tekshirish"""
    global db_pool
    try:
        logger.info(f"🔗 Connecting to DB...")
        logger.info(f"🔗 DATABASE_URL starts with: {DATABASE_URL[:30] if DATABASE_URL else 'EMPTY'}...")

        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
        async with db_pool.acquire() as conn:
            # Barcha jadvallarni ko'rish
            tables = await conn.fetch("""
                SELECT table_name FROM information_schema.tables 
                WHERE table_schema = 'public'
            """)
            logger.info(f"📋 Available tables: {[t['table_name'] for t in tables]}")

            # places jadvali mavjudligini tekshirish
            exists = await conn.fetchval("""
                SELECT COUNT(*) FROM information_schema.tables 
                WHERE table_schema = 'public' AND table_name = 'places'
            """)

            if exists:
                count = await conn.fetchval("SELECT COUNT(*) FROM places")
                null_count = await conn.fetchval("SELECT COUNT(*) FROM places WHERE lat IS NULL OR lng IS NULL")
                sample = await conn.fetch("SELECT id, name, lat, lng FROM places LIMIT 3")

                logger.info(f"✅ DB ulanish OK. 'places' jadvalida {count} ta joy bor.")
                logger.info(f"✅ NULL koordinatalar: {null_count}")
                logger.info(f"✅ Sample: {[dict(s) for s in sample]}")
            else:
                logger.warning("⚠️ 'places' jadvali topilmadi!")
    except Exception as e:
        logger.error(f"Database init error: {e}")
        raise

async def close_db():
    if db_pool:
        await db_pool.close()

# ------------------- Telegram InitData Validation -------------------
def validate_telegram_data(init_data: str, bot_token: str) -> Optional[dict]:
    try:
        if not init_data:
            return None
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

def parse_place_from_db(row) -> dict:
    """
    Food bot bazasidagi row ni WebApp formatiga o'tkazish.
    """
    r = dict(row)
    text = r.get('text_user') or r.get('text_channel') or ''

    # Telefon raqamini ajratib olish
    phone_match = re.search(r'📞\s*([+\d\s–()-]+)', text)
    phone = phone_match.group(1).strip() if phone_match else None

    # Telegramni ajratib olish
    tg_match = re.search(r'📱(?:\s*Telegram:)?\s*(@[\w\d_]+)', text)
    telegram = tg_match.group(1) if tg_match else None

    # Menyu havolasini ajratib olish
    menu_match = re.search(r'📋\s*<a\s+href=["\']([^"\']+)["\']', text)
    menu_url = menu_match.group(1) if menu_match else None

    # Manzilni ajratib olish
    addr_match = re.search(r'📍\s*<a[^>]*>([^<]+)</a>', text)
    address = addr_match.group(1) if addr_match else r.get('name', 'Unknown')

    # Ish vaqtini ajratib olish
    time_match = re.search(r'⏰\s*([^\n]+)', text)
    work_time = time_match.group(1).strip() if time_match else None

    # Yetkazib berish bormi?
    delivery = any(word in text.lower() for word in ['доставка', 'yetkazib', 'delivery'])

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
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def get_nearby(request: web.Request) -> web.Response:
    """Yaqin atrofdagi joylarni olish"""
    try:
        lat = float(request.query.get('lat', 0))
        lng = float(request.query.get('lng', 0))
        radius = float(request.query.get('radius', 50))

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

        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO places (name, lat, lng, text_user, text_channel)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
            """, 
                place.get('name'),
                place.get('lat'),
                place.get('lng'),
                place.get('text_user', ''),
                place.get('text_channel', '')
            )

            return web.json_response({"success": True, "id": row['id']})
    except Exception as e:
        logger.error(f"Create place error: {e}")
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

        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM places WHERE id = $1", place_id)
            return web.json_response({"success": True})
    except Exception as e:
        logger.error(f"Delete place error: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)

# ------------------- DEBUG Endpoints -------------------
async def debug_db(request: web.Request) -> web.Response:
    """Debug: bazadagi ma'lumotlar haqida umumiy ma'lumot"""
    try:
        async with db_pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM places")
            null_coords = await conn.fetchval("SELECT COUNT(*) FROM places WHERE lat IS NULL OR lng IS NULL")
            sample = await conn.fetch("SELECT id, name, lat, lng FROM places LIMIT 5")

            return web.json_response({
                "success": True,
                "total_places": total,
                "null_coordinates": null_coords,
                "sample": [dict(r) for r in sample]
            })
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def debug_raw_places(request: web.Request) -> web.Response:
    """Debug: barcha joylarni cheklamasdan olish"""
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT id, name, lat, lng FROM places ORDER BY id")
            return web.json_response({
                "success": True,
                "count": len(rows),
                "places": [dict(r) for r in rows]
            })
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

# ------------------- Main -------------------
async def init_app():
    app = web.Application(middlewares=[cors_middleware])

    app.router.add_get('/api/places', get_places)
    app.router.add_get('/api/nearby', get_nearby)
    app.router.add_post('/api/validate', validate_user)
    app.router.add_post('/api/places', create_place)
    app.router.add_put('/api/places/{id}', update_place)
    app.router.add_delete('/api/places/{id}', delete_place)
    app.router.add_get('/api/debug/db', debug_db)
    app.router.add_get('/api/debug/raw', debug_raw_places)

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
