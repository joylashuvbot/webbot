import os
import json
import hmac
import hashlib
import logging
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
ADMIN_IDS = set(int(i.strip()) for i in os.getenv("ADMIN_ID").split(","))
DATABASE_URL = os.getenv("DATABASE_URL")
WEBAPP_URL = os.getenv("WEBAPP_URL")  # HTTPS URL (GitHub Pages yoki own server)
BOT_WEBHOOK_HOST = os.getenv("BOT_WEBHOOK_HOST", "0.0.0.0")
BOT_WEBHOOK_PORT = int(os.getenv("BOT_WEBHOOK_PORT", 8080))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ------------------- Database -------------------
db_pool: Optional[asyncpg.Pool] = None

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS places (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                category TEXT CHECK (category IN ('food', 'service')),
                latitude DOUBLE PRECISION NOT NULL,
                longitude DOUBLE PRECISION NOT NULL,
                city TEXT,
                address TEXT,
                phone TEXT,
                telegram TEXT,
                menu_url TEXT,
                work_time TEXT,
                delivery BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_places_category ON places(category);
            CREATE INDEX IF NOT EXISTS idx_places_coords ON places(latitude, longitude);
        """)

async def close_db():
    if db_pool:
        await db_pool.close()

# ------------------- Telegram InitData Validation -------------------
def validate_telegram_data(init_data: str, bot_token: str) -> Optional[dict]:
    """Telegram WebApp initData ni tekshirish"""
    try:
        parsed_data = dict(parse_qsl(init_data))
        received_hash = parsed_data.pop('hash', None)
        
        if not received_hash:
            return None
            
        data_check_string = '\n'.join(
            f"{k}={v}" for k, v in sorted(parsed_data.items())
        )
        
        secret_key = hmac.new(
            key=b"WebAppData",
            msg=bot_token.encode(),
            digestmod=hashlib.sha256
        ).digest()
        
        calculated_hash = hmac.new(
            key=secret_key,
            msg=data_check_string.encode(),
            digestmod=hashlib.sha256
        ).hexdigest()
        
        if calculated_hash != received_hash:
            return None
            
        user = json.loads(parsed_data.get('user', '{}'))
        return user
    except Exception as e:
        logger.error(f"Validation error: {e}")
        return None

# ------------------- API Handlers -------------------
async def get_places(request: web.Request) -> web.Response:
    """Barcha joylarni olish (filter bilan)"""
    try:
        category = request.query.get('category')  # 'food', 'service', or None
        search = request.query.get('search', '').lower()
        
        async with db_pool.acquire() as conn:
            query = "SELECT * FROM places WHERE 1=1"
            params = []
            
            if category in ['food', 'service']:
                query += f" AND category = '{category}'"
            
            if search:
                query += f" AND (LOWER(name) LIKE '%{search}%' OR LOWER(city) LIKE '%{search}%')"
            
            rows = await conn.fetch(query)
            
            places = []
            for row in rows:
                places.append({
                    "id": row['id'],
                    "name": row['name'],
                    "description": row['description'],
                    "category": row['category'],
                    "lat": row['latitude'],
                    "lng": row['longitude'],
                    "city": row['city'],
                    "address": row['address'],
                    "phone": row['phone'],
                    "telegram": row['telegram'],
                    "menu_url": row['menu_url'],
                    "work_time": row['work_time'],
                    "delivery": row['delivery']
                })
            
            return web.json_response({"success": True, "data": places})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def get_nearby(request: web.Request) -> web.Response:
    """Yaqin atrofdagi joylarni olish"""
    try:
        lat = float(request.query.get('lat', 0))
        lng = float(request.query.get('lng', 0))
        radius = float(request.query.get('radius', 50))  # km
        
        # Haversine formula SQL da
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT *, (
                    6371 * acos(
                        cos(radians($1)) * cos(radians(latitude)) *
                        cos(radians(longitude) - radians($2)) +
                        sin(radians($1)) * sin(radians(latitude))
                    )
                ) AS distance
                FROM places
                HAVING distance <= $3
                ORDER BY distance
            """, lat, lng, radius)
            
            places = []
            for row in rows:
                places.append({
                    "id": row['id'],
                    "name": row['name'],
                    "category": row['category'],
                    "lat": row['latitude'],
                    "lng": row['longitude'],
                    "distance": round(row['distance'], 1),
                    "address": row['address'],
                    "phone": row['phone']
                })
            
            return web.json_response({"success": True, "data": places})
    except Exception as e:
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
                INSERT INTO places (name, description, category, latitude, longitude, 
                city, address, phone, telegram, menu_url, work_time, delivery)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                RETURNING id
            """, 
                place.get('name'),
                place.get('description'),
                place.get('category'),
                place.get('lat'),
                place.get('lng'),
                place.get('city'),
                place.get('address'),
                place.get('phone'),
                place.get('telegram'),
                place.get('menu_url'),
                place.get('work_time'),
                place.get('delivery', False)
            )
            
            return web.json_response({"success": True, "id": row['id']})
    except Exception as e:
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
                    name = $1, description = $2, category = $3,
                    latitude = $4, longitude = $5, city = $6,
                    address = $7, phone = $8, telegram = $9,
                    menu_url = $10, work_time = $11, delivery = $12
                WHERE id = $13
            """,
                place.get('name'), place.get('description'), place.get('category'),
                place.get('lat'), place.get('lng'), place.get('city'),
                place.get('address'), place.get('phone'), place.get('telegram'),
                place.get('menu_url'), place.get('work_time'), place.get('delivery'),
                place_id
            )
            return web.json_response({"success": True})
    except Exception as e:
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
        return web.json_response({"success": False, "error": str(e)}, status=500)

# ------------------- CORS Middleware -------------------
async def cors_middleware(app, handler):
    async def middleware(request):
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
    return middleware

# ------------------- Telegram Bot -------------------
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    is_admin = user_id in ADMIN_IDS
    
    text = "🗺 <b>Ezways Map</b>\n\nXaritadan restoran va xizmatlarni toping!"
    if is_admin:
        text += "\n\n👨‍💼 <b>Admin rejimi</b> faol. Siz boshqaruv paneliga kira olasiz."
    
    # Web App tugmasi
    web_app = types.WebAppInfo(url=WEBAPP_URL)
    
    markup = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(
            text="🗺 Xaritani ochish" if message.from_user.language_code == 'uz' else 
                 "🗺 Открыть карту" if message.from_user.language_code == 'ru' else 
                 "🗺 Open Map",
            web_app=web_app
        )]
    ])
    
    await message.answer(text, reply_markup=markup)

# ------------------- Main -------------------
async def init_app():
    """Aiohttp app yaratish"""
    app = web.Application(middlewares=[cors_middleware])
    
    # Routes
    app.router.add_get('/api/places', get_places)
    app.router.add_get('/api/nearby', get_nearby)
    app.router.add_post('/api/validate', validate_user)
    app.router.add_post('/api/places', create_place)
    app.router.add_put('/api/places/{id}', update_place)
    app.router.add_delete('/api/places/{id}', delete_place)
    
    return app

async def main():
    await init_db()
    
    # Web serverni ishga tushirish
    runner = web.AppRunner(await init_app())
    await runner.setup()
    site = web.TCPSite(runner, BOT_WEBHOOK_HOST, BOT_WEBHOOK_PORT)
    
    logger.info(f"🚀 Server started on http://{BOT_WEBHOOK_HOST}:{BOT_WEBHOOK_PORT}")
    
    # Bot va serverni parallel ishlatish
    await asyncio.gather(
        site.start(),
        dp.start_polling(bot, skip_updates=True)
    )

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())