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
    waiting_for_vin = State()          
    waiting_for_engine_details = State() # Ожидание уточнения мотора/КПП после VIN
    waiting_for_article = State()      
    waiting_for_support_message = State() 
    choosing_make = State()
    choosing_model = State()
    choosing_modification = State()

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

# --- ПОИСК АВТО ПО VIN (ЭКСПЕРТНЫЙ ФОРМАТ) ---
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
        f"• VIN валиден, производитель: <b>{make} {model}</b>.\n"
        f"• Год выпуска: <b>{year} г.в.</b>\n"
        f"• Точная модификация и код двигателя по базовому декодеру требуют верификации.\n\n"
        f"<b>2. Статус подбора</b>\n"
        f"• Прямой OEM-каталог производителя не подключён. Подбор ведется по кросс-номерам и базам поставщиков.\n\n"
        f"<b>3. Что нужно от вас для продолжения:</b>\n"
        f"1. Напишите точный код или объем двигателя (например, <i>1.4 TSI, BZB, 1.6 MPI</i>).\n"
        f"2. Укажите тип КПП (механика / автомат).\n"
        f"3. Напишите артикул или название детали, которую нужно подобрать.\n\n"
        f"👇 <i>Жду параметры вашего двигателя, чтобы зафиксировать применимость!</i>"
    )
    return report

# --- API КАТАЛОГА АВТОМОБИЛЕЙ ---
async def api_get_car_makes():
    url = f"https://api.pr-lg.ru/catalog/makes?secret={API_SECRET}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, ssl=False) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("data", data)
        except: pass
        return []

async def api_get_car_models(make_id: str):
    url = f"https://api.pr-lg.ru/catalog/models?secret={API_SECRET}&make_id={make_id}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, ssl=False) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("data", data)
        except: pass
        return []

async def api_get_car_modifications(model_id: str):
    url = f"https://api.pr-lg.ru/catalog/vehicles?secret={API_SECRET}&model_id={model_id}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, ssl=False) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("data", data)
        except: pass
        return []

# --- ЭКСПЕРТНЫЙ ПОИСК ЗАПЧАСТЕЙ ПО АРТИКУЛУ ---
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

                # Формируем экспертный отчет в вашем стиле
                engine_note = f"Двигатель верифицирован: <b>{engine_info}</b>." if engine_info else "Двигатель не верифицирован (требуется проверка)."
                answer = f"По артикулу <code>{requested_article}</code> — это <b>{target_desc}</b> ({target_brand}). Применимость: {engine_note}\n\n"
                answer += f"<b>1. Параметры и подбор</b>\n"
                answer += f"• Запрошенный артикул: <code>{requested_article}</code> ({target_brand})\n"
                answer += f"• Статус: кандидат на проверку по каталогу.\n\n"
                answer += f"<b>2. Доступность у поставщиков и аналоги:</b>\n\n"
                
                best_exact = None
                for em in exact_matches:
                    best_exact = get_best_offer(em)
                    if best_exact: break
                
                has_offers = False
                if best_exact:
                    has_offers = True
                    answer += f"<b>{best_exact['warehouse']}</b>\n"
                    answer += f"• {best_exact['brand']} <code>{best_exact['article']}</code>, {best_exact['name']} — <b>{best_exact['price']} ₽</b>, остаток {best_exact['stock']} шт., срок: {best_exact['delivery']}\n\n"
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
                        answer += f"• {item['brand']} <code>{item['article']}</code> — <b>{item['price']} ₽</b>, остаток {item['stock']} шт., 🚚 {item['delivery']}\n"
                        offers_data.append({k: v for k, v in item.items() if k not in ['is_own', 'delivery_hours', 'raw_price', 'tier']})
                        count += 1
                        has_offers = True
                    answer += "\n"
                    
                if not has_offers:
                    return f"⚠️ Деталь найдена, но нет предложений в наличии.", []
                    
                answer += "💡 <i>Нажмите на кнопку нужного товара ниже, чтобы положить его в корзину. Вы можете скопировать артикул нажатием на него.</i>"
                return answer.strip(), offers_data

        except Exception as e:
            return f"❌ Ошибка при поиске: {e}", []

