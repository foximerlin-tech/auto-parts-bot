import asyncio
import aiohttp
import re
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# 1. КЛЮЧИ И НАСТРОЙКИ
TOKEN = "9002525170:AAHPOCkeqNevwP4sz0T-XadVj3pJbSQoi0o"
API_SECRET = "2GlZZcxsrvPs70lgc6zDw-p0PW1tfwiR"
ADMIN_ID = 5212263401  # Ваш Telegram ID для связи и уведомлений

# URL для выгрузки заказа на ваш сайт (если потребуется в будущем)
WEBSITE_API_URL = "https://ваш-сайт.ру/api/create_bot_order"

bot = Bot(token=TOKEN)
dp = Dispatcher()

user_carts = {}    
last_search = {}   

class UserState(StatesGroup):
    waiting_for_vin = State()          
    waiting_for_article = State()      
    waiting_for_support_message = State() 

class AdminState(StatesGroup):
    waiting_for_reply = State()

# --- ВЫГРУЗКА ЗАКАЗА НА САЙТ ---
async def export_order_to_website(user_id, username, cart_items, total_sum):
    payload = {
        "user_id": user_id,
        "username": username,
        "total_sum": total_sum,
        "items": cart_items
    }
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(WEBSITE_API_URL, json=payload, ssl=False, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    return True, data.get("order_id", "OK")
                else:
                    return False, f"Ошибка сервера сайта: {response.status}"
        except Exception as e:
            print(f"⚠️ Не удалось выгрузить заказ на сайт: {e}")
            return False, str(e)

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
            
    if make in ["null", None, "Неизвестно"]: return "❌ Автомобиль не найден."
    return f"🚗 Ваш автомобиль: {make} {model} ({year} г.в.)"

# --- ПОИСК ЗАПЧАСТЕЙ С ДИФФЕРЕНЦИРОВАННОЙ НАЦЕНКОЙ ---
async def search_part_api(requested_article: str):
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
                    return f"⚠️ Деталь с артикулом <code>{requested_article}</code> не найдена.", []
                
                target_brand = items_list[0].get("brand")
                if not target_brand:
                    return f"⚠️ Не удалось определить бренд для <code>{requested_article}</code>.", []

            crosses_params = {
                "secret": API_SECRET,
                "article": requested_article,
                "brand": target_brand,
                "replaces": 1 
            }
            
            async with session.get(url_crosses, params=crosses_params, ssl=False) as resp2:
                if resp2.status != 200: return f"❌ Ошибка поиска аналогов.", []
                
                crosses_data = await resp2.json()
                items = crosses_data.get("data", crosses_data) if isinstance(crosses_data, dict) else crosses_data
                if isinstance(items, dict): items = list(items.values())
                
                if not items or not isinstance(items, list):
                    return f"⚠️ Для <b>{target_brand}</b> <code>{requested_article}</code> сейчас нет предложений.", []

                def get_best_offer(item_data):
                    products = item_data.get("products", [])
                    if isinstance(products, dict): products = list(products.values())
                    
                    valid_prods = []
                    for p in products:
                        if not isinstance(p, dict): continue
                        if not p.get("price"): continue
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
                    
                    wh_name = str(best_prod.get("custom_warehouse_name", "")).lower()
                    is_own = 0 if "профит" in wh_name or "profit" in wh_name else 1
                    try: delivery_hours = int(best_prod.get("delivery_time", 999))
                    except: delivery_hours = 999
                    
                    raw_price = best_prod.get("price", 0)
                    
                    # Дифференцированная наценка
                    try: 
                        base_price = float(raw_price)
                        if base_price < 2000: markup = 30
                        elif base_price < 4000: markup = 25
                        else: markup = 15
                        final_price = int(base_price * (1 + (markup / 100)))
                        price = f"{final_price:,}".replace(",", " ")
                    except: 
                        price = str(raw_price)
                    
                    return {
                        "article": item_data.get("article", "Неизвестно"),
                        "brand": item_data.get("brand", "Бренд"),
                        "name": item_data.get("description", "Деталь"),
                        "price": price,
                        "stock": best_prod.get("quantity", "0"),
                        "delivery": best_prod.get("show_date", "Не указан"),
                        "is_own": is_own,
                        "delivery_hours": delivery_hours,
                        "raw_price": float(raw_price) if raw_price else float('inf')
                    }

                req_art_clean = re.sub(r'[^a-zA-Z0-9]', '', requested_article.lower())
                exact_matches = []
                analogs = []

                for item in items:
                    if not isinstance(item, dict): continue
                    item_art = str(item.get("article", ""))
                    item_art_clean = re.sub(r'[^a-zA-Z0-9]', '', item_art.lower())
                    if item_art_clean == req_art_clean and item.get("brand", "").lower() == target_brand.lower():
                        exact_matches.append(item)
                    else:
                        analogs.append(item)

                answer = f"🔍 Запрос: <b>{target_brand} <code>{requested_article}</code></b>\n\n"
                
                best_exact = None
                for em in exact_matches:
                    best_exact = get_best_offer(em)
                    if best_exact: break
                
                if best_exact:
                    profit_star = "⭐ " if best_exact.get("is_own") == 0 else "🔹 "
                    answer += f"🎯 <b>ТОЧНОЕ СОВПАДЕНИЕ:</b>\n"
                    answer += f"{profit_star}<b>{best_exact['brand']}</b> (<code>{best_exact['article']}</code>)\n"
                    answer += f"<i>{best_exact['name']}</i>\n"
                    answer += f"➖➖➖➖➖➖➖➖➖➖\n"
                    answer += f"💰 Цена: <b>{best_exact['price']} ₽</b>\n"
                    answer += f"📦 Наличие: <b>{best_exact['stock']} шт.</b>\n"
                    answer += f"🚚 Доставка: <b>{best_exact['delivery']}</b>\n\n"
                    offers_data.append({k: v for k, v in best_exact.items() if k not in ['is_own', 'delivery_hours', 'raw_price']})
                
                processed_analogs = []
                for an in analogs:
                    best_an = get_best_offer(an)
                    if best_an: processed_analogs.append(best_an)
                
                processed_analogs.sort(key=lambda x: (x['is_own'], x['delivery_hours'], x['raw_price']))
                
                added_analogs = 0
                analog_text = ""
                
                for best_an in processed_analogs:
                    if added_analogs >= 7: break 
                    profit_star = "⭐ " if best_an.get("is_own") == 0 else "▫️ "
                    
                    # Вывод с описанием для аналогов
                    analog_text += f"{profit_star}<b>{best_an['brand']}</b> (<code>{best_an['article']}</code>)\n"
                    analog_text += f"<i>{best_an['name']}</i>\n"
                    analog_text += f"💰 <b>{best_an['price']} ₽</b>  |  📦 {best_an['stock']} шт.  |  🚚 {best_an['delivery']}\n"
                    analog_text += f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
                    
                    offers_data.append({k: v for k, v in best_an.items() if k not in ['is_own', 'delivery_hours', 'raw_price']})
                    added_analogs += 1
                
                if analog_text:
                    answer += f"💡 <b>АНАЛОГИ И ЗАМЕНЫ:</b>\n{analog_text}"
                    
                if not offers_data:
                    return f"⚠️ Деталь найдена, но сейчас нет предложений в наличии.", []
                    
                return answer.strip(), offers_data

        except Exception as e:
            return f"❌ Техническая ошибка при поиске: {e}", []

# ---------------- КЛАВИАТУРА ----------------
main_menu = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🔍 Подбор по VIN (Помощь менеджера)", callback_data="vin")],
    [InlineKeyboardButton(text="🛠 Поиск цены по артикулу", callback_data="catalog")],
    [InlineKeyboardButton(text="🛒 Моя корзина", callback_data="view_cart")],
    [InlineKeyboardButton(text="📞 Связь с менеджером", callback_data="support")]
])

