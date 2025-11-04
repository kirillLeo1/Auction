import asyncio
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update

from app.settings import settings
from app.db import init_models, async_session
from app.handlers.admin import admin_router
from app.handlers.user import user_router
from app.services.cascade import advance_cascade
from app.models import Offer, OfferStatus, Lot
from sqlalchemy import select, func
from app.services.monopay import verify_webhook_signature

# Aiogram v3: задаємо parse_mode через DefaultBotProperties
bot = Bot(
    token=settings.BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
dp.include_router(admin_router)
dp.include_router(user_router)


async def _polling_task():
    await dp.start_polling(bot)


async def _scheduler_task():
    # періодично перевіряємо дедлайни/каскади
    while True:
        try:
            await advance_cascade(bot)
        except Exception:
            # не валимо сервіс, якщо десь впало
            pass
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_models()
    # на всякий — прибираємо вебхук, щоб не було конфлікту з getUpdates
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

    t_poll = asyncio.create_task(_polling_task())
    t_sched = asyncio.create_task(_scheduler_task())
    try:
        yield
    finally:
        for t in (t_poll, t_sched):
            t.cancel()

app = FastAPI(title="AuctionBot", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"ok": True}


# MonoPay webhook: перевірка підпису, виставлення PAID, автоперехід до контактів
@app.post("/monopay/webhook", response_class=PlainTextResponse)
async def monopay_webhook(request: Request):
    raw = await request.body()
    x_sign = request.headers.get("X-Sign")
    if not x_sign or not await verify_webhook_signature(raw, x_sign):
        raise HTTPException(400, "Bad signature")

    offer_id = int(request.query_params.get("offer_id", "0") or "0")
    data = await request.json()
    status = data.get("status")           # created/processing/success/failure
    invoice_id = data.get("invoiceId")

    async with async_session() as session:
        off = await session.get(Offer, offer_id)
        if not off:
            return "ok"
        # захист від підміни чужого інвойсу
        if off.invoice_id and invoice_id and off.invoice_id != invoice_id:
            return "ok"

        if status == "success":
            off.status = OfferStatus.PAID
            off.paid_at = datetime.utcnow()
            lot = await session.get(Lot, off.lot_id)

            # якщо все викупили — знімаємо пост і закриваємо інші офери
            paid_cnt = (
                await session.execute(
                    select(func.count()).select_from(Offer).where(
                        Offer.lot_id == lot.id,
                        Offer.status == OfferStatus.PAID
                    )
                )
            ).scalar_one()

            if paid_cnt >= lot.quantity:
                # прибираємо пост з каналу (якщо є)
                if lot.channel_id and lot.channel_message_id:
                    try:
                        await bot.delete_message(lot.channel_id, lot.channel_message_id)
                    except Exception:
                        pass
                # інші активні заявки інвалідимо
                others = (
                    await session.execute(
                        select(Offer).where(
                            Offer.lot_id == lot.id,
                            Offer.status.in_([OfferStatus.OFFERED, OfferStatus.POSTPONED])
                        )
                    )
                ).scalars().all()
                for o in others:
                    o.status = OfferStatus.CANCELED

            # просимо користувача заповнити контакти (deep-link у /start)
            try:
                me = await bot.get_me()
                link = f"https://t.me/{me.username}?start=contact_{off.id}"
                await bot.send_message(
                    off.user_tg_id,
                    f"✅ Оплату за лот #{lot.public_id} зараховано!\n"
                    f"Заповніть контактні дані: {link}"
                )
            except Exception:
                pass

        await session.commit()

    return "ok"
