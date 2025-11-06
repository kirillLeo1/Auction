from __future__ import annotations

from datetime import datetime, timedelta

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InputMediaPhoto
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from sqlalchemy import select, func, desc

from app.db import async_session
from app.models import Lot, LotStatus, Bid, Offer, OfferStatus
from app.settings import settings
from app.utils import update_channel_caption, tri_buttons
from app.services.monopay import create_invoice

user_router = Router()


# ─────────── FSM
class BidSG(StatesGroup):
    ENTER_SUM = State()


class ContactOneSG(StatesGroup):
    # один крок: «ПІБ / місто / відділення або адреса / телефон / коментар…» (будь-що)
    ONE = State()


# ─────────── helpers
async def _me_username(bot: Bot) -> str:
    me = await bot.get_me()
    return me.username or ""


# ─────────── /start з deep-link’ами
@user_router.message(F.text.startswith("/start"))
async def start_entry(msg: Message, state: FSMContext, bot: Bot):
    arg = ""
    parts = msg.text.split(maxsplit=1)
    if len(parts) == 2:
        arg = parts[1].strip()

    # УВАГА: НЕ приймаємо contact_* (це робить вебхук без лінків).
    # РОЗПРОДАЖ (фіксована ціна: Купити)
    if arg.startswith("sale_"):
        pub_id = int(arg.split("_", 1)[1])
        async with async_session() as session:
            lot = (
                await session.execute(select(Lot).where(Lot.public_id == pub_id))
            ).scalar_one_or_none()
            if not lot or lot.status != LotStatus.ACTIVE or lot.min_step != 0:
                await msg.answer("Лот недоступний.")
                return

            # sold-out?
            paid_cnt = (
                await session.execute(
                    select(func.count()).select_from(Offer).where(
                        Offer.lot_id == lot.id, Offer.status == OfferStatus.PAID
                    )
                )
            ).scalar_one()
            if paid_cnt >= lot.quantity:
                await msg.answer("На жаль, товар вже продано.")
                return

            # існуюча активна заявка цього юзера?
            existing = (
                await session.execute(
                    select(Offer).where(
                        Offer.lot_id == lot.id,
                        Offer.user_tg_id == msg.from_user.id,
                        Offer.status.in_([OfferStatus.OFFERED, OfferStatus.POSTPONED]),
                    )
                )
            ).scalars().first()

            if existing:
                offer = existing
            else:
                offer = Offer(
                    lot_id=lot.id,
                    user_tg_id=msg.from_user.id,
                    offered_price=lot.current_price,
                    rank_index=1,
                    status=OfferStatus.OFFERED,
                    hold_until=datetime.utcnow()
                    + timedelta(hours=settings.HOLD_HOURS),
                )
                session.add(offer)
                await session.flush()

            inv_id, page_url = await create_invoice(
                amount_uah=offer.offered_price,
                reference=f"sale#{lot.public_id}-user{offer.user_tg_id}-offer{offer.id}",
                destination=f"Покупка товару #{lot.public_id}",
                comment=f"Лот #{lot.public_id}",
                offer_id=offer.id,
            )
            offer.invoice_id = inv_id
            offer.invoice_url = page_url
            await session.commit()

        kb = tri_buttons(page_url, f"postpone:{offer.id}", f"decline:{offer.id}")
        await msg.answer(
            f"Лот #{lot.public_id}: {lot.title}\n\n"
            f"Ціна до оплати: <b>{offer.offered_price} грн</b>\n"
            f"Оплатити потрібно протягом {settings.HOLD_HOURS} годин.",
            parse_mode="HTML",
            reply_markup=kb,
        )
        return

    # АУКЦІОН (Бронь → введення ставки)
    if arg.startswith("lot_") or arg.isdigit():
        pub_id = int(arg.split("_", 1)[1]) if arg.startswith("lot_") else int(arg)
        async with async_session() as session:
            lot = (
                await session.execute(select(Lot).where(Lot.public_id == pub_id))
            ).scalar_one_or_none()
        if not lot or lot.status != LotStatus.ACTIVE or lot.min_step <= 0:
            await msg.answer("Лот не знайдено або недоступний для ставок.")
            return
        await state.update_data(lot_pub_id=pub_id)
        await state.set_state(BidSG.ENTER_SUM)
        await msg.answer(
            f"Лот #{lot.public_id}: {lot.title}\n"
            f"Поточна ціна: {lot.current_price} грн\n"
            f"Крок: {lot.min_step} грн\n\n"
            "Введіть суму вашої ставки (ціле число):"
        )
        return

    await msg.answer(
        "Введіть ID поста або перейдіть за посиланням у пості (Бронь/Купити)."
    )