# ---------------- ОБРАБОТЧИКИ МЕНЮ ----------------
@dp.message(CommandStart())
@dp.callback_query(F.data == "back_to_menu")
async def start_handler(update, state: FSMContext):
    await state.clear()
    welcome_text = (
        "👋 <b>Добро пожаловать в магазин автозапчастей!</b>\n\n"
        "Выберите нужное действие в меню ниже:\n\n"
        "<i>(⭐ — товары с нашего центрального склада)</i>\n"
        "<i>(💡 — нажмите на артикул, чтобы скопировать его)</i>"
    )
    logo_url = "https://images.unsplash.com/photo-1530046339160-ce3e530c7d2f?q=80&w=800&auto=format&fit=crop"
    
    if isinstance(update, Message):
        try: await update.answer_photo(photo=logo_url, caption=welcome_text, reply_markup=main_menu, parse_mode="HTML")
        except: await update.answer(welcome_text, reply_markup=main_menu, parse_mode="HTML")
    else:
        await update.message.answer(welcome_text, reply_markup=main_menu, parse_mode="HTML")
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
    await callback.message.answer("🚗 Пожалуйста, отправьте 17-значный VIN-код вашего автомобиля и список необходимых запасных частей (или нажмите /cancel для выхода):")
    await callback.answer()

