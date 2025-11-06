import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.base import StorageKey
from sqlalchemy import select
from sqlalchemy import func as _func

from app.settings import settings
from app.db import init_models, async_session
from app.handlers.admin import admin_router
from app.handlers.user import user_router
from app.handlers.user import ContactOneSG   # <- одношагова форма
from app.services.cascade import advance_cascade
from app.models import Offer, OfferStatus, Lot
from app.services.monopay import verify_webhook_signature, _dbg
import logging, hashlib
from datetime import datetime
from fastapi import APIRouter
from app import main as __main  # той самий файл, щоб FastAPI бачив app

dbg_logger = logging.getLogger("app.monopay")
logging.basicConfig(level=logging.INFO)

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

def _dbg(msg: str, *args):
    try:
        from app.settings import settings
        if getattr(settings, "MONOPAY_DEBUG", False):
            dbg_logger.info("MONOPAY DEBUG: " + msg, *args)
    except Exception:
        pass


@app.post("/monopay/webhook", response_class=PlainTextResponse)
async def monopay_webhook(request: Request):
    # 0) тіло + заголовки (для дебагу)
    raw = await request.body()
    body_sha = hashlib.sha256(raw).hexdigest()
    x_sign = request.headers.get("X-Sign")
    ctype = request.headers.get("content-type")

    client_ip = getattr(getattr(request, "client", None), "host", "?")
    _dbg("hit POST /monopay/webhook | ip=%s | has X-Sign=True X-Signature=%s | ctype=%s | body_sha256=%s",
         client_ip, "True" if x_sign else "False", ctype, body_sha[:16])

    # 1) перевірка підпису
    if not x_sign or not await verify_webhook_signature(raw, x_sign):
        raise HTTPException(400, "Bad signature")

    # 2) JSON
    try:
        data = await request.json()
    except Exception:
        _dbg("reject 400: bad json")
        raise HTTPException(400, "Bad JSON")

    status    = data.get("status")           # created / processing / success / failure
    invoice_id = data.get("invoiceId")
    offer_id   = int(request.query_params.get("offer_id", "0") or 0)

    # 3) Неуспішні/проміжні стани просто ігноруємо (200 ОК)
    if status in ("created", "processing"):
        _dbg("noop: status is not success: %s", status)
        return "ok"
    if status != "success":
        _dbg("noop: status=%s (not success)", status)
        return "ok"

    # 4) success → відмічаємо оплату, відміняємо інших якщо треба, шлемо DM
    async with async_session() as session:
        off: Offer | None = await session.get(Offer, offer_id)
        if not off:
            _dbg("warn: offer %s not found", offer_id)
            return "ok"

        if off.invoice_id and off.invoice_id != invoice_id:
            _dbg("warn: invoice mismatch offer=%s off.invoice_id=%s got=%s", offer_id, off.invoice_id, invoice_id)
            return "ok"

        off.status = OfferStatus.PAID
        off.paid_at = datetime.utcnow()
        # (на всяк) збережемо invoice_id, якщо ще не зберігали
        if not off.invoice_id:
            off.invoice_id = invoice_id
        await session.flush()

        lot: Lot = await session.get(Lot, off.lot_id)

        # Якщо кількість вибрана — інші активні пропозиції закриваємо
        paid_cnt = (await session.execute(
            select(func.count()).select_from(Offer).where(
                Offer.lot_id == lot.id, Offer.status == OfferStatus.PAID
            )
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

    # 5) Пінгаємо користувача (ОСЬ ТУТ головне — вже без всяких _main)
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
