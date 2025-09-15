from __future__ import annotations
from typing import TYPE_CHECKING, Dict,List,Tuple
from cardinal import Cardinal
if TYPE_CHECKING:
    from cardinal import Cardinal
import re
from FunPayAPI.updater.events import NewOrderEvent, NewMessageEvent
import logging
from telebot import types
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from telebot.types import Message
import os
import json
import time
from pyrogram import Client
from pyrogram.errors.exceptions.bad_request_400 import StargiftUsageLimited
from pyrogram.enums import ChatType
from datetime import datetime,timedelta
import asyncio
logger = logging.getLogger("FPC.auto_gifts")
LOGGER_PREFIX = "[AUTOGIFTS]"

NAME = "Auto Gifts"
VERSION = "5.1.3"
DESCRIPTION = "Плагин для автовыдачи Telegram Gifts"
CREDITS = "@flammez0redd"
UUID = "a3d3f3c9-2da0-4f87-b51c-066038520c49"
SETTINGS_PAGE = False

RUNNING = False

config = {}
lot_mapping = {}
waiting_for_lots_upload = set()
auto_refunds = ""

CONFIG_PATH = os.path.join("storage", "cache", "gift_lots.json")
ORDERS_PATH = os.path.join("storage", "cache", "auto_gift_orders.json")
os.makedirs(os.path.dirname(ORDERS_PATH), exist_ok=True)

async def inform():
    async with Client("stars",workdir="sessions") as app:
        me = await app.get_me()
        stars = await app.get_stars_balance()
        logger.info("Сессия успешно инициализирована!")
        logger.info(f"Баланс сессии: {stars}")
        logger.info(f"Айди сессии: {me.id}")
loop = asyncio.new_event_loop()
try:
    loop.run_until_complete(inform())
finally:
    loop.close()

def save_config(cfg: Dict):

    logger.info("Сохранение конфигурации (gift_lots.json)...")
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=4)
    logger.info("Конфигурация сохранена.")

def load_config() -> Dict:
    logger.info("Загрузка конфигурации (gift_lots.json)...")
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        if "auto_refunds" not in cfg:
            cfg["auto_refunds"] = True
        if "active_lots" not in cfg:
            cfg['active_lots'] = True
        save_config(cfg)
        logger.info("Конфигурация успешно загружена.")
        return cfg
    else:
        logger.info("Конфигурационный файл не найден, создаём.")
        default_config = {
            "lot_mapping": {
                "lot_1": {
                    "name": "Тест лот",
                    "gift_id": 5170233102089322756,
                    "gift_name":"Медведь 🧸"
                }
            },
                "auto_refunds": True,
                "active_lots": True
        }
        save_config(default_config)
        return default_config



queue: Dict[str, Dict] = {}


def get_authorized_users() -> List[int]:
    """
    Считываем список (ключи) из storage/cache/tg_authorized_users.json
    Формат пример: {"8171383326": {}, "8029299947": {}}
    """
    path_ = os.path.join("storage", "cache", "tg_authorized_users.json")
    if not os.path.exists(path_):
        return []
    try:
        with open(path_, "r", encoding="utf-8") as f:
            data = json.load(f)
            return [int(k) for k in data.keys()]
    except:
        return []

async def check_username(c: Cardinal,msg_chat_id,username,order_id):
    async with Client("stars",workdir="sessions") as app:
        try:
            user = await app.get_chat(username)
            if user.type in (ChatType.PRIVATE,ChatType.CHANNEL):
                name = user.first_name
                logger.debug(f"{LOGGER_PREFIX} Получен name: {name} для заказа #{order_id}")
                return name
            else:
                logger.debug(f"{LOGGER_PREFIX} Получен {user.type} для заказа #{order_id}")
                c.send_message(msg_chat_id, "❌ Юзернейм неверный!\n📍 Отправьте еще раз в формате @username")
                return
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Произошла ошибка при обработке {username} для заказа #{order_id}: {e}")
            c.send_message(msg_chat_id, "❌ Юзернейм неверный!\n📍 Отправьте еще раз в формате @username")
            return

async def buy_gifts(c: Cardinal,msg_chat_id,username,gift_id,order_amount,order_id):
    async with Client("stars",workdir="sessions") as app:
        for attempt in range(order_amount):
            result = await app.send_gift(chat_id = username,gift_id = gift_id,hide_my_name = True)
            if result is not True:
                logger.debug(f"{LOGGER_PREFIX} Получилось ли отправить подарок: {result}")
                c.send_message(msg_chat_id,"❌ Не удалось отправить подарок!\n📌 Напишите в чате !help")
                logger.error(f"{LOGGER_PREFIX} Не удалось отправить {attempt+1} из {order_amount} подарков по заказу #{order_id}")
                return
            logger.info(f"{LOGGER_PREFIX} Отправлен {attempt+1} из {order_amount} подарков по заказу #{order_id}")
    return True

async def get_balance():
    async with Client("stars",workdir="sessions") as app:
        stars = await app.get_stars_balance()
        return stars

