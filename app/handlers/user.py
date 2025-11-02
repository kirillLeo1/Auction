# app/handlers/user.py
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from sqlalchemy import select, func, desc

from app.db import async_session
from app.models import Lot, LotStatus, Bid, Offer, OfferStatus
from app.utils import update_channel_caption

user_router = Router()


# ───────────────────────────────────────────────────────────────────────
# FSM
class BidSG(StatesGroup):
    ENTER_SUM = State()


class ContactSG(StatesGroup):
    FULLNAME = State()
    PHONE = State()
    CITYREG = State()
    DELIVERY = State()
    ADDRESS = State()
    COMMENT = State()


# ───────────────────────────────────────────────────────────────────────
# /start: deep-link сценарії
@user_router.message(F.text.startswith("/start"))
async def start(msg: Message, state: FSMContext):
    """
    Підтримує:
    - /start lot_<public_id>
    - /start <public_id>
    - /start contact_<offer_id>  -> початок форми контактних даних
    - /start                     -> підказка
    """
    args = msg.text.split(maxsplit=1)

    # deep-link "contact_<offer_id>" (після успішної оплати)
    if len(args) == 2 and args[1].startswith("contact_"):
        try:
            offer_id = int(args[1].split("_", 1)[1])
        except ValueError:
            await msg.answer("Посилання некоректне. Спробуйте ще раз /start.")
            return
        await state.update_data(offer_id=offer_id)
        await state.set_state(ContactSG.FULLNAME)
        await msg.answer("Оплата зарахована. Будь ласка, заповніть контактні дані.\n\nПІБ:")
        return

    # deep-link "lot_<public_id>" або просто число
    pub_id = None
    if len(args) == 2:
        if args[1].startswith("lot_"):
            try:
                pub_id = int(args[1].split("_", 1)[1])
            except ValueError:
                pub_id = None
        elif args[1].isdigit():
            pub_id = int(args[1])

    if pub_id is not None:
        async with async_session() as session:
            lot = (
                await session.execute(
                    select(Lot).where(Lot.public_id == pub_id)
                )
            ).scalar_one_or_none()

        if not lot or lot.status != LotStatus.ACTIVE:
            await msg.answer("Лот не знайдено або не активний.")
            return

        await state.update_data(lot_pub_id=pub_id)
        await state.set_state(BidSG.ENTER_SUM)
        await msg.answer(
            f"Лот #{lot.public_id}: {lot.title}\n"
            f"Поточна ціна: {lot.current_price} грн\n"
            f"Мін. крок: {lot.min_step} грн\n\n"
            f"Введіть суму вашої ставки (ціле число):"
        )
        return

    # дефолтна підказка
    await msg.answer(
        "Введіть ID поста (наприклад, 1) або перейдіть за посиланням у пості, щоб зробити ставку."
    )


# ───────────────────────────────────────────────────────────────────────
# Прийом ставки
@user_router.message(BidSG.ENTER_SUM, F.text.regexp(r"^\d+$"))
async def place_bid(msg: Message, state: FSMContext, bot: Bot):
    amount = int(msg.text)
    data = await state.get_data()
    pub_id = int(data["lot_pub_id"])

    prev_leader_id = None
    lot_vals = None  # (title, condition, size, start, step, current, public_id, channel_id, channel_msg_id)

    async with async_session() as session:
        lot = (
            await session.execute(
                select(Lot).where(Lot.public_id == pub_id)
            )
        ).scalar_one()

        min_allowed = lot.current_price + lot.min_step
        if amount < min_allowed:
            await msg.answer(f"Мінімальна ставка: {min_allowed} грн. Спробуйте ще раз.")
            return

        # Записуємо ставку
        session.add(
            Bid(
                lot_id=lot.id,
                user_tg_id=msg.from_user.id,
                username=msg.from_user.username,
                amount=amount,
            )
        )

        # Оновлюємо поточну ціну та лідера
        lot.current_price = amount
        prev_leader_id = lot.current_winner_tg_id

        stmt = (
            select(Bid.user_tg_id, func.max(Bid.amount).label("mx"))
            .where(Bid.lot_id == lot.id)
            .group_by(Bid.user_tg_id)
            .order_by(desc("mx"))
            .limit(1)
        )
        res = (await session.execute(stmt)).first()
        new_leader = res[0] if res else msg.from_user.id
        lot.current_winner_tg_id = new_leader

        lot_vals = (
            lot.title,
            lot.condition,
            lot.size,
            lot.start_price,
            lot.min_step,
            lot.current_price,
            lot.public_id,
            lot.channel_id,
            lot.channel_message_id,
        )

        await session.commit()

    await msg.answer("✅ Ставку прийнято! Ви в грі.")

    # Оновлюємо підпис/текст поста в каналі
    me = await bot.get_me()
    caption = (
        f"<b>{lot_vals[0]}</b>\n"
        f"Стан: {lot_vals[1]}\n"
        f"Розмір: {lot_vals[2]}\n\n"
        f"Стартова ціна: {lot_vals[3]} грн\n"
        f"Мін. крок: {lot_vals[4]} грн\n"
        f"Поточна ціна: <b>{lot_vals[5]} грн</b>\n\n"
        f"<a href=\"https://t.me/{me.username}?start=lot_{lot_vals[6]}\">ЗРОБИТИ СТАВКУ</a>\n\n"
        f"ID лота — #{lot_vals[6]}"
    )
    if lot_vals[8]:
        await update_channel_caption(bot, lot_vals[7], lot_vals[8], caption)

    # Сповіщення попереднього лідера
    if prev_leader_id and prev_leader_id != msg.from_user.id:
        try:
            await bot.send_message(
                prev_leader_id,
                (
                    f"⚠️ Вас перебили у #{lot_vals[6]}. Нова ціна {lot_vals[5]} грн.\n"
                    f"[ЗРОБИТИ СТАВКУ](https://t.me/{me.username}?start=lot_{lot_vals[6]})"
                ),
                parse_mode="Markdown",
            )
        except Exception:
            pass

    await state.clear()


