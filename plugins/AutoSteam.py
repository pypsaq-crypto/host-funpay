from __future__ import annotations
import json
import time
import requests
import uuid
import re
import logging
import threading
from queue import Queue
from typing import TYPE_CHECKING
from os.path import exists
import telebot
from telebot.types import InlineKeyboardMarkup as K, InlineKeyboardButton as B
from FunPayAPI.updater.events import NewMessageEvent
from FunPayAPI.types import OrderStatuses, SubCategoryTypes
from FunPayAPI.common import exceptions
import tg_bot
from tg_bot import CBT
from bs4 import BeautifulSoup
import os
import datetime

if TYPE_CHECKING:
    from cardinal import Cardinal

logger = logging.getLogger("FPC.auto_steam_top_up_plugin")
LOGGER_PREFIX = "[AUTOSTEAM PLUGIN]"

NAME = "Auto Steam"
VERSION = "2.0"
DESCRIPTION = "Автоматическое пополнение Steam через API NSGifts"
CREDITS = "@StockHostBot | @wormdcShop_bot "
UUID = "1db83dd6-71bf-49e0-b660-773add7a3100"
SETTINGS_PAGE = True

PLUGIN_DIR = "storage/plugins/steam_auto_top_up"
os.makedirs(PLUGIN_DIR, exist_ok=True)

SETTINGS_FILE = os.path.join(PLUGIN_DIR, "settings.json")
ORDERS_FILE = os.path.join(PLUGIN_DIR, "orders.json")
BLACK_LIST_FILE = os.path.join(PLUGIN_DIR, "black_list_users.json")

SETTINGS = {
    "lot_currency": {},
    "api_login": "",
    "api_password": "",
    "auto_refund_on_error": True,
    "notification_chats": [],
    "notifications_enabled": True,
    "notification_types": {"success": True, "error": True, "refund": True, "balance": True},
    "confirmation_reminder": True,
    "reminder_time": 2.5,
    "deactivate_lots_on_insufficient_funds": True,
    "balance_threshold": 30.0,
    "low_balance_notified": False,
    "auto_response_on_arbitrage": True,
    "order_verification_enabled": True
}

TOKEN_DATA = {"token": None, "expiry": 0}
FUNPAY_STATES = {}
USER_ORDER_QUEUES = {}
SUCCESSFUL_ORDERS = {}
previous_balance = None

tg = None
bot = None
cardinal_instance = None

MIN_AMOUNTS = {"RUB": 25, "UAH": 10, "KZT": 70}

def verify_order_exists(cardinal: Cardinal, order_id: str) -> bool:
    """
    Проверяет, существует ли заказ на самом деле.
    
    :param cardinal: Экземпляр Cardinal
    :param order_id: ID заказа для проверки
    :return: True если заказ существует и принадлежит продавцу, False в противном случае
    """
    try:
        # Убираем символ # если он есть
        clean_order_id = order_id.replace("#", "")
        
        # Пытаемся получить заказ через API
        order = cardinal.account.get_order(clean_order_id)
        
        # Проверяем, что заказ принадлежит текущему продавцу
        if order.seller_id == cardinal.account.id:
            logger.info(f"{LOGGER_PREFIX} Заказ #{clean_order_id} подтвержден как подлинный")
            return True
        else:
            logger.warning(f"{LOGGER_PREFIX} Заказ #{clean_order_id} не принадлежит продавцу (продавец: {order.seller_id}, текущий: {cardinal.account.id})")
            return False
            
    except exceptions.UnauthorizedError:
        logger.error(f"{LOGGER_PREFIX} Ошибка авторизации при проверке заказа #{order_id}")
        return False
    except exceptions.RequestFailedError as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка запроса при проверке заказа #{order_id}: {e.short_str()}")
        return False
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Неожиданная ошибка при проверке заказа #{order_id}: {e}")
        return False

def extract_order_id_from_message(message_text: str) -> str | None:
    """
    Извлекает ID заказа из текста сообщения.
    
    :param message_text: Текст сообщения
    :return: ID заказа или None если не найден
    """
    # Ищем паттерн #XXXXXXX в тексте
    match = re.search(r'#([A-Z0-9]+)', message_text)
    if match:
        return match.group(1)
    return None

def load_settings():
    global SETTINGS
    if exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            SETTINGS.update(json.load(f))

def save_settings():
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(SETTINGS, f, indent=4, ensure_ascii=False)