async def get_amount(gift_id):
    async with Client("stars",workdir="sessions") as app:
        gifts = await app.get_available_gifts()
        for gift in gifts:
            if gift.id == gift_id:
                amount = gift.price
                return amount


def get_tg_id_by_description(description: str) -> Tuple[int | None,int | None]:
    for lot_key, lot_data in lot_mapping.items():
        lot_name = lot_data["name"]
        if re.search(re.escape(lot_name), description, re.IGNORECASE):
            gift_id = lot_data["gift_id"]
            gift_name = lot_data["gift_name"]
            return gift_id,gift_name
    return None,None

def generate_lots_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    cfg = load_config()
    lot_map = cfg.get("lot_mapping", {})
    items = list(lot_map.items())

    per_page = 10
    start_ = page * per_page
    end_ = start_ + per_page
    chunk = items[start_:end_]

    kb = InlineKeyboardMarkup(row_width=1)
    for lot_key, lot_data in chunk:
        name_ = lot_data["name"]
        gift_id = lot_data["gift_id"]
        gift_name = lot_data["gift_name"]
        btn_text = f"{name_} [ID={gift_id}, Name={gift_name}]"
        cd = f"ed_lot_{lot_key}"
        kb.add(InlineKeyboardButton(btn_text, callback_data=cd))

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️", callback_data=f"pr_page_{page-1}"))
    if end_ < len(items):
        nav_buttons.append(InlineKeyboardButton("➡️", callback_data=f"ne_page_{page+1}"))
    if nav_buttons:
        kb.row(*nav_buttons)

    kb.add(InlineKeyboardButton("🔙 Назад", callback_data="to_setting"))
    return kb

def save_order_info(order_id: int, order_summa: float, lot_name: str, order_profit: float):
    data_ = {
        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "order_id": order_id,
        "summa": order_summa,
        "lot_name": lot_name,
        "profit": order_profit
    }
    if not os.path.exists(ORDERS_PATH):
        with open(ORDERS_PATH, 'w', encoding='utf-8') as f:
            json.dump([], f, indent=4, ensure_ascii=False)

    with open(ORDERS_PATH, 'r', encoding='utf-8') as f:
        orders = json.load(f)
    orders.append(data_)
    with open(ORDERS_PATH, 'w', encoding='utf-8') as f:
        json.dump(orders, f, indent=4, ensure_ascii=False)

def fast_get_lot_fields(cardinal: Cardinal, lot_id: int):
    return cardinal.account.get_lot_fields(lot_id)

def fast_save_lot(cardinal: Cardinal, lot_fields):
    cardinal.account.save_lot(lot_fields)

def force_set_lot_active(cardinal: Cardinal, lot_id: int, make_active: bool) -> bool:
    try:
        lf = fast_get_lot_fields(cardinal, lot_id)
    except Exception as e:
        logger.warning(f"{LOGGER_PREFIX} get_lot_fields(lot_id={lot_id}) ошибка: {e}")
        return False
    time.sleep(0.3)
    lf.active = make_active
    lf.renew_fields()
    try:
        fast_save_lot(cardinal, lf)
    except Exception as e:
        logger.warning(f"{LOGGER_PREFIX} save_lot(lot_id={lot_id}) ошибка: {e}")
        return False

    # Без финальной проверки => предполагаем успех
    return True

def get_my_subcategory_lots_fast(account, subcat_id: int):
    return account.get_my_subcategory_lots(subcat_id)

def toggle_subcat_status(cardinal: Cardinal, subcat_id: str) -> bool:

    old_st = is_subcat_active(cardinal, subcat_id)
    new_st = not old_st
    try:
        sc_id = int(subcat_id)
    except:
        return new_st

    changed = 0
    try:
        sub_lots = get_my_subcategory_lots_fast(cardinal.account, sc_id)
    except Exception as e:
        logger.warning(f"{LOGGER_PREFIX} get_my_subcategory_lots({subcat_id}) ошибка: {e}")
        return new_st

    for lt in sub_lots:
        if force_set_lot_active(cardinal, lt.id, new_st):
            changed += 1

    logger.info(f"{LOGGER_PREFIX} subcat={subcat_id} => {new_st}, changed={changed}.")
    return new_st


def is_subcat_active(cardinal: Cardinal, subcat_id: str) -> bool:
    try:
        sc_id = int(subcat_id)
    except:
        return False
    try:
        lots = get_my_subcategory_lots_fast(cardinal.account, sc_id)
        if not lots:
            return False
        return any(l.active for l in lots)
    except:
        logger.warning(f"{LOGGER_PREFIX} is_subcat_active({subcat_id}): ошибка => вернём False.")
        return False



