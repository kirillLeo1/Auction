from aiogram import Router, F
from aiogram.types import Message, ContentType
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from sqlalchemy import select, func
from app.db import async_session
from app.models import Lot, LotPhoto, LotStatus
from app.settings import settings

admin_router = Router()

class CreateLotSG(StatesGroup):
    TITLE = State()
    COND = State()
    SIZE = State()
    START = State()
    STEP = State()
    QTY = State()
    PHOTOS = State()
    CONFIRM = State()

@admin_router.message(F.text == "/admin")
async def admin_menu(msg: Message):
    if msg.from_user.id not in settings.admin_id_set:
        return
    txt = (
        "Адмін-меню:\n"
        "/createlot – створити лот\n"
        "/publish <id> – публікація лота в канал\n"
        "/finish <id> – завершити торги (каскад)\n"
        "/mylots – списки Чернетки/Активні/Завершені\n"
    )
    await msg.answer(txt)

@admin_router.message(F.text.startswith("/createlot"))
async def createlot_start(msg: Message, state: FSMContext):
    if msg.from_user.id not in settings.admin_id_set:
        return
    await state.set_state(CreateLotSG.TITLE)
    await msg.answer("Введіть назву лота")

@admin_router.message(CreateLotSG.TITLE)
async def s_title(msg: Message, state: FSMContext):
    await state.update_data(title=msg.text.strip())
    await state.set_state(CreateLotSG.COND)
    await msg.answer("Стан (коротко)")

@admin_router.message(CreateLotSG.COND)
async def s_cond(msg: Message, state: FSMContext):
    await state.update_data(cond=msg.text.strip())
    await state.set_state(CreateLotSG.SIZE)
    await msg.answer("Розмір/комплектація")

@admin_router.message(CreateLotSG.SIZE)
async def s_size(msg: Message, state: FSMContext):
    await state.update_data(size=msg.text.strip())
    await state.set_state(CreateLotSG.START)
    await msg.answer("Стартова ціна (ціле число)")

@admin_router.message(CreateLotSG.START, F.text.regexp(r"^\d+$"))
async def s_start(msg: Message, state: FSMContext):
    await state.update_data(start=int(msg.text))
    await state.set_state(CreateLotSG.STEP)
    await msg.answer("Мінімальний крок (ціле число)")

@admin_router.message(CreateLotSG.START)
async def s_start_err(msg: Message):
    await msg.answer("Тільки ціле число. Спробуйте ще раз.")

@admin_router.message(CreateLotSG.STEP, F.text.regexp(r"^\d+$"))
async def s_step(msg: Message, state: FSMContext):
    await state.update_data(step=int(msg.text))
    await state.set_state(CreateLotSG.QTY)
    await msg.answer("Кількість (ціле число)")

@admin_router.message(CreateLotSG.STEP)
async def s_step_err(msg: Message):
    await msg.answer("Тільки ціле число. Спробуйте ще раз.")

@admin_router.message(CreateLotSG.QTY, F.text.regexp(r"^\d+$"))
async def s_qty(msg: Message, state: FSMContext):
    await state.update_data(qty=int(msg.text))
    await state.set_state(CreateLotSG.PHOTOS)
    await msg.answer("Фото: надішліть одне фото або альбом (медіагрупу)")

@admin_router.message(CreateLotSG.QTY)
async def s_qty_err(msg: Message):
    await msg.answer("Тільки ціле число. Спробуйте ще раз.")

