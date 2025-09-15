from __future__ import annotations

import time
from typing import TYPE_CHECKING

import FunPayAPI

if TYPE_CHECKING:
    from cardinal import Cardinal
from FunPayAPI.account import Account
import logging
from telebot.types import Message, CallbackQuery, InlineKeyboardMarkup as K, InlineKeyboardButton as B
from tg_bot import CBT
from typing import Union

NAME = "Delete lots plugin"
VERSION = "0.0.2"
DESCRIPTION = "Плагин, удаляющий лоты на аккаунте"
CREDITS = "@arthells"
UUID = "d3737eff-dae0-40a1-8632-de3166e9da95"
SETTINGS_PAGE = False

logger = logging.getLogger("FPC.del_lots_plugin")
LOGGER_PREFIX = "[DELETE LOTS PLUGIN]"

CBT_HIDE_ALL_LOTS = 'HIDE_LOTS'
CBT_HIDE_LOTS_IN_CATEGORY = 'HIDE_LOTS_IN_CATEGORY'
CBT_ACCEPT_HIDE_ALL_LOTS = 'ACCEPT_HIDE_LOTS'


def init(cardinal: Cardinal, *args):
    if not cardinal.telegram:
        return
    tg = cardinal.telegram
    bot = tg.bot

    def get_lots_info(t: Union[CallbackQuery, Message], profile: FunPayAPI.types.UserProfile,
                      category_id: int = None) -> list[FunPayAPI.types.LotFields]:
        """
        Получает данные о всех лотах (кроме валюты) на текущем аккаунте.

        :param t: экземпляр триггера.
        :param profile: экземпляр текущего аккаунта.
        :param category_id: ID категории, лоты которой нужно получить.

        :return: список экземпляров лотов.
        """
        result = []
        if isinstance(t, CallbackQuery):
            chat_id = t.message.chat.id
        else:
            chat_id = t.chat.id
        for i in profile.get_lots():
            if i.subcategory.type == FunPayAPI.types.SubCategoryTypes.CURRENCY:
                continue
            if category_id is not None and i.subcategory.id != category_id:
                continue
            attempts = 3
            while attempts:
                try:
                    lot_fields = cardinal.account.get_lot_fields(i.id)
                    fields = lot_fields.fields
                    if "secrets" in fields.keys():
                        fields["secrets"] = ""
                        del fields["auto_delivery"]
                    result.append(lot_fields)
                    logger.info(f"{LOGGER_PREFIX} Получил данные о лоте {i.id}.")
                    break
                except:
                    logger.error(f"{LOGGER_PREFIX} Не удалось получить данные о лоте {i.id}.")
                    logger.debug("TRACEBACK", exc_info=True)
                    time.sleep(2)
                    attempts -= 1
            else:
                bot.send_message(chat_id, f"❌ Не удалось получить данные о "
                                          f"<a href=\"https://funpay.com/lots/offer?id={i.id}\">лоте {i.id}</a>."
                                          f" Пропускаю.")
                time.sleep(1)
                continue
            time.sleep(0.5)
        return result

    def hide_lot(acc: Account, lot: FunPayAPI.types.LotFields):
        lot_id = lot.lot_id
        fields = lot.fields
        logger.info(fields)
        fields["deleted"] = "on"
        lot.set_fields(fields)
        logger.info(fields)
        attempts = 3
        while attempts:
            try:
                headers = {
                    "accept": "*/*",
                    "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "x-requested-with": "XMLHttpRequest",
                }
                fields = lot.renew_fields().fields
                fields["location"] = "trade"

                response = acc.method("post", "lots/offerSave", headers, fields, raise_not_200=True)
                logger.info(response.json())
                logger.info(f"{LOGGER_PREFIX} Скрыл лот {lot_id}.")
                return
            except Exception as e:
                logger.error(f"{LOGGER_PREFIX} Не удалось скрыть лот {lot_id}.")
                logger.debug("TRACEBACK", exc_info=True)
                if isinstance(e, FunPayAPI.exceptions.RequestFailedError):
                    logger.debug(e.response.content.decode())
                time.sleep(2)
                attempts -= 1
        else:
            raise Exception

    def msg_hide_all_lots(c: CallbackQuery):
        """
        callback: CBT_HIDE_ALL_LOTS
        """
        bot.send_message(c.message.chat.id, "Ты уверен что хочешь удалить все лоты?", reply_markup=K().row(
            B("✅ Потдвердить", None, f'{CBT_ACCEPT_HIDE_ALL_LOTS}'),
            B("🚫 Отменить", None, f'{CBT.PLUGIN_SETTINGS}:{UUID}:0')
        ))

    def hide_all_lots(c: CallbackQuery):
        """
        f'{CBT_ACCEPT_HIDE_ALL_LOTS}'
        """
        bot.answer_callback_query(c.id)
        lots_hided = 0
        while True:
            if cardinal.profile:
                lots = get_lots_info(c, cardinal.profile)
                break
            time.sleep(1)
        for lot in lots:
            hide_lot(cardinal.account, lot)
            lots_hided += 1
        bot.send_message(c.message.chat.id, f'✅ Успешно удалил `{lots_hided}` лотов', parse_mode="Markdown")

    def st_hide_lots_in_category(c: CallbackQuery):
        """
        CBT_HIDE_LOTS_IN_CATEGORY
        """
        result = bot.send_message(c.message.chat.id, "Отправь мне ID категории в которой хочешь удалить лоты",
                                  reply_markup=K().row(
                                      B("🚫 Отменить", None, f'{CBT.CLEAR_STATE}')
                                  ))
        tg.set_state(c.message.chat.id, result.id, c.from_user.id, CBT_HIDE_LOTS_IN_CATEGORY)
        bot.answer_callback_query(c.id)

    def hide_lots_in_category(m: Message):
        tg.clear_state(m.chat.id, m.from_user.id, True)
        if not m.text.isdigit():
            bot.send_message(m.chat.id, 'ID категории должен содержать только цифры!')
            return
        category_id = int(m.text)
        lots = get_lots_info(m, cardinal.profile, category_id)
        lots_hided = 0
        for lot in lots:
            hide_lot(cardinal.account, lot)
            lots_hided += 1
        bot.send_message(m.chat.id, f'✅ Успешно удалил `{lots_hided}` лотов', parse_mode="Markdown")

    def open_menu(m: Message):
        """
        f'{CBT.PLUGIN_SETTINGS}:{UUID}:0'
        """
        kb = (K().row(B('Удалить лоты по всех категориях', None, CBT_HIDE_ALL_LOTS))
              .row(B('Удалить лоты в опред. категории', None, CBT_HIDE_LOTS_IN_CATEGORY))
              .row(B('Назад', None, f'{CBT.EDIT_PLUGIN}:{UUID}:0')))
        bot.send_message(chat_id=m.chat.id, text='Настройки плагина:', reply_markup=kb)
        bot.answer_callback_query(c.id)

    tg.cbq_handler(open_menu, lambda call: call.data.startswith(f'{CBT.PLUGIN_SETTINGS}:{UUID}'))
    tg.cbq_handler(msg_hide_all_lots, lambda c: c.data == CBT_HIDE_ALL_LOTS)
    tg.cbq_handler(hide_all_lots, lambda c: c.data == CBT_ACCEPT_HIDE_ALL_LOTS)
    tg.cbq_handler(st_hide_lots_in_category, lambda c: c.data == CBT_HIDE_LOTS_IN_CATEGORY)
    tg.msg_handler(hide_lots_in_category, func=lambda m: tg.check_state(m.chat.id, m.from_user.id, CBT_HIDE_LOTS_IN_CATEGORY))
    tg.msg_handler(open_menu, commands=['del_lots'])
    cardinal.add_telegram_commands(UUID, [
        ("del_lots", "Удалить лоты", True)
    ])


BIND_TO_PRE_INIT = [init]
BIND_TO_DELETE = None
