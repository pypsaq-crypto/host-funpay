from __future__ import annotations

import os
import json
import time
import telebot
from datetime import datetime, timedelta
from typing import Dict, Set
from logging import getLogger

import FunPayAPI.types
from FunPayAPI.types import OrderStatuses
from FunPayAPI.account import Account
from FunPayAPI.updater.events import OrderStatusChangedEvent

NAME = "Lot Description Editor"
VERSION = "0.5.0"
DESCRIPTION = "Auto-updates lot descriptions with day/week/total sales + permanent-lot feature."
CREDITS = "@exador + @terop11"
UUID = "d9a8e1f3-45b6-4a7c-8c89-7b1a3f5b2e7d"
SETTINGS_PAGE = False

logger = getLogger("FPC.desc_editor")

PLUGIN_DIR = os.path.dirname(__file__)
ORDERS_FILE = os.path.join(PLUGIN_DIR, "orders_history.json")
ALLOWED_CATEGORIES_FILE = os.path.join(PLUGIN_DIR, "allowed_categories.json")
ALL_CATEGORIES_FILE = os.path.join(PLUGIN_DIR, "all_categories_ids.json")
PERMANENT_LOTS_FILE = os.path.join(PLUGIN_DIR, "permanent_lots.json")

selected_categories: Dict[int, Set[str]] = {}
RUNNING = False

