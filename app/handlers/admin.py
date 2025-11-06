# app/handlers/admin.py
from aiogram import Router, F, Bot
from aiogram.types import Message, ContentType, InputMediaPhoto
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from html import escape
import asyncio

from app.db import async_session
from app.models import Lot, LotPhoto, LotStatus, Bid
from app.settings import settings
from app.utils import update_channel_caption
from app.services.cascade import start_cascade

admin_router = Router()

# ─────────────── Спрощений конструктор лота (опис → ціна → кількість → фото → підтвердження)
class CreateItemSG(StatesGroup):
    DESC = State()
    PRICE = State()
    QTY = State()
    PHOTOS = State()
    CONFIRM = State()

@admin_router.message(F.text == "/admin")
async def admin_menu(msg: Message):
    if msg.from_user.id not in settings.admin_id_set:
        return
    txt = (
        "Адмін-меню:\n"
        "/createlot – створити ЛОТ (аукціон, крок 15 грн)\n"
        "/createsale – створити ЛОТ-РОЗПРОДАЖ (фіксована ціна)\n"
        "/publish 1 – публікація лота\n"
        "/allpub – публікація ВСІХ чернеток\n"
        "/finish 1 – завершити торги (каскад)\n"
        "/finall – завершити ВСІ активні (зі ставками → каскад; без ставок → Купити)\n"
        "/mylots – списки Чернетки/Активні/Завершені\n"
    )
    # без HTML, щоб не ловити помилок парсера в меню
    await msg.answer(txt, parse_mode=None)

# ─────────────── старт створення (аукціон/розпродаж)
@admin_router.message(F.text.startswith("/createlot"))
async def createlot_start(msg: Message, state: FSMContext):
    if msg.from_user.id not in settings.admin_id_set:
        return
    await state.clear()
    await state.set_state(CreateItemSG.DESC)
    await state.update_data(is_sale=False, min_step=settings.DEFAULT_MIN_STEP)  # аукціон
    await msg.answer("Введіть опис товару (одним повідомленням).")

@admin_router.message(F.text.startswith("/createsale"))
async def createsale_start(msg: Message, state: FSMContext):
    if msg.from_user.id not in settings.admin_id_set:
        return
    await state.clear()
    await state.set_state(CreateItemSG.DESC)
    await state.update_data(is_sale=True, min_step=0)    # розпродаж
    await msg.answer("Введіть опис товару для розпродажу (одним повідомленням).")

@admin_router.message(CreateItemSG.DESC)
async def s_desc(msg: Message, state: FSMContext):
    await state.update_data(desc=msg.text.strip())
    await state.set_state(CreateItemSG.PRICE)
    await msg.answer("Вкажіть ціну (ціле число, грн).")

@admin_router.message(CreateItemSG.PRICE, F.text.regexp(r"^\d+$"))
async def s_price(msg: Message, state: FSMContext):
    await state.update_data(price=int(msg.text))
    await state.set_state(CreateItemSG.QTY)
    await msg.answer("Кількість (ціле число ≥ 1).")

@admin_router.message(CreateItemSG.PRICE)
async def s_price_err(msg: Message):
    await msg.answer("Лише ціле число. Спробуйте ще раз.")

@admin_router.message(CreateItemSG.QTY, F.text.regexp(r"^\d+$"))
async def s_qty(msg: Message, state: FSMContext):
    await state.update_data(qty=int(msg.text))
    await state.set_state(CreateItemSG.PHOTOS)
    await msg.answer("Надішліть фото (одне або альбом). Коли закінчите — напишіть 'так' для підтвердження.")

@admin_router.message(CreateItemSG.QTY)
async def s_qty_err(msg: Message):
    await msg.answer("Лише ціле число. Спробуйте ще раз.")

