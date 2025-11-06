# app/main.py
import asyncio
import hashlib
import logging
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from sqlalchemy import select, func

from app.settings import settings
from app.db import init_models, async_session
from app.handlers.admin import admin_router
from app.handlers.user import user_router
from app.services.cascade import advance_cascade
from app.models import Offer, OfferStatus, Lot
from app.services.monopay import verify_webhook_signature

# ── Логирование (включай MONOPAY_DEBUG=1 в .env, чтобы видеть отладку)
logging.basicConfig(level=logging.INFO)
dbg_logger = logging.getLogger("app.monopay")


def _dbg(msg: str, *args):
    """Лёгкий дебаг-логгер, который пишет только если MONOPAY_DEBUG включён."""
    try:
        if getattr(settings, "MONOPAY_DEBUG", False):
            dbg_logger.info("MONOPAY DEBUG: " + msg, *args)
    except Exception:
        pass


# ── Aiogram
bot = Bot(token=settings.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
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


# ── FastAPI
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


# ── MonoPay webhook
@app.post("/monopay/webhook", response_class=PlainTextResponse)
async def monopay_webhook(request: Request) -> str:
    # 0) тело и заголовки (для дебага)
    raw = await request.body()
    body_sha = hashlib.sha256(raw).hexdigest()
    x_sign = request.headers.get("X-Sign")
    ctype = request.headers.get("content-type")
    client_ip = getattr(getattr(request, "client", None), "host", "?")

    _dbg(
        "hit POST /monopay/webhook | ip=%s | has X-Sign=%s | ctype=%s | body_sha256=%s",
        client_ip, bool(x_sign), ctype, body_sha[:16],
    )

    # 1) проверка подписи
    if not x_sign or not await verify_webhook_signature(raw, x_sign):
        _dbg("reject 400: signature invalid (body_sha256=%s)", body_sha)
        raise HTTPException(400, "Bad signature")

    # 2) JSON
    try:
        data = await request.json()
    except Exception:
        _dbg("reject 400: bad json")
        raise HTTPException(400, "Bad JSON")

    status = data.get("status")        # created / processing / success / failure
    invoice_id = data.get("invoiceId")
    offer_id = int(request.query_params.get("offer_id", "0") or 0)

    # 3) промежуточные/неуспешные статусы просто подтверждаем 200 OK
    if status in ("created", "processing"):
        _dbg("noop: status is not success: %s", status)
        return "ok"
    if status != "success":
        _dbg("noop: status=%s (not success)", status)
        return "ok"

    # 4) success → отмечаем оплату, каскадно закрываем остальных при необходимости
    async with async_session() as session:
        off: Offer | None = await session.get(Offer, offer_id)
        if not off:
            _dbg("warn: offer %s not found", offer_id)
            return "ok"

        if off.invoice_id and off.invoice_id != invoice_id:
            _dbg(
                "warn: invoice mismatch offer=%s expected=%s got=%s",
                offer_id, off.invoice_id, invoice_id
            )
            return "ok"

        off.status = OfferStatus.PAID
        off.paid_at = datetime.utcnow()
        if not off.invoice_id:
            off.invoice_id = invoice_id
        await session.flush()

        lot: Lot = await session.get(Lot, off.lot_id)

        # если набрали нужное количество — закрыть активные предложения
        paid_cnt = (await session.execute(
            select(func.count())
            .select_from(Offer)
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

        await session.commit()
        _dbg("offer_id=%s marked as PAID; invoice_id=%s", offer_id, invoice_id)

    # 5) пушим пользователю форму контактов через deep-link
    try:
        me = await bot.get_me()
        link = f"https://t.me/{me.username}?start=contact_{offer_id}"
        await bot.send_message(
            off.user_tg_id,
            f"✅ Оплату за лот #{lot.public_id} зараховано!\n"
            f"Натисніть, щоб заповнити контактні дані: {link}"
        )
    except Exception as e:
        _dbg("warn: cannot push contact form: %r", e)

    return "ok"