def load_orders_history() -> dict:
    if not os.path.exists(ORDERS_FILE):
        return {}
    with open(ORDERS_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_orders_history(data: dict):
    with open(ORDERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def update_orders_history(order_id: str, info: dict):
    orders = load_orders_history()
    orders[order_id] = info
    save_orders_history(orders)

def load_permanent_lots() -> Set[int]:
    if not os.path.exists(PERMANENT_LOTS_FILE):
        return set()
    with open(PERMANENT_LOTS_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
        return set(int(x) for x in data)

def save_permanent_lots(lot_ids: Set[int]):
    with open(PERMANENT_LOTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(list(lot_ids), f, ensure_ascii=False, indent=4)

def add_permanent_lot(lot_id: int) -> bool:
    lots = load_permanent_lots()
    if lot_id in lots:
        return False
    lots.add(lot_id)
    save_permanent_lots(lots)
    return True

def remove_permanent_lot(lot_id: int) -> bool:
    lots = load_permanent_lots()
    if lot_id not in lots:
        return False
    lots.remove(lot_id)
    save_permanent_lots(lots)
    return True

def fetch_all_sales(cardinal):
    """
    Pulls all orders (including paid/closed/refunded).
    If status is CLOSED or PAID, sets 'closed_time' = actual closed_time or fallback now().
    Adds a minimal 0.01s delay after each get_order() to avoid 429 errors.
    """
    start_from = None
    found_closed = 0

    while True:
        try:
            start_from, shortcuts = cardinal.account.get_sells(
                start_from=start_from,
                include_paid=True,
                include_closed=True,
                include_refunded=True
            )
            logger.info(f"Fetched {len(shortcuts)} orders in this batch.")

            for sc in shortcuts:
                logger.info(f"Order #{sc.id} has status {sc.status}")
                if sc.status in [OrderStatuses.CLOSED, OrderStatuses.PAID]:
                    try:
                        full_order = cardinal.account.get_order(sc.id)
                        time.sleep(0.01)
                        if hasattr(full_order, "closed_time") and full_order.closed_time:
                            ctime = full_order.closed_time
                        else:
                            logger.warning(
                                f"Order #{sc.id} has no 'closed_time'; fallback to now()."
                            )
                            ctime = datetime.now()
                        update_orders_history(sc.id, {"closed_time": ctime.isoformat()})
                        found_closed += 1
                    except Exception as ex:
                        logger.error(f"Error fetching full order #{sc.id}: {ex}")

            if not start_from:
                break

        except Exception as ex:
            logger.error(f"Error fetching orders: {ex}")
            break

    logger.info(f"Total closed/paid orders found/updated: {found_closed}")

def get_sales_data() -> dict:
    orders = load_orders_history()
    now = datetime.now()
    day_count = 0
    week_count = 0

    for rec in orders.values():
        closed_str = rec.get("closed_time")
        if not closed_str:
            continue
        closed_dt = datetime.fromisoformat(closed_str)
        diff = now - closed_dt
        if diff < timedelta(days=1):
            day_count += 1
        if diff < timedelta(weeks=1):
            week_count += 1

    return {
        "day": day_count,
        "week": week_count,
        "total": len(orders),
    }

def load_allowed_categories() -> Set[str]:
    if not os.path.exists(ALLOWED_CATEGORIES_FILE):
        return set()
    with open(ALLOWED_CATEGORIES_FILE, 'r', encoding='utf-8') as f:
        return set(json.load(f))

def save_allowed_categories(cats: Set[str]):
    with open(ALLOWED_CATEGORIES_FILE, 'w', encoding='utf-8') as f:
        json.dump(list(cats), f, ensure_ascii=False, indent=4)

def update_lot_description(lot_id: int, cardinal):
    try:
        stats = get_sales_data()
        try:
            lot_fields = cardinal.account.get_lot_fields(lot_id)
        except Exception as ex:
            if "Предложение не найдено" in str(ex):
                logger.warning(f"Lot #{lot_id} doesn’t exist or isn’t yours. Skipping.")
            else:
                logger.error(f"Error updating lot #{lot_id}: {ex}")
            return

        old_lines = lot_fields.description_ru.split("\n")
        filtered = [ln for ln in old_lines if not ln.startswith("Продаж за ")]

        new_desc = (
            f"Продаж за день: {stats['day']}\n"
            f"Продаж за неделю: {stats['week']}\n"
            f"Продаж за все время: {stats['total']}\n\n"
            + "\n".join(filtered)
        )

        lot_fields.description_ru = new_desc
        cardinal.account.save_lot(lot_fields)
        logger.info(
            f"[Lot #{lot_id}] updated: day={stats['day']}, week={stats['week']}, total={stats['total']}"
        )
    except Exception as ex:
        logger.error(f"Error updating lot #{lot_id}: {ex}")

def update_lot_descriptions_for_permanent_lots(cardinal):
    lots = load_permanent_lots()
    if not lots:
        logger.info("No permanent lots to update.")
        return

    for lot_id in lots:
        update_lot_description(lot_id, cardinal)
        time.sleep(0.01)

def update_lot_descriptions_for_allowed_categories(cardinal):
    allowed = load_allowed_categories()
    if not allowed:
        logger.info("No allowed categories; skipping category-based update.")
        return
    if not os.path.exists(ALL_CATEGORIES_FILE):
        logger.error(f"{ALL_CATEGORIES_FILE} not found. Run /get_lot_ids_all.")
        return

    try:
        with open(ALL_CATEGORIES_FILE, 'r', encoding='utf-8') as f:
            cat_data = json.load(f)
    except Exception as ex:
        logger.error(f"Error loading categories from {ALL_CATEGORIES_FILE}: {ex}")
        return

    for cat_id in allowed:
        if cat_id not in cat_data:
            logger.warning(f"Category {cat_id} not found in {ALL_CATEGORIES_FILE}.")
            continue
        for lot_id in cat_data[cat_id]:
            update_lot_description(int(lot_id), cardinal)
            time.sleep(0.01)

def update_all_selected_and_permanent(cardinal):
    update_lot_descriptions_for_permanent_lots(cardinal)
    update_lot_descriptions_for_allowed_categories(cardinal)

def handle_order_status_changed(cardinal, event: OrderStatusChangedEvent):
    if not hasattr(event, "order"):
        return
    order_status = event.order.status
    logger.info(f"[OrderStatusChangedEvent] Order #{event.order.id} status changed to {order_status}")

    if order_status in [OrderStatuses.CLOSED, OrderStatuses.PAID]:
        try:
            full_order = cardinal.account.get_order(event.order.id)
            time.sleep(0.01)
            if hasattr(full_order, "closed_time") and full_order.closed_time:
                ctime = full_order.closed_time
            else:
                logger.warning(
                    f"Order #{event.order.id} is closed/paid but no 'closed_time'; fallback now()."
                )
                ctime = datetime.now()

            update_orders_history(event.order.id, {"closed_time": ctime.isoformat()})
            update_all_selected_and_permanent(cardinal)
        except Exception as ex:
            logger.error(f"Error handling order #{event.order.id}: {ex}")

def get_categories_keyboard(chat_id: int, cardinal) -> telebot.types.InlineKeyboardMarkup:
    keyboard = telebot.types.InlineKeyboardMarkup()
    try:
        with open(ALL_CATEGORIES_FILE, 'r', encoding='utf-8') as f:
            cat_data = json.load(f)
    except Exception as ex:
        logger.error(f"Error reading {ALL_CATEGORIES_FILE}: {ex}")
        cardinal.telegram.bot.send_message(chat_id, "❌ Не удалось загрузить список категорий.")
        return keyboard

    selected = selected_categories.get(chat_id, set())
    for category_id in cat_data:
        mark = "✅" if category_id in selected else "◻️"
        btn = telebot.types.InlineKeyboardButton(
            f"{mark} Категория {category_id}",
            callback_data=f"toggle_cat_{category_id}"
        )
        keyboard.add(btn)

    keyboard.row(
        telebot.types.InlineKeyboardButton("🚀 Подтвердить", callback_data="confirm_edit"),
        telebot.types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_edit")
    )
    return keyboard

def handle_category_toggle(cardinal, cq: telebot.types.CallbackQuery):
    cat_id = cq.data.split("_")[-1]
    chat_id = cq.message.chat.id

    if chat_id not in selected_categories:
        selected_categories[chat_id] = set()

    if cat_id in selected_categories[chat_id]:
        selected_categories[chat_id].remove(cat_id)
    else:
        selected_categories[chat_id].add(cat_id)

    try:
        cardinal.telegram.bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=cq.message.message_id,
            reply_markup=get_categories_keyboard(chat_id, cardinal)
        )
    except Exception as ex:
        logger.error(f"Error editing category selection: {ex}")

def handle_edit_confirmation(cardinal, cq: telebot.types.CallbackQuery):
    chat_id = cq.message.chat.id
    cardinal.telegram.bot.edit_message_reply_markup(chat_id, cq.message.message_id, None)

    if cq.data == "cancel_edit":
        cardinal.telegram.bot.send_message(chat_id, "❌ Отменено.")
        return

    chosen = selected_categories.get(chat_id, set())
    if not chosen:
        cardinal.telegram.bot.send_message(chat_id, "❌ Не выбрано категорий!")
        return

    save_allowed_categories(chosen)
    cardinal.telegram.bot.send_message(chat_id, "🔄 Обновляю описания...")
    update_all_selected_and_permanent(cardinal)
    cardinal.telegram.bot.send_message(chat_id, "✅ Описания обновлены!")

def get_lot_ids_all_cmd(cardinal, m: telebot.types.Message):
    global RUNNING
    if RUNNING:
        cardinal.telegram.bot.send_message(m.chat.id, "❌ Уже запущено.")
        return

    RUNNING = True
    try:
        profile = cardinal.account.get_user(cardinal.account.id)
        lots = profile.get_lots()
        lot_map = {}
        count = 0

        for lot in lots:
            cat_id = str(lot.subcategory.id)
            if cat_id not in lot_map:
                lot_map[cat_id] = []
            lot_map[cat_id].append(lot.id)
            count += 1
            logger.info(f"Found lot #{lot.id} in category {cat_id}")

        with open(ALL_CATEGORIES_FILE, 'w', encoding='utf-8') as f:
            json.dump(lot_map, f, ensure_ascii=False, indent=4)

        cardinal.telegram.bot.send_message(
            m.chat.id,
            f"✅ Найдено {count} лотов в {len(lot_map)} категориях.\n"
            f"Сохранено в {ALL_CATEGORIES_FILE}"
        )
    except Exception as ex:
        logger.error(f"Error: {ex}")
        cardinal.telegram.bot.send_message(m.chat.id, f"❌ Ошибка: {ex}")
    finally:
        RUNNING = False

def fetch_sales_cmd(cardinal, m: telebot.types.Message):
    cardinal.telegram.bot.send_message(m.chat.id, "🔄 Сканирую все заказы...")
    fetch_all_sales(cardinal)
    cardinal.telegram.bot.send_message(m.chat.id, "✅ История продаж обновлена!")

def edit_descriptions_cmd(cardinal, m: telebot.types.Message):
    if not os.path.exists(ALL_CATEGORIES_FILE):
        cardinal.telegram.bot.send_message(
            m.chat.id,
            f"❌ Файл {ALL_CATEGORIES_FILE} отсутствует. Сначала /get_lot_ids_all."
        )
        return

    cardinal.telegram.bot.send_message(
        m.chat.id,
        "📦 Выберите нужные категории:",
        reply_markup=get_categories_keyboard(m.chat.id, cardinal)
    )

def always_lot_add_cmd(cardinal, m: telebot.types.Message):
    parts = m.text.strip().split()
    if len(parts) < 2:
        cardinal.telegram.bot.send_message(m.chat.id, "❌ Укажите ID лота. Пример: /always_lot_add 2418")
        return

    try:
        lot_id = int(parts[1])
    except ValueError:
        cardinal.telegram.bot.send_message(m.chat.id, "❌ ID лота должен быть числом.")
        return

    if add_permanent_lot(lot_id):
        cardinal.telegram.bot.send_message(m.chat.id, f"✅ Лот {lot_id} добавлен в список постоянных.")
    else:
        cardinal.telegram.bot.send_message(m.chat.id, f"⚠ Лот {lot_id} уже в списке постоянных.")

def always_lot_del_cmd(cardinal, m: telebot.types.Message):
    parts = m.text.strip().split()
    if len(parts) < 2:
        cardinal.telegram.bot.send_message(m.chat.id, "❌ Укажите ID лота. Пример: /always_lot_del 2418")
        return

    try:
        lot_id = int(parts[1])
    except ValueError:
        cardinal.telegram.bot.send_message(m.chat.id, "❌ ID лота должен быть числом.")
        return

    if remove_permanent_lot(lot_id):
        cardinal.telegram.bot.send_message(m.chat.id, f"✅ Лот {lot_id} убран из списка постоянных.")
    else:
        cardinal.telegram.bot.send_message(m.chat.id, f"⚠ Лот {lot_id} не найден в списке постоянных.")

def always_lot_list_cmd(cardinal, m: telebot.types.Message):
    lots = load_permanent_lots()
    if not lots:
        cardinal.telegram.bot.send_message(m.chat.id, "Список постоянных лотов пуст.")
        return

    text = "Постоянные лоты:\n" + "\n".join(str(x) for x in sorted(lots))
    cardinal.telegram.bot.send_message(m.chat.id, text)

def init_commands(cardinal):
    if not cardinal.account.is_initiated:
        try:
            cardinal.account.get()
        except Exception as exc:
            logger.error(f"Could not init account: {exc}")
            return

    if not hasattr(cardinal, "telegram") or not cardinal.telegram:
        return

    bot = cardinal.telegram.bot

    fetch_all_sales(cardinal)
    update_lot_descriptions_for_permanent_lots(cardinal)

    cardinal.add_telegram_commands(UUID, [
        ("fetch_sales", "Обновить историю продаж", True),
        ("get_lot_ids_all", "Получить лоты и категории", True),
        ("edit_descriptions", "Редактировать описания (категории)", True),
        ("always_lot_add", "Добавить лот в постоянное обновление", True),
        ("always_lot_del", "Убрать лот из постоянного обновления", True),
        ("always_lot_list", "Показать постоянные лоты", True),
    ])

    @bot.message_handler(commands=["fetch_sales"])
    def cmd_fetch_sales(m: telebot.types.Message):
        fetch_sales_cmd(cardinal, m)

    @bot.message_handler(commands=["get_lot_ids_all"])
    def cmd_get_lot_ids_all(m: telebot.types.Message):
        get_lot_ids_all_cmd(cardinal, m)

    @bot.message_handler(commands=["edit_descriptions"])
    def cmd_edit_desc(m: telebot.types.Message):
        edit_descriptions_cmd(cardinal, m)

    @bot.message_handler(commands=["always_lot_add"])
    def cmd_alot_add(m: telebot.types.Message):
        always_lot_add_cmd(cardinal, m)

    @bot.message_handler(commands=["always_lot_del"])
    def cmd_alot_del(m: telebot.types.Message):
        always_lot_del_cmd(cardinal, m)

    @bot.message_handler(commands=["always_lot_list"])
    def cmd_alot_list(m: telebot.types.Message):
        always_lot_list_cmd(cardinal, m)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("toggle_cat_"))
    def cbq_toggle_cat(cq: telebot.types.CallbackQuery):
        handle_category_toggle(cardinal, cq)

    @bot.callback_query_handler(func=lambda c: c.data in ["confirm_edit", "cancel_edit"])
    def cbq_confirm_edit(cq: telebot.types.CallbackQuery):
        handle_edit_confirmation(cardinal, cq)

BIND_TO_INIT = [init_commands]
BIND_TO_ORDER_STATUS_CHANGED = [handle_order_status_changed]
BIND_TO_DELETE = None
