from __future__ import annotations

import re
from datetime import datetime, timedelta

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
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
    ONE = State()  # "ПІБ / місто / відділення НП/УП або адреса / телефон"


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

    # після успішної оплати: запит одноразової контакт-форми
    if arg.startswith("contact_"):
        offer_id = int(arg.split("_", 1)[1])
        await state.update_data(offer_id=offer_id)
        await state.set_state(ContactOneSG.ONE)
        await msg.answer(
            "Введіть ваші дані одним повідомленням:\n"
            "<i>ПІБ / місто / відділення НП/УП або адреса / телефон</i>\n\n"
            "Наприклад: Іван Іванов / Київ / НП відділення 45 / +380991112233",
            parse_mode="HTML",
        )
        return

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
        hold_text = offer.hold_until.strftime("%d.%m %H:%M") if offer.hold_until else "—"
        await msg.answer(
            f"Лот #{lot.public_id}: {lot.title}\n\n"
            f"Ціна до оплати: <b>{offer.offered_price} грн</b>\n"
            f"Тримаємо за вами до <b>{hold_text}</b> (Київ).",
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
        await session.commit()

    await state.update_data(offer_id=offer_id)
    await state.set_state(ContactOneSG.ONE)
    await call.message.answer(
        "Введіть ваші дані одним повідомленням:\n"
        "<i>ПІБ / місто / відділення НП/УП або адреса / телефон</i>",
        parse_mode="HTML",
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

    # розбиваємо по "/" або "|"
    parts = [p.strip() for p in re.split(r"[|/]", msg.text) if p.strip()]
    if len(parts) < 4:
        await msg.answer(
            "Будь ласка, відправте у форматі: "
            "<code>ПІБ / місто / відділення НП або адреса / телефон</code>",
            parse_mode="HTML",
        )
        return

    fullname, city, address, phone = parts[0], parts[1], parts[2], parts[3]

    async with async_session() as session:
        off = await session.get(Offer, offer_id)
        if not off:
            await msg.answer("Посилання застаріло. Спробуйте ще раз /start")
            await state.clear()
            return
        off.contact_fullname = fullname
        off.contact_city_region = city
        off.contact_address = address
        off.contact_phone = phone
        off.contact_delivery = "НП/УП або адресна"
        await session.flush()
        lot = await session.get(Lot, off.lot_id)
        await session.commit()

    # лише менеджерський чат
    if settings.MANAGER_CHAT_ID:
        tag = "Оплачено" if off.status == OfferStatus.PAID else "Відкладено"
        user_link = (
            f"@{msg.from_user.username}"
            if msg.from_user.username
            else str(msg.from_user.id)
        )
        txt = (
            f"{tag} | Лот #{lot.public_id} | {user_link} | {off.offered_price} грн\n"
            f"ПІБ: {fullname}\nМісто: {city}\nАдреса/Відділення: {address}\nТелефон: {phone}"
        )
        try:
            await bot.send_message(settings.MANAGER_CHAT_ID, txt)
        except Exception:
            pass

    await msg.answer("Дякуємо! Замовлення передано менеджеру.")
    await state.clear()