def get_statistics():
    if not os.path.exists(ORDERS_PATH):
        return None
    with open(ORDERS_PATH, 'r', encoding='utf-8') as f:
        orders = json.load(f)
    now = datetime.now()
    day_ago = now - timedelta(days=1)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    day_orders = [o for o in orders if datetime.strptime(o["date"], "%Y-%m-%d %H:%M:%S") >= day_ago]
    week_orders = [o for o in orders if datetime.strptime(o["date"], "%Y-%m-%d %H:%M:%S") >= week_ago]
    month_orders = [o for o in orders if datetime.strptime(o["date"], "%Y-%m-%d %H:%M:%S") >= month_ago]
    all_orders = orders

    day_total = round(sum(o["summa"] for o in day_orders),2)
    week_total = round(sum(o["summa"] for o in week_orders),2)
    month_total = round(sum(o["summa"] for o in month_orders),2)
    all_total = round(sum(o["summa"] for o in all_orders),2)

    day_profit = round(sum(o.get("profit", 0) for o in day_orders),2)
    week_profit = round(sum(o.get("profit", 0) for o in week_orders),2)
    month_profit = round(sum(o.get("profit", 0) for o in month_orders),2)
    all_profit = round(sum(o.get("profit", 0) for o in all_orders),2)

    def find_best_service(os_):
        if not os_:
            return "Нет"
        freq = {}
        for _o in os_:
            srv = _o.get("lot_name", "Неизвестно")
            freq[srv] = freq.get(srv, 0) + 1
        return max(freq, key=freq.get, default="Нет")

    return {
        "day_orders": len(day_orders),
        "day_total": day_total,
        "day_profit": day_profit,
        "week_orders": len(week_orders),
        "week_total": week_total,
        "week_profit": week_profit,
        "month_orders": len(month_orders),
        "month_total": month_total,
        "month_profit": month_profit,
        "all_time_orders": len(all_orders),
        "all_time_total": all_total,
        "all_time_profit": all_profit,
        "best_day_service": find_best_service(day_orders),
        "best_week_service": find_best_service(week_orders),
        "best_month_service": find_best_service(month_orders),
        "best_all_time_service": find_best_service(all_orders),
    }


def reindex_lots(cfg: Dict):
    lot_map = cfg.get("lot_mapping", {})
    sorted_lots = sorted(
        lot_map.items(),
        key=lambda x: int(x[0].split('_')[1]) if x[0].startswith('lot_') and x[0].split('_')[1].isdigit() else 0
    )
    new_lot_map = {}
    for idx, (lot_key, lot_data) in enumerate(sorted_lots, start=1):
        new_key = f"lot_{idx}"
        new_lot_map[new_key] = lot_data
    cfg["lot_mapping"] = new_lot_map
    save_config(cfg)
    logger.info("Лоты были переиндексированы после удаления.")


