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


# ─────────────────────────────────────────────────────────────────────────────
# MonoPay webhook: перевіряємо підпис, оновлюємо статус оффера, шлемо користувачу повідомлення
async def _handle_monopay_webhook(request: Request) -> str:
    # 0) сире тіло + заголовки
    raw = await request.body()
    body_sha = hashlib.sha256(raw).hexdigest()

    # Mono інколи присилає X-Sign (офіційно) або X-Signature (деякі проксі/SDK)
    x_sign = request.headers.get("X-Sign") or request.headers.get("X-Signature")
    ctype = request.headers.get("content-type")

    client = getattr(request, "client", None)
    client_ip = getattr(client, "host", "?")

    _dbg(
        "hit POST /monopay/webhook | ip=%s | has X-Sign=%s X-Signature=%s | ctype=%s | body_sha256=%s"
        % (
            client_ip,
            bool(request.headers.get("X-Sign")),
            bool(request.headers.get("X-Signature")),
            ctype,
            body_sha[:16],
        )
    )

    # 1) перевірка підпису
    if not x_sign:
        _dbg("reject 400: missing signature header; headers_keys=%s" % list(request.headers.keys()))
        raise HTTPException(400, "Bad signature")

    ok = await verify_webhook_signature(raw, x_sign)
    if not ok:
        _dbg("reject 400: signature invalid (body_sha256=%s)" % body_sha)
        raise HTTPException(400, "Bad signature")

    # 2) парсимо JSON
    try:
        data = await request.json()
    except Exception:
        _dbg("reject 400: bad json")
        raise HTTPException(400, "Bad JSON")

    status = data.get("status")            # created / processing / success / failure
    invoice_id = data.get("invoiceId")

    # 3) беремо offer_id з query (?offer_id=...)
    try:
        offer_id = int(request.query_params.get("offer_id", "0"))
    except Exception:
        offer_id = 0

    if not offer_id:
        _dbg("skip: no offer_id in query")
        return "ok"

    # 4) оновлюємо БД
    async with async_session() as session:
        off = await session.get(Offer, offer_id)
        if not off:
            _dbg(f"skip: offer {offer_id} not found")
            return "ok"

        # захист від не того інвойсу
        if off.invoice_id and invoice_id and off.invoice_id != invoice_id:
            _dbg(f"skip: invoice mismatch saved={off.invoice_id} got={invoice_id}")
            return "ok"

        if status != "success":
            _dbg(f"noop: status is not success: {status}")
            return "ok"

        # success → фіксуємо оплату
        off.status = OfferStatus.PAID
        off.paid_at = datetime.utcnow()
        await session.flush()

        # якщо усі штуки викуплені — скасовуємо інші активні пропозиції
        lot = await session.get(Lot, off.lot_id)
        if lot:
            from sqlalchemy import select, func
            paid_cnt = (
                await session.execute(
                    select(func.count()).select_from(Offer).where(
                        Offer.lot_id == lot.id, Offer.status == OfferStatus.PAID
                    )
                )
            ).scalar_one()
            if paid_cnt >= lot.quantity:
                others = (
                    await session.execute(
                        select(Offer).where(
                            Offer.lot_id == lot.id,
                            Offer.status.in_([OfferStatus.OFFERED, OfferStatus.POSTPONED]),
                        )
                    )
                ).scalars().all()
                for o in others:
                    o.status = OfferStatus.CANCELED

        await session.commit()

    _dbg(f"ok: offer_id={offer_id} marked as PAID; invoice_id={invoice_id}")
    return "ok"


# Публічний ендпоінт — залиш як є, або так:
@app.post("/monopay/webhook", response_class=PlainTextResponse)
async def monopay_webhook(request: Request):
    return await _handle_monopay_webhook(request)



# альтернативний шлях, якщо випадково у BASE_URL колись додаси префікс /telegram/webhook
@app.post("/telegram/webhook/monopay/webhook", response_class=PlainTextResponse)
async def monopay_webhook_alt(request: Request):
    return await _handle_monopay_webhook(request)
