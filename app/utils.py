from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

async def update_channel_caption(bot: Bot, channel_id: int, message_id: int, caption: str):
    try:
        await bot.edit_message_caption(chat_id=channel_id, message_id=message_id, caption=caption, parse_mode="HTML")
    except Exception:
        try:
            await bot.edit_message_text(chat_id=channel_id, message_id=message_id, text=caption, parse_mode="HTML")
        except Exception:
            pass

def tri_buttons(pay_url: str, postpone_cb: str, decline_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 ОПЛАТИТИ", url=pay_url)],
        [InlineKeyboardButton(text="⏳ ВІДКЛАСТИ", callback_data=postpone_cb)],
        [InlineKeyboardButton(text="🚫 ВІДМОВИТИСЬ", callback_data=decline_cb)],
    ])