import asyncio
import aiohttp
import re
import os
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# 1. КЛЮЧИ И НАСТРОЙКИ
TOKEN = "9002525170:AAHPOCkeqNevwP4sz0T-XadVj3pJbSQoi0o"
API_SECRET = "2GlZZcxsrvPs70lgc6zDw-p0PW1tfwiR"
ADMIN_ID = 5212263401  # Ваш Telegram ID для связи и уведомлений

bot = Bot(token=TOKEN)
dp = Dispatcher()

user_carts = {}    
last_search = {}   

class UserState(StatesGroup):
    waiting_for_engine_details = State() # Ожидание уточнения мотора после VIN
    waiting_for_support_message = State() 

class AdminState(StatesGroup):
    waiting_for_reply = State()

# --- ВЕБ-СЕРВЕР-ЗАГЛУШКА ДЛЯ RENDER ---
async def handle(request):
    return web.Response(text="Bot is running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"🌐 Веб-сервер запущен на порту {port}")

# --- КЛАССИФИКАТОР БРЕНДОВ ---
PREMIUM_BRANDS = {
    "BOSCH", "BREMBO", "SACHS", "KAYABA", "KYB", "MAHLE", "DENSO", "NGK", 
    "SKF", "SNR", "TIMKEN", "ZIMMERMANN", "TEXTAR", "FAG", "LUK", "VALEO", 
    "HENGST", "MANN", "MANN-FILTER", "BILSTEIN", "MONROE", "CONTITECH", "GATES", "UFI"
}
MEDIUM_BRANDS = {
    "FEBI", "LEMFORDER", "TRW", "CTR", "MEYLE", "INA", "DAYCO", "SIDEM", 
    "NISSENS", "BEHR", "HELLA", "CORTECO", "SWAG", "VDO", "HERTH+BUSS", "KS", "KUJIWA"
}

def get_brand_tier(brand_name: str) -> int:
    b_upper = brand_name.strip().upper()
    if b_upper in PREMIUM_BRANDS: return 1
    elif b_upper in MEDIUM_BRANDS: return 2
    else: return 3

# --- ПОИСК АВТО ПО VIN ---
async def decode_vin_vpic(vin: str) -> str:
    url = f"https://vpic.nhtsa.dot.gov/api/vehicles/decodevin/{vin}?format=json"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, ssl=False) as response:
                if response.status != 200: return "❌ Ошибка базы данных VIN."
                data = await response.json()
        except: return "❌ Ошибка соединения с сервером VIN."

    make, model, year = "Неизвестно", "Неизвестно", "Неизвестно"
    for item in data.get("Results", []):
        if item["Variable"] == "Make" and item["Value"]: make = item["Value"]
        elif item["Variable"] == "Model" and item["Value"]: model = item["Value"]
        elif item["Variable"] == "Model Year" and item["Value"]: year = item["Value"]
            
    if make in ["null", None, "Неизвестно"]: make = "VAG / Европа"
    
    report = (
        f"🔍 <b>Разбор по вашему VIN <code>{vin}</code>:</b>\n\n"
        f"<b>1. Автомобиль и параметры</b>\n"
        f"• Производитель: <b>{make} {model}</b> ({year} г.в.).\n\n"
        f"<b>2. Что нужно для продолжения:</b>\n"
        f"Напишите объем или код двигателя (например, <i>1.4 TSI, BZB, 1.6 MPI</i>), чтобы зафиксировать применимость."
    )
    return report