def init_commands(c: Cardinal):
    global config, lot_mapping
    logger.info("=== init_commands() from auto_gifts ===")
    if not c.telegram:
        return
    bot = c.telegram.bot

    @bot.message_handler(content_types=['document'])
    def handle_document_upload(message: types.Message):
        user_id = message.from_user.id
        logger.info(f"Получен документ от {user_id}. Проверка ожидания...")
        if user_id not in waiting_for_lots_upload:
            logger.info(f"Пользователь {user_id} не ожидает загрузки JSON")
            bot.send_message(message.chat.id, "❌ Вы не активировали загрузку JSON. Используйте меню настроек.")
            return
        waiting_for_lots_upload.remove(user_id)
        logger.info(f"Пользователь {user_id} удалён из ожидания. Обрабатываю файл...")
        file_id = message.document.file_id
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        try:
            data = json.loads(downloaded_file.decode('utf-8'))
            if "lot_mapping" not in data:
                bot.send_message(message.chat.id, "❌ Ошибка: в файле нет ключа 'lot_mapping'.")
                logger.error("JSON не содержит 'lot_mapping'")
                return
            save_config(data)
            kb_ = InlineKeyboardMarkup()
            kb_.add(InlineKeyboardButton("🔙 Назад", callback_data="to_setting"))
            bot.send_message(message.chat.id, "✅ Новый gift_lots.json успешно загружен и сохранён!", reply_markup=kb_)
            logger.info("JSON успешно загружен и сохранён")
        except json.JSONDecodeError as e:
            bot.send_message(message.chat.id, f"❌ Ошибка: Не удалось считать JSON. Проверьте синтаксис. ({e})")
            logger.error(f"Ошибка декодирования JSON: {e}")
        except Exception as e:
            bot.send_message(message.chat.id, f"❌ Произошла ошибка при загрузке файла: {e}")
            logger.error(f"Неизвестная ошибка при загрузке: {e}")

    def start(m:Message):
        global RUNNING,app
        if RUNNING is False:
            bot.send_message(m.chat.id, f"✅Auto gifts включен!")
            RUNNING = True
            return
        bot.send_message(m.chat.id, f"❌Auto gifts уже включен!")
    def stop(m:Message):
        global RUNNING,app
        if RUNNING is False:
            bot.send_message(m.chat.id, f"❌Auto gifts уже выключен!")
            return
        bot.send_message(m.chat.id, f"✅Auto gifts выключен!")
        RUNNING = False

    cfg = load_config()
    config.update(cfg)
    lot_mapping.clear()
    lot_mapping.update(cfg.get("lot_mapping", {}))


    def edit_lot(call: types.CallbackQuery, lot_key: str):
        cfg = load_config()
        lot_map = cfg.get("lot_mapping", {})
        if lot_key not in lot_map:
            bot.edit_message_text(f"❌ Лот {lot_key} не найден.", call.message.chat.id, call.message.message_id)
            return

        ld = lot_map[lot_key]
        txt = f"""
<b>{lot_key}</b>
Название: <code>{ld['name']}</code>
GIFT ID:  <code>{ld['gift_id']}</code>
GIFT NAME: <code>{ld['gift_name']}</code>
""".strip()

        kb_ = InlineKeyboardMarkup(row_width=1)
        kb_.add(
            InlineKeyboardButton("Изменить название", callback_data=f"changing_lot_{lot_key}"),
            InlineKeyboardButton("Изменить GIFT ID", callback_data=f"changing_id_{lot_key}"),
            InlineKeyboardButton("Изменить GIFT NAME", callback_data=f"changing_nam_{lot_key}"),
        )
        kb_.add(InlineKeyboardButton("❌ Удалить лот", callback_data=f"deletin_one_lot_{lot_key}"))
        kb_.add(InlineKeyboardButton("◀️ К списку", callback_data="return_t_lot"))
        bot.edit_message_text(txt, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb_)


    def process_lot_change(message: types.Message, lot_key: str):
        new_name = message.text.strip()
        cfg = load_config()
        lot_map = cfg.get("lot_mapping", {})
        if lot_key not in lot_map:
            bot.send_message(message.chat.id, f"❌ Лот {lot_key} не найден.")
            return
        lot_map[lot_key]["name"] = new_name
        cfg["lot_mapping"] = lot_map
        save_config(cfg)
        kb_ = InlineKeyboardMarkup()
        kb_.add(InlineKeyboardButton("◀️ К лотам", callback_data="return_t_lot"))
        bot.send_message(message.chat.id, f"✅ Название лота {lot_key} изменено на {new_name}.", reply_markup=kb_)

    def process_new_lot(message: types.Message):
        try:
            lot_id = int(message.text.strip())
        except ValueError:

            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("🔙 Назад", callback_data="to_setting"))
            bot.send_message(message.chat.id, "❌ Ошибка: ID лота должно быть числом.", reply_markup=kb)
            return

        try:
            lot_fields = c.account.get_lot_fields(lot_id)
            fields = lot_fields.fields
            name = fields.get("fields[summary][ru]", "Без названия")
        except Exception as e:
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("🔙 Назад", callback_data="to_setting"))
            bot.send_message(message.chat.id, f"❌ Не удалось получить данные лота: {e}", reply_markup=kb)
            return

        cfg = load_config()
        lot_map = cfg.get("lot_mapping", {})

        new_lot_key = f"lot_{len(lot_map) + 1}"


        lot_map[new_lot_key] = {
            "name": name,
            "gift_id": 1,
            "gift_name":""
        }

        cfg["lot_mapping"] = lot_map
        save_config(cfg)


        def process_new_lot2(message: types.Message):
            try:
                gift_id = int(message.text.strip())
            except ValueError:
                kb = InlineKeyboardMarkup()
                kb.add(InlineKeyboardButton("🔙 Назад", callback_data="to_setting"))
                bot.send_message(message.chat.id, "❌ Ошибка: GIFT ID должен быть числом.", reply_markup=kb)
                return
            lot_map[new_lot_key]["gift_id"] = gift_id
            cfg["lot_mapping"] = lot_map
            save_config(cfg)

        msg = bot.send_message(message.chat.id, "Введите GIFT ID для добавления:")
        bot.register_next_step_handler(msg, process_new_lot2)

        while True:
            cfg = load_config()
            lot_map = cfg.get("lot_mapping", {})
            if lot_map[new_lot_key]['gift_id'] == 1:
                time.sleep(2)
            else:
                break

        def process_new_lot3(message: types.Message):
            gift_name = message.text.strip()
            lot_map[new_lot_key]["gift_name"] = gift_name
            cfg["lot_mapping"] = lot_map
            save_config(cfg)

        msg = bot.send_message(message.chat.id, "Введите GIFT Name для добавления:")
        bot.register_next_step_handler(msg, process_new_lot3)

        while True:
            cfg = load_config()
            lot_map = cfg.get("lot_mapping", {})
            if lot_map[new_lot_key]['gift_name'] == "":
                time.sleep(2)
            else:
                break

        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🔙 К настройкам", callback_data="to_setting"))
        bot.send_message(message.chat.id, f"✅ Добавлен новый лот {new_lot_key} с названием: {name}", reply_markup=kb)

    def process_id_change(message: types.Message, lot_key: str):
        try:
            new_id = int(message.text.strip())
        except ValueError:
            bot.send_message(message.chat.id, "❌ Ошибка: GIFT ID должно быть числом.")
            return
        cfg = load_config()
        lot_map = cfg.get("lot_mapping", {})
        if lot_key not in lot_map:
            bot.send_message(message.chat.id, f"❌ Лот {lot_key} не найден.")
            return
        lot_map[lot_key]["gift_id"] = new_id
        cfg["lot_mapping"] = lot_map
        save_config(cfg)
        kb_ = InlineKeyboardMarkup()
        kb_.add(InlineKeyboardButton("◀️ К лотам", callback_data="return_t_lot"))
        bot.send_message(message.chat.id, f"✅ GIFT ID для {lot_key} изменён на {new_id}.", reply_markup=kb_)

    def process_name_change(message: types.Message, lot_key: str):
        new_id = message.text.strip()
        cfg = load_config()
        lot_map = cfg.get("lot_mapping", {})
        if lot_key not in lot_map:
            bot.send_message(message.chat.id, f"❌ Лот {lot_key} не найден.")
            return
        lot_map[lot_key]["gift_name"] = new_id
        cfg["lot_mapping"] = lot_map
        save_config(cfg)
        kb_ = InlineKeyboardMarkup()
        kb_.add(InlineKeyboardButton("◀️ К лотам", callback_data="return_t_lot"))
        bot.send_message(message.chat.id, f"✅ GIFT NAME для {lot_key} изменён на {new_id}.", reply_markup=kb_)


    def delete_one_lot(call: types.CallbackQuery, lot_key: str):
        cfg = load_config()
        lot_map = cfg.get("lot_mapping", {})
        if lot_key in lot_map:
            del lot_map[lot_key]
            cfg["lot_mapping"] = lot_map
            reindex_lots(cfg)
            bot.edit_message_text(f"✅ Лот {lot_key} удалён и лоты переиндексированы.", call.message.chat.id, call.message.message_id, reply_markup=generate_lots_keyboard(0))
        else:
            bot.edit_message_text(f"❌ Лот {lot_key} не найден.", call.message.chat.id, call.message.message_id)


    def auto_gifts_settings(message: types.Message):
        cfg = load_config()
        lmap = cfg.get("lot_mapping", {})
        auto_refunds = cfg.get("auto_refunds", True)
        active_lots = cfg.get("active_lots", True)
        loop = asyncio.new_event_loop()
        try:
            stars = loop.run_until_complete(get_balance())
        finally:
            loop.close()

        txt = f"""
<b>⚙️ Настройки Auto Gifts v{VERSION}</b>
Разработчик: {CREDITS}

📊 <b>Инфо:</b> Лотов: {len(lmap)}
🌟 <b>Баланс звезд:</b> {stars}
📝 <b>Описание:</b> {DESCRIPTION}
        """.strip()

        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("🛠️ Лоты", callback_data="lot_se"),
            InlineKeyboardButton("📥 Загрузить лоты", callback_data="upload_lots"),
            InlineKeyboardButton(f"{'🟢' if auto_refunds else '🔴'} Автовозвраты", callback_data="auto_refund"),
            InlineKeyboardButton(f"{'🟢' if active_lots else '🔴'} Лоты", callback_data="active_lot"),
            InlineKeyboardButton("➕ Добавить лот", callback_data="add_lot"),
            InlineKeyboardButton("📊 Статистика", callback_data="show_stat"),
        )
        bot.send_message(message.chat.id, txt, parse_mode='HTML', reply_markup=kb)


    @bot.callback_query_handler(func=lambda call: call.data == "add_lot")
    def add_new_lot(call: types.CallbackQuery):
        bot.delete_message(call.message.chat.id, call.message.message_id)

        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🔙 Назад", callback_data="to_setting"))
        msg = bot.send_message(call.message.chat.id, "Введите ID лота для добавления:", reply_markup=kb)
        bot.register_next_step_handler(msg, process_new_lot)

    @bot.callback_query_handler(func=lambda call: call.data.startswith("ed_lot_"))
    def edit_lot_callback(call: types.CallbackQuery):
        lot_key = call.data.split("_", 2)[2]
        edit_lot(call, lot_key)

    @bot.callback_query_handler(func=lambda call: call.data == "return_t_lot")
    def return_to_lots(call: types.CallbackQuery):
        bot.edit_message_text("Выберите лот:", call.message.chat.id, call.message.message_id, reply_markup=generate_lots_keyboard(0))

    @bot.callback_query_handler(func=lambda call: call.data.startswith("deletin_one_lot_"))
    def delete_one_lot_callback(call: types.CallbackQuery):
        lot_key = call.data.split("_", 3)[3]
        delete_one_lot(call, lot_key)

    @bot.callback_query_handler(func=lambda call: call.data.startswith("changing_lot_"))
    def change_name(call: types.CallbackQuery):
        lot_key = call.data.split("_", 2)[2]
        msg_ = bot.edit_message_text(f"Введите новое название для {lot_key}:", call.message.chat.id, call.message.message_id)
        bot.register_next_step_handler(msg_, process_lot_change, lot_key)

    @bot.callback_query_handler(func=lambda call: call.data.startswith("changing_id_"))
    def change_id(call: types.CallbackQuery):
        lot_key = call.data.split("_", 2)[2]
        msg_ = bot.edit_message_text(f"Введите новый GIFT ID для {lot_key}:", call.message.chat.id, call.message.message_id)
        bot.register_next_step_handler(msg_, process_id_change, lot_key)

    @bot.callback_query_handler(func=lambda call: call.data.startswith("changing_nam_"))
    def change_id(call: types.CallbackQuery):
        lot_key = call.data.split("_", 2)[2]
        msg_ = bot.edit_message_text(f"Введите новый GIFT NAME для {lot_key}:", call.message.chat.id, call.message.message_id)
        bot.register_next_step_handler(msg_, process_name_change, lot_key)

    @bot.callback_query_handler(func=lambda call: call.data == "active_lot")
    def lot_active(call: types.CallbackQuery):
        state = toggle_subcat_status(c,3064)
        cfg = load_config()
        if state is False:
            stat = "деактивированы"
            cfg['active_lots'] = False
        else:
            stat = "активированы"
            cfg['active_lots'] = True
        save_config(cfg)
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🔙 Назад", callback_data="to_setting"))
        bot.edit_message_text(f"Лоты успешно {stat} ", call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=kb)
    @bot.callback_query_handler(func=lambda call: call.data == "lot_se")
    def lot_set(call: types.CallbackQuery):
        bot.edit_message_text("Выберите лот:", call.message.chat.id, call.message.message_id, reply_markup=generate_lots_keyboard(0))

    @bot.callback_query_handler(func=lambda call: call.data.startswith("pr_page_") or call.data.startswith("ne_page_"))
    def page_navigation(call: types.CallbackQuery):
        try:
            page_ = int(call.data.split("_")[-1])
        except ValueError:
            page_ = 0
        bot.edit_message_text("Выберите лот:", call.message.chat.id, call.message.message_id, reply_markup=generate_lots_keyboard(page_))

    @bot.callback_query_handler(func=lambda call: call.data == "to_setting")
    def to_settings(call: types.CallbackQuery):
        cfg = load_config()
        lmap = cfg.get("lot_mapping", {})
        auto_refunds = cfg.get("auto_refunds", True)
        active_lots = cfg.get("active_lots", True)
        loop = asyncio.new_event_loop()
        try:
            stars = loop.run_until_complete(get_balance())
        finally:
            loop.close()

        txt = f"""
<b>⚙️ Настройки Auto Gifts v{VERSION}</b>
Разработчик: {CREDITS}

📊 <b>Инфо:</b> Лотов: {len(lmap)}
🌟 <b>Баланс звезд:</b> {stars}
📝 <b>Описание:</b> {DESCRIPTION}
        """.strip()

        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("🛠️ Лоты", callback_data="lot_se"),
            InlineKeyboardButton("📥 Загрузить лоты", callback_data="upload_lots"),
            InlineKeyboardButton(f"{'🟢' if auto_refunds else '🔴'} Автовозвраты", callback_data="auto_refund"),
            InlineKeyboardButton(f"{'🟢' if active_lots else '🔴'} Лоты", callback_data="active_lot"),
            InlineKeyboardButton("➕ Добавить лот", callback_data="add_lot"),
            InlineKeyboardButton("📊 Статистика", callback_data="show_stat"),
        )
        bot.edit_message_text(txt, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=kb)


    @bot.callback_query_handler(func=lambda call: call.data == "show_stat")
    def show_orders(call: types.CallbackQuery):
        stats = get_statistics()
        if not stats:
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("🔙 Назад", callback_data="to_setting"))
            bot.edit_message_text("❌ Нет данных о заказах.", call.message.chat.id, call.message.message_id, reply_markup=kb)
            return

        text = f"""
📊 Статистика заказов

⏰ За последние 24 часа:
🔥 Заказов: {stats['day_orders']}
💸 Общая сумма: {stats['day_total']} руб.
💰 Чистая прибыль: <b>{stats['day_profit']} руб.</b>
🌟 Лучший товар: <code>{stats['best_day_service']}</code>

📅 За последнюю неделю:
🔥 Заказов: {stats['week_orders']}
💸 Общая сумма: {stats['week_total']} руб.
💰 Чистая прибыль: <b>{stats['week_profit']} руб.</b>
🌟 Лучший товар: <code>{stats['best_week_service']}</code>

🗓 За последний месяц:
🔥 Заказов: {stats['month_orders']}
💸 Общая сумма: {stats['month_total']} руб.
💰 Чистая прибыль: <b>{stats['month_profit']} руб.</b>
🌟 Лучший товар: <code>{stats['best_month_service']}</code>

📈 За все время:
🔥 Заказов: {stats['all_time_orders']}
💸 Общая сумма: {stats['all_time_total']} руб.
💰 Чистая прибыль: <b>{stats['all_time_profit']} руб.</b>
🌟 Лучший товар: <code>{stats['best_all_time_service']}</code>
        """.strip()

        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🔙 Назад", callback_data="to_setting"))
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=kb)

    @bot.callback_query_handler(func=lambda call: call.data == "upload_lots")
    def upload_lots_json(call: types.CallbackQuery):
        user_id = call.from_user.id
        waiting_for_lots_upload.add(user_id)
        logger.info(f"Добавлен пользователь {user_id} в waiting_for_lots_upload: {waiting_for_lots_upload}")
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🔙 Назад", callback_data="to_setting"))

        bot.edit_message_text("Пришлите файл JSON (можно любым названием).", call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=kb)

    @bot.callback_query_handler(func=lambda call: call.data == "auto_refund")
    def auto_refund(call: types.CallbackQuery):
        cfg = load_config()
        if cfg['auto_refunds'] is True:
            cfg['auto_refunds'] = False
            auto_refunds = "выключены"
        else:
            cfg['auto_refunds'] = True
            auto_refunds = "включены"
        save_config(cfg)
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🔙 Назад", callback_data="to_setting"))
        bot.edit_message_text(f"Автовозвраты успешно {auto_refunds} ", call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=kb)

    c.telegram.msg_handler(start, commands=["start_gifts"])
    c.telegram.msg_handler(stop, commands=["stop_gifts"])
    c.telegram.msg_handler(auto_gifts_settings, commands=["auto_gifts_settings"])
    c.add_telegram_commands(UUID, [
        ("start_gifts", "старт автопродажи", True),
        ("stop_gifts","стоп автопродажи", True),
        ("auto_gifts_settings", "Настройки автопополнения", True),
    ])