# ─────────── ставка
@user_router.message(BidSG.ENTER_SUM, F.text.regexp(r"^\d+$"))
async def place_bid(msg: Message, state: FSMContext, bot: Bot):
    amount = int(msg.text)
    data = await state.get_data()
    pub_id = int(data.get("lot_pub_id"))

    prev_leader_id: int | None = None

    async with async_session() as session:
        lot = (
            await session.execute(select(Lot).where(Lot.public_id == pub_id))
        ).scalar_one_or_none()
        if not lot or lot.status != LotStatus.ACTIVE or lot.min_step <= 0:
            await msg.answer("Лот недоступний.")
            await state.clear()
            return

        min_allowed = lot.current_price + lot.min_step
        if amount < min_allowed:
            await msg.answer(f"Мінімальна ставка: {min_allowed} грн. Спробуйте ще раз.")
            return

        # запис ставки
        session.add(
            Bid(
                lot_id=lot.id,
                user_tg_id=msg.from_user.id,
                username=msg.from_user.username,
                amount=amount,
            )
        )
        # оновлюємо поточну ціну
        lot.current_price = amount
        prev_leader_id = lot.current_winner_tg_id

        # визначаємо поточного лідера
        stmt = (
            select(Bid.user_tg_id, func.max(Bid.amount).label("mx"))
            .where(Bid.lot_id == lot.id)
            .group_by(Bid.user_tg_id)
            .order_by(desc("mx"))
            .limit(1)
        )
        res = (await session.execute(stmt)).first()
        lot.current_winner_tg_id = res[0] if res else msg.from_user.id

        # значення для оновлення підпису
        chan_id, chan_mid = lot.channel_id, lot.channel_message_id
        title, step = lot.title, lot.min_step

        await session.commit()

    await msg.answer("✅ Ставку прийнято! Ви в грі.")

    # Оновлюємо підпис поста в каналі
    username = await _me_username(bot)
    caption = (
        f"<b>{title}</b>\n\n"
        f"Поточна ціна: <b>{amount} грн</b>\n"
        f"Крок: {step} грн\n\n"
        f"<a href=\"https://t.me/{username}?start=lot_{pub_id}\">Бронь</a>\n\n"
        f"ID лота — #{pub_id}"
    )
    if chan_mid:
        await update_channel_caption(bot, chan_id, chan_mid, caption)

    # Оповіщення попереднього лідера
    if prev_leader_id and prev_leader_id != msg.from_user.id:
        try:
            await bot.send_message(
                prev_leader_id,
                f"⚠️ Вас перебили у #{pub_id}. Нова ціна {amount} грн.\n"
                f"<a href=\"https://t.me/{username}?start=lot_{pub_id}\">Бронь</a>",
                parse_mode="HTML",
            )
        except Exception:
            pass

    await state.clear()


@user_router.message(BidSG.ENTER_SUM)
async def bid_err(msg: Message):
    await msg.answer("Тільки ціле число. Спробуйте ще раз.")


# ─────────── callbacks: ВІДКЛАСТИ / ВІДМОВИТИСЬ
@user_router.callback_query(F.data.startswith("postpone:"))
async def cb_postpone(call: CallbackQuery, state: FSMContext):
    offer_id = int(call.data.split(":", 1)[1])
    async with async_session() as session:
        off = await session.get(Offer, offer_id)
        if not off or off.user_tg_id != call.from_user.id:
            await call.answer("Недійсно", show_alert=True)
            return
        off.status = OfferStatus.POSTPONED
        # якщо чомусь не стоїть дедлайн — виставимо 24 години від зараз
        if not off.hold_until:
            off.hold_until = datetime.utcnow() + timedelta(hours=settings.HOLD_HOURS)
        await session.commit()

    await state.update_data(offer_id=offer_id)
    await state.set_state(ContactOneSG.ONE)
    await call.message.answer(
        "Окей, відкладаємо. Надішліть ваші дані одним повідомленням (будь-який формат, приклад є у /start)."
    )
    await call.answer()