# Collect one photo or whole media group
@admin_router.message(CreateLotSG.PHOTOS, F.content_type == ContentType.PHOTO)
async def s_photos(msg: Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get("photos", [])
    photos.append(msg.photo[-1].file_id)
    await state.update_data(photos=photos)
    if msg.media_group_id is None:
        await state.set_state(CreateLotSG.CONFIRM)
        await msg.answer("Підтверджуємо створення? (так/ні)")
    else:
        await msg.answer("Фото додано. Коли закінчите додавати – напишіть 'так' для підтвердження.")

@admin_router.message(CreateLotSG.PHOTOS)
async def s_photos_err(msg: Message):
    await msg.answer("Надішліть фото (одне або альбом). Або напишіть 'так' для підтвердження після завантаження.")
async def s_photos_err(msg: Message):
    await msg.answer("Надішліть фото (одне або альбом).")

@admin_router.message(CreateLotSG.CONFIRM, F.text.casefold().in_({"так", "yes", "y", "+"}))
async def s_confirm_yes(msg: Message, state: FSMContext):
    data = await state.get_data()
    async with async_session() as session:
        last_pub = (await session.execute(select(func.max(Lot.public_id)))).scalar() or 0
        lot = Lot(
            public_id=last_pub + 1,
            title=data["title"],
            condition=data["cond"],
            size=data["size"],
            start_price=data["start"],
            min_step=data["step"],
            quantity=data["qty"],
            status=LotStatus.DRAFT,
            current_price=data["start"],
            created_by=msg.from_user.id,
            channel_id=settings.CHANNEL_ID,
        )
        session.add(lot)
        await session.flush()
        for fid in data.get("photos", []):
            session.add(LotPhoto(lot_id=lot.id, file_id=fid))
        await session.commit()
    await state.clear()
    await msg.answer(f"Чернетка створена: публічний ID <b>#{lot.public_id}</b>", parse_mode="HTML")

@admin_router.message(CreateLotSG.CONFIRM)
async def s_confirm_no(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Скасовано.")

@admin_router.message(F.text.startswith("/mylots"))
async def mylots(msg: Message):
    if msg.from_user.id not in settings.admin_id_set:
        return
    async with async_session() as session:
        drafts = (await session.execute(select(Lot).where(Lot.status==LotStatus.DRAFT).order_by(Lot.public_id))).scalars().all()
        actives = (await session.execute(select(Lot).where(Lot.status==LotStatus.ACTIVE).order_by(Lot.public_id))).scalars().all()
        fins = (await session.execute(select(Lot).where(Lot.status==LotStatus.FINISHED).order_by(Lot.public_id))).scalars().all()
    def fmt(items, hint):
        if not items: return "—"
        return "\n".join([f"#{x.public_id} – {x.title} | {hint.format(x.public_id)}" for x in items])
    txt = (
        "Чернетки:\n" + fmt(drafts, "Опублікувати: /publish {}") +
        "\n\nАктивні:\n" + fmt(actives, "Завершити: /finish {}") +
        "\n\nЗавершені:\n" + ("\n".join([f"#{x.public_id} – {x.title}" for x in fins]) or "—")
    )
    await msg.answer(txt)

@admin_router.message(F.text.startswith("/publish"))
async def publish(msg: Message, bot):
    if msg.from_user.id not in settings.admin_id_set:
        return
    parts = msg.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await msg.answer("Формат: /publish 123")
        return
    pub_id = int(parts[1])
    async with async_session() as session:
        lot = (await session.execute(select(Lot).where(Lot.public_id == pub_id))).scalar_one_or_none()
        if not lot:
            await msg.answer("Лот не знайдено")
            return
        if lot.status != LotStatus.DRAFT:
            await msg.answer("Лот не в статусі Чернетка")
            return
        me = await bot.get_me()
        deeplink = f"https://t.me/{me.username}?start=lot_{lot.public_id}"
        caption = (
            f"<b>{lot.title}</b>\n"
            f"Стан: {lot.condition}\nРозмір: {lot.size}\n\n"
            f"Стартова ціна: {lot.start_price} грн\nМін. крок: {lot.min_step} грн\n"
            f"Поточна ціна: <b>{lot.current_price} грн</b>\n\n"
            f"<a href=\"{deeplink}\">ЗРОБИТИ СТАВКУ</a>\n\nID лота — #{lot.public_id}"
        )
        # send to channel
        if lot.photos:
            if len(lot.photos) == 1:
                m = await bot.send_photo(settings.CHANNEL_ID, photo=lot.photos[0].file_id, caption=caption, parse_mode="HTML")
                lot.channel_message_id = m.message_id
            else:
                media = []
                from aiogram.types import InputMediaPhoto
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
    await msg.answer("Опубліковано")

from app.services.cascade import start_cascade

@admin_router.message(F.text.startswith("/finish"))
async def finish(msg: Message, bot):
    if msg.from_user.id not in settings.admin_id_set:
        return
    parts = msg.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await msg.answer("Формат: /finish 123")
        return
    pub_id = int(parts[1])
    async with async_session() as session:
        lot = (await session.execute(select(Lot).where(Lot.public_id==pub_id))).scalar_one_or_none()
        if not lot:
            await msg.answer("Лот не знайдено")
            return
    await start_cascade(bot, lot.id)
    await msg.answer("Каскад запущено.")