def load_orders():
    if exists(ORDERS_FILE):
        with open(ORDERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def save_orders(orders):
    with open(ORDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(orders, f, indent=4, ensure_ascii=False)

def load_black_list():
    if exists(BLACK_LIST_FILE):
        with open(BLACK_LIST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def save_black_list(black_list):
    with open(BLACK_LIST_FILE, "w", encoding="utf-8") as f:
        json.dump(black_list, f, indent=4, ensure_ascii=False)

def get_balance():
    try:
        token = get_token()
        response = requests.post("https://api.ns.gifts/api/v1/check_balance", headers={"Authorization": f"Bearer {token}"})
        if response.status_code == 200:
            data = response.json()
            logger.info(f"{LOGGER_PREFIX} Баланс успешно получен!")
            logger.info(f"{LOGGER_PREFIX} Баланс: {data}")
            if isinstance(data, (int, float)):
                return data
            return data.get("balance", 0)
        return 0
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка при получении баланса: {e}")
        return 0

def check_balance_periodically(cardinal: Cardinal):
    global previous_balance
    while True:
        time.sleep(300)
        balance = get_balance()
        if balance is not None:
            if previous_balance is not None and balance > previous_balance:
                send_notification(cardinal, "", "balance", {"message": f"<b>🔔 Уведомление о пополнении баланса</b>\n\n<b>L Новый баланс:</b> <code>{balance:.2f}$</code>\n<b>• Дата:</b> <code>{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}</code>"}, parse_mode="HTML")
            if balance < SETTINGS["balance_threshold"] and not SETTINGS["low_balance_notified"]:
                send_notification(cardinal, "", "balance", {"message": f"<b>🔔 Уведомление о низком балансе</b>\n\n<b>L Текущий баланс:</b> <code>{balance:.2f}$</code>\n<b>• Дата:</b> <code>{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}</code>"}, parse_mode="HTML")
                SETTINGS["low_balance_notified"] = True
                save_settings()
            elif balance >= SETTINGS["balance_threshold"] and SETTINGS["low_balance_notified"]:
                SETTINGS["low_balance_notified"] = False
                save_settings()
            previous_balance = balance

def format_amount(amount: float, currency: str) -> str:
    return f"{int(amount)} {currency}"

def get_currency_rates():
    try:
        token = get_token()
        response = requests.post("https://api.ns.gifts/api/v1/steam/get_currency_rate", headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        if response.status_code == 200:
            data = response.json()
            logger.info(f"{LOGGER_PREFIX} Получены курсы валют: {data}")
            return data
        logger.error(f"{LOGGER_PREFIX} Ошибка API при получении курсов валют: {response.status_code} - {response.text}")
        return None
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка при получении курсов валют: {e}")
        return None

def get_max_amounts():
    balance = get_balance()
    if balance is None:
        logger.warning(f"{LOGGER_PREFIX} Не удалось получить баланс для расчета максимальных сумм")
        return {"RUB": 0, "UAH": 0, "KZT": 0}
    rates = get_currency_rates()
    if rates is None:
        logger.warning(f"{LOGGER_PREFIX} Не удалось получить курсы валют для расчета максимальных сумм")
        return {"RUB": 0, "UAH": 0, "KZT": 0}
    max_amounts = {}
    for currency in ["RUB", "UAH", "KZT"]:
        rate_key = f"{currency.lower()}/usd"
        rate = rates.get(rate_key)
        logger.info(f"{LOGGER_PREFIX} Курс для {currency}: {rate_key} = {rate}")
        if rate:
            max_amounts[currency] = float(balance) * float(rate)
            logger.info(f"{LOGGER_PREFIX} Максимальная сумма для {currency}: {max_amounts[currency]} (баланс: {balance}, курс: {rate})")
        else:
            max_amounts[currency] = 0
            logger.warning(f"{LOGGER_PREFIX} Курс для {currency} не найден в ответе API")
    logger.info(f"{LOGGER_PREFIX} Итоговые максимальные суммы: {max_amounts}")
    return max_amounts

def open_settings(call: telebot.types.CallbackQuery, cardinal: Cardinal):
    if call.message.chat.id not in SETTINGS["notification_chats"]:
        SETTINGS["notification_chats"].append(call.message.chat.id)
        save_settings()
    kb = K()
    kb.row(B("✏️ Настроить API", callback_data="as_set_api"), B("🚫 Черный список", callback_data="as_black_list"))
    kb.row(B(f"{'🔔' if SETTINGS['notifications_enabled'] else '🔕'} Уведомления", callback_data="as_toggle_notifications"), B(f"{'🟢' if SETTINGS['auto_refund_on_error'] else '🔴'} Авто-возврат", callback_data="as_toggle_auto_refund"))
    kb.row(B(f"{'🛡️' if SETTINGS['order_verification_enabled'] else '⚠️'} Проверка заказов", callback_data="as_toggle_order_verification"), B("📊 Статистика", callback_data="as_statistics"))
    kb.row(B("📜 История заказов", callback_data="steam_order_history:1"))
    lots = cardinal.account.get_my_subcategory_lots(1086)
    all_active = all(lot.active for lot in lots) if lots else False
    toggle_button = B("🔴 Деактивировать лоты", callback_data="as_toggle_lots_deactivate") if all_active else B("🟢 Активировать лоты", callback_data="as_toggle_lots_activate")
    kb.row(toggle_button)
    kb.row(B("🔄 Обновить информацию", callback_data="as_refresh_info"))
    # Дополнительные ссылки в главном меню
    kb.row(B("🔗 Вход NSGifts", url="https://wholesale.ns.gifts/login/"), B("🆘 Поддержка NSGifts", url="https://t.me/ns_gifts"))
    kb.row(B("📖 Инструкция", callback_data="as_instruction"), B("📣 Наш канал", url="https://t.me/stockhostnews"))
    kb.add(B("◀️ Назад", callback_data=f"{CBT.EDIT_PLUGIN}:{UUID}:0"))
    balance = get_balance()
    balance_text = f"{balance:.2f}" if balance is not None else "ошибка получения"
    login = SETTINGS["api_login"] or "(От нсгифтс)"
    password = SETTINGS["api_password"] or "(от нс гифтс)"
    active_lots = sum(1 for lot in lots if lot.active) if lots else 0
    orders = load_orders()
    total_sales = len(orders)
    black_list = load_black_list()
    black_list_count = len(black_list) if black_list else 0
    currency_rates = get_currency_rates()
    logger.info(f"{LOGGER_PREFIX} Курсы валют для отображения: {currency_rates}")
    if currency_rates:
        rates_items = []
        for k, v in currency_rates.items():
            if k.upper() != 'DATE':
                rates_items.append(f"L {k.upper()}: <code>{v}</code>")
        rates_text = "<b>• Курсы валют:</b>\n" + "\n".join(rates_items) + "\n"
        logger.info(f"{LOGGER_PREFIX} Сформированный текст курсов валют: {rates_text}")
    else:
        rates_text = "<b>• Курсы валют:</b> <code>ошибка получения</code>\n"
        logger.warning(f"{LOGGER_PREFIX} Не удалось получить курсы валют для отображения")
    funpay_nick = cardinal.account.username if hasattr(cardinal.account, 'username') and cardinal.account.username else "ошибка"
    current_time = datetime.datetime.now().strftime("%H:%M:%S")
    settings_text = (
        f"<b>🚀 Auto Steam v{VERSION}</b> — <i>автопополнение Steam</i> (<code>{funpay_nick}</code>)\n\n"
        f"<b>⚠️ Внимание:</b> пополнение в <b>рублях (RUB)</b> временно недоступно.\n\n"
        f"<b>💫 FunPay:</b>\n"
        f"  • Ник: <code>{funpay_nick}</code>\n"
        f"  • Активные продажи: <code>{active_lots}</code>\n"
        f"  • Продано пополнений: <code>{total_sales}</code>\n"
        f"  • Черный список логинов: <code>{black_list_count}</code>\n"
        f"  • Проверка заказов: <code>{'Включена' if SETTINGS['order_verification_enabled'] else 'Выключена'}</code>\n\n"
        f"<b>💛 NSGifts:</b>\n"
        f"  • Логин: <code>{login}</code>\n"
        f"  • Пароль: <tg-spoiler>{password}</tg-spoiler>\n"
        f"  • Баланс: <code>{balance_text} $</code>\n\n"
        f"{rates_text}"
        f"<b>• Обновлено:</b> <code>{current_time}</code>"
    )
    bot.edit_message_text(settings_text, call.message.chat.id, call.message.id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(call.id)

def statistics(call):
    chat_id = call.message.chat.id
    orders = load_orders()
    all_orders = [order for order in orders if order.get("status") == "success"]
    now = time.time()
    day_orders = [p for p in all_orders if p.get("timestamp", 0) >= now - 86400]
    week_orders = [p for p in all_orders if p.get("timestamp", 0) >= now - 604800]
    month_orders = [p for p in all_orders if p.get("timestamp", 0) >= now - 2592000]
    day_count, week_count, month_count = len(day_orders), len(week_orders), len(month_orders)
    day_sum = sum(float(p.get("sum", 0)) for p in day_orders)
    week_sum = sum(float(p.get("sum", 0)) for p in week_orders)
    month_sum = sum(float(p.get("sum", 0)) for p in month_orders)
    stats_text = f"📊 <b>Статистика продаж (<code>{cardinal_instance.account.username}</code>)</b>\n\n🤑 <b>Продажи:</b>\nL За день: <code>{day_count} шт. ({round(day_sum, 2)} ₽)</code>\nL За неделю: <code>{week_count} шт. ({round(week_sum, 2)} ₽)</code>\nL За месяц: <code>{month_count} шт. ({round(month_sum, 2)} ₽)</code>"
    bot.edit_message_text(stats_text, chat_id, call.message.id, reply_markup=K().add(B("◀️ Назад", callback_data=f"{CBT.PLUGIN_SETTINGS}:{UUID}:0")), parse_mode="HTML")
    bot.answer_callback_query(call.id)

def toggle_lots(call: telebot.types.CallbackQuery, cardinal: Cardinal):
    action = call.data.split('_')[-1]
    try:
        lots = cardinal.account.get_my_subcategory_lots(1086)
        updated = False
        for lot in lots:
            if action == 'deactivate' and lot.active or action == 'activate' and not lot.active:
                lot_fields = cardinal.account.get_lot_fields(lot.id)
                lot_fields.active = action == 'activate'
                cardinal.account.save_lot(lot_fields)
                updated = True
                logger.info(f"{LOGGER_PREFIX} Лот {lot.id} успешно {'деактивирован' if action == 'deactivate' else 'активирован'}")
                time.sleep(0.7)
        bot.answer_callback_query(call.id, f"{'✅ Лоты Steam деактивированы' if action == 'deactivate' else '✅ Лоты Steam активированы'}" if updated else f"ℹ️ Все ваши лоты уже {'деактивированы' if action == 'deactivate' else 'активны'}")
        logger.info(f"{LOGGER_PREFIX} Все лоты успешно {'деактивированы' if action == 'deactivate' else 'активированы'}")
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка при {'деактивации' if action == 'deactivate' else 'активации'} лотов: {e}")
        bot.answer_callback_query(call.id, f"❌ Ошибка при {'деактивации' if action == 'deactivate' else 'активации'} лотов")
    open_settings(call, cardinal)

def deactivate_lots_on_error(cardinal: Cardinal):
    for lot in [lot for lot in cardinal.account.get_user(cardinal.account.id).get_lots() if lot.subcategory.id == 1086]:
        lot_fields = cardinal.account.get_lot_fields(lot.id)
        lot_fields.active = False
        cardinal.account.save_lot(lot_fields)
        time.sleep(0.7)
    if SETTINGS["notification_types"]["error"]:
        send_notification(cardinal, "", "error", {"message": f"<b>🔔 Уведомление об ошибке</b>\n\n<b>L Описание:</b> <code>Лоты Steam деактивированы из-за недостатка средств</code>\n\n<b>• Дата:</b> <code>{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}</code>"}, parse_mode="HTML")

def refresh_info(call: telebot.types.CallbackQuery, cardinal: Cardinal):
    balance = get_balance()
    bot.answer_callback_query(call.id, "✅ Информация успешно обновлена" if balance is not None else "❌ Ошибка при обновлении информации")
    open_settings(call, cardinal)

def show_instruction(call: telebot.types.CallbackQuery):
    chat_id = call.message.chat.id
    text = (
        "<b>📖 Инструкция по авто-пополнению Steam</b>\n\n"
        "1) Откройте: Настройка API → укажите логин и пароль NSGifts.\n"
        "2) Включите уведомления, чтобы получать статусы заказов.\n"
        "3) Проверьте наличие активных лотов Steam и баланс NSGifts.\n"
        "4) После покупки на FunPay в ответ отправьте логин Steam, подтвердите «+».\n\n"
        "<b>⚠️ Важно:</b> пополнение в <b>рублях (RUB)</b> временно недоступно. Используйте UAH/KZT.\n\n"
        "Полезное: подписывайтесь на наш канал — <a href=\"https://t.me/stockhostnews\">Stock Host Новости</a>."
    )
    kb = K().row(B("📣 Наш канал", url="https://t.me/stockhostnews"))
    kb.add(B("◀️ Назад", callback_data=f"{CBT.PLUGIN_SETTINGS}:{UUID}:0"))
    bot.edit_message_text(text, chat_id, call.message.id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(call.id)

def black_list_menu(call: telebot.types.CallbackQuery, cardinal: Cardinal):
    black_list = load_black_list()
    text = "Список юзернеймов в Черном Списке\n" + "\n".join(black_list) if black_list else "Список юзернеймов в Черном Списке\nПусто"
    kb = K().row(B("➕ Добавить", callback_data="as_add_to_black_list"), B("➖ Удалить", callback_data="as_remove_from_black_list"))
    kb.add(B("◀️ Назад", callback_data=f"{CBT.PLUGIN_SETTINGS}:{UUID}:0"))
    bot.edit_message_text(text, call.message.chat.id, call.message.id, reply_markup=kb)
    bot.answer_callback_query(call.id)

def add_to_black_list(call: telebot.types.CallbackQuery, cardinal: Cardinal):
    kb = K().add(B("❌ Отмена", callback_data="cancel_input"))
    msg = bot.send_message(call.message.chat.id, "Введите логин Steam для добавления в черный список:", reply_markup=kb)
    tg.set_state(call.message.chat.id, msg.id, call.from_user.id, "as_add_to_black_list", {"call": call, "msg_id": msg.id})
    bot.answer_callback_query(call.id)

def on_add_to_black_list(message: telebot.types.Message):
    state = tg.get_state(message.chat.id, message.from_user.id)
    call, msg_id = state["data"]["call"], state["data"]["msg_id"]
    login = message.text.strip().lower()
    black_list = load_black_list()
    if login not in black_list:
        black_list.append(login)
        save_black_list(black_list)
        kb = K().add(B("◀️ Вернуться в меню", callback_data=f"{CBT.PLUGIN_SETTINGS}:{UUID}:0"))
        bot.send_message(message.chat.id, f"✅ Логин {login} добавлен в черный список.", reply_markup=kb)
    else:
        bot.send_message(message.chat.id, f"❌ Логин {login} уже в черном списке.")
    bot.delete_message(message.chat.id, message.id)
    bot.delete_message(message.chat.id, msg_id)
    tg.clear_state(message.chat.id, message.from_user.id)

def remove_from_black_list(call: telebot.types.CallbackQuery, cardinal: Cardinal):
    black_list = load_black_list()
    if not black_list:
        bot.send_message(call.message.chat.id, "❌ Черный список пуст.")
        bot.answer_callback_query(call.id)
        return
    kb = K()
    for login in black_list:
        kb.add(B(login, callback_data=f"as_remove_black_list_confirm:{login}"))
    kb.add(B("◀️ Назад", callback_data="as_black_list"))
    bot.edit_message_text("Выберите логин для удаления:", call.message.chat.id, call.message.id, reply_markup=kb)
    bot.answer_callback_query(call.id)

def remove_black_list_confirm(call: telebot.types.CallbackQuery, cardinal: Cardinal):
    login = call.data.split(":")[1]
    black_list = load_black_list()
    if login in black_list:
        black_list.remove(login)
        save_black_list(black_list)
        bot.answer_callback_query(call.id, f"✅ Логин {login} удален из черного списка.")
    else:
        bot.answer_callback_query(call.id, f"❌ Логин {login} не найден в черном списке.")
    black_list_menu(call, cardinal)

def toggle_option(call: telebot.types.CallbackQuery, cardinal: Cardinal, key: str, subkey: str = None):
    if subkey:
        SETTINGS[key][subkey] = not SETTINGS[key][subkey]
        status = "включены" if SETTINGS[key][subkey] else "выключены"
    else:
        SETTINGS[key] = not SETTINGS[key]
        status = "включены" if SETTINGS[key] else "выключены"
    save_settings()
    open_settings(call, cardinal)

def send_notification(cardinal: Cardinal, order_id: str, status: str, details: dict, parse_mode: str = None):
    if not SETTINGS["notifications_enabled"]: return
    if status == "balance":
        message = details["message"]
    else:
        order = cardinal.account.get_order(order_id) if order_id else None
        buyer_username = order.buyer_username if order else "Неизвестно"
        buyer_id = order.buyer_id if order else None
        quantity = details.get("quantity", 0)
        currency = details.get("currency", "RUB")
        steam_login = details.get("steam_login", "Не указан")
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(details.get("timestamp", time.time())))
        amount_usd = details.get("amount_usd", 0)
        rate = details.get("rate", 0)
        status_text = {"success": "успешном пополнении", "error": "ошибке", "refund": "возврате"}.get(status, status)
        message = f"<b>🔔 Уведомление о {status_text}</b>\n\n<b>💙 FunPay:</b>\n<b>L ID Заказа:</b> <code>#{order_id}</code>\n<b>L Покупатель:</b> <code>{buyer_username}</code>\n<b>L Цена на FunPay:</b> <code>{order.sum if order else 'Неизвестно'} ₽</code>\n\n<b>💙 Steam:</b>\n<b>L Логин Steam:</b> <code>{steam_login}</code>\n<b>L Сумма пополнения:</b> <code>{format_amount(quantity, currency)}</code>\n<b>L Валюта:</b> <code>{currency}</code>"
        if status == "success":
            message += f"\n<b>L Сумма в USD:</b> <code>{amount_usd:.2f}$</code>\n<b>L Курс обмена ({currency}/USD):</b> <code>{rate}</code>"
            balance = get_balance()
            message += f"\n<b>L Остаток баланса:</b> <code>{balance:.2f}$</code>"
        if "message" in details:
            message += f"\n<b>L Дополнительно:</b> <code>{details['message']}</code>"
        message += f"\n\n<b>• Дата:</b> <code>{timestamp}</code>"
    kb = K(row_width=2).add(B("💙 FunPay", url=f"https://funpay.com/orders/{order_id}/"), B("💙 Покупатель", url=f"https://funpay.com/users/{buyer_id if order else 'Неизвестно'}/"))
    for chat_id in SETTINGS["notification_chats"]:
        try:
            bot.send_message(chat_id, message, parse_mode="HTML", reply_markup=kb if kb.keyboard else None)
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка уведомления в чат {chat_id}: {e}")

def cancel_input(call: telebot.types.CallbackQuery, cardinal: Cardinal):
    tg.clear_state(call.message.chat.id, call.from_user.id)
    try:
        bot.delete_message(call.message.chat.id, call.message.id)
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка при удалении сообщения: {e}")
    open_settings(call, cardinal_instance)
    bot.answer_callback_query(call.id)

def set_api(call: telebot.types.CallbackQuery, cardinal: Cardinal):
    kb = K().row(B("🔑 Изменить логин", callback_data="as_set_api_login"), B("🔒 Изменить пароль", callback_data="as_set_api_password"))
    kb.add(B("◀️ Назад", callback_data=f"{CBT.PLUGIN_SETTINGS}:{UUID}:0"))
    bot.edit_message_text("✏️ Настройка API", call.message.chat.id, call.message.id, reply_markup=kb)
    bot.answer_callback_query(call.id)

def set_api_field(call: telebot.types.CallbackQuery, cardinal: Cardinal, field: str):
    kb = K().add(B("❌ Отмена", callback_data="cancel_input"))
    msg = bot.send_message(call.message.chat.id, f"{'👤' if field == 'login' else '🔑'} Введите новый {field} для API:", reply_markup=kb)
    tg.set_state(call.message.chat.id, msg.id, call.from_user.id, f"as_set_api_{field}", {"call": call, "msg_id": msg.id})
    bot.answer_callback_query(call.id)

def on_api_field(message: telebot.types.Message, field: str):
    state = tg.get_state(message.chat.id, message.from_user.id)
    call, msg_id = state["data"]["call"], state["data"]["msg_id"]
    SETTINGS[f"api_{field}"] = message.text.strip()
    save_settings()
    bot.delete_message(message.chat.id, message.id)
    bot.delete_message(message.chat.id, msg_id)
    bot.answer_callback_query(call.id, f"✅ {field.capitalize()} API обновлён")
    open_settings(call, cardinal_instance)
    tg.clear_state(message.chat.id, message.from_user.id)

def handle_new_message(cardinal: Cardinal, event: NewMessageEvent):
    message = event.message
    state_key = (message.chat_id, message.author_id)
    state = FUNPAY_STATES.get(state_key)

    # Исправлено: только системные сообщения о платеже вызывают process_new_order
    if message.author_id == 0 and message.type and message.type.name == "ORDER_PURCHASED":
        order_id = extract_order_id_from_message(message.text)
        if order_id:
            if not SETTINGS["order_verification_enabled"] or verify_order_exists(cardinal, order_id):
                try:
                    order = cardinal.account.get_order(order_id)
                    buyer_id = order.buyer_id
                    USER_ORDER_QUEUES.setdefault(buyer_id, Queue()).put({"order_id": order_id, "chat_id": message.chat_id})
                    threading.Thread(target=process_user_orders, args=(cardinal, buyer_id), daemon=True).start()
                    logger.info(f"{LOGGER_PREFIX} Заказ #{order_id} добавлен в очередь обработки")
                except Exception as e:
                    logger.error(f"{LOGGER_PREFIX} Ошибка при получении информации о заказе #{order_id}: {e}")
            else:
                logger.warning(f"{LOGGER_PREFIX} Обнаружена попытка подделки заказа #{order_id}")
                if SETTINGS["notification_types"]["error"]:
                    send_notification(cardinal, order_id, "error", {
                        "message": f"<b>🔔 Обнаружена попытка подделки заказа</b>\n\n<b>L ID заказа:</b> <code>#{order_id}</code>\n<b>L Покупатель:</b> <code>Неизвестно</code>\n<b>L Статус:</b> <code>Заказ не существует или не принадлежит продавцу</code>\n<b>• Дата:</b> <code>{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}</code>"
                    }, parse_mode="HTML")
        return

    if state and state.get("data", {}).get("order_id"):
        order_id = state["data"]["order_id"]
        try:
            order = cardinal.account.get_order(order_id)
            if order.status in [OrderStatuses.CLOSED, OrderStatuses.REFUNDED]:
                FUNPAY_STATES.pop(state_key, None)
                return
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка проверки статуса заказа #{order_id}: {e}")
            FUNPAY_STATES.pop(state_key, None)
            return

    if state and state["state"] == "waiting_for_steam_login":
        steam_login = message.text.strip()
        order_id = state["data"]["order_id"]
        order = cardinal.account.get_order(order_id)
        if order.status in [OrderStatuses.CLOSED, OrderStatuses.REFUNDED]:
            FUNPAY_STATES.pop(state_key, None)
            return
        if re.match(r'^[a-zA-Z0-9]+$', steam_login):
            currency = extract_currency(order.html) or "RUB"
            quantity = extract_quantity(order.html) or 1
            cardinal.send_message(message.chat_id, f"• Проверьте данные:\nL Логин Steam: {steam_login}\nL Сумма пополнения: {format_amount(quantity, currency)}\n\n• Если всё верно, отправьте «+» без кавычек\nL Либо отправьте новый логин")
            logger.info(f"{LOGGER_PREFIX} Запросил у пользователя {order.buyer_username} подтверждение логина")
            logger.info(f"{LOGGER_PREFIX} ID Заказа: #{order_id}")
            logger.info(f"{LOGGER_PREFIX} Логин Steam: {steam_login}")
            logger.info(f"{LOGGER_PREFIX} Сумма пополнения: {format_amount(quantity, currency)}")
            FUNPAY_STATES[state_key] = {"state": "confirming_login", "data": {"steam_login": steam_login, "order_id": order_id, "currency": currency, "quantity": quantity}}
        return
    
    if state and state["state"] == "confirming_login":
        order_id = state["data"]["order_id"]
        order = cardinal.account.get_order(order_id)
        if order.status in [OrderStatuses.CLOSED, OrderStatuses.REFUNDED]:
            FUNPAY_STATES.pop(state_key, None)
            return
        if message.text.strip() == "+" or message.text.strip() == "«+»":
            queue_size = USER_ORDER_QUEUES.get(message.author_id, Queue()).qsize() + 1
            wait_time = int(queue_size * 15)
            cardinal.send_message(message.chat_id, f"⏳ Ваш запрос на пополнение Steam добавлен в очередь.\nL Ваша позиция: {queue_size}.\nL Примерное время ожидания: {wait_time} сек.")
            logger.info(f"{LOGGER_PREFIX} Начал пополнение Steam для заказа #{order_id}")
            logger.info(f"{LOGGER_PREFIX} Ник покупателя: {order.buyer_username}")
            logger.info(f"{LOGGER_PREFIX} Логин Steam: {state['data']['steam_login']}")
            logger.info(f"{LOGGER_PREFIX} Сумма пополнения: {state['data']['quantity']} {state['data']['currency']}")
            perform_top_up(cardinal, state["data"]["order_id"], state["data"]["steam_login"], state["data"]["currency"], state["data"]["quantity"], message.chat_id, message.author_id)
        elif re.match(r'^[a-zA-Z0-9]+$', message.text.strip()):
            new_steam_login = message.text.strip()
            currency = extract_currency(order.html) or "RUB"
            quantity = extract_quantity(order.html) or 1
            cardinal.send_message(message.chat_id, f"• Проверьте данные:\nL Логин Steam: {new_steam_login}\nL Сумма пополнения: {format_amount(quantity, currency)}\n\n• Если всё верно, отправьте «+» без кавычек\nL Либо отправьте новый логин")
            logger.info(f"{LOGGER_PREFIX} Запросил у пользователя {order.buyer_username} подтверждение логина")
            logger.info(f"{LOGGER_PREFIX} ID Заказа: #{order_id}")
            logger.info(f"{LOGGER_PREFIX} Логин Steam: {state['data']['steam_login']}")
            logger.info(f"{LOGGER_PREFIX} Сумма пополнения: {format_amount(quantity, currency)}")
            FUNPAY_STATES[state_key] = {"state": "confirming_login", "data": {"steam_login": new_steam_login, "order_id": order_id, "currency": currency, "quantity": quantity}}
        return

    if state and (state["state"] == "waiting_for_steam_login" or state["state"] == "confirming_login"):
        return

    # Удалено: if "оплатил" in message.text.lower() and "заказ" in message.text.lower() or "#" in message.text:
    #          process_new_order(cardinal, message)

def refund_and_cleanup(cardinal: Cardinal, order_id: str, chat_id: int, author_id: int, steam_login: str = "Не указан"):
    try:
        order = cardinal.account.get_order(order_id)
        if order.status != OrderStatuses.REFUNDED:
            cardinal.account.refund(order_id)
            cardinal.send_message(chat_id, "❌ Средства возвращены из-за ошибки.\nL Приносим извинения за доставленные неудобства")
            logger.info(f"{LOGGER_PREFIX} Заказ #{order_id} успешно возвращен")
            if SETTINGS["notification_types"]["refund"]:
                send_notification(cardinal, order_id, "refund", {"steam_login": steam_login, "quantity": extract_quantity(order.html) or 1, "currency": extract_currency(order.html) or "RUB", "timestamp": time.time()})
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка возврата: {e}")
        if SETTINGS["notification_types"]["error"]:
            send_notification(cardinal, order_id, "error", {"steam_login": steam_login, "quantity": extract_quantity(order.html) or 1, "currency": extract_currency(order.html) or "RUB", "timestamp": time.time(), "message": f"Ошибка при возврате: {e}"})
    finally:
        FUNPAY_STATES.pop((chat_id, author_id), None)

def process_order(cardinal: Cardinal, order_id: str, chat_id: int, buyer_id: int):
    time.sleep(3)
    try:
        # Дополнительная проверка подлинности заказа только если включена соответствующая настройка
        if SETTINGS["order_verification_enabled"] and not verify_order_exists(cardinal, order_id):
            logger.warning(f"{LOGGER_PREFIX} Заказ #{order_id} не прошел проверку подлинности в process_order")
            FUNPAY_STATES.pop((chat_id, buyer_id), None)
            return
            
        order = cardinal.account.get_order(order_id)
        if order.status in [OrderStatuses.CLOSED, OrderStatuses.REFUNDED]:
            FUNPAY_STATES.pop((chat_id, buyer_id), None)
            return
        if order.subcategory.id != 1086:
            return
        quantity, currency = extract_quantity(order.html) or 1, extract_currency(order.html) or "RUB"
        min_amount = MIN_AMOUNTS.get(currency, 0)
        max_amounts = get_max_amounts()
        max_amount = max_amounts.get(currency, 0)
        if not (min_amount <= float(quantity) <= max_amount):
            cardinal.account.refund(order_id)
            cardinal.send_message(chat_id, f"❌ Количество {format_amount(quantity, currency)} вне лимитов ({min_amount} - {max_amount}). Средства возвращены.")
            if SETTINGS["notification_types"]["refund"]:
                send_notification(cardinal, order_id, "refund", {"steam_login": "Не указан", "quantity": quantity, "currency": currency, "timestamp": time.time()})
            FUNPAY_STATES.pop((chat_id, buyer_id), None)
            return
        steam_login = extract_steam_login(order.html)
        if steam_login:
            if order.status in [OrderStatuses.CLOSED, OrderStatuses.REFUNDED]:
                FUNPAY_STATES.pop((chat_id, buyer_id), None)
                return
            cardinal.send_message(chat_id, f"❤️ Спасибо за покупку!\n\n• Проверьте данные:\nL Логин Steam: {steam_login}\nL Сумма пополнения: {format_amount(quantity, currency)}\n\n• Если всё верно, отправьте «+» без кавычек\nL Либо отправьте новый логин")
            logger.info(f"{LOGGER_PREFIX} Запросил у пользователя {order.buyer_username} подтверждение логина")
            logger.info(f"{LOGGER_PREFIX} ID Заказа: #{order_id}")
            logger.info(f"{LOGGER_PREFIX} Логин Steam: {steam_login}")
            logger.info(f"{LOGGER_PREFIX} Сумма пополнения: {format_amount(quantity, currency)}")
            FUNPAY_STATES[(chat_id, buyer_id)] = {"state": "confirming_login", "data": {"steam_login": steam_login, "order_id": order_id, "currency": currency, "quantity": float(quantity)}}
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка обработки заказа #{order_id}: {e}")
        FUNPAY_STATES.pop((chat_id, buyer_id), None)

def extract_field(html: str, field: str):
    soup = BeautifulSoup(html, 'lxml')
    for item in soup.find_all('div', class_='param-item'):
        h5 = item.find('h5')
        if h5 and field in h5.text:
            bold = item.find('div', class_='text-bold')
            if bold:
                text = bold.text.strip()
                if field == "Количество":
                    return float(re.sub(r'[^\d.]', '', text)) if re.sub(r'[^\d.]', '', text) else None
                return text
    return None

extract_steam_login = lambda html: extract_field(html, "Логин Steam")
extract_currency = lambda html: extract_field(html, "Тип валюты")
extract_quantity = lambda html: extract_field(html, "Количество")

def get_token():
    if time.time() < TOKEN_DATA["expiry"]:
        return TOKEN_DATA["token"]
    payload = {"email": SETTINGS["api_login"], "password": SETTINGS["api_password"]}
    response = requests.post("https://api.ns.gifts/api/v1/get_token", json=payload)
    if response.status_code == 200:
        data = response.json()
        TOKEN_DATA["token"] = data.get("token") or data.get("access_token") or data["data"]["token"]
        TOKEN_DATA["expiry"] = data.get("valid_thru", time.time() + 7200)
        return TOKEN_DATA["token"]
    raise Exception(f"Не удалось получить токен: {response.status_code}")

def get_steam_amount(amount: float, currency: str = "RUB"):
    token = get_token()
    response = requests.post("https://api.ns.gifts/api/v1/steam/get_amount", json={"amount": round(amount, 2), "currency": currency}, headers={"Authorization": f"Bearer {token}"})
    if response.status_code == 200:
        return float(response.json().get("usd_price", 0))
    logger.error(f"{LOGGER_PREFIX} Ошибка при получении курса: {response.status_code} - {response.text}")
    raise Exception(f"Ошибка API NSGifts: {response.status_code}")

def create_order(service_id: int, quantity: str, data: str):
    token = get_token()
    custom_id = str(uuid.uuid4())
    response = requests.post("https://api.ns.gifts/api/v1/create_order", json={"service_id": service_id, "quantity": quantity, "custom_id": custom_id, "data": data}, headers={"Authorization": f"Bearer {token}"})
    if response.status_code == 200:
        return response.json().get("custom_id")
    error_text = response.text
    if response.status_code == 400 and "There is no such login" in error_text:
        raise Exception("InvalidLogin")
    raise Exception(f"Не удалось создать заказ: {response.status_code} - {error_text}")

def pay_order(custom_id: str):
    token = get_token()
    response = requests.post("https://api.ns.gifts/api/v1/pay_order", json={"custom_id": custom_id}, headers={"Authorization": f"Bearer {token}"})
    if response.status_code == 200:
        return True
    error_message = response.json().get("detail", "Неизвестная ошибка")
    if "Недостаточно средств" in error_message:
        raise Exception("InsufficientFunds")
    raise Exception(f"Не удалось оплатить: {response.status_code} - {error_message}")

def perform_top_up(cardinal: Cardinal, order_id: str, steam_login: str, currency: str, quantity: float, chat_id: int, author_id: int):
    state_key = (chat_id, author_id)
    black_list = load_black_list()
    if steam_login.lower() in black_list:
        cardinal.send_message(chat_id, "❌ Ваш логин Steam находится в черном списке. Ожидайте продавца.")
        logger.info(f"{LOGGER_PREFIX} Логин {steam_login} находится в черном списке")
        order = cardinal.account.get_order(order_id)
        send_notification(cardinal, order_id, "error", {"message": f"<b>🔔 Уведомление об ошибке</b>\n\n<b>L Причина:</b> <code>Обнаружен логин из черного списка</code>\n<b>L Покупатель:</b> <code>{order.buyer_username}</code>\n<b>L Логин Steam:</b> <code>{steam_login}</code>\n<b>• Дата:</b> <code>{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}</code>"}, parse_mode="HTML")
        FUNPAY_STATES.pop(state_key, None)
        return
    try:
        order = cardinal.account.get_order(order_id)
        if order.status in [OrderStatuses.CLOSED, OrderStatuses.REFUNDED]:
            FUNPAY_STATES.pop(state_key, None)
            return
        rates = get_currency_rates()
        rate_key = f"{currency.lower()}/usd"
        rate = rates.get(rate_key, 0) if rates else 0
        logger.info(f"{LOGGER_PREFIX} Курс для пополнения {currency}: {rate_key} = {rate}")
        amount_usd = round(float(quantity) / float(rate), 2) if rate != 0 else 0.22
        logger.info(f"{LOGGER_PREFIX} Сумма в USD для {quantity} {currency}: {amount_usd} (курс: {rate})")
        custom_id = create_order(1, f"{amount_usd:.2f}", steam_login)
        pay_order(custom_id)
        current_time = time.strftime('%H:%M:%S | %Y-%m-%d')
        cardinal.send_message(chat_id, f"⁡🎉⁡-----------------------------------------------------------🎉\n\n💙 Средства успешно отправлены!\n\nL Логин Steam: {steam_login}\nL Сумма пополнения: {format_amount(quantity, currency)}\nL Время выполнения: {current_time}\n\n• Подтвердите заказ: https://funpay.com/orders/{order_id}/\n\n❤️ Не забудьте оставить отзыв с упоминанием полной автоматизации заказа, приятного использования!")
        logger.info(f"{LOGGER_PREFIX} Заказ #{order_id} успешно выполнен!")
        logger.info(f"{LOGGER_PREFIX} Никнейм покупателя: {order.buyer_username}")
        logger.info(f"{LOGGER_PREFIX} Логин Steam: {steam_login}")
        logger.info(f"{LOGGER_PREFIX} Сумма пополнения: {format_amount(quantity, currency)}")
        logger.info(f"{LOGGER_PREFIX} Сумма в USD: {amount_usd:.2f}")
        logger.info(f"{LOGGER_PREFIX} Курс обмена {currency}/USD: {rate}")
        logger.info(f"{LOGGER_PREFIX} Время выполнения: {current_time}")
        if SETTINGS["notification_types"]["success"]:
            send_notification(cardinal, order_id, "success", {"steam_login": steam_login, "quantity": float(quantity), "currency": currency, "timestamp": time.time(), "amount_usd": amount_usd, "rate": rate}, parse_mode="HTML")
        SUCCESSFUL_ORDERS[order_id] = time.time()
        threading.Thread(target=check_order_confirmation, args=(cardinal, order_id, chat_id, author_id), daemon=True).start()
        order_info = {"order_id": order_id, "buyer_username": order.buyer_username, "buyer_id": order.buyer_id, "sum": order.sum, "currency": currency, "quantity": float(quantity), "steam_login": steam_login, "status": "success", "timestamp": time.time(), "amount_usd": amount_usd, "rate": rate}
        orders = load_orders()
        orders.append(order_info)
        save_orders(orders)
        FUNPAY_STATES.pop(state_key, None)
    except Exception as e:
        error_msg = str(e)
        if error_msg == "InvalidLogin":
            cardinal.send_message(chat_id, "❌ Ошибка: Указанного логина в Steam не существует.\nL Пожалуйста, введите правильный логин Steam.")
            logger.warning(f"{LOGGER_PREFIX} Логин {steam_login} не существует")
            FUNPAY_STATES[state_key] = {"state": "waiting_for_steam_login", "data": {"order_id": order_id}}
        else:
            cardinal.send_message(chat_id, "❌ Произошла ошибка при выполнении вашего заказа")
            logger.error(f"{LOGGER_PREFIX} Произошла ошибка при выполнении заказа #{order_id}")
            logger.error(f"{LOGGER_PREFIX} Ошибка: {error_msg}")
            if SETTINGS["notification_types"]["error"]:
                send_notification(cardinal, order_id, "error", {"steam_login": steam_login, "quantity": float(quantity), "currency": currency, "timestamp": time.time(), "message": f"Ошибка: {error_msg}"}, parse_mode="HTML")
            if error_msg == "InsufficientFunds":
                deactivate_lots_on_error(cardinal)
            if SETTINGS["auto_refund_on_error"]:
                refund_and_cleanup(cardinal, order_id, chat_id, author_id, steam_login)
            else:
                send_notification(cardinal, order_id, "refund", {"message": f"<b>🔔 Уведомление о возврате</b>\n\n<b>L Причина:</b> <code>Требуется возврат вручную</code>\n<b>L Покупатель:</b> <code>{cardinal.account.get_order(order_id).buyer_username}</code>\n<b>L Логин Steam:</b> <code>{steam_login}</code>\n<b>L Сумма пополнения:</b> <code>{format_amount(quantity, currency)}</code>\n\n<b>• Дата:</b> <code>{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}</code>"}, parse_mode="HTML")
            if cardinal.account.get_order(order_id).status in [OrderStatuses.CLOSED, OrderStatuses.REFUNDED]:
                FUNPAY_STATES.pop(state_key, None)

def check_order_confirmation(cardinal: Cardinal, order_id: str, chat_id: int, author_id: int):
    time.sleep(2 * 60)
    order = cardinal.account.get_order(order_id)
    if order.status not in [OrderStatuses.CLOSED, OrderStatuses.REFUNDED]:
        cardinal.send_message(chat_id, f"⁡🔔 Напоминание: Пожалуйста, подтвердите заказ. Это является обязательным!\n\n• Ссылка на заказ: https://funpay.com/orders/{order_id}/")
        logger.info(f"{LOGGER_PREFIX} Напоминание о подтверждении заказа #{order_id} отправлено")

def order_history(call: telebot.types.CallbackQuery, cardinal: Cardinal):
    chat_id, page = call.message.chat.id, int(call.data.split(":")[1]) if len(call.data.split(":")) > 1 else 1
    orders = sorted(load_orders(), key=lambda x: x.get("timestamp", 0), reverse=True)
    if not orders:
        bot.edit_message_text("📜 История заказов пуста.", chat_id, call.message.id, reply_markup=K().add(B("◀️ Назад", callback_data=f"{CBT.PLUGIN_SETTINGS}:{UUID}:0")), parse_mode="HTML")
        bot.answer_callback_query(call.id)
        return
    items_per_page, total_items = 10, len(orders)
    total_pages = (total_items + items_per_page - 1) // items_per_page
    page = max(1, min(page, total_pages))
    start_idx = (page - 1) * items_per_page
    page_orders = orders[start_idx:start_idx + items_per_page]
    markup = K(row_width=1).add(*[B(f"💙 #{order.get('order_id', 'Неизвестно')} | {order.get('buyer_username', 'Неизвестно')} | {order.get('sum', 'Неизвестно')} ₽", callback_data=f"steam_order_details:{order.get('order_id', 'Неизвестно')}:{start_idx}") for order in page_orders])
    if total_pages > 1:
        buttons = []
        if total_pages > 2:
            buttons.append(B("⏪️", callback_data=f"steam_pagination_prev:1"))
        buttons.append(B("⬅️", callback_data=f"steam_pagination_prev:{page-1}" if page > 1 else f"steam_pagination_prev:1"))
        buttons.append(B(f"{page}/{total_pages}", callback_data="dummy"))
        buttons.append(B("➡️", callback_data=f"steam_pagination_next:{page+1}" if page < total_pages else f"steam_pagination_next:{total_pages}"))
        if total_pages > 2:
            buttons.append(B("⏩️", callback_data=f"steam_pagination_next:{total_pages}"))
        markup.row(*buttons)
    markup.add(B("◀️ Назад", callback_data=f"{CBT.PLUGIN_SETTINGS}:{UUID}:0"))
    bot.edit_message_text(f"📜 <b>История заказов:</b>\n\nВсего заказов: <code>{total_items}</code>", chat_id, call.message.id, reply_markup=markup, parse_mode="HTML")
    bot.answer_callback_query(call.id)

def pagination_prev(call: telebot.types.CallbackQuery, cardinal: Cardinal):
    page = int(call.data.split(":")[1])
    call.data = f"steam_order_history:{page}"
    order_history(call, cardinal)

def pagination_next(call: telebot.types.CallbackQuery, cardinal: Cardinal):
    page = int(call.data.split(":")[1])
    call.data = f"steam_order_history:{page}"
    order_history(call, cardinal)

def dummy_callback(call: telebot.types.CallbackQuery, cardinal: Cardinal):
    bot.answer_callback_query(call.id)

def order_details(call: telebot.types.CallbackQuery, cardinal: Cardinal):
    order_id, start_idx = call.data.split(":")[1], int(call.data.split(":")[2])
    chat_id = call.message.chat.id
    order = next((order for order in load_orders() if order.get("order_id") == order_id), None)
    if not order:
        bot.edit_message_text(f"❌ Заказ #{order_id} не найден.", chat_id, call.message.id, reply_markup=K().add(B("◀️ Назад", callback_data="steam_order_history:1")), parse_mode="HTML")
        bot.answer_callback_query(call.id)
        return
    details_text = f"📋 <u><b>Детали заказа #{order_id}:</b></u>\n\n<b>💙 FunPay:</b>\nL <b>Статус:</b> <code>Успешно</code>\nL <b>Покупатель:</b> <code>{order.get('buyer_username', 'Неизвестно')}</code>\nL <b>Цена на FunPay:</b> <code>{order.get('sum', 'Неизвестно')} ₽</code>\n\n<b>💙 Steam:</b>\nL <b>Логин Steam:</b> <code>{order.get('steam_login', 'Неизвестно')}</code>\nL <b>Сумма пополнения:</b> <code>{format_amount(order.get('quantity', 0), order.get('currency', 'RUB'))}</code>\n\n• <b>Дата покупки:</b> <code>{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(order.get('timestamp', 0)))}</code>"
    page = (start_idx // 10) + 1
    markup = K(row_width=2).add(B("💙 FunPay", url=f"https://funpay.com/orders/{order_id}/"), B("💙 Покупатель", url=f"https://funpay.com/users/{order.get('buyer_id', 'Неизвестно')}/"))
    markup.add(B("◀️ Назад", callback_data=f"steam_order_history:{page}"))
    bot.edit_message_text(details_text, chat_id, call.message.id, reply_markup=markup, parse_mode="HTML")
    bot.answer_callback_query(call.id)

def process_new_order(cardinal: Cardinal, message: NewMessageEvent):
    order_id = extract_order_id_from_message(message.text)
    if order_id:
        # Проверяем подлинность заказа только если включена соответствующая настройка
        if not SETTINGS["order_verification_enabled"] or verify_order_exists(cardinal, order_id):
            try:
                order = cardinal.account.get_order(order_id)
                buyer_id = order.buyer_id
                USER_ORDER_QUEUES.setdefault(buyer_id, Queue()).put({"order_id": order_id, "chat_id": message.chat_id})
                threading.Thread(target=process_user_orders, args=(cardinal, buyer_id), daemon=True).start()
                logger.info(f"{LOGGER_PREFIX} Заказ #{order_id} добавлен в очередь обработки через process_new_order")
            except Exception as e:
                logger.error(f"{LOGGER_PREFIX} Ошибка при получении информации о заказе #{order_id}: {e}")
        else:
            logger.warning(f"{LOGGER_PREFIX} Обнаружена попытка подделки заказа #{order_id} в process_new_order")
            # Отправляем уведомление о попытке подделки
            if SETTINGS["notification_types"]["error"]:
                send_notification(cardinal, order_id, "error", {
                    "message": f"<b>🔔 Обнаружена попытка подделки заказа</b>\n\n<b>L ID заказа:</b> <code>#{order_id}</code>\n<b>L Покупатель:</b> <code>Неизвестно</code>\n<b>L Статус:</b> <code>Заказ не существует или не принадлежит продавцу</code>\n<b>• Дата:</b> <code>{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}</code>"
                }, parse_mode="HTML")

def process_user_orders(cardinal: Cardinal, buyer_id: int):
    if buyer_id not in USER_ORDER_QUEUES: return
    queue = USER_ORDER_QUEUES[buyer_id]
    while not queue.empty():
        order_data = queue.get()
        process_order(cardinal, order_data["order_id"], order_data["chat_id"], buyer_id)
        queue.task_done()
    del USER_ORDER_QUEUES[buyer_id]

def init(cardinal: Cardinal):
    global tg, bot, cardinal_instance, previous_balance
    tg, bot, cardinal_instance = cardinal.telegram, cardinal.telegram.bot, cardinal
    load_settings()
    threading.Thread(target=check_balance_periodically, args=(cardinal,), daemon=True).start()
    handlers = [
        (lambda c: open_settings(c, cardinal), lambda c: f"{CBT.PLUGIN_SETTINGS}:{UUID}" in c.data),
        (lambda c: show_instruction(c), lambda c: c.data == "as_instruction"),
        (lambda c: set_api(c, cardinal), lambda c: c.data == "as_set_api"),
        (lambda c: set_api_field(c, cardinal, "login"), lambda c: c.data == "as_set_api_login"),
        (lambda c: set_api_field(c, cardinal, "password"), lambda c: c.data == "as_set_api_password"),
        (lambda c: toggle_lots(c, cardinal), lambda c: c.data.startswith("as_toggle_lots_")),
        (lambda c: black_list_menu(c, cardinal), lambda c: c.data == "as_black_list"),
        (lambda c: add_to_black_list(c, cardinal), lambda c: c.data == "as_add_to_black_list"),
        (lambda c: remove_from_black_list(c, cardinal), lambda c: c.data == "as_remove_from_black_list"),
        (lambda c: remove_black_list_confirm(c, cardinal), lambda c: c.data.startswith("as_remove_black_list_confirm:")),
        (lambda c: toggle_option(c, cardinal, "auto_refund_on_error"), lambda c: c.data == "as_toggle_auto_refund"),
        (lambda c: toggle_option(c, cardinal, "notifications_enabled"), lambda c: c.data == "as_toggle_notifications"),
        (lambda c: toggle_option(c, cardinal, "order_verification_enabled"), lambda c: c.data == "as_toggle_order_verification"),
        (lambda c: refresh_info(c, cardinal), lambda c: c.data == "as_refresh_info"),
        (lambda c: order_history(c, cardinal), lambda c: c.data.startswith("steam_order_history:")),
        (lambda c: pagination_prev(c, cardinal), lambda c: c.data.startswith("steam_pagination_prev:")),
        (lambda c: pagination_next(c, cardinal), lambda c: c.data.startswith("steam_pagination_next:")),
        (lambda c: dummy_callback(c, cardinal), lambda c: c.data == "as_dummy"),
        (lambda c: order_details(c, cardinal), lambda c: c.data.startswith("steam_order_details:")),
        (lambda c: statistics(c), lambda c: c.data == "as_statistics"),
    ]
	#"\n\n• Разработано: @gderobi"
    msg_handlers = [
        (lambda m: on_api_field(m, "login"), lambda m: tg.check_state(m.chat.id, m.from_user.id, "as_set_api_login")),
        (lambda m: on_api_field(m, "password"), lambda m: tg.check_state(m.chat.id, m.from_user.id, "as_set_api_password")),
        (on_add_to_black_list, lambda m: tg.check_state(m.chat.id, m.from_user.id, "as_add_to_black_list")),
    ]
    for handler, condition in handlers:
        tg.cbq_handler(handler, condition)
    for handler, condition in msg_handlers:
        tg.msg_handler(handler, func=condition)
    handle_new_message.plugin_uuid = UUID
    if handle_new_message not in cardinal.new_message_handlers:
        cardinal.new_message_handlers.append(handle_new_message)

BIND_TO_PRE_INIT = [init]
BIND_TO_DELETE = None