def message_hook(c: Cardinal,e:NewMessageEvent):
    global queue
    if not RUNNING:
        return
    tg = c.telegram
    bot = tg.bot
    my_id = c.account.id
    if e.message.author_id == my_id:
        return

    msg_text = e.message.text.strip()
    msg_author_id = e.message.author_id
    msg_chat_id = e.message.chat_id

    logger.debug(f"{LOGGER_PREFIX} Новое сообщение от {e.message.author}: {msg_text}")

    for buyer_id, data in queue.items():
        if buyer_id == msg_author_id:
            if data["step"] == "await_username":
                username_match = re.search(r'.*?@(.+)', msg_text)
                if not username_match:
                    c.send_message(msg_chat_id, "❌ Юзернейм неверный!\n📍 Отправьте еще раз в формате @username")
                    return
                username = username_match.group(1)
                usss = msg_text
                order_id = data['order_id']
                logger.debug(f"{LOGGER_PREFIX} Обрабатываю username {username}")
                loop = asyncio.new_event_loop()
                try:
                    name = loop.run_until_complete(check_username(c,msg_chat_id,username,order_id))
                finally:
                    loop.close()
                if name is None:
                    return
                order_amount = data["order_amount"]
                amount = data["amount"]
                gift_name = data['gift_name']
                order_text = f'👤 Юзернейм: {usss}\n✍️ Ник: {name}\n🌟 Подарки: {order_amount} по {amount} звезд ({gift_name})\n✅ Если данные верны, отправьте +\n❌ Если вы хотите изменить данные, отправьте -'
                c.send_message(msg_chat_id, order_text)
                data['name'] = name
                data["step"] = "await_confirm"
                data['username'] = username
                logger.debug(f"{LOGGER_PREFIX} Обработал username {username}")
                return

            elif data["step"] == "await_confirm":
                order_id = data["order_id"]
                amount = data["amount"]
                order_amount = data["order_amount"]
                username = data['username']
                name = data['name']
                order_time = data['order_time']
                gift_id = data['gift_id']
                gift_name = data['gift_name']
                order_price = data['order_price']
                order_profit = data['order_profit']
                loop = asyncio.new_event_loop()
                try:
                    stars = loop.run_until_complete(get_balance())
                finally:
                    loop.close()
                if order_amount * amount < stars:
                    try:
                        loop = asyncio.new_event_loop()
                        try:
                            result = loop.run_until_complete(buy_gifts(c,msg_chat_id,username,gift_id,order_amount,order_id))
                        finally:
                            loop.close()
                        if result is None:
                            data["step"] = "await_username"
                            return
                        order_url = f"https://funpay.com/orders/{order_id}/"
                        success_text =f"🎁 Подарки отправлены!\n👌 Не забудьте подтвердить заказ и оставить отзыв\n📍 Ссылка на подтверждение заказа: {order_url}"
                        c.send_message(msg_chat_id, success_text)
                        logger.info(f"{LOGGER_PREFIX} Заказ #{order_id} успешно выполнен")
                        time = datetime.now().strftime("%H:%M:%S")
                        text = (
                            f"🎉 Заказ <a href='https://funpay.com/orders/{order_id}/'>{order_id}</a> выполнен!\n\n"
                            f"👤 Юзернейм: @{username}\n"
                            f"✍️ Ник: {name}\n"
                            f"🎁 Подарки: {order_amount} по {amount} ({gift_name})\n"
                            f"💸 Сумма заказа: {order_price}\n"
                            f"💰 Профит: {order_profit}\n\n"
                            f"⌛️ Время добавления в очередь: <code>{order_time}</code>\n"
                            f"⌛️ Время выполнения: <code>{time}</code>\n"
                        )
                        for user_id in get_authorized_users():
                            bot.send_message(
                                user_id,
                                text = text,
                                parse_mode='HTML',
                            )
                        queue.pop(buyer_id, None)
                        return
                    except StargiftUsageLimited:
                        logger.error("Этот подарок уже распродан!")
                        for user_id in get_authorized_users():
                            bot.send_message(
                                user_id,
                                text = text,
                                parse_mode='HTML',
                            )
                    except Exception as e:
                        logger.error(f"{LOGGER_PREFIX} Ошибка:{e}")
                        for user_id in get_authorized_users():
                            bot.send_message(
                                user_id,
                                text = text,
                                parse_mode='HTML',
                            )
                        c.send_message(msg_chat_id,"❌ Что-то сломалось!\n📌 Напишите в чате !help чтобы позвать продавца")
                        data["step"] = "await_username"
                        return
                else:
                    logger.warning(f"Сидор оплашал,плагин бахнул...")
                    cfg = load_config()
                    auto_refunds = cfg.get("auto_refunds", True)
                    if auto_refunds:
                        c.account.refund(order_id)
                        c.send_message(msg_chat_id,"❌ Баланса не хватило для оплаты,поэтому был осуществлен возврат средств,приношу свои искренние извинения")
                    else:
                        c.send_message(msg_chat_id,"❌ Баланса не хватило для оплаты, возврат средств требует ручного подтверждения. Напишите !help чтобы позвать продавца")
                        for user_id in get_authorized_users():
                            bot.send_message(
                                user_id,
                                text = f"⚠️ Требуется ручной возврат средств для заказа #{order_id}\n🔗 Перейдите по ссылке, чтобы вернуть деньги: {order_url}",
                                parse_mode='HTML',
                            )
                    queue.pop(buyer_id, None)
                    state = is_subcat_active(c,3064)
                    if state is False:
                        return
                    status = toggle_subcat_status(c,3064)
                    if status is False:
                        cfg['active_lots'] = False
                    save_config(cfg)
                    #kb = InlineKeyboardMarkup()
                    #kb.add(InlineKeyboardButton("🔙 Назад", callback_data="to_setting"))
                    for user_id in get_authorized_users():
                        bot.send_message(
                            user_id,
                            text = f"✅ Звезды закончились,лоты успешно деактивированы",
                            parse_mode='HTML',
                            #reply_markup=kb
                        )
                    return


