import asyncio
import threading
import logging
import json
import os
import uuid
import re
import math
from typing import Dict, List, Optional
import httpx
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from FunPayAPI.updater.events import NewMessageEvent
from FunPayAPI.types import MessageTypes, LotFields
from FunPayAPI.common.utils import RegularExpressions
from FunPayAPI import Account
from FunPayAPI.common.enums import SubCategoryTypes


# ========= Configuration =========
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "autorobux_config.json")
ORDERS_FILE = "orders_by_buyer_id.json"

def load_config() -> dict:
    default = {
        "accounts": [
            {"cookie": "ROBLOX_COOKIE_1", "proxy": None}
        ],
        "gamepass_name_template": "FP-{order_id}-{rnd}",
        "auto_refund": False,
        "funpay_user_id": 123456,
        "post_payment_message": "Спасибо за покупку! Пожалуйста, создайте GamePass стоимостью {expected_price} Robux и отправьте его ID \n Инструкция по создания GamePass: https://telegra.ph/Kak-sozdat-gamepass-06-11",
        "selected_lot_id": ""
    }
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(default, f, indent=4, ensure_ascii=False)
        return default.copy()
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        cfg = default.copy()
    for k, v in default.items():
        if k not in cfg:
            cfg[k] = v
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)
    return cfg

# Load configuration
enabled_config = load_config()
ACCOUNTS: List[Dict[str, Optional[str]]] = enabled_config["accounts"]
TEMPLATE = enabled_config["gamepass_name_template"]
FUNPAY_USER_ID = enabled_config["funpay_user_id"]
AUTO_REFUND = enabled_config.get("auto_refund", False)
SELECTED_LOT_ID = enabled_config.get("selected_lot_id", "").strip()
POST_PAYMENT_MESSAGE = enabled_config.get("post_payment_message")