@user_router.callback_query(F.data.startswith("decline:"))
async def cb_decline(call: CallbackQuery):
    offer_id = int(call.data.split(":", 1)[1])
    async with async_session() as session:
        off = await session.get(Offer, offer_id)
        if not off or off.user_tg_id != call.from_user.id:
            await call.answer("Недійсно", show_alert=True)
            return
        off.status = OfferStatus.DECLINED
        await session.commit()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await call.answer("Відхилено")


# ─────────── одноразова контакт-форма (після PAID або POSTPONED)
@user_router.message(ContactOneSG.ONE)
async def one_shot_contacts(msg: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    offer_id = int(data.get("offer_id", 0))

    async with async_session() as session:
        off = await session.get(Offer, offer_id)
        if not off:
            await msg.answer("Посилання застаріло. Спробуйте ще раз /start")
            await state.clear()
            return

        # Приймаємо будь-який текст як «дані покупця»
        off.contact_comment = msg.text
        await session.flush()

        # Забираємо лот разом із фото
        from sqlalchemy import select as _select
        from sqlalchemy.orm import selectinload
        lot = (
            await session.execute(
                _select(Lot)
                .options(selectinload(Lot.photos))
                .where(Lot.id == off.lot_id)
            )
        ).scalar_one()

        await session.commit()

    tag = "Оплачено" if off.status == OfferStatus.PAID else "Відкладено"
    user_link = f"@{msg.from_user.username}" if msg.from_user.username else f"id:{msg.from_user.id}"

   # Формуємо ОДИН caption: спочатку опис лота (як написав адмін),
# нижче — заявка (статус/ціна/контакти). Без «жирних шапок».
    desc_text = (lot.title or "").strip()              # тут у тебе весь опис поста
    if not desc_text:
        desc_text = f"Лот #{lot.public_id}"            # підстраховка, якщо опис порожній

    order_block = (
        f"\n\n<b>Заявка</b> — <b>{tag}</b>\n"
        f"Ціна: <b>{off.offered_price} грн</b>\n"
        f"Покупець: {user_link}\n"
        f"{msg.text}"
    )

# Якщо хочеш взагалі без рядка з ID — просто прибери наступний рядок із ID
    base_caption = f"{desc_text}\n\nID лота — #{lot.public_id}{order_block}"

# Ліміт підпису ~1024 — спочатку збережемо заявку цілою, уріжемо тільки опис
    if len(base_caption) > 1024:
        fixed_tail = f"\n\nID лота — #{lot.public_id}{order_block}"
        max_desc = 1024 - len(fixed_tail)
    # мінімальна страховка, щоб не піти в мінус
        if max_desc < 10:
        # якщо опис надто довгий, лишаємо тільки заявку + ID
            base_caption = fixed_tail.lstrip()
        else:
            desc_cut = desc_text[:max_desc-3] + "..."
            base_caption = f"{desc_cut}{fixed_tail}"


    sent = False
    target_chat = settings.MANAGER_CHAT_ID or 0

    try:
        if target_chat:
            # Є фото → шлемо фото/медіагрупу з підписом до першого кадра
            if lot.photos:
                if len(lot.photos) == 1:
                    await bot.send_photo(
                        chat_id=target_chat,
                        photo=lot.photos[0].file_id,
                        caption=base_caption,
                        parse_mode="HTML",
                    )
                else:
                    media = [InputMediaPhoto(media=p.file_id) for p in lot.photos]
                    media[0].caption = base_caption
                    media[0].parse_mode = "HTML"
                    await bot.send_media_group(chat_id=target_chat, media=media)
            else:
                # без фото — просто текст
                await bot.send_message(chat_id=target_chat, text=base_caption, parse_mode="HTML")
            sent = True
    except Exception:
        sent = False

    # Фолбек: якщо менеджерський чат не заданий/недоступний — розсилаємо адмінам
    if not sent:
        for adm in settings.admin_id_set:
            try:
                if lot.photos:
                    if len(lot.photos) == 1:
                        await bot.send_photo(adm, lot.photos[0].file_id, caption=base_caption, parse_mode="HTML")
                    else:
                        media = [InputMediaPhoto(media=p.file_id) for p in lot.photos]
                        media[0].caption = base_caption
                        media[0].parse_mode = "HTML"
                        await bot.send_media_group(adm, media)
                else:
                    await bot.send_message(adm, base_caption, parse_mode="HTML")
            except Exception:
                pass

    await msg.answer("Дякуємо! Дані отримано. Менеджер вже бачить вашу заявку.")
    await state.clear()