# ---------------- КЛАВИАТУРА ----------------
main_menu = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🔍 Подбор по VIN (Экспертный разбор)", callback_data="vin")],
    [InlineKeyboardButton(text="🛠 Поиск по артикулу", callback_data="catalog")],
    [InlineKeyboardButton(text="🚗 Подбор по автомобилю", callback_data="car_catalog_start")],
    [InlineKeyboardButton(text="🛒 Моя корзина", callback_data="view_cart")],
    [InlineKeyboardButton(text="📞 Связь с менеджером", callback_data="support")]
])

# ---------------- ОБРАБОТЧИКИ МЕНЮ ----------------
@dp.message(CommandStart())
@dp.callback_query(F.data.in_({"back_to_menu", "catalog_back"}))
async def start_handler(update, state: FSMContext):
    await state.clear()
    welcome_text = (
        "👋 <b>Добро пожаловать в экспертный магазин автозапчастей!</b>\n\n"
        "Выберите нужное действие в меню ниже:\n\n"
        "<i>(💡 Нажимайте на артикулы в тексте, чтобы копировать их)</i>"
    )
    logo_url = "https://images.unsplash.com/photo-1530046339160-ce3e530c7d2f?q=80&w=800&auto=format&fit=crop"
    
    if isinstance(update, Message):
        try: await update.answer_photo(photo=logo_url, caption=welcome_text, reply_markup=main_menu, parse_mode="HTML")
        except: await update.answer(welcome_text, reply_markup=main_menu, parse_mode="HTML")
    else:
        try: await update.message.edit_text(welcome_text, reply_markup=main_menu, parse_mode="HTML")
        except: await update.message.answer(welcome_text, reply_markup=main_menu, parse_mode="HTML")
        await update.answer()

@dp.message(Command("cancel"))
@dp.message(F.text.lower() == "отмена")
async def cancel_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено. Вы вернулись в главное меню 🏠", reply_markup=main_menu)

# --- ВОРОНКА 1: ПОИСК ПО VIN ---
@dp.callback_query(F.data == "vin")
async def vin_handler(callback: CallbackQuery, state: FSMContext):
    await state.set_state(UserState.waiting_for_vin)
    await callback.message.answer("🚗 Пожалуйста, отправьте 17-значный VIN-код вашего автомобиля:")
    await callback.answer()

@dp.message(UserState.waiting_for_vin)
async def process_vin(message: Message, state: FSMContext):
    user_vin = message.text.strip().upper()
    if len(user_vin) != 17:
        await message.answer("⚠️ VIN-код должен состоять ровно из 17 символов. Попробуйте еще раз.")
        return 
    
    wait_msg = await message.answer("⏳ Проверяем VIN-код...")
    result_text = await decode_vin_vpic(user_vin)
    await wait_msg.delete()
    
    # Сохраняем VIN и переводим в режим ожидания параметров двигателя
    await state.update_data(vin_info=result_text, vin_code=user_vin)
    await state.set_state(UserState.waiting_for_engine_details)
    await message.answer(result_text, parse_mode="HTML")

@dp.message(UserState.waiting_for_engine_details)
async def process_engine_details(message: Message, state: FSMContext):
    engine_text = message.text.strip()
    await state.update_data(engine_info=engine_text)
    
    await state.set_state(UserState.waiting_for_article)
    await message.answer(
        f"✅ Отлично! Двигатель верифицирован как: <b>{engine_text}</b>.\n\n"
        f"🛠 Теперь отправьте артикул нужной детали (например, <code>030115561AN</code>), и я выдам подробный экспертный расклад по наличию и ценам:",
        parse_mode="HTML"
    )