def order_hook(c: Cardinal,e:NewOrderEvent):
    if not RUNNING:
        return
    order = e.order
    order_description = order.description
    gift_id,gift_name = get_tg_id_by_description(order_description)
    if gift_id is None or gift_name is None:
        logger.info("Лот не найден по описанию. Пропуск обработки.")
        return
    loop = asyncio.new_event_loop()
    try:
        amount = loop.run_until_complete(get_amount(gift_id))
    finally:
        loop.close()
    order_id = order.id
    order_price = order.price
    buyer_id = int(order.buyer_id)
    order_amount = int(order.amount)
    order_fulldata = c.account.get_order(order_id)
    chat_id = order_fulldata.chat_id
    order_profit = round(order_price - order_amount * amount * 1.35)
    save_order_info(order_id, order_price, order_description, order_profit)

    logger.info(f"{LOGGER_PREFIX} 🛒 Оплачен заказ #{order_id} на {order_amount} подарков ({gift_name})")
    start_text = f"🛒 Оплачен заказ #{order_id} на {order_amount} подарков ({gift_name})\n🎁 Пожалуйста, предоставьте ваш юзернейм в формате: @username. Без корректного @username подарки не будут выданы!"

    logger.debug(f"{LOGGER_PREFIX} #{order_id} | gift_id: {gift_id}")
    logger.debug(f"{LOGGER_PREFIX} #{order_id} | gift_name: {gift_name}")

    if gift_id is None:
        c.send_message(chat_id,"❌ У пользователя неверно настроены лоты,заказ не будет выполнен")
        return
    c.send_message(chat_id,start_text)
    order_time = datetime.now().strftime("%H:%M:%S")
    queue[buyer_id] =    {
                                    "order_id": order_id,
                                    "chat_id": chat_id,
                                    "step": "await_username",
                                    "amount":amount,
                                    "order_amount":order_amount,
                                    "order_time":order_time,
                                    "gift_id":gift_id,
                                    "gift_name":gift_name,
                                    "order_price":order_price,
                                    "order_profit":order_profit
                                    }
    logger.debug(f"{LOGGER_PREFIX} Очередь: {queue}")



BIND_TO_PRE_INIT = [init_commands]
BIND_TO_NEW_MESSAGE = [message_hook]
BIND_TO_NEW_ORDER = [order_hook]
BIND_TO_DELETE = None