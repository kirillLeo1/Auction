# app/main.py
import asyncio
import logging
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse

from aiogram import Bot, Dispatcher

from app.settings import settings
from app.db import init_models, async_session
from app.handlers.admin import admin_router
from app.handlers.user import user_router
from app.services.cascade import advance_cascade
from app.models import Offer, OfferStatus, Lot
from app.services.monopay import verify_webhook_signature

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ───────────────────────────────────────────────────────────────────────
# Aiogram bot + routers
bot = Bot(token=settings.BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()
dp.include_router(admin_router)
dp.include_router(user_router)


async def _polling_task():
    """Run aiogram polling in background."""
    await dp.start_polling(bot)


async def _scheduler_task():
    """Periodic cascade advancement (HOLD дедлайни, відкриття наступних претендентів)."""
    while True:
        try:
            await advance_cascade(bot)
        except Exception as e:
            logger.exception("advance_cascade error: %s", e)
        await asyncio.sleep(30)


# ───────────────────────────────────────────────────────────────────────
# FastAPI app with shared lifespan (runs polling + scheduler)
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_models()
    t_poll = asyncio.create_task(_polling_task())
    t_sched = asyncio.create_task(_scheduler_task())
    try:
        yield
    finally:
        for t in (t_poll, t_sched):
            t.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(t_poll, t_sched)


app = FastAPI(
    title="AuctionBot",
    default_response_class=JSONResponse,
    lifespan=lifespan,
)


# ───────────────────────────────────────────────────────────────────────
# Healthcheck
@app.get("/health")
async def health():
    return {"ok": True}


# ───────────────────────────────────────────────────────────────────────
# MonoPay webhook: verify signature, mark PAID, handle sold-out, prompt contact form
@app.post("/monopay/webhook", response_class=PlainTextResponse)
async def monopay_webhook(request: Request):
    raw = await request.body()
    x_sign = request.headers.get("X-Sign")
    if not x_sign or not await verify_webhook_signature(raw, x_sign):
        raise HTTPException(status_code=400, detail="Bad signature")

    # offer_id із query (?offer_id=)
    offer_id_str = request.query_params.get("offer_id")
    try:
        offer_id = int(offer_id_str or "0")
    except ValueError:
        offer_id = 0

    data = await request.json()
    status = data.get("status")       # created | processing | success | failure
    invoice_id = data.get("invoiceId")

    from sqlalchemy import select, func

    async with async_session() as session:
        off = await session.get(Offer, offer_id)
        if not off:
            return "ok"

        # Якщо в офері вже є invoice_id — перевіримо відповідність (захист від плутанини)
        if off.invoice_id and invoice_id and off.invoice_id != invoice_id:
            return "ok"

        if status == "success" and off.status != OfferStatus.PAID:
            # позначаємо як оплачено
            from datetime import datetime

            off.status = OfferStatus.PAID
            off.paid_at = datetime.utcnow()

            lot = await session.get(Lot, off.lot_id)

            # Якщо вже викуплено потрібну кількість — скасовуємо інші активні офери
            paid_cnt = (
                await session.execute(
                    select(func.count()).select_from(Offer).where(
                        Offer.lot_id == lot.id,
                        Offer.status == OfferStatus.PAID,
                    )
                )
            ).scalar_one()

            if paid_cnt >= lot.quantity:
                active_others = (
                    await session.execute(
                        select(Offer).where(
                            Offer.lot_id == lot.id,
                            Offer.status.in_([OfferStatus.OFFERED, OfferStatus.POSTPONED]),
                        )
                    )
                ).scalars().all()
                for o in active_others:
                    o.status = OfferStatus.CANCELED

            # Надсилаємо переможцю лінк на старт контакт-форми
            try:
                me = await bot.get_me()
                link = f"https://t.me/{me.username}?start=contact_{off.id}"
                await bot.send_message(
                    off.user_tg_id,
                    f"✅ Оплату за лот #{lot.public_id} зараховано!\n"
                    f"Натисніть, щоб заповнити контактні дані: {link}"
                )
            except Exception as e:
                logger.warning("Failed to DM user for contact form: %s", e)

        await session.commit()

    return "ok"