# --- ПОИСК ЗАПЧАСТЕЙ ПО АРТИКУЛУ ---
async def search_part_api(requested_article: str, engine_info: str = ""):
    url_products = "https://api.pr-lg.ru/search/products"
    url_crosses = "https://api.pr-lg.ru/search/crosses"
    
    offers_data = [] 
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url_products, params={"secret": API_SECRET, "article": requested_article}, ssl=False) as resp1:
                if resp1.status != 200: return f"❌ Ошибка сервера (Код {resp1.status}).", []
                
                prod_data = await resp1.json()
                items_list = prod_data.get("data", prod_data) if isinstance(prod_data, dict) else prod_data
                if isinstance(items_list, dict): items_list = list(items_list.values())
                
                if not items_list or not isinstance(items_list, list):
                    return f"⚠️ Деталь с артикулом <code>{requested_article}</code> не найдена в базе поставщиков.", []
                
                target_brand = items_list[0].get("brand")
                target_desc = items_list[0].get("description", "Деталь")
                if not target_brand:
                    return f"⚠️ Не удалось определить бренд для <code>{requested_article}</code>.", []

            crosses_params = {"secret": API_SECRET, "article": requested_article, "brand": target_brand, "replaces": 1}
            
            async with session.get(url_crosses, params=crosses_params, ssl=False) as resp2:
                if resp2.status != 200: return f"❌ Ошибка поиска аналогов.", []
                
                crosses_data = await resp2.json()
                items = crosses_data.get("data", crosses_data) if isinstance(crosses_data, dict) else crosses_data
                if isinstance(items, dict): items = list(items.values())
                
                if not items or not isinstance(items, list):
                    items = items_list

                def get_best_offer(item_data):
                    products = item_data.get("products", [])
                    if isinstance(products, dict): products = list(products.values())
                    
                    valid_prods = []
                    for p in products:
                        if not isinstance(p, dict) or not p.get("price"): continue
                        qty_str = str(p.get("quantity", "0")).strip().replace(">", "").replace("<", "")
                        if qty_str in ["0", "0.0", "", "Нет", "None"]: continue
                        valid_prods.append(p)
                        
                    if not valid_prods: return None 
                    
                    def sort_logic(x):
                        wh_name = str(x.get("custom_warehouse_name", "")).lower()
                        is_own = 0 if "профит" in wh_name or "profit" in wh_name else 1
                        try: delivery_hours = int(x.get("delivery_time", 999))
                        except: delivery_hours = 999
                        try: price = float(x.get("price", 0))
                        except: price = float('inf')
                        return (is_own, delivery_hours, price)
                    
                    valid_prods.sort(key=sort_logic)
                    best_prod = valid_prods[0] 
                    
                    wh_name = str(best_prod.get("custom_warehouse_name", "Склад поставщика")).lower()
                    is_own = 0 if "профит" in wh_name or "profit" in wh_name else 1
                    warehouse_title = best_prod.get("custom_warehouse_name", "Основной склад")
                    
                    try: delivery_hours = int(best_prod.get("delivery_time", 999))
                    except: delivery_hours = 999
                    
                    raw_price = best_prod.get("price", 0)
                    try: 
                        base_price = float(raw_price)
                        markup = 30 if base_price < 2000 else (25 if base_price < 4000 else 15)
                        final_price = int(base_price * (1 + (markup / 100)))
                        price = f"{final_price:,}".replace(",", " ")
                    except: 
                        price = str(raw_price)
                    
                    brand_name = item_data.get("brand", "Бренд")
                    return {
                        "article": item_data.get("article", "Неизвестно"),
                        "brand": brand_name,
                        "name": item_data.get("description", "Деталь"),
                        "price": price,
                        "stock": best_prod.get("quantity", "0"),
                        "delivery": best_prod.get("show_date", "Срок уточняется"),
                        "warehouse": warehouse_title,
                        "tier": get_brand_tier(brand_name),
                        "is_own": is_own,
                        "delivery_hours": delivery_hours,
                        "raw_price": float(raw_price) if raw_price else float('inf')
                    }

                req_art_clean = re.sub(r'[^a-zA-Z0-9]', '', requested_article.lower())
                exact_matches, analogs = [], []

                for item in items:
                    if not isinstance(item, dict): continue
                    item_art = str(item.get("article", ""))
                    item_art_clean = re.sub(r'[^a-zA-Z0-9]', '', item_art.lower())
                    if item_art_clean == req_art_clean and item.get("brand", "").lower() == target_brand.lower():
                        exact_matches.append(item)
                    else:
                        analogs.append(item)

                engine_note = f"Двигатель верифицирован: <b>{engine_info}</b>." if engine_info else "Двигатель не верифицирован."
                answer = f"По артикулу <code>{requested_article}</code> — это <b>{target_desc}</b> ({target_brand}). Применимость: {engine_note}\n\n"
                answer += f"<b>Доступность у поставщиков и аналоги:</b>\n\n"
                
                best_exact = None
                for em in exact_matches:
                    best_exact = get_best_offer(em)
                    if best_exact: break
                
                has_offers = False
                if best_exact:
                    has_offers = True
                    answer += f"<b>Срок поставки: {best_exact['delivery']}</b> | Склад: {best_exact['warehouse']}\n"
                    answer += f"• <b>{best_exact['brand']}</b> <code>{best_exact['article']}</code> — <b>{best_exact['price']} ₽</b> (остаток: {best_exact['stock']} шт.)\n\n"
                    offers_data.append({k: v for k, v in best_exact.items() if k not in ['is_own', 'delivery_hours', 'raw_price', 'tier']})
                
                processed_analogs = [get_best_offer(an) for an in analogs]
                processed_analogs = [x for x in processed_analogs if x is not None]
                processed_analogs.sort(key=lambda x: (x['tier'], x['is_own'], x['delivery_hours'], x['raw_price']))
                
                tier_names = {1: "🏆 Премиум-аналоги", 2: "⚖️ Средний сегмент", 3: "💰 Бюджетные аналоги (Эконом)"}
                grouped = {1: [], 2: [], 3: []}
                for an in processed_analogs: grouped[an['tier']].append(an)
                
                for tier_id in [1, 2, 3]:
                    tier_items = grouped[tier_id]
                    if not tier_items: continue
                    
                    answer += f"<b>{tier_names[tier_id]}</b>\n"
                    count = 0
                    for item in tier_items:
                        if count >= 3: break 
                        answer += f"• ⏱ <b>Срок: {item['delivery']}</b> | <b>{item['brand']}</b> <code>{item['article']}</code> — <b>{item['price']} ₽</b> (остаток: {item['stock']} шт.)\n"
                        offers_data.append({k: v for k, v in item.items() if k not in ['is_own', 'delivery_hours', 'raw_price', 'tier']})
                        count += 1
                        has_offers = True
                    answer += "\n"
                    
                if not has_offers:
                    return f"⚠️ Деталь найдена, но нет предложений в наличии.", []
                    
                answer += "💡 <i>Нажмите на артикул в тексте выше, чтобы добавить этот товар в корзину.</i>"
                return answer.strip(), offers_data

        except Exception as e:
            return f"❌ Ошибка при поиске: {e}", []