# прийом фото / альбому
@admin_router.message(CreateItemSG.PHOTOS, F.content_type == ContentType.PHOTO)
async def s_photos(msg: Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get("photos", [])
    photos.append(msg.photo[-1].file_id)
    await state.update_data(photos=photos)

    if msg.media_group_id is None:
        await state.set_state(CreateItemSG.CONFIRM)
        await msg.answer("Підтверджуємо створення? (так/ні)")
    else:
        await msg.answer("Фото додано. Коли закінчите — напишіть 'так' для підтвердження.")

@admin_router.message(CreateItemSG.PHOTOS, F.text.casefold().in_({"так", "yes", "y", "+"}))
async def s_photos_confirm(msg: Message, state: FSMContext):
    # підтвердження прямо зі стану PHOTOS (зручно для альбомів)
    await _create_draft_from_state(msg, state)

@admin_router.message(CreateItemSG.PHOTOS)
async def s_photos_hint(msg: Message):
    await msg.answer("Надішліть фото (одне або альбом) або напишіть 'так' для підтвердження.")

# підтвердження (коли було одиночне фото)
@admin_router.message(CreateItemSG.CONFIRM, F.text.casefold().in_({"так", "yes", "y", "+"}))
async def s_confirm_yes(msg: Message, state: FSMContext):
    await _create_draft_from_state(msg, state)

@admin_router.message(CreateItemSG.CONFIRM)
async def s_confirm_no(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Скасовано.")

# створити одну чернетку з даних FSM
async def _create_draft_from_state(msg: Message, state: FSMContext):
    data = await state.get_data()
    desc: str = data["desc"]
    price: int = int(data["price"])
    qty: int = int(data["qty"])
    min_step: int = int(data["min_step"])   # 15 для аукціону, 0 для розпродажу
    photos: list[str] = data.get("photos", [])

    async with async_session() as session:
        last_pub = (await session.execute(select(func.max(Lot.public_id)))).scalar() or 0
        lot = Lot(
            public_id=last_pub + 1,
            title=desc,
            condition="",
            size="",
            start_price=price,
            min_step=min_step,
            quantity=qty,
            status=LotStatus.DRAFT,
            current_price=price,
            created_by=msg.from_user.id,
            channel_id=settings.CHANNEL_ID,
        )
        session.add(lot)
        await session.flush()
        for fid in photos:
            session.add(LotPhoto(lot_id=lot.id, file_id=fid))
        await session.commit()

    await state.clear()
    await msg.answer(f"✅ Чернетка збережена. Публічний ID: <b>#{lot.public_id}</b>", parse_mode="HTML")

# списки
@admin_router.message(F.text.startswith("/mylots"))
async def mylots(msg: Message):
    if msg.from_user.id not in settings.admin_id_set:
        return
    async with async_session() as session:
        drafts = (await session.execute(
            select(Lot).where(Lot.status == LotStatus.DRAFT).order_by(Lot.public_id)
        )).scalars().all()
        actives = (await session.execute(
            select(Lot).where(Lot.status == LotStatus.ACTIVE).order_by(Lot.public_id)
        )).scalars().all()
        fins = (await session.execute(
            select(Lot).where(Lot.status == LotStatus.FINISHED).order_by(Lot.public_id)
        )).scalars().all()

    def fmt(items, hint):
        if not items:
            return "—"
        return "\n".join([f"#{x.public_id} – {x.title[:60]} | {hint.format(x.public_id)}" for x in items])

    txt = (
        "Чернетки:\n" + fmt(drafts, "Опублікувати: /publish {}") +
        "\n\nАктивні:\n" + fmt(actives, "Завершити: /finish {}") +
        "\n\nЗавершені:\n" + ("\n".join([f"#{x.public_id} – {x.title[:60]}" for x in fins]) or "—")
    )
    await msg.answer(txt)

# ─────────────── публікація одного лота
async def _publish_lot(pub_id: int, bot: Bot) -> bool:
    async with async_session() as session:
        stmt = select(Lot).options(selectinload(Lot.photos)).where(Lot.public_id == pub_id)
        lot = (await session.execute(stmt)).scalar_one_or_none()
        if not lot or lot.status != LotStatus.DRAFT:
            return False

        me = await bot.get_me()
        is_sale = (lot.min_step == 0)
        link_text = "Купити" if is_sale else "БРОНЬ"
        deeplink = f"https://t.me/{me.username}?start={'sale' if is_sale else 'lot'}_{lot.public_id}"

        step_line = "" if is_sale else "Крок: 15 грн\n"
        safe_title = escape(lot.title)

        caption = (
            f"<b>{safe_title}</b>\n\n"
            f"{'Ціна' if is_sale else 'Поточна ціна'}: <b>{lot.current_price} грн</b>\n"
            f"{step_line}"
            f"<a href=\"{deeplink}\">{link_text}</a>\n\n"
            f"ID лота — #{lot.public_id}"
        )

        if lot.photos:
            if len(lot.photos) == 1:
                m = await bot.send_photo(settings.CHANNEL_ID, photo=lot.photos[0].file_id, caption=caption, parse_mode="HTML")
                lot.channel_message_id = m.message_id
            else:
                media = []
                for i, p in enumerate(lot.photos):
                    if i == 0:
                        media.append(InputMediaPhoto(media=p.file_id, caption=caption, parse_mode="HTML"))
                    else:
                        media.append(InputMediaPhoto(media=p.file_id))
                msgs = await bot.send_media_group(settings.CHANNEL_ID, media)
                lot.channel_message_id = msgs[0].message_id
        else:
            m = await bot.send_message(settings.CHANNEL_ID, caption, parse_mode="HTML")
            lot.channel_message_id = m.message_id

        lot.status = LotStatus.ACTIVE
        await session.commit()
        return True

@admin_router.message(F.text.startswith("/publish"))
async def publish(msg: Message, bot: Bot):
    if msg.from_user.id not in settings.admin_id_set:
        return
    parts = msg.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await msg.answer("Формат: /publish 123")
        return
    ok = await _publish_lot(int(parts[1]), bot)
    await msg.answer("Опубліковано" if ok else "Лот не знайдено або не в Чернетках")

# ─────────────── публікація всіх чернеток
@admin_router.message(F.text == "/allpub")
async def publish_all(msg: Message, bot: Bot):
    if msg.from_user.id not in settings.admin_id_set:
        return

    # беремо лише public_id — далі дочитуємо кожен лот в окремій транзакції
    async with async_session() as session:
        pub_ids = (await session.execute(
            select(Lot.public_id).where(Lot.status == LotStatus.DRAFT).order_by(Lot.public_id)
        )).scalars().all()

    if not pub_ids:
        await msg.answer("Чернеток немає.")
        return

    ok, fail = 0, 0
    for pid in pub_ids:
        try:
            if await _publish_lot(pid, bot):
                ok += 1
            else:
                fail += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.8)  # акуратніше з лімітами каналу

    await msg.answer(f"Готово: опубліковано {ok}, пропущено/помилок {fail}.")

# ─────────────── завершення: один
@admin_router.message(F.text.startswith("/finish "))
async def finish(msg: Message, bot: Bot):
    if msg.from_user.id not in settings.admin_id_set:
        return
    parts = msg.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await msg.answer("Формат: /finish 123")
        return
    pub_id = int(parts[1])
    async with async_session() as session:
        lot = (await session.execute(select(Lot).where(Lot.public_id == pub_id))).scalar_one_or_none()
        if not lot:
            await msg.answer("Лот не знайдено.")
            return
    await start_cascade(bot, lot.id)
    await msg.answer("Каскад запущено.")

# ─────────────── завершення: усі (зі ставками → каскад; без ставок → Купити)
@admin_router.message(F.text == "/finall")
async def finish_all(msg: Message, bot: Bot):
    if msg.from_user.id not in settings.admin_id_set:
        return

    async with async_session() as session:
        actives = (await session.execute(
            select(Lot).options(selectinload(Lot.photos)).where(Lot.status == LotStatus.ACTIVE).order_by(Lot.public_id)
        )).scalars().all()

    if not actives:
        await msg.answer("Активних немає.")
        return

    me = await bot.get_me()
    cascaded = 0
    converted = 0

    for lot in actives:
        # чи були ставки?
        async with async_session() as session:
            cnt = (await session.execute(
                select(func.count()).select_from(Bid).where(Bid.lot_id == lot.id)
            )).scalar_one()

        if cnt > 0 and lot.min_step > 0:
            await start_cascade(bot, lot.id)
            cascaded += 1
        else:
            # конвертуємо в «Купити»
            deeplink = f"https://t.me/{me.username}?start=sale_{lot.public_id}"
            safe_title = escape(lot.title)
            caption = (
                f"<b>{safe_title}</b>\n\n"
                f"Ціна: <b>{lot.current_price} грн</b>\n"
                f"<a href=\"{deeplink}\">Купити</a>\n\n"
                f"ID лота — #{lot.public_id}"
            )
            if lot.channel_message_id:
                await update_channel_caption(bot, settings.CHANNEL_ID, lot.channel_message_id, caption)
            # перевести лот у «SALE mode» (крок 0)
            async with async_session() as session:
                db_lot = await session.get(Lot, lot.id)
                if db_lot:
                    db_lot.min_step = 0
                    await session.commit()
            converted += 1

        await asyncio.sleep(0.4)

    await msg.answer(f"Готово. Каскад: {cascaded}. Конвертовано у «Купити»: {converted}.")