# --- ПОШАГОВЫЙ ПОДБОР ПО АВТОМОБИЛЮ ---
@dp.callback_query(F.data == "car_catalog_start")
async def start_car_catalog(callback: CallbackQuery, state: FSMContext):
    await state.set_state(UserState.choosing_make)
    await callback.message.answer("⏳ Загружаю список марок автомобилей...")
    
    makes = await api_get_car_makes()
    if not makes:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 В меню", callback_data="back_to_menu")]])
        await callback.message.answer("⚠️ Каталог марок временно недоступен.", reply_markup=kb)
        await callback.answer()
        return

    keyboard = []
    row = []
    for make in makes[:30]:
        make_id = str(make.get("id", ""))
        make_name = make.get("name", "Марка")
        row.append(InlineKeyboardButton(text=make_name, callback_data=f"make_{make_id}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row: keyboard.append(row)
    keyboard.append([InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_to_menu")])
    
    await callback.message.answer("🚗 <b>Выберите марку вашего автомобиля:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()

@dp.callback_query(UserState.choosing_make, F.data.startswith("make_"))
async def process_make_choice(callback: CallbackQuery, state: FSMContext):
    make_id = callback.data.split("_")[1]
    await state.update_data(make_id=make_id)
    models = await api_get_car_models(make_id)
    
    keyboard = []
    row = []
    for model in models[:30]:
        model_id = str(model.get("id", ""))
        model_name = model.get("name", "Модель")
        row.append(InlineKeyboardButton(text=model_name, callback_data=f"model_{model_id}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row: keyboard.append(row)
    keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="car_catalog_start")])
    
    await callback.message.answer("🚙 <b>Выберите модель:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()

@dp.callback_query(UserState.choosing_model, F.data.startswith("model_"))
async def process_model_choice(callback: CallbackQuery, state: FSMContext):
    model_id = callback.data.split("_")[1]
    await state.update_data(model_id=model_id)
    modifications = await api_get_car_modifications(model_id)
    
    keyboard = []
    for mod in modifications[:20]:
        mod_id = str(mod.get("id", ""))
        mod_name = f"{mod.get('name', '')} ({mod.get('power', '')} л.с.)"
        keyboard.append([InlineKeyboardButton(text=mod_name, callback_data=f"mod_{mod_id}")])
    keyboard.append([InlineKeyboardButton(text="🏠 В меню", callback_data="back_to_menu")])
    
    await callback.message.answer("⚙️ <b>Выберите модификацию / двигатель:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()

@dp.callback_query(F.data.startswith("mod_"))
async def process_modification_choice(callback: CallbackQuery, state: FSMContext):
    await state.set_state(UserState.waiting_for_article)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_to_menu")]])
    await callback.message.answer("✅ Автомобиль зафиксирован! Теперь отправьте артикул детали для подбора:", reply_markup=kb)
    await callback.answer()

# --- ПОИСК ПО АРТИКУЛУ ---
@dp.callback_query(F.data == "catalog")
async def catalog_handler(callback: CallbackQuery, state: FSMContext):
    await state.set_state(UserState.waiting_for_article)
    await callback.message.answer("Введите артикул детали (например, <code>030115561AN</code>):", parse_mode="HTML")
    await callback.answer()

@dp.message(UserState.waiting_for_article)
async def process_article(message: Message, state: FSMContext):
    article = message.text.strip()
    data = await state.get_data()
    engine_info = data.get("engine_info", "")
    
    wait_msg = await message.answer(f"⏳ Анализируем артикул <code>{article}</code> по базам поставщиков...")
    
    result_text, offers = await search_part_api(article, engine_info)
    await wait_msg.delete() 
    
    if offers:
        last_search[message.from_user.id] = offers
        
        keyboard = []
        row = []
        for i, offer in enumerate(offers):
            btn_text = f"{offer['brand']} | {offer['article']} | {offer['price']}₽"
            row.append(InlineKeyboardButton(text=btn_text, callback_data=f"add_cart_{i}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row: keyboard.append(row)
            
        keyboard.append([InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_to_menu")])
        markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
        
        await message.answer(f"{result_text}\n\n👇 Выберите вариант для добавления в корзину:", parse_mode="HTML", reply_markup=markup)
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_to_menu")]])
        await message.answer(f"{result_text}", parse_mode="HTML", reply_markup=kb)

# Добавление в корзину
@dp.callback_query(F.data.startswith("add_cart_"))
async def add_to_cart_handler(callback: CallbackQuery):
    user_id = callback.from_user.id
    offer_idx = int(callback.data.split("_")[2])
    user_offers = last_search.get(user_id, [])
    
    if offer_idx < len(user_offers):
        offer = user_offers[offer_idx]
        if user_id not in user_carts: user_carts[user_id] = []
        user_carts[user_id].append(offer)
        await callback.answer(f"✅ Добавлено: {offer['brand']} ({offer['price']} ₽)", show_alert=True)
    else:
        await callback.answer("❌ Товар устарел, сделайте поиск заново.", show_alert=True)

# --- КОРЗИНА И ОПЛАТА ---
@dp.callback_query(F.data == "view_cart")
async def view_cart_handler(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user_id = callback.from_user.id
    cart = user_carts.get(user_id, [])
    
    if not cart:
        await callback.message.answer("🛒 Ваша корзина пуста.", reply_markup=main_menu)
        await callback.answer()
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
        [InlineKeyboardButton(text="🗑 Очистить корзину", callback_data="clear_cart")],
        [InlineKeyboardButton(text="◀️ В меню", callback_data="back_to_menu")]
    ])
    
    await callback.message.answer(text, parse_mode="HTML", reply_markup=cart_menu)
    await callback.answer()

@dp.callback_query(F.data == "clear_cart")
async def clear_cart_handler(callback: CallbackQuery):
    user_carts[callback.from_user.id] = []
    await callback.message.answer("🗑 Корзина очищена.", reply_markup=main_menu)
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
    
    await bot.send_message(chat_id=ADMIN_ID, text=admin_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💬 Написать клиенту", callback_data=f"reply_{user_id}")]]))
    user_carts[user_id] = []
    
    req_text = (
        f"🎉 <b>Заказ оформлен!</b>\nК оплате: <b>{total_sum:,} ₽</b>\n\n".replace(",", " ") +
        f"💳 <b>Реквизиты СБП (Т-Банк / Сбер):</b>\nТелефон: <code>+7 (999) 123-45-67</code>\n\n"
        f"👉 <i>Пришлите скриншот чека в ответном сообщении!</i>"
    )
    await callback.message.answer(req_text, parse_mode="HTML", reply_markup=main_menu)
    await callback.answer()

# --- СВЯЗЬ С МЕНЕДЖЕРОМ ---
@dp.callback_query(F.data == "support")
async def support_handler(callback: CallbackQuery, state: FSMContext):
    await state.set_state(UserState.waiting_for_support_message)
    await callback.message.answer("Напишите ваш вопрос или оставьте запрос для менеджера:")
    await callback.answer()

@dp.message(UserState.waiting_for_support_message)
async def process_support_message(message: Message, state: FSMContext):
    user_text = message.text or message.caption or "<i>[Фото без текста]</i>"
    user_id = message.from_user.id
    username = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
    
    admin_text = f"📩 <b>Обращение от {username}</b> (ID: <code>{user_id}</code>):\n\n{user_text}"
    await bot.send_message(chat_id=ADMIN_ID, text=admin_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💬 Ответить", callback_data=f"reply_{user_id}")]]))
    await message.answer("✅ Ваше сообщение передано менеджеру!", reply_markup=main_menu)
    await state.clear()

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
    await bot.set_my_commands([BotCommand(command="start", description="Главное меню"), BotCommand(command="cancel", description="Отмена")])
    print("Бот запущен!")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