# ---------------- СТАРТ БЕЗ МЕНЮ ----------------
@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    await state.clear()
    welcome_text = (
        "👋 <b>Магазин автозапчастей на связи!</b>\n\n"
        "<b>Инструкция по работе:</b>\n"
        "• Отправьте <b>17-значный VIN-код</b> — для подбора по авто.\n"
        "• Отправьте <b>артикул детали</b> — для мгновенной проценки.\n"
        "• Отправьте <b>любое текстовое сообщение</b> — чтобы связаться с менеджером.\n"
        "• Напишите /cart — для просмотра корзины."
    )
    await message.answer(welcome_text, parse_mode="HTML")

@dp.message(Command("cart"))
async def cart_command(message: Message, state: FSMContext):
    await view_cart_handler(message)

# --- УМНЫЙ РОУТЕР СООБЩЕНИЙ ---
@dp.message()
async def smart_router(message: Message, state: FSMContext):
    text = message.text.strip()
    user_id = message.from_user.id
    
    # 1. Проверяем клик по артикулу из предыдущего поиска для добавления в корзину
    user_offers = last_search.get(user_id, [])
    matched_offer = None
    for offer in user_offers:
        if text.upper().replace(" ", "") == offer['article'].upper().replace(" ", ""):
            matched_offer = offer
            break
            
    if matched_offer:
        if user_id not in user_carts: user_carts[user_id] = []
        user_carts[user_id].append(matched_offer)
        await message.answer(f"✅ Добавлено в корзину: <b>{matched_offer['brand']}</b> (<code>{matched_offer['article']}</code>) за <b>{matched_offer['price']} ₽</b>\n\nНапишите /cart для оформления заказа.", parse_mode="HTML")
        return

    # 2. Проверяем, находится ли пользователь в режиме ожидания уточнения мотора после VIN
    current_state = await state.get_state()
    if current_state == UserState.waiting_for_engine_details.state:
        await state.update_data(engine_info=text)
        await message.answer(f"✅ Двигатель зафиксирован: <b>{text}</b>.\n\nТеперь отправьте артикул нужной детали:", parse_mode="HTML")
        return

    # 3. Проверяем, является ли сообщение 17-значным VIN-кодом
    clean_text = text.upper()
    if len(clean_text) == 17 and clean_text.isalnum():
        wait_msg = await message.answer("⏳ Проверяем VIN-код...")
        result_text = await decode_vin_vpic(clean_text)
        await wait_msg.delete()
        
        await state.update_data(vin_info=result_text, vin_code=clean_text)
        await state.set_state(UserState.waiting_for_engine_details)
        await message.answer(result_text, parse_mode="HTML")
        return

    # 4. Проверяем, похоже ли сообщение на артикул (короткий буквенно-цифровой код без пробелов или с дефисами)
    if len(text) <= 25 and re.match(r'^[a-zA-Z0-9\-\.]+$', text) and any(char.isdigit() for char in text):
        data = await state.get_data()
        engine_info = data.get("engine_info", "")
        
        wait_msg = await message.answer(f"⏳ Процениваем артикул <code>{text}</code>...")
        result_text, offers = await search_part_api(text, engine_info)
        await wait_msg.delete()
        
        if offers:
            last_search[user_id] = offers
        await message.answer(result_text, parse_mode="HTML")
        return

    # 5. Во всех остальных случаях — отправляем сообщение менеджеру
    username = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
    admin_text = f"📩 <b>Обращение от {username}</b> (ID: <code>{user_id}</code>):\n\n{text}"
    
    reply_markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💬 Ответить клиенту", callback_data=f"reply_{user_id}")]])
    
    try:
        await bot.send_message(chat_id=ADMIN_ID, text=admin_text, parse_mode="HTML", reply_markup=reply_markup)
        await message.answer("✅ Ваше сообщение передано менеджеру. Ожидайте ответ!")
    except:
        await message.answer("❌ Ошибка отправки сообщения менеджеру.")

