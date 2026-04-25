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
            # 1. Places jadvalini yaratish (agar yo'q bo'lsa) - faqat id bilan
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
                'name': 'TEXT NOT NULL DEFAULT \'\'',
                'lat': 'DOUBLE PRECISION',
                'lng': 'DOUBLE PRECISION',
                'text_user': 'TEXT NOT NULL DEFAULT \'\'',
                'text_channel': 'TEXT NOT NULL DEFAULT \'\'',
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
    """Baza bo'sh bo'lsa, initial restoranlarni qo'shish"""
    for place in INITIAL_PLACES:
        await conn.execute("""
            INSERT INTO places (name, lat, lng, text_user, text_channel)
            VALUES ($1, $2, $3, $4, $5)
        """, place["name"], place.get("lat"), place.get("lng"), 
            place["text_user"], place["text_channel"])

async def close_db():
    if db_pool:
        await db_pool.close()

# ------------------- Initial Places (Restoranlar ro'yxati) -------------------
# Agar boshqa restoranlar kerak bo'lsa, shu ro'yxatni almashtiring
INITIAL_PLACES = [
    {
        "name": "CHAIHANA-AMIR",
        "lat": 38.61700400,
        "lng": -121.53797100,
        "text_user": (
            "🍽️ CHAIHANA-AMIR\n"
            "📍 Sacramento, CA\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 2–3 ч до доставки\n"
            "⏰ 24/7\n"
            "🚘 Доставка есть\n"
            "📞 +19167506977  +19169405677\n"
            "📋 Меню: https://t.me/myhalalmenu/8\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>CHAIHANA-AMIR</b>\n"
            '📍 <a href="https://www.google.com/maps?q=38.61700400,-121.53797100">Sacramento, CA</a>\n'
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 2–3 ч до доставки\n"
            "⏰ 24/7\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/8">Меню</a>\n'
            "📞 +19167506977  +19169405677\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "XADICHAI-KUBRO",
        "lat": 38.61708200,
        "lng": -121.53778900,
        "text_user": (
            "🍽️ XADICHAI-KUBRO\n"
            "📍 Sacramento, CA\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 6–7 ч до доставки\n"
            "⏰ 08:00 – 19:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/9 (в комментариях)\n"
            "📞 +12797901986\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>XADICHAI-KUBRO</b>\n"
            '📍 <a href="https://www.google.com/maps?q=38.61708200,-121.53778900">Sacramento, CA</a>\n'
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 6–7 ч до доставки\n"
            "⏰ 08:00 – 19:00\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/9">Меню</a> (в комментариях)\n'
            "📞 +12797901986\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "UMAR-UZBEK-NATIONAL-FOOD",
        "lat": 38.61700400,
        "lng": -121.53797100,
        "text_user": (
            "🍽️ UMAR UZBEK NATIONAL FOOD\n"
            "📍 Sacramento, CA\n"
            "🏠 Домашняя кухня\n"
            "🧾 Заказы за 4–5 ч до доставки\n"
            "⏰ 10:00 – 20:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/10 (в комментариях)\n"
            "📞 +19165333778\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>UMAR UZBEK NATIONAL FOOD</b>\n"
            '📍 <a href="https://www.google.com/maps?q=38.61700400,-121.53797100">Sacramento, CA</a>\n'
            "🏠 Домашняя кухня\n"
            "🧾 Заказы за 4–5 ч до доставки\n"
            "⏰ 10:00 – 20:00\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/10">Меню</a> (в комментариях)\n'
            "📞 +19165333778\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "RANO-OPA-KITCHEN",
        "lat": 37.80681200,
        "lng": -122.41256100,
        "text_user": (
            "🍽️ RANO OPA KITCHEN – HALOL MILLIY UZBEK TAOMLARI\n"
            "📍 San Francisco, CA\n"
            "🏠 Домашняя кухня\n"
            "🧾 Заказы за 3–4 ч до доставки\n"
            "⏰ 10:00 – 22:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/11 (в комментариях)\n"
            "📞 +15107782614\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>RANO OPA KITCHEN – HALOL MILLIY UZBEK TAOMLARI</b>\n"
            '📍 <a href="https://www.google.com/maps?q=37.80681200,-122.41256100">San Francisco, CA</a>\n'
            "🏠 Домашняя кухня\n"
            "🧾 Заказы за 3–4 ч до доставки\n"
            "⏰ 10:00 – 22:00\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/11">Меню</a> (в комментариях)\n'
            "📞 +15107782614\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "DENVER-HALAL-FOOD",
        "lat": 39.79106000,
        "lng": -104.90467400,
        "text_user": (
            "🍽️ DENVER HALAL FOOD\n"
            "📍 Denver, CO\n"
            "🏠 Домашняя кухня\n"
            "🧾 Заказы за 3–4 ч до доставки\n"
            "⏰ 09:00 – 00:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/12 (в комментариях)\n"
            "📞 +17207564155\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>DENVER HALAL FOOD</b>\n"
            '📍 <a href="https://www.google.com/maps?q=39.79106000,-104.90467400">Denver, CO</a>\n'
            "🏠 Домашняя кухня\n"
            "🧾 Заказы за 3–4 ч до доставки\n"
            "⏰ 09:00 – 00:00\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/12">Меню</a> (в комментариях)\n'
            "📞 +17207564155\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "TRUCKERS-HALAL-FOOD",
        "lat": 39.73438200,
        "lng": -104.84645600,
        "text_user": (
            "🍽️ TRUCKERS HALAL FOOD\n"
            "📍 Denver, CO\n"
            "🏠 Домашняя кухня\n"
            "🧾 Заказы за 3–4 ч до доставки\n"
            "⏰ 08:00 – 00:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/13 (в комментариях)\n"
            "📞 +17209935823\n"
            "📱 Telegram: @MYHALAL_FOOD, @Denverfood"
        ),
        "text_channel": (
            "🍽️ <b>TRUCKERS HALAL FOOD</b>\n"
            '📍 <a href="https://www.google.com/maps?q=39.73438200,-104.84645600">Denver, CO</a>\n'
            "🏠 Домашняя кухня\n"
            "🧾 Заказы за 3–4 ч до доставки\n"
            "⏰ 08:00 – 00:00\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/13">Меню</a> (в комментариях)\n'
            "📞 +17209935823\n"
            "📱 Telegram: @MYHALAL_FOOD, @Denverfood"
        )
    },
    {
        "name": "BAUYRSAQ-EXPRESS",
        "lat": 47.24476600,
        "lng": -122.38548700,
        "text_user": (
            "🍽️ BAUYRSAQ EXPRESS – Uzbek · Kazakh · Kirgiz kitchen\n"
            "📍 Tacoma, WA\n"
            "🏠 Домашняя кухня\n"
            "🧾 Заказы за 2–3 ч до доставки\n"
            "⏰ 24/7\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/14 (в комментариях)\n"
            "📞 +14257577206\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>BAUYRSAQ EXPRESS – Uzbek · Kazakh · Kirgiz kitchen</b>\n"
            '📍 <a href="https://www.google.com/maps?q=47.24476600,-122.38548700">Tacoma, WA</a>\n'
            "🏠 Домашняя кухня\n"
            "🧾 Заказы за 2–3 ч до доставки\n"
            "⏰ 24/7\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/14">Меню</a> (в комментариях)\n'
            "📞 +14257577206\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "ASIA-HALAL-FOOD",
        "lat": 47.24476600,
        "lng": -122.38548700,
        "text_user": (
            "🍽️ ASIA HALAL FOOD\n"
            "📍 Tacoma, WA\n"
            "🏠 Домашняя кухня\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ 24/7\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/15 (в комментариях)\n"
            "📞 +18782294148  +18782294149\n"
            "📱 Telegram: @MYHALAL_FOOD, @AsiaHalalFood"
        ),
        "text_channel": (
            "🍽️ <b>ASIA HALAL FOOD</b>\n"
            '📍 <a href="https://www.google.com/maps?q=47.24476600,-122.38548700">Tacoma, WA</a>\n'
            "🏠 Домашняя кухня\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ 24/7\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/15">Меню</a> (в комментариях)\n'
            "📞 +18782294148  +18782294149\n"
            "📱 Telegram: @MYHALAL_FOOD, @AsiaHalalFood"
        )
    },
    {
        "name": "UZBEK-HALOL-FOOD",
        "lat": 47.24476600,
        "lng": -122.38548700,
        "text_user": (
            "🍽️ UZBEK HALOL FOOD\n"
            "📍 Tacoma, WA\n"
            "🏠 Домашняя кухня\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ 08:00 – 22:00\n"
            "🚘 Доставка бесплатно\n"
            "📋 Меню: https://t.me/myhalalmenu/16 (в комментариях)\n"
            "📞 +13609306392  +12534485190\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>UZBEK HALOL FOOD</b>\n"
            '📍 <a href="https://www.google.com/maps?q=47.24476600,-122.38548700">Tacoma, WA</a>\n'
            "🏠 Домашняя кухня\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ 08:00 – 22:00\n"
            "🚘 Доставка бесплатно\n"
            '📋 <a href="https://t.me/myhalalmenu/16">Меню</a> (в комментариях)\n'
            "📞 +13609306392  +12534485190\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "AMIN-FOOD",
        "lat": 47.24476600,
        "lng": -122.38548700,
        "text_user": (
            "🍽️ AMIN FOOD\n"
            "📍 Tacoma, WA\n"
            "🏠 Домашняя кухня\n"
            "🧾 Заказы за 3–4 ч до доставки\n"
            "⏰ 08:00 – 22:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/18 (в комментариях)\n"
            "📞 +19167380322\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>AMIN FOOD</b>\n"
            '📍 <a href="https://www.google.com/maps?q=47.24476600,-122.38548700">Tacoma, WA</a>\n'
            "🏠 Домашняя кухня\n"
            "🧾 Заказы за 3–4 ч до доставки\n"
            "⏰ 08:00 – 22:00\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/18">Меню</a> (в комментариях)\n'
            "📞 +19167380322\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "CARAVAN-RESTAURANT-2",
        "lat": 47.66120600,
        "lng": -122.32378600,
        "text_user": (
            "🍽️ CARAVAN RESTAURANT – 2\n"
            "📍 Seattle, WA\n"
            "🏠 Ресторан\n"
            "🗺 Адреса:\n"
            "— 405 NE 45th St, Seattle, WA 98105\n"
            "— 7801 Detroit Ave SW, Seattle, WA 98106\n"
            "— 3215 4th Ave S, Seattle, WA\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ 11:00 – 23:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/19 (в комментариях)\n"
            "📞 +12065457499\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>CARAVAN RESTAURANT – 2</b>\n"
            '📍 <a href="https://www.google.com/maps?q=47.66120600,-122.32378600">Seattle, WA</a>\n'
            "🏠 Ресторан\n"
            "🗺 Адреса:\n"
            "— <a href=\"https://maps.app.goo.gl/RiKVT3aQoJbWZ3xg8\">405 NE 45th St, Seattle, WA 98105</a>\n"
            "— <a href=\"https://maps.app.goo.gl/LrTdvgjfGZzxe2mr6\">7801 Detroit Ave SW, Seattle, WA 98106</a>\n"
            "— <a href=\"https://maps.app.goo.gl/zs2dnzLgCF6h1SoC8\">3215 4th Ave S, Seattle, WA</a>\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ 11:00 – 23:00\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/19">Меню</a> (в комментариях)\n'
            "📞 +12065457499\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "SADIYA-OSHXONASI",
        "lat": 39.27019000,
        "lng": -84.44163700,
        "text_user": (
            "🍽️ SADIYA OSHXONASI VA CAKE LAB\n"
            "📍 Cincinnati, OH\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 3–4 ч до доставки\n"
            "⏰ 09:00 – 22:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/20 (в комментариях)\n"
            "📞 +15134449371\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>SADIYA OSHXONASI VA CAKE LAB</b>\n"
            '📍 <a href="https://www.google.com/maps?q=39.27019000,-84.44163700">Cincinnati, OH</a>\n'
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 3–4 ч до доставки\n"
            "⏰ 09:00 – 22:00\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/20">Меню</a> (в комментариях)\n'
            "📞 +15134449371\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "DELICIOUS-FOODS",
        "lat": 39.26986100,
        "lng": -84.43900900,
        "text_user": (
            "🍽️ DELICIOUS FOODS\n"
            "📍 Cincinnati, OH\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 4 ч до доставки\n"
            "⏰ 09:00 – 20:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/21 (в комментариях)\n"
            "📞 +15134046762\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>DELICIOUS FOODS</b>\n"
            '📍 <a href="https://www.google.com/maps?q=39.26986100,-84.43900900">Cincinnati, OH</a>\n'
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 4 ч до доставки\n"
            "⏰ 09:00 – 20:00\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/21">Меню</a> (в комментариях)\n'
            "📞 +15134046762\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "ROBIYA-BAKERY",
        "lat": 39.26866500,
        "lng": -84.43942300,
        "text_user": (
            "🍽️ ROBIYA BAKERY\n"
            "📍 Cincinnati, OH\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 4–5 ч до доставки\n"
            "⏰ 09:00 – 21:00\n"
            "🚘 Доставка по Dayton и Hebron\n"
            "📋 Меню: https://t.me/myhalalmenu/22 (в комментариях)\n"
            "📞 +15132249300\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>ROBIYA BAKERY</b>\n"
            '📍 <a href="https://www.google.com/maps?q=39.26866500,-84.43942300">Cincinnati, OH</a>\n'
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 4–5 ч до доставки\n"
            "⏰ 09:00 – 21:00\n"
            "🚘 Доставка по Dayton и Hebron\n"
            '📋 <a href="https://t.me/myhalalmenu/22">Меню</a> (в комментариях)\n'
            "📞 +15132249300\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "CHAYHANA-1",
        "lat": 39.31210400,
        "lng": -84.37738100,
        "text_user": (
            "🍽️ CHAYHANA №1\n"
            "📍 Cincinnati, OH\n"
            "🍴 Ресторан\n"
            "🧾 Блюда готовы, можно забрать\n"
            "⏰ 10:00 – 22:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/23 (в комментариях)\n"
            "📞 +15137550596\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>CHAYHANA №1</b>\n"
            '📍 <a href="https://www.google.com/maps?q=39.31210400,-84.37738100">Cincinnati, OH</a>\n'
            "🍴 Ресторан\n"
            "🧾 Блюда готовы, можно забрать\n"
            "⏰ 10:00 – 22:00\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/23">Меню</a> (в комментариях)\n'
            "📞 +15137550596\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "SHEF-MOM",
        "lat": 39.38454100,
        "lng": -84.34233300,
        "text_user": (
            "🍽️ SHEF MOM – CAKE – SUSHI\n"
            "📍 Cincinnati, OH\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 5 ч до доставки\n"
            "⏰ 10:00 – 22:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/24 (в комментариях)\n"
            "📞 +14704000770\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>SHEF MOM – CAKE – SUSHI</b>\n"
            '📍 <a href="https://www.google.com/maps?q=39.38454100,-84.34233300">Cincinnati, OH</a>\n'
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 5 ч до доставки\n"
            "⏰ 10:00 – 22:00\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/24">Меню</a> (в комментариях)\n'
            "📞 +14704000770\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "TAJIKSKO-UZBEKSKAYA-KUHNYA",
        "lat": 41.28132000,
        "lng": -96.21969700,
        "text_user": (
            "🍽️ Таджикско-узбекская Национальная кухня\n"
            "📍 Omaha, NE\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 2–3 ч до доставки\n"
            "⏰ 24/7\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/25 (в комментариях)\n"
            "📞 +14026168772\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>Таджикско-узбекская Национальная кухня</b>\n"
            '📍 <a href="https://www.google.com/maps?q=41.28132000,-96.21969700">Omaha, NE</a>\n'
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 2–3 ч до доставки\n"
            "⏰ 24/7\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/25">Меню</a> (в комментариях)\n'
            "📞 +14026168772\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "ZARINA-FOOD",
        "lat": 40.28957100,
        "lng": -76.88458100,
        "text_user": (
            "🍽️ ZARINA FOOD UYGʻUR OSHXONASI\n"
            "📍 Harrisburg, PA\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 2–3 ч до доставки\n"
            "⏰ 08:00 – 18:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/26 (в комментариях)\n"
            "📞 +17175626326\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>ZARINA FOOD UYGʻUR OSHXONASI</b>\n"
            '📍 <a href="https://www.google.com/maps?q=40.28957100,-76.88458100">Harrisburg, PA</a>\n'
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 2–3 ч до доставки\n"
            "⏰ 08:00 – 18:00\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/26">Меню</a> (в комментариях)\n'
            "📞 +17175626326\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "PIZZA-BARI",
        "lat": 40.44370500,
        "lng": -79.99612500,
        "text_user": (
            "🍽️ PIZZA BARI\n"
            "📍 Pittsburgh, PA\n"
            "🏠 Кафе\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ 10:00 – 02:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/28 (в комментариях)\n"
            "📞 +14124020444  +14126090714\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>PIZZA BARI</b>\n"
            '📍 <a href="https://www.google.com/maps?q=40.44370500,-79.99612500">Pittsburgh, PA</a>\n'
            "🏠 Кафе\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ 10:00 – 02:00\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/28">Меню</a> (в комментариях)\n'
            "📞 +14124020444  +14126090714\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "MUSOJON",
        "lat": 33.55247500,
        "lng": -112.15317400,
        "text_user": (
            "🍽️ MUSOJON\n"
            "📍 Phoenix, AZ\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ 05:00 – 22:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/29 (в комментариях)\n"
            "📞 +16028201597\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>MUSOJON</b>\n"
            '📍 <a href="https://www.google.com/maps?q=33.55247500,-112.15317400">Phoenix, AZ</a>\n'
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ 05:00 – 22:00\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/29">Меню</a> (в комментариях)\n'
            "📞 +16028201597\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "ARIZONA-HALAL-FOOD-1",
        "lat": 33.53869100,
        "lng": -112.18625700,
        "text_user": (
            "🍽️ ARIZONA HALAL FOOD\n"
            "📍 Phoenix, AZ\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 4–5 ч до доставки\n"
            "⏰ 08:00 – 20:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/30 (в комментариях)\n"
            "📞 +14807891711\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>ARIZONA HALAL FOOD</b>\n"
            '📍 <a href="https://www.google.com/maps?q=33.53869100,-112.18625700">Phoenix, AZ</a>\n'
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 4–5 ч до доставки\n"
            "⏰ 08:00 – 20:00\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/30">Меню</a> (в комментариях)\n'
            "📞 +14807891711\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "TOSHKENT-MILLIY-TAOMLARI",
        "lat": 33.49340800,
        "lng": -112.33416100,
        "text_user": (
            "🍽️ TOSHKENT MILLIY TAOMLARI\n"
            "📍 Phoenix, AZ\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ 07:00 – 21:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/31 (в комментариях)\n"
            "📞 +16232056021  +16023489938\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>TOSHKENT MILLIY TAOMLARI</b>\n"
            '📍 <a href="https://www.google.com/maps?q=33.49340800,-112.33416100">Phoenix, AZ</a>\n'
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ 07:00 – 21:00\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/31">Меню</a> (в комментариях)\n'
            "📞 +16232056021  +16023489938\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "ALIS-KITCHEN",
        "lat": 33.46092400,
        "lng": -112.25515400,
        "text_user": (
            "🍽️ ALI'S KITCHEN\n"
            "📍 Phoenix, AZ\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ 09:00 – 00:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/32 (в комментариях)\n"
            "📞 +16026997010\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>ALI'S KITCHEN</b>\n"
            '📍 <a href="https://www.google.com/maps?q=33.46092400,-112.25515400">Phoenix, AZ</a>\n'
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ 09:00 – 00:00\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/32">Меню</a> (в комментариях)\n'
            "📞 +16026997010\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "UZBEK-HALAL-FOODS-MEMPHIS",
        "lat": 35.04594700,
        "lng": -90.02337700,
        "text_user": (
            "🍽️ UZBEK HALAL FOODS\n"
            "📍 Memphis, TN (Arkansas border)\n"
            "🏠 Фудтрак\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ 09:00 – 23:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/33 (в комментариях)\n"
            "📞 +15126693163\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>UZBEK HALAL FOODS</b>\n"
            '📍 <a href="https://maps.app.goo.gl/DxTwbfJaypEZvf647">Memphis, TN</a> (Arkansas border)\n'
            "🏠 Фудтрак\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ 09:00 – 23:00\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/33">Меню</a> (в комментариях)\n'
            "📞 +15126693163\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "MADI-FOOD",
        "lat": 28.03012900,
        "lng": -82.45883800,
        "text_user": (
            "🍽️ MADI FOOD (Uygʻurcha taomlar)\n"
            "📍 Tampa, FL\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 3–4 ч до доставки\n"
            "⏰ 10:00 – 22:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/34 (в комментариях)\n"
            "📞 +17178058368\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>MADI FOOD (Uygʻurcha taomlar)</b>\n"
            '📍 <a href="https://www.google.com/maps?q=28.03012900,-82.45883800">Tampa, FL</a>\n'
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 3–4 ч до доставки\n"
            "⏰ 10:00 – 22:00\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/34">Меню</a> (в комментариях)\n'
            "📞 +17178058368\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "CHAYHANA-ORLANDO",
        "lat": 28.66596900,
        "lng": -81.41681300,
        "text_user": (
            "🍽️ CHAYHANA ORLANDO\n"
            "📍 Orlando, FL\n"
            "🏠 Ресторан\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ 11:00 – 22:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/35 (в комментариях)\n"
            "📞 +13214220143\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>CHAYHANA ORLANDO</b>\n"
            '📍 <a href="https://www.google.com/maps?q=28.66596900,-81.41681300">Orlando, FL</a>\n'
            "🏠 Ресторан\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ 11:00 – 22:00\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/35">Меню</a> (в комментариях)\n'
            "📞 +13214220143\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "CARAVAN-RESTAURANT-CHICAGO",
        "lat": 41.87811400,
        "lng": -87.62979800,
        "text_user": (
            "🍽️ CARAVAN RESTAURANT\n"
            "📍 Chicago, IL\n"
            "🏠 Ресторан\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ 10:00 – 22:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/36 (в комментариях)\n"
            "📞 +17733673258\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>CARAVAN RESTAURANT</b>\n"
            '📍 <a href="https://maps.app.goo.gl/gj72DoxeAVhTFgsy5">Chicago, IL</a>\n'
            "🏠 Ресторан\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ 10:00 – 22:00\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/36">Меню</a> (в комментариях)\n'
            "📞 +17733673258\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "TAKU-FOOD",
        "lat": 41.98429200,
        "lng": -87.69751100,
        "text_user": (
            "🍽️ TAKU FOOD\n"
            "📍 Chicago, IL\n"
            "🏠 Ресторан\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ 08:00 – 23:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/37 (в комментариях)\n"
            "📞 +12247600211  +17736812626\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>TAKU FOOD</b>\n"
            '📍 <a href="https://www.google.com/maps?q=41.98429200,-87.69751100">Chicago, IL</a>\n'
            "🏠 Ресторан\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ 08:00 – 23:00\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/37">Меню</a> (в комментариях)\n'
            "📞 +12247600211  +17736812626\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "KAZAN-KEBAB",
        "lat": 41.77922600,
        "lng": -88.34295400,
        "text_user": (
            "🍽️ KAZAN KEBAB\n"
            "📍 Chicago, IL\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ 24/7\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/38 (в комментариях)\n"
            "📞 +15517869980\n"
            "📱 Telegram: @Ali071188, @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>KAZAN KEBAB</b>\n"
            '📍 <a href="https://www.google.com/maps?q=41.77922600,-88.34295400">Chicago, IL</a>\n'
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ 24/7\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/38">Меню</a> (в комментариях)\n'
            "📞 +15517869980\n"
            "📱 Telegram: @Ali071188, @MYHALAL_FOOD"
        )
    },
    {
        "name": "MAKSAT-FOOD-TRUCK",
        "lat": 45.52630600,
        "lng": -122.63703900,
        "text_user": (
            "🍽️ MAKSAT FOOD TRUCK\n"
            "📍 Portland, OR\n"
            "🚛 Фудтрак\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ 10:00 – 23:00\n"
            "🚘 Доставка бесплатная\n"
            "📋 Меню: https://t.me/myhalalmenu/39 (в комментариях)\n"
            "📞 +13602108483\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>MAKSAT FOOD TRUCK</b>\n"
            '📍 <a href="https://www.google.com/maps?q=45.52630600,-122.63703900">Portland, OR</a>\n'
            "🚛 Фудтрак\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ 10:00 – 23:00\n"
            "🚘 Доставка бесплатная\n"
            '📋 <a href="https://t.me/myhalalmenu/39">Меню</a> (в комментариях)\n'
            "📞 +13602108483\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "NAVAT-PDX",
        "lat": 45.54936400,
        "lng": -122.66185700,
        "text_user": (
            "🍽️ NAVAT PDX\n"
            "📍 Portland, OR\n"
            "🚛 Фудтрак\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ 11:00 – 22:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/40 (в комментариях)\n"
            "📞 +14254282011  +17253774764\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>NAVAT PDX</b>\n"
            '📍 <a href="https://www.google.com/maps?q=45.54936400,-122.66185700">Portland, OR</a>\n'
            "🚛 Фудтрак\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ 11:00 – 22:00\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/40">Меню</a> (в комментариях)\n'
            "📞 +14254282011  +17253774764\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "OSH-RESTAURANT-AND-GRILL",
        "lat": 36.11125400,
        "lng": -86.74126300,
        "text_user": (
            "🍽️ OSH RESTAURANT AND GRILL\n"
            "📍 Nashville, TN\n"
            "🏠 Ресторан\n"
            "🧾 Заказы до 21:00\n"
            "⏰ Вт–Вс: 11:00 – 21:00 | Пн: выходной\n"
            "🚘 Доставка: 10:00 – 02:00\n"
            "📋 Меню: https://t.me/myhalalmenu/42 (в комментариях)\n"
            "📞 +16157102288  +16159684444  +16157129985\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>OSH RESTAURANT AND GRILL</b>\n"
            '📍 <a href="https://www.google.com/maps?q=36.11125400,-86.74126300">Nashville, TN</a>\n'
            "🏠 Ресторан\n"
            "🧾 Заказы до 21:00\n"
            "⏰ Вт–Вс: 11:00 – 21:00 | Пн: выходной\n"
            "🚘 Доставка: 10:00 – 02:00\n"
            '📋 <a href="https://t.me/myhalalmenu/42">Меню</a> (в комментариях)\n'
            "📞 +16157102288  +16159684444  +16157129985\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "BROOKLYN-PIZZA",
        "lat": 36.11934500,
        "lng": -86.74898100,
        "text_user": (
            "🍽️ BROOKLYN PIZZA\n"
            "📍 Nashville, TN\n"
            "🏠 Кафе\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ 10:00 – 22:00\n"
            "🚘 Доставка: 24/7 — $1 за милю\n"
            "📋 Меню: https://t.me/myhalalmenu/43 (в комментариях)\n"
            "📞 +16159552222  +16159257070\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>BROOKLYN PIZZA</b>\n"
            '📍 <a href="https://www.google.com/maps?q=36.11934500,-86.74898100">Nashville, TN</a>\n'
            "🏠 Кафе\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ 10:00 – 22:00\n"
            "🚘 Доставка: 24/7 — $1 за милю\n"
            '📋 <a href="https://t.me/myhalalmenu/43">Меню</a> (в комментариях)\n'
            "📞 +16159552222  +16159257070\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "KAMOLA-OSHXONASI",
        "lat": 35.96075200,
        "lng": -83.92075000,
        "text_user": (
            "🍽️ KAMOLA OSHXONASI\n"
            "📍 Knoxville, TN\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 4–5 ч до доставки\n"
            "⏰ 09:00 – 22:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/44 (в комментариях)\n"
            "📞 +18654100845\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>KAMOLA OSHXONASI</b>\n"
            '📍 <a href="https://maps.app.goo.gl/Z83tPnCtbYSxLuCL9">Knoxville, TN</a>\n'
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 4–5 ч до доставки\n"
            "⏰ 09:00 – 22:00\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/44">Меню</a> (в комментариях)\n'
            "📞 +18654100845\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "UZBEGIM-RESTAURANT",
        "lat": 36.16266400,
        "lng": -86.78160200,
        "text_user": (
            "🍽️ UZBEGIM RESTAURANT\n"
            "📍 Nashville, TN\n"
            "🏠 Кафе\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ Время уточняется\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/45 (в комментариях)\n"
            "📞 +13476138691\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>UZBEGIM RESTAURANT</b>\n"
            '📍 <a href="https://maps.app.goo.gl/9U3e96s2EmA6sUMG6">Nashville, TN</a>\n'
            "🏠 Кафе\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ Время уточняется\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/45">Меню</a> (в комментариях)\n'
            "📞 +13476138691\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "BARAKAT-HALAL-FOOD",
        "lat": 29.78456000,
        "lng": -95.80117000,
        "text_user": (
            "🍽️ BARAKAT HALAL FOOD\n"
            "📍 Houston, TX\n"
            "🏠 Фудтрак\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ 24/7\n"
            "🚘 Доставка 24/7\n"
            "📋 Меню: https://t.me/myhalalmenu/46 (в комментариях)\n"
            "📞 +13463772939\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>BARAKAT HALAL FOOD</b>\n"
            '📍 <a href="https://www.google.com/maps?q=29.78456000,-95.80117000">Houston, TX</a>\n'
            "🏠 Фудтрак\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ 24/7\n"
            "🚘 Доставка 24/7\n"
            '📋 <a href="https://t.me/myhalalmenu/46">Меню</a> (в комментариях)\n'
            "📞 +13463772939\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "DIYAR-HOUSTON-FOOD",
        "lat": 29.77985100,
        "lng": -95.88196500,
        "text_user": (
            "🍽️ DIYAR HOUSTON FOOD\n"
            "📍 Houston, TX\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 4–5 ч до доставки\n"
            "⏰ 09:30 – 23:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/47 (в комментариях)\n"
            "📞 +13462740363\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>DIYAR HOUSTON FOOD</b>\n"
            '📍 <a href="https://www.google.com/maps?q=29.77985100,-95.88196500">Houston, TX</a>\n'
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 4–5 ч до доставки\n"
            "⏰ 09:30 – 23:00\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/47">Меню</a> (в комментариях)\n'
            "📞 +13462740363\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "CARAVAN-HOUSE",
        "lat": 41.04526200,
        "lng": -81.58033400,
        "text_user": (
            "🍽️ CARAVAN HOUSE\n"
            "📍 Akron, OH\n"
            "🏠 Ресторан рядом с AMAZON\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ 09:00 – 23:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/48 (в комментариях)\n"
            "📞 +14405755555  +12344020202\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>CARAVAN HOUSE</b>\n"
            '📍 <a href="https://www.google.com/maps?q=41.04526200,-81.58033400">Akron, OH</a>\n'
            "🏠 Ресторан рядом с AMAZON\n"
            "🧾 Продукты готовы, можно купить сразу\n"
            "⏰ 09:00 – 23:00\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/48">Меню</a> (в комментариях)\n'
            "📞 +14405755555  +12344020202\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "CHAYHANA-PERRYSBURG",
        "lat": 41.57081200,
        "lng": -83.62053800,
        "text_user": (
            "🍽️ CHAYHANA\n"
            "📍 Perrysburg / Toledo, OH\n"
            "🏠 Ресторан\n"
            "🧾 Заказы за 4–5 ч до доставки\n"
            "⏰ 08:00 – 00:00\n"
            "🚘 Доставка через Uber / DoorDash\n"
            "📋 Меню: https://t.me/myhalalmenu/49 (в комментариях)\n"
            "📞 +14196034800\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>CHAYHANA</b>\n"
            "📍 Perrysburg / Toledo, OH\n"
            "🏠 Ресторан\n"
            "🧾 Заказы за 4–5 ч до доставки\n"
            "⏰ 08:00 – 00:00\n"
            "🚘 Доставка через Uber / DoorDash\n"
            '📋 <a href="https://t.me/myhalalmenu/49">Меню</a> (в комментариях)\n'
            "📞 +14196034800\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "TASHKENTFOOD-HALAL",
        "lat": 39.44555600,
        "lng": -84.20035400,
        "text_user": (
            "🍽️ Tashkentfood Xalal\n"
            "📍 Lebanon, OH\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 2 ч до получения\n"
            "⏰ 08:00 – 21:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/50\n"
            "📞 +15133321404\n"
            "📱 Telegram: @MYHALAL_FOOD, @Tashkent halal food Ohio"
        ),
        "text_channel": (
            "🍽️ <b>Tashkentfood Xalal</b>\n"
            '📍 <a href="https://maps.app.goo.gl/8aKnspJrH5vPfMq79">Lebanon, OH</a>\n'
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 2 ч до получения\n"
            "⏰ 08:00 – 21:00\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/50">Меню</a>\n'
            "📞 +15133321404\n"
            "📱 Telegram: @MYHALAL_FOOD, @Tashkent halal food Ohio"
        )
    },
    {
        "name": "NUR-KITCHEN",
        "lat": 30.43137000,
        "lng": -97.75393400,
        "text_user": (
            "🍽️ NUR KITCHEN\n"
            "📍 Austin, TX\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 3–4 ч до доставки\n"
            "⏰ 09:00 – 21:00\n"
            "🚘 Доставка: бесплатно по Austin, Pflugerville, San Marcos\n"
            "📋 Меню: https://t.me/myhalalmenu/53 (в комментариях)\n"
            "📞 +17377078330\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>NUR KITCHEN</b>\n"
            '📍 <a href="https://www.google.com/maps?q=30.43137000,-97.75393400">Austin, TX</a>\n'
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 3–4 ч до доставки\n"
            "⏰ 09:00 – 21:00\n"
            "🚘 Доставка: бесплатно по Austin, Pflugerville, San Marcos\n"
            '📋 <a href="https://t.me/myhalalmenu/53">Меню</a> (в комментариях)\n'
            "📞 +17377078330\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "MAZALI-CHARLOTTE-OSHXONASI",
        "lat": 35.23408200,
        "lng": -80.87282000,
        "text_user": (
            "🍽️ MAZALI CHARLOTTE OSHXONASI\n"
            "📍 Charlotte, NC\n"
            "🏠 Ресторан\n"
            "🧾 Заказы за 3–4 ч до доставки\n"
            "⏰ Пн–Пт: 11:00 – 20:00 | Сб–Вс: выходной\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/54 (в комментариях)\n"
            "📞 +13477856222  +13476666930\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>MAZALI CHARLOTTE OSHXONASI</b>\n"
            '📍 <a href="https://www.google.com/maps?q=35.23408200,-80.87282000">Charlotte, NC</a>\n'
            "🏠 Ресторан\n"
            "🧾 Заказы за 3–4 ч до доставки\n"
            "⏰ Пн–Пт: 11:00 – 20:00 | Сб–Вс: выходной\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/54">Меню</a> (в комментариях)\n'
            "📞 +13477856222  +13476666930\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "NND-FOOD",
        "lat": 35.25497600,
        "lng": -80.97975000,
        "text_user": (
            "🍽️ N.N.D FOOD\n"
            "📍 Charlotte, NC\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 2–3 ч до доставки\n"
            "⏰ 24/7\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/55 (в комментариях)\n"
            "📞 +17045764025  +17046191145  +19802393354\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>N.N.D FOOD</b>\n"
            '📍 <a href="https://www.google.com/maps?q=35.25497600,-80.97975000">Charlotte, NC</a>\n'
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 2–3 ч до доставки\n"
            "⏰ 24/7\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/55">Меню</a> (в комментариях)\n'
            "📞 +17045764025  +17046191145  +19802393354\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "AFSONA",
        "lat": 40.63575300,
        "lng": -73.97448900,
        "text_user": (
            "🍽️ Afsona\n"
            "📍 Brooklyn, NY\n"
            "🏠 Ресторан\n"
            "🧾 Заказы заранее, еду можно забирать\n"
            "⏰ 06:00 – 23:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/57 (в комментариях)\n"
            "📞 +17186333006  +19296224444  +19294002252\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>Afsona</b>\n"
            '📍 <a href="https://www.google.com/maps?q=40.63575300,-73.97448900">Brooklyn, NY</a>\n'
            "🏠 Ресторан\n"
            "🧾 Заказы заранее, еду можно забирать\n"
            "⏰ 06:00 – 23:00\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/57">Меню</a> (в комментариях)\n'
            "📞 +17186333006  +19296224444  +19294002252\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "UZBEKISTAN-TAOMLARI",
        "lat": 40.09541213,
        "lng": -75.04420414,
        "text_user": (
            "🍽️ UZBEKISTAN TAOMLARI\n"
            "📍 Bustleton, PA\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы заранее\n"
            "⏰ Время уточняется\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/58 (в комментариях)\n"
            "📞 +12672442371\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>UZBEKISTAN TAOMLARI</b>\n"
            '📍 <a href="https://www.google.com/maps?q=40.09541213,-75.04420414">Bustleton, PA</a>\n'
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы заранее\n"
            "⏰ Время уточняется\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/58">Меню</a> (в комментариях)\n'
            "📞 +12672442371\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "BARAKAT-KAZAKH-CUISINE",
        "lat": 34.11959200,
        "lng": -83.76195000,
        "text_user": (
            "🍽️ Barakat Казахская Cuisine\n"
            "📍 Braselton, GA\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 2–3 ч до доставки\n"
            "⏰ 09:00 – 18:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/59 (в комментариях)\n"
            "📞 +14706689307\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>Barakat Казахская Cuisine</b>\n"
            '📍 <a href="https://www.google.com/maps?q=34.11959200,-83.76195000">Braselton, GA</a>\n'
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 2–3 ч до доставки\n"
            "⏰ 09:00 – 18:00\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/59">Меню</a> (в комментариях)\n'
            "📞 +14706689307\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "VIRGINIA-DC-UZBEK-HALAL",
        "lat": 38.79516300,
        "lng": -77.52366300,
        "text_user": (
            "🍽️ Virginia & DC Uzbek Halal Food\n"
            "📍 Virginia / DC Area\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 3–4 ч до доставки\n"
            "⏰ 07:00 – 00:00\n"
            "🚘 Доставка: I-66, I-95, I-81\n"
            "📋 Меню: https://t.me/myhalalmenu/60 (в комментариях)\n"
            "📞 +15716327034\n"
            "📱 Telegram: @MYHALAL_FOOD, @virginia_halal_food"
        ),
        "text_channel": (
            "🍽️ <b>Virginia & DC Uzbek Halal Food</b>\n"
            '📍 <a href="https://www.google.com/maps?q=38.79516300,-77.52366300">Virginia / DC Area</a>\n'
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 3–4 ч до доставки\n"
            "⏰ 07:00 – 00:00\n"
            "🚘 Доставка: I-66, I-95, I-81\n"
            '📋 <a href="https://t.me/myhalalmenu/60">Меню</a> (в комментариях)\n'
            "📞 +15716327034\n"
            "📱 Telegram: @MYHALAL_FOOD, @virginia_halal_food"
        )
    },
    {
        "name": "ISLOM-BALTIMORE-FOOD",
        "lat": 39.36578700,
        "lng": -76.75882500,
        "text_user": (
            "🍽️ ISLOM BALTIMORE FOOD\n"
            "📍 Baltimore, MD\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 3–4 ч до доставки\n"
            "⏰ 07:00 – 18:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/61 (в комментариях)\n"
            "📞 +15677070708\n"
            "📱 Telegram: @MYHALAL_FOOD, @Madinakhonmd"
        ),
        "text_channel": (
            "🍽️ <b>ISLOM BALTIMORE FOOD</b>\n"
            '📍 <a href="https://www.google.com/maps?q=39.36578700,-76.75882500">Baltimore, MD</a>\n'
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 3–4 ч до доставки\n"
            "⏰ 07:00 – 18:00\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/61">Меню</a> (в комментариях)\n'
            "📞 +15677070708\n"
            "📱 Telegram: @MYHALAL_FOOD, @Madinakhonmd"
        )
    },
    {
        "name": "IRODA-OSHXONASI",
        "lat": 30.41205600,
        "lng": -88.82872200,
        "text_user": (
            "🍽️ IRODA OSHXONASI\n"
            "📍 Ocean Springs, MS\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за день до доставки\n"
            "⏰ 10:00 – 22:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/62 (в комментариях)\n"
            "📞 +12282432635\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>IRODA OSHXONASI</b>\n"
            '📍 <a href="https://maps.app.goo.gl/wCDtog9z5zeqyAeY8">Ocean Springs, MS</a>\n'
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за день до доставки\n"
            "⏰ 10:00 – 22:00\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/62">Меню</a> (в комментариях)\n'
            "📞 +12282432635\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "TASHKENT-CUISINE",
        "lat": 40.44291300,
        "lng": -80.08243800,
        "text_user": (
            "🍽️ TASHKENT CUISINE\n"
            "📍 Pittsburgh, PA\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 2–3 ч до доставки\n"
            "⏰ 10:00 – 22:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/63 (в комментариях)\n"
            "📞 +14125190156\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>TASHKENT CUISINE</b>\n"
            '📍 <a href="https://www.google.com/maps?q=40.44291300,-80.08243800">Pittsburgh, PA</a>\n'
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 2–3 ч до доставки\n"
            "⏰ 10:00 – 22:00\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/63">Меню</a> (в комментариях)\n'
            "📞 +14125190156\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "ARIZONA-HALAL-FOOD-2",
        "lat": 33.46083600,
        "lng": -112.20724400,
        "text_user": (
            "🍽️ ARIZONA HALAL FOOD\n"
            "📍 Phoenix, AZ\n"
            "🏠 Кухня на вынос из дома\n"
            "🧾 Заказы за 2–3 ч до доставки\n"
            "⏰ 08:00 – 00:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/64 (в комментариях)\n"
            "📞 +14806343188\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>ARIZONA HALAL FOOD</b>\n"
            '📍 <a href="https://www.google.com/maps?q=33.46083600,-112.20724400">Phoenix, AZ</a>\n'
            "🏠 Кухня на вынос из дома\n"
            "🧾 Заказы за 2–3 ч до доставки\n"
            "⏰ 08:00 – 00:00\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/64">Меню</a> (в комментариях)\n'
            "📞 +14806343188\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "SILK-ROAD-UZBEK-KAZAKH",
        "lat": 34.05223500,
        "lng": -117.60254700,
        "text_user": (
            "🍽️ SILK ROAD UZBEK - KAZAKH kitchen\n"
            "📍 Ontario, CA (TA Truck Stop)\n"
            "🚛 Фудтрак\n"
            "🧾 Блюда готовы к выдаче\n"
            "⏰ 08:00 – 23:00\n"
            "🚘 Доставка до 50 миль\n"
            "📋 Меню: https://t.me/myhalalmenu/65 (в комментариях)\n"
            "📞 +18722221736\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>SILK ROAD UZBEK - KAZAKH kitchen</b>\n"
            '📍 <a href="https://maps.app.goo.gl/LbdR5qiVbxSYt4F49">Ontario, CA (TA Truck Stop)</a>\n'
            "🚛 Фудтрак\n"
            "🧾 Блюда готовы к выдаче\n"
            "⏰ 08:00 – 23:00\n"
            "🚘 Доставка до 50 миль\n"
            '📋 <a href="https://t.me/myhalalmenu/65">Меню</a> (в комментариях)\n'
            "📞 +18722221736\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "HALAL-FOOD-IN-NASHVILLE",
        "lat": 36.04294500,
        "lng": -86.74166700,
        "text_user": (
            "🍽️ HALAL FOOD IN NASHVILLE\n"
            "📍 Nashville, TN\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 30 мин до доставки\n"
            "⏰ 07:00 – 23:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/66 (в комментариях)\n"
            "📞 +16156913309\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>HALAL FOOD IN NASHVILLE</b>\n"
            '📍 <a href="https://www.google.com/maps?q=36.04294500,-86.74166700">Nashville, TN</a>\n'
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 30 мин до доставки\n"
            "⏰ 07:00 – 23:00\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/66">Меню</a> (в комментариях)\n'
            "📞 +16156913309\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "HALOL-FOOD-MUHAMMADAMIN-ASAKA",
        "lat": 36.18959100,
        "lng": -86.47507800,
        "text_user": (
            "🍽️ HALOL FOOD MUHAMMADAMIN ASAKA\n"
            "📍 Nashville, TN\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 2 ч до доставки\n"
            "⏰ 24/7\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/67 (в комментариях)\n"
            "📞 +12159296717  +18352059595\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>HALOL FOOD MUHAMMADAMIN ASAKA</b>\n"
            '📍 <a href="https://www.google.com/maps?q=36.18959100,-86.47507800">Nashville, TN</a>\n'
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 2 ч до доставки\n"
            "⏰ 24/7\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/67">Меню</a> (в комментариях)\n'
            "📞 +12159296717  +18352059595\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "UZBEK-FOOD-MINNESOTA",
        "lat": 44.97775300,
        "lng": -93.26501100,
        "text_user": (
            "🍽️ UZBEK FOOD MINNESOTA\n"
            "📍 Minneapolis, MN\n"
            "🏠 Кухня на вынос из дома\n"
            "🧾 Заказы за 4–5 ч до доставки\n"
            "⏰ 08:00 – 22:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: смотреть в комментариях\n"
            "📞 +16513525551\n"
            "📱 Telegram: @Manzura_Burkhan, @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>UZBEK FOOD MINNESOTA</b>\n"
            "📍 Minneapolis, MN\n"
            "🏠 Кухня на вынос из дома\n"
            "🧾 Заказы за 4–5 ч до доставки\n"
            "⏰ 08:00 – 22:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: смотреть в комментариях\n"
            "📞 +16513525551\n"
            "📱 Telegram: @Manzura_Burkhan, @MYHALAL_FOOD"
        )
    },
    {
        "name": "OASIS-DLYA-TRAKEROV",
        "lat": 32.77666500,
        "lng": -96.79698900,
        "text_user": (
            "🍽️ ОАЗИС ДЛЯ ТРАКЕРОВ\n"
            "📍 Dallas, TX\n"
            "🏠 Доставка свежей домашней еды к вашей парковке (до 30 миль)\n"
            "✨ Условия доставки:\n"
            "— Минимум $30\n"
            "— Доставка $15\n"
            "— Бесплатно от $250\n"
            "🧾 100% халяль: борщи, плов, пельмени, салаты, выпечка\n"
            "🚚 Заказ за 3–4 ч до получения\n"
            "💰 Скидки постоянным\n"
            "🌐 Меню: https://t.me/oasiseda\n"
            "📞 +13478881927\n"
            "📱 Telegram: https://t.me/oasiseda , @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>ОАЗИС ДЛЯ ТРАКЕРОВ</b>\n"
            "📍 Dallas, TX\n"
            "🏠 Доставка свежей домашней еды к вашей парковке (до 30 миль)\n"
            "✨ Условия доставки:\n"
            "— Минимум $30\n"
            "— Доставка $15\n"
            "— Бесплатно от $250\n"
            "🧾 100% халяль: борщи, плов, пельмени, салаты, выпечка\n"
            "🚚 Заказ за 3–4 ч до получения\n"
            "💰 Скидки постоянным\n"
            '🌐 <a href="https://t.me/oasiseda">Меню</a>\n'
            "📞 +13478881927\n"
            "📱 Telegram: https://t.me/oasiseda , @MYHALAL_FOOD"
        )
    },
    {
        "name": "GOLDEN-BY-NUSAYBA",
        "lat": 39.92883400,
        "lng": -74.23729300,
        "text_user": (
            "🍽️ GOLDEN BY NUSAYBA\n"
            "📍 New Jersey, Lakewood\n"
            "🏠 Домашняя кухня\n"
            "🧾 Готовлю по желанию клиента\n"
            "⏰ 08:00 – 00:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: смотреть в Instagram\n"
            "📞 +13478137000\n"
            "📱 Instagram: @golden_by_nusayba_nj"
        ),
        "text_channel": (
            "🍽️ <b>GOLDEN BY NUSAYBA</b>\n"
            '📍 <a href="https://maps.app.goo.gl/N58gFq6UrewBrBWm7">New Jersey, Lakewood</a>\n'
            "🏠 Домашняя кухня\n"
            "🧾 Готовлю по желанию клиента\n"
            "⏰ 08:00 – 00:00\n"
            "🚘 Доставка есть\n"
            "📋 Меню: смотреть в Instagram\n"
            "📞 +13478137000\n"
            "📱 Instagram: @golden_by_nusayba_nj"
        )
    },
    {
        "name": "UZBEKISTAN-RESTAURANT-CINCINNATI",
        "lat": 39.10311800,
        "lng": -84.51202000,
        "text_user": (
            "🍽️ UZBEKISTAN RESTAURANT\n"
            "📍 Cincinnati Ohio\n"
            "🏠 Домашняя кухня\n"
            "🧾 Заказы за 3–4 ч до доставки\n"
            "⏰ 10:00 – 22:00\n"
            "🚘 Доставка 24/7\n"
            "📋 Меню: https://t.me/myhalalmenu/72\n"
            "📞 +12674230301\n"
            "📱 Telegram: @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>UZBEKISTAN RESTAURANT</b>\n"
            '📍 <a href="https://maps.app.goo.gl/28d42BXtNPUZ9D7GA">Cincinnati Ohio</a>\n'
            "🏠 Домашняя кухня\n"
            "🧾 Заказы за 3–4 ч до доставки\n"
            "⏰ 10:00 – 22:00\n"
            "🚘 Доставка 24/7\n"
            '📋 <a href="https://t.me/myhalalmenu/72">Меню</a>\n'
            "📞 +12674230301\n"
            "📱 Telegram: @MYHALAL_FOOD"
        )
    },
    {
        "name": "BISMILLAH-HALAL-FOOD",
        "lat": 41.87811400,
        "lng": -87.62979800,
        "text_user": (
            "🍽️ Bismillah HALAL FOOD\n"
            "📍 Chicago IL\n"
            "🏠 Домашняя кухня\n"
            "🧾 Заказы за 3–4 ч до доставки\n"
            "⏰ 24/7\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/73\n"
            "📞 +14075957655"
        ),
        "text_channel": (
            "🍽️ <b>Bismillah HALAL FOOD</b>\n"
            '📍 <a href="https://maps.app.goo.gl/az7BJLtakcbejw4K6">Chicago IL</a>\n'
            "🏠 Домашняя кухня\n"
            "🧾 Заказы за 3–4 ч до доставки\n"
            "⏰ 24/7\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/73">Меню</a>\n'
            "📞 +14075957655"
        )
    },
    {
        "name": "KHOZYAYUSHKA-UZBEK-KITCHEN",
        "lat": 36.07954100,
        "lng": -86.69676900,
        "text_user": (
            "🍽️ Хозяюшка Uzbek kitchen\n"
            "📍 Nashville, TN\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 2–3 ч до доставки\n"
            "⏰ 24/7\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/78 (в комментариях)\n"
            "📞 +16159799172\n"
            "📱 Telegram: @Xozayush, @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>Хозяюшка Uzbek kitchen</b>\n"
            '📍 <a href="https://www.google.com/maps?q=36.07954100,-86.69676900">Nashville, TN</a>\n'
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 2–3 ч до доставки\n"
            "⏰ 24/7\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/78">Меню</a> (в комментариях)\n'
            "📞 +16159799172\n"
            "📱 Telegram: @Xozayush, @MYHALAL_FOOD"
        )
    },
    {
        "name": "ATLAS-KITCHEN",
        "lat": 38.85842400,
        "lng": -94.81290200,
        "text_user": (
            "🍽️ ATLAS KITCHEN\n"
            "📍 Kansas City, KS/MO\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 4–5 ч до доставки\n"
            "⏰ 15:00 – 22:00\n"
            "🚘 Доставка: Договорная\n"
            "📋 Меню: https://t.me/myhalalmenu/81 (в комментариях)\n"
            "📞 +19134869109  +19899544770\n"
            "📱 Telegram: @Sabru_jamil1, @Bek_KC"
        ),
        "text_channel": (
            "🍽️ <b>ATLAS KITCHEN</b>\n"
            '📍 <a href="https://www.google.com/maps?q=38.85842400,-94.81290200">Kansas City, KS/MO</a>\n'
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 4–5 ч до доставки\n"
            "⏰ 15:00 – 22:00\n"
            "🚘 Доставка: Договорная\n"
            '📋 <a href="https://t.me/myhalalmenu/81">Меню</a> (в комментариях)\n'
            "📞 +19134869109  +19899544770\n"
            "📱 Telegram: @Sabru_jamil1, @Bek_KC"
        )
    },
    {
        "name": "RAIANA-HALAL-FOOD",
        "lat": 38.58157200,
        "lng": -121.49440000,
        "text_user": (
            "🍽️ RAIANA halal food\n"
            "📍 Sacramento, CA\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 2–3 ч до доставки\n"
            "⏰ 24/7\n"
            "🚘 Доставка есть\n"
            "📋 Меню: https://t.me/myhalalmenu/79 (в комментариях)\n"
            "📞 +17732567187  +1773256893\n"
            "📱 Telegram: @Raiana_halal_food, @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>RAIANA halal food</b>\n"
            '📍 <a href="https://maps.app.goo.gl/bgCVHfHMcR3hfdzx5">Sacramento, CA</a>\n'
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 2–3 ч до доставки\n"
            "⏰ 24/7\n"
            "🚘 Доставка есть\n"
            '📋 <a href="https://t.me/myhalalmenu/79">Меню</a> (в комментариях)\n'
            "📞 +17732567187  +1773256893\n"
            "📱 Telegram: @Raiana_halal_food, @MYHALAL_FOOD"
        )
    },
    {
        "name": "HALAL-JASMIN-KITCHEN",
        "lat": 39.09972700,
        "lng": -94.57856700,
        "text_user": (
            "🍽️ Halal Jasmin Kitchen\n"
            "📍 Kansas\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 1.5–2 ч до доставки\n"
            "⏰ 09:00 – 00:00\n"
            "🚘 Бесплатная доставка по Kansas City\n"
            "📋 Меню: https://t.me/myhalalmenu/80 (в комментариях)\n"
            "📞 +18162991870\n"
            "📱 Telegram: @Rozazhasmin, @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>Halal Jasmin Kitchen</b>\n"
            '📍 <a href="https://maps.app.goo.gl/MTc7JWSzKxafXtH27">Kansas</a>\n'
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 1.5–2 ч до доставки\n"
            "⏰ 09:00 – 00:00\n"
            "🚘 Бесплатная доставка по Kansas City\n"
            '📋 <a href="https://t.me/myhalalmenu/80">Меню</a> (в комментариях)\n'
            "📞 +18162991870\n"
            "📱 Telegram: @Rozazhasmin, @MYHALAL_FOOD"
        )
    },
    {
        "name": "YASINA-FOOD",
        "lat": 28.53833600,
        "lng": -81.37923400,
        "text_user": (
            "🍽️ Yasina Food\n"
            "📍 Orlando FL\n"
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 3–4 ч до доставки\n"
            "⏰ 09:00 – 22:00\n"
            "🚘 Доставка по тракстопам\n"
            "📋 Меню: https://t.me/myhalalmenu/82 (в комментариях)\n"
            "📞 +16892389299\n"
            "📱 Telegram: @yasishfood, @MYHALAL_FOOD"
        ),
        "text_channel": (
            "🍽️ <b>Yasina Food</b>\n"
            '📍 <a href="https://maps.app.goo.gl/eVZw1iT74fqb9LSMA">Orlando FL</a>\n'
            "🏠 Домашняя кухня на вынос\n"
            "🧾 Заказы за 3–4 ч до доставки\n"
            "⏰ 09:00 – 22:00\n"
            "🚘 Доставка по тракстопам\n"
            '📋 <a href="https://t.me/myhalalmenu/82">Меню</a> (в комментариях)\n'
            "📞 +16892389299\n"
            "📱 Telegram: @yasishfood, @MYHALAL_FOOD"
        )
    }
]

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

# ------------------- Geocoding -------------------
async def geocode_address(address: str) -> Optional[tuple]:
    """OpenStreetMap Nominatim orqali manzilni koordinataga o'girish"""
    import aiohttp
    try:
        url = "https://nominatim.openstreetmap.org/search"
        params = {"q": address, "format": "json", "limit": 1}
        headers = {"User-Agent": "MyFoodMap/1.0"}

        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data and len(data) > 0:
                        return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:
        logger.error(f"Geocoding error for '{address}': {e}")
    return None

def parse_place_from_db(row) -> dict:
    """
    Food bot bazasidagi row ni WebApp formatiga o'tkazish.
    Plain text formatini qo'llab-quvvatlaydi (HTML talab qilmaydi).
    """
    r = dict(row)
    text = r.get('text_user') or r.get('text_channel') or ''

    # Telefon raqamini ajratib olish - bir nechta formatni qo'llab-quvvatlash
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

    # Menyu havolasini ajratib olish - plain text URLni ham qo'llab-quvvatlash
    menu_url = None
    menu_patterns = [
        r'📋.*?\(?(https?://[^\s\)\n]+)',
        r'📋\s*<a\s+href=["\']([^"\']+)["\']',
        r'https?://[^\s\n]+(?:menu|menyu)[^\s\n]*',
    ]
    for pattern in menu_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            menu_url = match.group(1).strip().rstrip(')')
            break

    # Manzilni ajratib olish - plain text
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

async def get_all_places_list(request: web.Request) -> web.Response:
    """Barcha joylarni ro'yxat ko'rinishida olish (koordinatasizlar ham)"""
    try:
        search = request.query.get('search', '')
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

        # Agar koordinatalar berilmagan bo'lsa, manzilni geocode qilishga urinish
        lat = place.get('lat')
        lng = place.get('lng')
        address = place.get('address', '')

        if (not lat or not lng) and address:
            coords = await geocode_address(address)
            if coords:
                lat, lng = coords
                logger.info(f"📍 Geocoded '{address}' -> {lat}, {lng}")

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

async def geocode_place(request: web.Request) -> web.Response:
    """Mavjud joyni manzili bo'yicha geocode qilish (admin)"""
    try:
        place_id = int(request.match_info['id'])
        data = await request.json()
        init_data = data.get('init_data', '')

        user = validate_telegram_data(init_data, BOT_TOKEN)
        if not user or user.get('id') not in ADMIN_IDS:
            return web.json_response({"success": False, "error": "Unauthorized"}, status=403)

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
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def geocode_all_places(request: web.Request) -> web.Response:
    """Barcha koordinatasiz joylarni avtomatik geocode qilish (admin)"""
    try:
        data = await request.json()
        init_data = data.get('init_data', '')

        user = validate_telegram_data(init_data, BOT_TOKEN)
        if not user or user.get('id') not in ADMIN_IDS:
            return web.json_response({"success": False, "error": "Unauthorized"}, status=403)

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
        return web.json_response({"success": False, "error": str(e)}, status=500)

# ------------------- DEBUG Endpoints -------------------
async def debug_db(request: web.Request) -> web.Response:
    """Debug: bazadagi ma'lumotlar haqida umumiy ma'lumot"""
    try:
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
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def debug_raw_places(request: web.Request) -> web.Response:
    """Debug: barcha joylarni cheklamasdan olish"""
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT id, name, lat, lng, text_user FROM places ORDER BY id")
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