@dp.message(UserState.waiting_for_vin)
async def process_vin(message: Message, state: FSMContext):
    user_vin = message.text.strip().upper()
    if len(user_vin) != 17:
        await message.answer("⚠️ VIN-код должен состоять ровно из 17 символов. Попробуйте еще раз.")
        return 
    
    wait_msg = await message.answer("⏳ Проверяю VIN по базе...")
    result_text = await decode_vin_vpic(user_vin)
    await wait_msg.delete()
    
    if "❌" in result_text:
        await message.answer(result_text, reply_markup=main_menu)
        await state.clear()
    else:
        await state.update_data(vin_info=result_text)
        await state.set_state(UserState.waiting_for_support_message)
        
        ask_parts_text = f"{result_text}\n\n👇 <b>Напишите списком, какие запчасти вам нужны:</b>"
        await message.answer(ask_parts_text, parse_mode="HTML")

# --- ВОРОНКА 2: ПОИСК ПО АРТИКУЛУ И КОРЗИНА ---
@dp.callback_query(F.data == "catalog")
async def catalog_handler(callback: CallbackQuery, state: FSMContext):
    await state.set_state(UserState.waiting_for_article)
    await callback.message.answer("Введите артикул детали для проценки (или нажмите /cancel для выхода):")
    await callback.answer()