# --- ПРОСМОТР КОРЗИНЫ ---
async def view_cart_handler(message: Message):
    user_id = message.from_user.id
    cart = user_carts.get(user_id, [])
    
    if not cart:
        await message.answer("🛒 Ваша корзина пуста.")
        return
        
    text = "<b>🛒 ВАША КОРЗИНА:</b>\n\n"
    total_sum = 0
    for i, item in enumerate(cart, 1):
        text += f"{i}. <b>{item['brand']}</b> (<code>{item['article']}</code>)\n└ {item['name']} — {item['price']} ₽ ({item['warehouse']})\n\n"
        try: total_sum += int(str(item['price']).replace(" ", ""))
        except: pass
        
    text += f"<b>Итого к оплате: {total_sum:,} ₽</b>\n".replace(",", " ")
    cart_menu = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Оформить и Оплатить", callback_data="checkout")],
        [InlineKeyboardButton(text="🗑 Очистить корзину", callback_data="clear_cart")]
    ])
    
    await message.answer(text, parse_mode="HTML", reply_markup=cart_menu)

@dp.callback_query(F.data == "clear_cart")
async def clear_cart_handler(callback: CallbackQuery):
    user_carts[callback.from_user.id] = []
    await callback.message.answer("🗑 Корзина очищена.")
    await callback.answer()

@dp.callback_query(F.data == "checkout")
async def checkout_handler(callback: CallbackQuery):
    user_id = callback.from_user.id
    cart = user_carts.get(user_id, [])
    if not cart: return
    
    username = f"@{callback.from_user.username}" if callback.from_user.username else callback.from_user.first_name
    admin_text = f"🚨 <b>НОВЫЙ ЗАКАЗ ИЗ КОРЗИНЫ!</b>\n👤 От: {username} (ID: <code>{user_id}</code>)\n\n"
    total_sum = 0
    for i, item in enumerate(cart, 1):
        admin_text += f"{i}. {item['brand']} (<code>{item['article']}</code>) — {item['price']} ₽\n"
        try: total_sum += int(str(item['price']).replace(" ", ""))
        except: pass
    admin_text += f"\n💰 <b>ИТОГО: {total_sum:,} ₽</b>".replace(",", " ")
    
    reply_markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💬 Написать клиенту", callback_data=f"reply_{user_id}")]])
    await bot.send_message(chat_id=ADMIN_ID, text=admin_text, parse_mode="HTML", reply_markup=reply_markup)
    user_carts[user_id] = []
    
    req_text = (
        f"🎉 <b>Заказ оформлен!</b>\nК оплате: <b>{total_sum:,} ₽</b>\n\n".replace(",", " ") +
        f"💳 <b>Реквизиты СБП (Т-Банк / Сбер):</b>\nТелефон: <code>+7 (999) 123-45-67</code>\n\n"
        f"👉 <i>Пришлите скриншот чека в чат для подтверждения!</i>"
    )
    await callback.message.answer(req_text, parse_mode="HTML")
    await callback.answer()

# --- СВЯЗЬ АДМИНА С КЛИЕНТОМ ---
@dp.callback_query(F.data.startswith("reply_"), F.from_user.id == ADMIN_ID)
async def admin_reply_callback(callback: CallbackQuery, state: FSMContext):
    client_id = int(callback.data.split("_")[1])
    await state.update_data(client_id=client_id)
    await state.set_state(AdminState.waiting_for_reply)
    await callback.message.answer("✍️ Напишите ответ клиенту:", parse_mode="HTML")
    await callback.answer()

@dp.message(AdminState.waiting_for_reply, F.from_user.id == ADMIN_ID)
async def send_reply_to_client(message: Message, state: FSMContext):
    data = await state.get_data()
    client_id = data.get("client_id")
    try:
        await bot.send_message(chat_id=client_id, text=f"👨‍💼 <b>Менеджер:</b>\n\n{message.text}", parse_mode="HTML")
        await message.answer("✅ Ответ отправлен клиенту!")
    except:
        await message.answer("❌ Ошибка отправки.")
    await state.clear()

async def main():
    await start_web_server()
    await bot.set_my_commands([BotCommand(command="start", description="Инструкция"), BotCommand(command="cart", description="Корзина")])
    print("Бот запущен в режиме умного роутинга без меню!")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