def load_orders():
    if not os.path.exists(ORDERS_FILE):
        return {}
    with open(ORDERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_orders(orders):
    with open(ORDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(orders, f, indent=2, ensure_ascii=False)

# =========== Logging ==========
import sys

logger = logging.getLogger("AutoRobux")
logger.setLevel(logging.DEBUG)
log_path = os.path.join(os.path.dirname(__file__), "autorobux.log")
fh = logging.FileHandler(log_path)
fh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(fh)

# –– чтобы видеть логи плагина в консоли Cardinal
ch = logging.StreamHandler(sys.stdout)
ch.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(ch)


ORDER_CHATS_FILE = os.path.join(os.path.dirname(__file__), "order_chats.json")

def load_order_chats() -> dict:
    if not os.path.exists(ORDER_CHATS_FILE):
        return {}
    with open(ORDER_CHATS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_order_chats(mapping: dict):
    with open(ORDER_CHATS_FILE, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)




# ======== Order Storage ========
orders_info: Dict[int, List[dict]] = {}
# ===== FSM: Ожидание GamePass ID через FunPay =====
waiting_for_gamepass_id = {}  # order_id → {chat_id, buyer_id, expected_price}
order_chat_map = {}  # chat_id → order_id


# маппинг order_id -> chat_id
# ===== Debug Context ========
DEBUG_CTX: Dict[int, int] = {}

# ========== HTTP Helpers ==========
def _get_proxies_dict(proxy: Optional[str]) -> Optional[dict]:
    if proxy:
        return {"http://": proxy, "https://": proxy}
    return None

# ===== Utility: Account Info ==========
def get_robux_balance(account: dict) -> int:
    raw = account.get("cookie", "").strip()
    cookie_header = raw if raw.startswith(".ROBLOSECURITY=") else f".ROBLOSECURITY={raw}"
    headers = {"Cookie": cookie_header, "User-Agent": "Mozilla/5.0"}
    client = httpx.Client()
    try:
        auth_url = "https://users.roblox.com/v1/users/authenticated"
        r = client.get(auth_url, headers=headers, timeout=10)
        logger.debug(f"[Balance] Auth status: {r.status_code}")
        if r.status_code != 200:
            logger.warning(f"Authentication failed: {r.status_code}")
            return -1
        user_data = r.json()
        user_id = user_data.get("id")
        if not user_id:
            logger.error("User ID not found in authenticated response")
            return -1
        bal_url = f"https://economy.roblox.com/v1/users/{user_id}/currency"
        r2 = client.get(bal_url, headers=headers, timeout=10)
        logger.debug(f"[Balance] Currency status: {r2.status_code}")
        if r2.status_code == 200:
            data = r2.json()
            return int(data.get("robux", 0))
        logger.warning(f"Unexpected status {r2.status_code} from currency API")
        return -1
    except Exception as e:
        logger.error(f"Error fetching balance: {e}")
        return -1
    finally:
        client.close()

def get_username_sync(account: dict) -> str:
    """
    Возвращает юзернейм Roblox по .ROBLOSECURITY cookie.
    """
    url = "https://users.roblox.com/v1/users/authenticated"
    raw = account.get("cookie", "").strip()
    cookie_header = raw if raw.startswith(".ROBLOSECURITY=") else f".ROBLOSECURITY={raw}"
    headers = {"Cookie": cookie_header, "User-Agent": "Mozilla/5.0"}
    try:
        client = httpx.Client()
        r = client.get(url, headers=headers, timeout=10)
        client.close()
        data = r.json()
        logger.debug(f"[User] Data: {data}")
        # используем либо username, либо displayName
        return data.get("name") or data.get("displayName") or "Unknown"
    except Exception as e:
        logger.error(f"Error fetching username: {e}")
        return "Unknown"


# ========== Purchasing ==========
def purchase_gamepass(account: dict, gamepass_id: int, expected_price: int = 0) -> bool:
    raw = account.get("cookie", "").strip()
    cookie_header = raw if raw.startswith(".ROBLOSECURITY=") else f".ROBLOSECURITY={raw}"
    headers = {
        "Cookie":      cookie_header,
        "User-Agent":  "Mozilla/5.0",
    }

    logger.info(f"[Purchase] Called purchase_gamepass for GamePass {gamepass_id}")

    try:
        # 1) GET product-info
        info_url   = f"https://apis.roblox.com/game-passes/v1/game-passes/{gamepass_id}/product-info"
        info_resp  = httpx.get(info_url, headers=headers, timeout=10)
        logger.info(f"[Purchase] product-info → {info_resp.status_code} {info_resp.text}")
        if info_resp.status_code != 200:
            logger.error(f"[Purchase] product-info failed: {info_resp.status_code}")
            return False

        info       = info_resp.json()
        product_id = info.get("ProductId")
        price      = info.get("PriceInRobux")
        seller_id  = info.get("Creator", {}).get("Id")
        logger.info(f"[Purchase] parsed ProductId={product_id}, Price={price}, SellerId={seller_id}")

        # 2) Проверяем, что цена GamePass совпадает с ожиданием
        if expected_price and price != expected_price:
            logger.error(f"[Purchase] Price mismatch: expected {expected_price} but actual price is {price}")
            return False

        if not all([product_id, price is not None, seller_id is not None]):
            logger.error(f"[Purchase] bad product-info payload: {info}")
            return False

        # 3) fetch CSRF token
        purchase_url = f"https://economy.roblox.com/v1/purchases/products/{product_id}"
        token_resp   = httpx.post(purchase_url, headers=headers, timeout=10)
        csrf_token   = token_resp.headers.get("x-csrf-token")
        logger.info(f"[Purchase] csrf-fetch → {token_resp.status_code}, token={csrf_token}")
        if not csrf_token:
            logger.error("[Purchase] no x-csrf-token in response")
            return False

        # 4) final POST to purchase
        purchase_headers = {
            **headers,
            "X-CSRF-Token": csrf_token,
            "Content-Type":  "application/json",
        }
        payload = {
            "expectedCurrency":  1,
            "expectedPrice":     price,
            "expectedSellerId":  seller_id,
        }
        logger.info(f"[Purchase] sending payload: {payload}")
        purchase_resp = httpx.post(purchase_url, headers=purchase_headers, json=payload, timeout=10)
        logger.info(f"[Purchase] final → {purchase_resp.status_code} {purchase_resp.text}")

        # parse JSON response
        resp_json = purchase_resp.json()
        if not resp_json.get("purchased", False):
            reason  = resp_json.get("reason", "<no-reason>")
            err_msg = resp_json.get("errorMsg", "")
            logger.error(f"[Purchase] Transaction failed: {reason} – {err_msg}")
            return False

        logger.info("[Purchase] Transaction succeeded")
        return True

    except Exception as e:
        logger.exception(f"[Purchase] exception: {e}")
        return False

# ======== Payment Processor =========
class PaymentProcessor:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.queue: asyncio.Queue = asyncio.Queue()
        threading.Thread(target=self._run, daemon=True).start()
        asyncio.run_coroutine_threadsafe(self._worker(), self.loop)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def enqueue(self, c, buyer_id: int, order: dict):
        logger.info(f"[Processor] Enqueuing order {order['order_id']} for buyer {buyer_id}")
        asyncio.run_coroutine_threadsafe(self.queue.put((c, buyer_id, order)), self.loop)

    async def _worker(self):
        while True:
            c, buyer_id, order = await self.queue.get()
            try:
                await self._handle_order(c, buyer_id, order)
            finally:
                self.queue.task_done()

    async def _handle_order(self, c, buyer_id: int, order: dict):
        gid = order.get('gamepass_id')
        logger.info(f"[Processor] Handling order {order['order_id']} for buyer {buyer_id}, gamepass {gid}")
        if not gid:
            msg = "❌ Gamepass ID не указан."
            logger.warning(f"[Processor] {msg}")
            c.send_message(buyer_id, msg)
            return
        success = False
        for account in ACCOUNTS:
            if purchase_gamepass(account, gid):
                success = True
                break
        if success:
            msg = f"✅ Заказ #{order['order_id']} выполнен: gamepass {gid}. После проверки подтвердите выполнение заказа!"
            logger.info(f"[Processor] {msg}")
            c.send_message(buyer_id, msg)
            c.telegram.bot.send_message(
                FUNPAY_USER_ID,
                f"🌟 Заказ #{order['order_id']} для {buyer_id}: {order['amount']} Robux через gamepass {gid}."
            )
            try:
                # 1. Определяем новый quantity
                balances = [get_robux_balance(acc) for acc in ACCOUNTS]
                max_bal = max(balances) if balances else 0
                new_qty = math.floor(max_bal * 0.7)

                # 2. Получаем все LotFields в категории 99
                lots = fetch_lots_by_subcategory(c.account, subcategory_id=99)

                # 3. Применяем update для каждого лота
                for lot in lots:
                    update_lot_quantity(c.account, lot.id, new_qty)
                logger.info(f"[sync] Количество лотов обновлено → {new_qty}")
            except Exception as e:
                logger.error(f"[sync] Ошибка при обновлении лотов: {e}")
        else:
            msg = "❌ Не удалось выполнить покупку."
            logger.error(f"[Processor] {msg} order {order['order_id']}")
            c.send_message(buyer_id, msg)
            # Проверяем авто-возврат из конфига
            if enabled_config.get("auto_refund", False):
                logger.info(f"[Processor] Auto-refund for order {order['order_id']}")
                c.account.refund(order['order_id'])

processor = PaymentProcessor()

# ========= FunPay Handlers =========
def handle_new_order(c, e, *args):
    order = e.order
    order_id = order.id
    if order.subcategory.id != 99:
        return  # ⛔ Не Robux

    m = re.search(r"(\d+(?:\.\d+)?)\s*ед\.\s*робуксов", order.description or "", flags=re.IGNORECASE)
    robux_amt = math.floor(float(m.group(1))) if m else math.floor(float(re.findall(r"(\d+(?:\.\d+)?)", order.description or "")[-1]))
    expected_price = math.ceil(robux_amt / 0.7)
    buyer_id = str(order.buyer_id)
    chat_id = (
        getattr(e, "chat_id", None)
        or getattr(order, "chat_id", None)
        or getattr(order, "chatId", None)
        or getattr(order, "buyer_id", None)
    )

    logger.info(f"[handle_new_order] 🧾 Заказ #{order_id} от {chat_id} (buyer_id={buyer_id}) на {robux_amt} Robux → GamePass {expected_price}")

    # Загружаем текущие заказы
    orders = load_orders()
    # Сохраняем новый заказ по buyer_id
    orders[buyer_id] = {
        "order_id": order_id,
        "expected_price": expected_price,
        "robux_amt": robux_amt,
        "chat_id": chat_id
    }
    save_orders(orders)

    msg = (
        f"🛒 Заказ #{order_id} на {robux_amt} Robux\n"
        f"Пожалуйста, создайте GamePass стоимостью {expected_price} Robux и отправьте его ID.\n"
        f"Инструкция по создания GamePass: https://telegra.ph/Kak-sozdat-gamepass-06-11"
    )
    template = enabled_config.get("post_payment_message", "").strip()
    if template:
        context = {
            "order_id":       order_id,
            "robux_amt":      robux_amt,
            "expected_price": expected_price,
        }
        try:
            text = template.format(**context)
        except KeyError as ke:
            logger.error(f"Formatting error for post_payment_message: unknown key {ke}")
            text = template
        c.send_message(chat_id, text)


def handle_new_message(c, e, *args):
    msg = e.message
    chat_id = str(getattr(msg, "chat_id", None))
    text = (getattr(msg, "text", "") or "").strip()
    author_id = str(getattr(msg, "author_id", None) or getattr(msg, "from_id", None) or getattr(msg, "from_user_id", None))

    if not chat_id:
        logger.warning("[handle_new_message] ❌ Нет chat_id в сообщении.")
        return
    if not text:
        logger.warning("[handle_new_message] ❌ Пустой текст.")
        return
    if not author_id:
        logger.warning("[handle_new_message] ❌ Нет author_id в сообщении.")
        return

    # Загружаем заказы
    orders = load_orders()
    order = orders.get(author_id)
    if not order:
        logger.info(f"[handle_new_message] 🚫 Нет активного заказа для author_id={author_id}")
        return

    order_id = order['order_id']
    expected_price = order['expected_price']

    # Достаём GamePass ID из текста
    match = re.search(r'game-pass(?:/|\?id=)(\d+)', text, re.IGNORECASE)
    if match:
        gamepass_id = int(match.group(1))
    elif text.isdigit():
        gamepass_id = int(text)
    else:
        logger.info(f"[handle_new_message] ⛔ Не удалось распознать GamePass ID.")
        return

    # Проверяем цену GamePass
    try:
        info_url = f"https://apis.roblox.com/game-passes/v1/game-passes/{gamepass_id}/product-info"
        resp = httpx.get(info_url, timeout=10)
        logger.debug(f"[handle_new_message] 🌐 GamePass info → {resp.status_code} {resp.text[:200]}")
        if resp.status_code != 200:
            raise Exception(f"Status {resp.status_code}: {resp.text}")
        info = resp.json()
        price = int(info.get("PriceInRobux", -1))
        logger.info(f"[handle_new_message] 💰 Цена GamePass: {price}, ожидалось: {expected_price}")
    except Exception as ex:
        logger.exception("[handle_new_message] ❌ Ошибка запроса цены GamePass")
        c.send_message(chat_id, "⚠️ Ошибка при проверке GamePass. Убедитесь, что ID правильный.")
        return

    if price != expected_price:
        c.send_message(
            chat_id,
            f"❌ GamePass должен стоить ровно {expected_price} Robux для заказа {order_id}.\n"
            "Пожалуйста, создайте новый и отправьте ID снова."
        )
        return

    # Покупаем GamePass
    success = False
    for acc in ACCOUNTS:
        if purchase_gamepass(acc, gamepass_id, expected_price):
            success = True
            break

    if success:
        c.send_message(chat_id, f"✅ Заказ #{order_id} выполнен: GamePass {gamepass_id} куплен! Не забудьте подтвердить заказ!")
        # Удаляем заказ после успешной покупки
        orders.pop(author_id)
        save_orders(orders)
    else:
        c.send_message(chat_id, "❌ Не удалось купить GamePass. Попробуйте другой или позже.")
        # Проверяем авто-возврат из конфига
        if enabled_config.get("auto_refund", False):
            try:
                c.account.refund(order_id)
                c.send_message(chat_id, "💸 Средства возвращены.")
            except Exception as e:
                logger.error(f"[Refund] ❌ Ошибка возврата #{order_id}: {e}")




#узнаём на каком акке самый большой баланс
def _get_max_balance() -> int:  
    balances = [get_robux_balance(acc) for acc in ACCOUNTS]  
    return max(balances, default=0)

# ===== FunPay API Helpers ==========
def fetch_lots_by_subcategory(account, subcategory_id: int):
    """Получает все лоты в указанной подкатегории"""
    try:
        lots = account.get_lots()
        return [lot for lot in lots if lot.subcategory.id == subcategory_id]
    except Exception as e:
        logger.error(f"[fetch_lots] Ошибка получения лотов: {e}")
        return []

def update_lot_quantity(account, lot_id: int, new_quantity: int):
    """Обновляет количество товара в лоте"""
    try:
        account.update_lot(lot_id, {"quantity": new_quantity})
        logger.info(f"[update_lot] Обновлен лот {lot_id}, количество: {new_quantity}")
    except Exception as e:
        logger.error(f"[update_lot] Ошибка обновления лота {lot_id}: {e}")



# ===== Administration Panel ==========
def autorobux_config_panel(c, m):
    rs = "Вкл" if enabled_config.get("auto_refund") else "Выкл"
    lines = []
    for idx, acc in enumerate(ACCOUNTS):
        name = get_username_sync(acc)
        bal = get_robux_balance(acc)
        lines.append(f"{name}: {bal} Robux")
    accounts_text = "\n".join(lines) if lines else "Нет аккаунтов"

    panel_text = (
        f"✨ <b>AutoRobux Panel</b> ✨\n"
        f"Авто-возврат: {rs}\n"
        f"{accounts_text}"
    )

    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="manage_accounts"),
        InlineKeyboardButton(text="🔧 Настройка прокси", callback_data="proxy_settings")
    )
    kb.add(
        InlineKeyboardButton(text="🗑️ Удалить аккаунт", callback_data="delete_account_menu"),
        InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh_panel")
    )
    kb.add(
        InlineKeyboardButton(text="📋 Логи", callback_data="send_logs"),
        InlineKeyboardButton(text="⚙️ Настройки", callback_data="open_settings"),
        InlineKeyboardButton(text="🐞 Отладка", callback_data="debug_menu"),
        InlineKeyboardButton(text="✏️ Сообщение после оплаты", callback_data="edit_post_msg")
    )
    kb.add(
        InlineKeyboardButton("Наш Telegram-канал", url="https://t.me/msmshopDozxz")
    )
    
    c.telegram.bot.send_message(m.chat.id, panel_text, reply_markup=kb, parse_mode="HTML")

# ===== Debug Handlers =====
def handle_debug_amount(c, m):
    global BOT
    logger.info(f"[AutoRobux] handle_debug_amount triggered with text: {m.text}")
    try:
        total = int(m.text.strip())
    except ValueError:
        BOT.send_message(m.chat.id, "❌ Введите число.")
        c.send_message(m.chat.id, "❌ Введите число.")
        return
    price = math.ceil(total / 0.7)
    DEBUG_CTX[m.chat.id] = price

    text1 = f"Стоимость Gamepass: {price} Robux. Пожалуйста, создайте Gamepass этой стоимости и отправьте его ID."
    BOT.send_message(m.chat.id, text1)
    c.send_message(m.chat.id, text1)

    c.send_message(chat_to_reply, "Отправьте мне, пожалуйста, ID GamePass для этого заказа.")

    BOT.register_next_step_handler(msg, lambda m2: handle_debug_gamepass_id(c, m2))
    logger.info("[AutoRobux] Registered next step handler for handle_debug_gamepass_id")


def handle_debug_gamepass_id(c, m):
    if not ACCOUNTS:
        BOT.send_message(m.chat.id, "❌ Ошибка: не загружено ни одного аккаунта для покупки.")
        c.send_message(m.chat.id, "❌ Ошибка: не загружено ни одного аккаунта для покупки.")
        return

    if not m.text.isdigit():
        BOT.send_message(m.chat.id, "❌ Введите числовой ID GamePass.")
        c.send_message(m.chat.id, "❌ Введите числовой ID GamePass.")
        return

    gid = int(m.text.strip())
    price = DEBUG_CTX.get(m.chat.id)

    # Пробуем купить через все аккаунты
    success = False
    for account in ACCOUNTS:
        logtxt = f"[Debug] Попытка купить GamePass {gid} за {price} Robux через аккаунт {account['cookie'][:12]}"
        logger.info(logtxt)
        BOT.send_message(m.chat.id, logtxt)
        c.send_message(m.chat.id, logtxt)

        result = purchase_gamepass(account, gid, expected_price=price)
        res_txt = f"[Debug] purchase_gamepass → {result}"
        logger.info(res_txt)
        BOT.send_message(m.chat.id, res_txt)
        c.send_message(m.chat.id, res_txt)

        if result:
            ok_txt = f"✅ Debug: покупка GamePass {gid} за {price} Robux — УСПЕШНА."
            BOT.send_message(m.chat.id, ok_txt)
            c.send_message(m.chat.id, ok_txt)
            return
        else:
            warn_txt = f"❌ Debug: не удалось купить через аккаунт {account['cookie'][:12]}"
            logger.warning(warn_txt)
            BOT.send_message(m.chat.id, warn_txt)
            c.send_message(m.chat.id, warn_txt)

    # Если ни одна покупка не сработала
    fail_txt = f"❌ Debug: покупка GamePass {gid} за {price} Robux — НЕ УДАЛАСЬ."
    BOT.send_message(m.chat.id, fail_txt)
    c.send_message(m.chat.id, fail_txt)





# ===== Initialization ==========
def init_commands(c: 'Cardinal'):
    """
    Регистрирует команды и хендлеры для бота.
    """
    global BOT
    BOT = c.telegram.bot
    logger.info("AutoRobux: init_commands called")

    # Команда панели
    @BOT.message_handler(commands=["autorobux_config"])
    def _cmd(m):
        try:
            autorobux_config_panel(c, m)
        except Exception as e:
            logger.error(f"[AutoRobux] Error displaying panel: {e}")

    # Общий callback_query handler для всех кнопок панели
    @BOT.callback_query_handler(func=lambda call: call.data in [
        "manage_accounts", "proxy_settings", "proxy_add_menu", "proxy_delete_menu",
        "delete_account_menu", "refresh_panel", "send_logs", "open_settings", "toggle_refund",
        "debug_menu", "debug_buy_menu", "edit_post_msg"
    ] or call.data.startswith(("add_proxy_", "delete_proxy_", "delete_account_")))

    def _panel_handler(call):
        chat_id = call.message.chat.id
        data    = call.data
        BOT.answer_callback_query(call.id)

        #Изменение сообщения после оплаты
        if data == "edit_post_msg":
            BOT.send_message(chat_id, "✏️ Введите новое сообщение, которое будет отправляться покупателю после оплаты:")
            BOT.register_next_step_handler(call.message, handle_set_post_msg)
            return


        # — Добавить аккаунт
        if data == "manage_accounts":
            BOT.send_message(chat_id, "🌐 Отправьте cookie (.ROBLOSECURITY=...) через сообщение:")
            BOT.register_next_step_handler(call.message, handle_add_account)
            return

        # — Proxy settings
        if data == "proxy_settings":
            kb2 = InlineKeyboardMarkup(row_width=1)
            kb2.add(
                InlineKeyboardButton("➕ Добавить прокси", callback_data="proxy_add_menu"),
                InlineKeyboardButton("➖ Удалить прокси", callback_data="proxy_delete_menu"),
                InlineKeyboardButton("❌ Назад",      callback_data="refresh_panel"),
            )
            c.telegram.bot.send_message(chat_id, "🔧 Настройка прокси — выберите действие:", reply_markup=kb2)
            return

        # — Add proxy menu
        if data == "proxy_add_menu":
            kb2 = InlineKeyboardMarkup(row_width=1)
            for idx in range(len(ACCOUNTS)):
                kb2.add(InlineKeyboardButton(f"Акк #{idx+1}", callback_data=f"add_proxy_{idx}"))
            kb2.add(InlineKeyboardButton("❌ Назад", callback_data="proxy_settings"))
            c.telegram.bot.send_message(chat_id, "Выберите аккаунт для добавления прокси:", reply_markup=kb2)
            return

        # — Delete proxy menu
        if data == "proxy_delete_menu":
            kb2 = InlineKeyboardMarkup(row_width=1)
            for idx, acc in enumerate(ACCOUNTS):
                if acc.get("proxy"):
                    kb2.add(InlineKeyboardButton(f"Акк #{idx+1}", callback_data=f"delete_proxy_{idx}"))
            if not kb2.keyboard:
                c.telegram.bot.send_message(chat_id, "⚠️ Нет привязанных прокси")
                return
            kb2.add(InlineKeyboardButton("❌ Назад", callback_data="proxy_settings"))
            c.telegram.bot.send_message(chat_id, "Выберите аккаунт для удаления прокси:", reply_markup=kb2)
            return

        # — Delete account menu
        if data == "delete_account_menu":
            kb2 = InlineKeyboardMarkup(row_width=1)
            if not ACCOUNTS:
                c.telegram.bot.send_message(chat_id, "⚠️ Нет аккаунтов")
                return
            for idx in range(len(ACCOUNTS)):
                kb2.add(InlineKeyboardButton(f"Акк #{idx+1}", callback_data=f"delete_account_{idx}"))
            kb2.add(InlineKeyboardButton("❌ Назад", callback_data="refresh_panel"))
            c.telegram.bot.send_message(chat_id, "Выберите аккаунт для удаления:", reply_markup=kb2)
            return

        # — Handle prefix commands
        if data.startswith("add_proxy_"):
            idx = int(data.split("_")[-1])
            c.telegram.bot.send_message(chat_id, f"Введите proxy для аккаунта #{idx+1}:")
            BOT.register_next_step_handler(call.message, lambda m, i=idx: handle_add_proxy(m, i))
            return

        if data.startswith("delete_proxy_"):
            idx = int(data.split("_")[-1])
            ACCOUNTS[idx]["proxy"] = None
            enabled_config["accounts"] = ACCOUNTS
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(enabled_config, f, indent=4, ensure_ascii=False)
            c.telegram.bot.send_message(chat_id, f"✅ Прокси удалён у аккаунта #{idx+1}")
            autorobux_config_panel(c, call.message)
            return

        if data.startswith("delete_account_"):
            idx = int(data.split("_")[-1])
            ACCOUNTS.pop(idx)
            enabled_config["accounts"] = ACCOUNTS
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(enabled_config, f, indent=4, ensure_ascii=False)
            c.telegram.bot.send_message(chat_id, f"✅ Удалён аккаунт #{idx+1}")
            autorobux_config_panel(c, call.message)
            return

        # — Refresh panel
        if data == "refresh_panel":
            autorobux_config_panel(c, call.message)
            return

        # — Send logs
        if data == "send_logs":
            if os.path.exists(log_path):
                with open(log_path, 'rb') as f:
                    c.telegram.bot.send_document(chat_id, f, caption="📋 Логи AutoRobux")
            else:
                c.telegram.bot.send_message(chat_id, "❌ Лог не найден.")
            return

        # — Open settings
        if data == "open_settings":
            kb2 = InlineKeyboardMarkup(row_width=1)
            auto_refund_status = "Вкл" if enabled_config.get("auto_refund", False) else "Выкл"
            kb2.add(
                InlineKeyboardButton(f"🔄 Авто-возврат: {auto_refund_status}", callback_data="toggle_refund"),
                InlineKeyboardButton("❌ Назад", callback_data="refresh_panel")
            )
            c.telegram.bot.send_message(chat_id, "⚙️ Настройки — выберите действие:", reply_markup=kb2)
            return
            

        # — Toggle refund
        if data == "toggle_refund":
            enabled_config["auto_refund"] = not enabled_config.get("auto_refund", False)
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(enabled_config, f, indent=4, ensure_ascii=False)
            c.telegram.bot.send_message(chat_id, f"✅ Авто-возврат: {'Вкл' if enabled_config['auto_refund'] else 'Выкл'}")
            # Возвращаемся в меню настроек
            kb2 = InlineKeyboardMarkup(row_width=1)
            auto_refund_status = "Вкл" if enabled_config.get("auto_refund", False) else "Выкл"
            kb2.add(
                InlineKeyboardButton(f"🔄 Авто-возврат: {auto_refund_status}", callback_data="toggle_refund"),
                InlineKeyboardButton("❌ Назад", callback_data="refresh_panel")
            )
            c.telegram.bot.send_message(chat_id, "⚙️ Настройки — выберите действие:", reply_markup=kb2)
            return

        # — Debug menu
        if data == "debug_menu":
            kb = InlineKeyboardMarkup(row_width=1)
            kb.add(
                InlineKeyboardButton("Купить GamePass", callback_data="debug_buy_menu"),
                InlineKeyboardButton("❌ Назад",       callback_data="refresh_panel")
            )
            c.telegram.bot.send_message(chat_id, "🔧 Отладка — выберите действие:", reply_markup=kb)
            return

        # — Debug buy menu
        if data == "debug_buy_menu":
            msg = c.telegram.bot.send_message(chat_id, "Введите сумму Robux для покупки:")
            BOT.register_next_step_handler(msg, lambda m: handle_debug_amount(c, m))
            return
        



        # — Unknown command
        c.telegram.bot.send_message(chat_id, "⚠️ Неизвестная команда.")

    # Привязываем команду панели в UI Cardinal
    c.add_telegram_commands(UUID, [
        ("autorobux_config", "Показать панель управления AutoRobux", True)
    ])

# Helper functions

def handle_set_post_msg(m):
    enabled_config["post_payment_message"] = m.text
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(enabled_config, f, indent=4, ensure_ascii=False)
    BOT.send_message(m.chat.id, "✅ Сообщение после оплаты обновлено.")


def handle_add_account(m):
    cookie = m.text.strip()
    ACCOUNTS.append({"cookie": cookie, "proxy": None})
    enabled_config["accounts"] = ACCOUNTS
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(enabled_config, f, indent=4, ensure_ascii=False)
    BOT.send_message(m.chat.id, "✅ Аккаунт добавлен")


def handle_set_lot_id(m):
    raw = m.text.strip()
    enabled_config["selected_lot_id"] = raw
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(enabled_config, f, indent=4, ensure_ascii=False)
    BOT.send_message(m.chat.id, f"✅ ID лота сохранён: {raw}")

def handle_add_proxy(m, idx: int):
    proxy = m.text.strip() or None
    ACCOUNTS[idx]["proxy"] = proxy
    enabled_config["accounts"] = ACCOUNTS
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(enabled_config, f, indent=4, ensure_ascii=False)
    BOT.send_message(m.chat.id, f"✅ Прокси для аккаунта #{idx+1} обновлён")

# ===== Metadata =====
NAME = "AutoRobux"
VERSION = "1.2"
DESCRIPTION = "Авто-выдача Robux через Gamepass"
CREDITS = "@wormdcShop_bot"
UUID = "e2f2b1c0-3d4a-4fa5-b123-abcdef123456"
SETTINGS_PAGE = False
BIND_TO_PRE_INIT    = [init_commands]
BIND_TO_NEW_ORDER   = [handle_new_order]
BIND_TO_NEW_MESSAGE = [handle_new_message]
BIND_TO_DELETE      = []
# End of plugin