@dp.message(UserState.waiting_for_article)
async def process_article(message: Message, state: FSMContext):
    article = message.text.strip()
    wait_msg = await message.answer(f"⏳ Ищу деталь <code>{article}</code>...")
    
    result_text, offers = await search_part_api(article)
    await wait_msg.delete() 
    
    if offers:
        last_search[message.from_user.id] = offers
        keyboard = []
        for i, offer in enumerate(offers):
            btn_text = f"🛒 {offer['brand']} ({offer['price']} ₽)"
            keyboard.append([InlineKeyboardButton(text=btn_text, callback_data=f"add_cart_{i}")])
        
        keyboard.append([InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_to_menu")])
        markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
        
        await message.answer(f"{result_text}\n\n👇 <b>Выберите деталь для корзины или отправьте следующий артикул:</b>", parse_mode="HTML", reply_markup=markup)
    else:
        await message.answer(f"{result_text}\n\n👇 Отправьте следующий артикул:", parse_mode="HTML")

@dp.callback_query(F.data.startswith("add_cart_"))
async def add_to_cart_handler(callback: CallbackQuery):
    user_id = callback.from_user.id
    offer_idx = int(callback.data.split("_")[2])
    
    user_offers = last_search.get(user_id, [])
    if offer_idx < len(user_offers):
        offer = user_offers[offer_idx]
        if user_id not in user_carts: user_carts[user_id] = []
        user_carts[user_id].append(offer)
        await callback.answer(f"✅ {offer['brand']} добавлен в корзину!", show_alert=True)
    else:
        await callback.answer("❌ Ошибка. Товар не найден.", show_alert=True)

# --- ПРОСМОТР КОРЗИНЫ С ПОШТУЧНЫМ УДАЛЕНИЕМ ---
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
    cart_keyboard = []
    
    for i, item in enumerate(cart, 1):
        text += f"{i}. <b>{item['brand']}</b> (<code>{item['article']}</code>)\n"
        text += f"└ <i>{item['name']}</i>\n"
        text += f"└ Цена: {item['price']} ₽ | Доставка: {item['delivery']}\n\n"
        try: total_sum += int(str(item['price']).replace(" ", ""))
        except: pass
        
        cart_keyboard.append([InlineKeyboardButton(text=f"❌ Убрать: {item['brand']} ({item['price']} ₽)", callback_data=f"del_item_{i-1}")])
        
    text += f"<b>Итого к оплате: {total_sum:,} ₽</b>\n".replace(",", " ")
    
    cart_keyboard.extend([
        [InlineKeyboardButton(text="✅ Оформить и Оплатить", callback_data="checkout")],
        [InlineKeyboardButton(text="🗑 Очистить всю корзину", callback_data="clear_cart")],
        [InlineKeyboardButton(text="◀️ В меню", callback_data="back_to_menu")]
    ])
    
    cart_menu = InlineKeyboardMarkup(inline_keyboard=cart_keyboard)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=cart_menu)
    await callback.answer()

# --- ПОШТУЧНОЕ УДАЛЕНИЕ ИЗ КОРЗИНЫ ---
@dp.callback_query(F.data.startswith("del_item_"))
async def remove_single_item_handler(callback: CallbackQuery):
    user_id = callback.from_user.id
    item_idx = int(callback.data.split("_")[2])
    
    cart = user_carts.get(user_id, [])
    if 0 <= item_idx < len(cart):
        removed_item = cart.pop(item_idx)
        await callback.answer(f"🗑 {removed_item['brand']} удален из корзины.", show_alert=True)
        
        fake_state = FSMContext(storage=dp.storage, key=callback.message.chat.id)
        await view_cart_handler(callback, fake_state)
    else:
        await callback.answer("❌ Товар уже удален или не найден.", show_alert=True)

@dp.callback_query(F.data == "clear_cart")
async def clear_cart_handler(callback: CallbackQuery):
    user_carts[callback.from_user.id] = []
    await callback.message.answer("🗑 Корзина полностью очищена.", reply_markup=main_menu)
    await callback.answer()

# --- ОФОРМЛЕНИЕ ЗАКАЗА ---
@dp.callback_query(F.data == "checkout")
async def checkout_handler(callback: CallbackQuery):
    user_id = callback.from_user.id
    cart = user_carts.get(user_id, [])
    
    if not cart:
        await callback.answer("Корзина пуста!", show_alert=True)
        return
        
    username = f"@{callback.from_user.username}" if callback.from_user.username else callback.from_user.first_name
    admin_text = f"🚨 <b>НОВЫЙ ЗАКАЗ ИЗ КОРЗИНЫ (Ожидает оплаты)!</b>\n👤 От: {username}\n🆔 ID: <code>{user_id}</code>\n\n"
    total_sum = 0
    
    for i, item in enumerate(cart, 1):
        admin_text += f"{i}. {item['brand']} (<code>{item['article']}</code>) - {item['price']} ₽\n"
        try: total_sum += int(str(item['price']).replace(" ", ""))
        except: pass
        
    admin_text += f"\n💰 <b>ИТОГО: {total_sum:,} ₽</b>".replace(",", " ")
    
    reply_markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💬 Написать клиенту", callback_data=f"reply_{user_id}")]])
    
    try:
        await bot.send_message(chat_id=ADMIN_ID, text=admin_text, parse_mode="HTML", reply_markup=reply_markup)
        user_carts[user_id] = []
        
        req_text = (
            f"🎉 <b>Ваш заказ успешно оформлен!</b>\n\n"
            f"💰 К оплате: <b>{total_sum:,} ₽</b>\n\n".replace(",", " ") +
            f"💳 <b>Реквизиты для перевода (СБП):</b>\n"
            f"Банк: <b>СберБанк</b> (или ВТБ, ОЗОН-банк)\n"
            f"Телефон: <code>+7 (978) 020-6663</code>\n"
            f"Получатель: Дмитрий Викторович З.\n\n"
            f"👉 <i>Сразу после перевода, пожалуйста, пришлите сюда скриншот или квитанцию об оплате. Мы проверим поступление и сразу отправим заказ в работу!</i>"
        )
        await callback.message.answer(req_text, parse_mode="HTML", reply_markup=main_menu)
        
    except Exception as e:
        await callback.message.answer("❌ Произошла ошибка при оформлении заказа. Попробуйте еще раз.", reply_markup=main_menu)
    await callback.answer()


# ---------------- ДВУСТОРОННЯЯ СВЯЗЬ И ПРИЕМ СКРИНШОТОВ ----------------
@dp.callback_query(F.data == "support")
async def support_handler(callback: CallbackQuery, state: FSMContext):
    await state.set_state(UserState.waiting_for_support_message)
    await callback.message.answer("Напишите ваш вопрос (или нажмите /cancel для отмены):")
    await callback.answer()

@dp.message(UserState.waiting_for_support_message)
async def process_support_message(message: Message, state: FSMContext):
    user_text = message.text or message.caption or "<i>[Прислано фото без текста]</i>"
    user_id = message.from_user.id
    username = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
    
    data = await state.get_data()
    vin_info = data.get("vin_info", "")
    
    admin_text = f"📩 <b>Новое обращение / Подбор!</b>\n👤 От: {username}\n🆔 ID: <code>{user_id}</code>\n\n"
    if vin_info:
        admin_text += f"🚙 <b>Данные авто:</b>\n{vin_info}\n\n"
    admin_text += f"💬 <b>Сообщение/Список:</b>\n{user_text}"
    
    reply_markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💬 Ответить клиенту", callback_data=f"reply_{user_id}")]])
    
    try:
        if message.photo:
            await bot.send_photo(chat_id=ADMIN_ID, photo=message.photo[-1].file_id, caption=admin_text, parse_mode="HTML", reply_markup=reply_markup)
        else:
            await bot.send_message(chat_id=ADMIN_ID, text=admin_text, parse_mode="HTML", reply_markup=reply_markup)
            
        await message.answer("✅ Ваше сообщение успешно отправлено менеджеру!", reply_markup=main_menu)
    except:
        await message.answer("❌ Ошибка при отправке.", reply_markup=main_menu)
    await state.clear()

@dp.message(F.photo)
async def process_photo_message(message: Message):
    user_id = message.from_user.id
    username = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
    
    admin_text = f"💳 <b>Возможно, это скриншот оплаты!</b>\n👤 От: {username}\n🆔 ID: <code>{user_id}</code>"
    if message.caption:
        admin_text += f"\n\n💬 Подпись: {message.caption}"
        
    reply_markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💬 Написать клиенту", callback_data=f"reply_{user_id}")]])
    
    try:
        await bot.send_photo(chat_id=ADMIN_ID, photo=message.photo[-1].file_id, caption=admin_text, parse_mode="HTML", reply_markup=reply_markup)
        await message.answer("✅ Фотография отправлена менеджеру на проверку.")
    except:
        pass

@dp.callback_query(F.data.startswith("reply_"), F.from_user.id == ADMIN_ID)
async def admin_reply_callback(callback: CallbackQuery, state: FSMContext):
    client_id = int(callback.data.split("_")[1])
    await state.update_data(client_id=client_id)
    await state.set_state(AdminState.waiting_for_reply)
    await callback.message.answer("✍️ <b>Режим ответа.</b>\nНапишите текст ответа клиенту (или <i>Отмена</i>).", parse_mode="HTML")
    await callback.answer()

@dp.message(AdminState.waiting_for_reply, F.from_user.id == ADMIN_ID)
async def send_reply_to_client(message: Message, state: FSMContext):
    data = await state.get_data()
    client_id = data.get("client_id")
    reply_text = f"👨‍💼 <b>Менеджер:</b>\n\n{message.text}"
    
    try:
        await bot.send_message(chat_id=client_id, text=reply_text, parse_mode="HTML")
        await message.answer("✅ Ответ отправлен!")
    except:
        await message.answer("❌ Ошибка: клиент заблокировал бота.")
    await state.clear()

async def main():
    commands = [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="cancel", description="Отменить ввод")
    ]
    await bot.set_my_commands(commands)
    
    print("Бот успешно запущен! Нажмите Ctrl+C для остановки.")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())