@user_router.message(BidSG.ENTER_SUM)
async def bid_err(msg: Message):
    await msg.answer("Тільки ціле число. Спробуйте ще раз.")


# ───────────────────────────────────────────────────────────────────────
# Кнопки каскаду: ВІДКЛАСТИ / ВІДМОВИТИСЬ
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

    await call.message.answer("Окей, відкладаємо. Потрібні контактні дані.")
    await state.update_data(offer_id=offer_id)
    await state.set_state(ContactSG.FULLNAME)
    await call.message.answer("ПІБ:")
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
    await call.message.answer("Ви відмовились. Передаємо право попередньому претенденту…")
    await call.answer()


# ───────────────────────────────────────────────────────────────────────
# Форма контактних даних (після оплати або «Відкласти»)
@user_router.message(ContactSG.FULLNAME)
async def c_full(msg: Message, state: FSMContext):
    await state.update_data(fullname=msg.text.strip())
    await state.set_state(ContactSG.PHONE)
    await msg.answer("Телефон:")


@user_router.message(ContactSG.PHONE)
async def c_phone(msg: Message, state: FSMContext):
    await state.update_data(phone=msg.text.strip())
    await state.set_state(ContactSG.CITYREG)
    await msg.answer("Місто/область:")


@user_router.message(ContactSG.CITYREG)
async def c_city(msg: Message, state: FSMContext):
    await state.update_data(city=msg.text.strip())
    await state.set_state(ContactSG.DELIVERY)
    await msg.answer("Доставка: Нова Пошта (відділення/поштомат) або Адресна. Введіть тип:")


@user_router.message(ContactSG.DELIVERY)
async def c_delivery(msg: Message, state: FSMContext):
    await state.update_data(delivery=msg.text.strip())
    await state.set_state(ContactSG.ADDRESS)
    await msg.answer("Адреса / номер відділення / поштомат:")


@user_router.message(ContactSG.ADDRESS)
async def c_address(msg: Message, state: FSMContext):
    await state.update_data(address=msg.text.strip())
    await state.set_state(ContactSG.COMMENT)
    await msg.answer("Коментар (можна пропустити '-'):")


@user_router.message(ContactSG.COMMENT)
async def c_comment(msg: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    offer_id = int(data.get("offer_id"))

    async with async_session() as session:
        off = await session.get(Offer, offer_id)
        if not off:
            await msg.answer("Щось пішло не так. Спробуйте ще раз /start")
            await state.clear()
            return

        off.contact_fullname = data["fullname"]
        off.contact_phone = data["phone"]
        off.contact_city_region = data["city"]
        off.contact_delivery = data["delivery"]
        off.contact_address = data["address"]
        off.contact_comment = msg.text if msg.text != "-" else None
        await session.commit()

        lot = await session.get(Lot, off.lot_id)

    tag = "Оплачено" if off.status == OfferStatus.PAID else "Відкладено"
    username_or_id = msg.from_user.username or str(msg.from_user.id)
    adm_txt = (
        f"{tag} | Лот #{lot.public_id} | Юзер @{username_or_id} | Сума {off.offered_price}\n"
        f"ПІБ: {data['fullname']} | Телефон: {data['phone']}\n"
        f"Місто/обл: {data['city']} | Доставка: {data['delivery']} | Адреса: {data['address']}\n"
        f"Коментар: {off.contact_comment or '-'}"
    )

    # Розсилка адмінам
    # Використовуй settings.ADMIN_IDS -> settings.admin_id_set у settings.py
    from app.settings import settings  # імпортимо тут, щоб уникнути циклів при імпорті
    for adm in settings.admin_id_set:
        try:
            await bot.send_message(adm, adm_txt)
        except Exception:
            pass

    await msg.answer("Дякуємо! Дані отримано.")
    await state.clear()
