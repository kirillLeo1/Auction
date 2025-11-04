import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from sqlalchemy import select, func as _func

from app.settings import settings
from app.db import init_models, async_session
from app.handlers.admin import admin_router
from app.handlers.user import user_router
from app.services.cascade import advance_cascade
from app.models import Offer, OfferStatus, Lot
from app.services.monopay import verify_webhook_signature

bot = Bot(token=settings.BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()
dp.include_router(admin_router)
dp.include_router(user_router)

async def _polling_task():
    await dp.start_polling(bot)

async def _scheduler_task():
    while True:
        try:
            await advance_cascade(bot)
        except Exception:
            pass
        await asyncio.sleep(30)

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_models()
    t1 = asyncio.create_task(_polling_task())
    t2 = asyncio.create_task(_scheduler_task())
    try:
        yield
    finally:
        for t in (t1, t2):
            t.cancel()

app = FastAPI(title="AuctionBot", lifespan=lifespan)

@app.get("/health")
async def health():
    return {"ok": True}

# ─────────────────────────────────────────────────────────
# Єдина реалізація логіки вебхука (щоб викликати з двох шляхів)
async def _handle_monopay_webhook(request: Request) -> str:
    raw = await request.body()
    x_sign = request.headers.get("X-Sign")
    if not x_sign or not await verify_webhook_signature(raw, x_sign):
        raise HTTPException(400, "Bad signature")

    offer_id = int(request.query_params.get("offer_id", "0"))
    data = await request.json()
    status = data.get("status")        # created / processing / success / failure
    invoice_id = data.get("invoiceId") # без цього інколи не приходить — але у нас перевірка є

    async with async_session() as session:
        off = await session.get(Offer, offer_id)
        if not off:
            return "ok"  # нічого не знаємо про цей offer_id

        # якщо Mono відправила вебхук не по тій інвойс-id – ігноруємо, але не падаємо
        if off.invoice_id and invoice_id and off.invoice_id != invoice_id:
            return "ok"

        if status == "success":
            off.status = OfferStatus.PAID
            from datetime import datetime
            off.paid_at = datetime.utcnow()
            await session.flush()

            lot = await session.get(Lot, off.lot_id)

            # якщо все продано — прибираємо решту «бронь» і видаляємо пост з каналу
            paid_cnt = (await session.execute(
                select(_func.count()).select_from(Offer)
                .where(Offer.lot_id == lot.id, Offer.status == OfferStatus.PAID)
            )).scalar_one()

            if paid_cnt >= lot.quantity:
                others = (await session.execute(
                    select(Offer).where(
                        Offer.lot_id == lot.id,
                        Offer.status.in_([OfferStatus.OFFERED, OfferStatus.POSTPONED])
                    )
                )).scalars().all()
                for o in others:
                    o.status = OfferStatus.CANCELED

                # видаляємо пост з каналу (і для аукціону, і для розпродажу)
                if lot.channel_id and lot.channel_message_id:
                    try:
                        await bot.delete_message(lot.channel_id, lot.channel_message_id)
                    except Exception:
                        pass

            # даємо лінк на форму контактів
            try:
                me = await bot.get_me()
                link = f"https://t.me/{me.username}?start=contact_{off.id}"
                await bot.send_message(
                    off.user_tg_id,
                    f"✅ Оплату за лот #{lot.public_id} зараховано!\n"
                    f"Натисніть, щоб заповнити контактні дані: {link}"
                )
            except Exception:
                pass

        # інші статуси теж «ok», щоб Mono не ретраїла безкінечно
        await session.commit()
    return "ok"

# Правильний шлях (як у create_invoice)
@app.post("/monopay/webhook", response_class=PlainTextResponse)
async def monopay_webhook(request: Request):
    return await _handle_monopay_webhook(request)

@app.post("/telegram/webhook/monopay/webhook", response_class=PlainTextResponse)
async def monopay_webhook_alt(request: Request):
    return await _handle_monopay_webhook(request)

