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
from app.services.monopay import verify_webhook_signature
import logging, hashlib
dbg_logger = logging.getLogger("app.monopay")

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


async def _handle_monopay_webhook(request: Request) -> str:
    from datetime import datetime
    from sqlalchemy import select as _select
    from sqlalchemy import func as _func

    # 0) знімаємо сире тіло і ключові заголовки
    raw = await request.body()
    body_sha = hashlib.sha256(raw).hexdigest()
    x_sign = request.headers.get("X-Sign") or request.headers.get("X-Signature")
    ctype = request.headers.get("content-type")

    client_ip = getattr(getattr(request, "client", None), "host", "?")
    _dbg("hit %s %s | ip=%s | has X-Sign=%s X-Signature=%s | ctype=%s | body_sha256=%s",
         request.method, request.url.path, client_ip,
         bool(request.headers.get('X-Sign')), bool(request.headers.get('X-Signature')),
         ctype, body_sha[:16])

    # 1) перевірка підпису (ЛОГУЄМО причину перед 400)
    if not x_sign:
        _dbg("reject 400: missing signature header; headers_keys=%s", list(request.headers.keys()))
        raise HTTPException(400, "Bad signature")

    ok = await verify_webhook_signature(raw, x_sign)
    _dbg("verify_webhook_signature=%s (x_sign_len=%s)", ok, len(x_sign or ""))
    if not ok:
        _dbg("reject 400: signature invalid (body_sha256=%s)", body_sha)
        raise HTTPException(400, "Bad signature")

    # 2) розбираємо payload
    offer_id = int(request.query_params.get("offer_id", "0") or 0)
    try:
        data = await request.json()
    except Exception as e:
        _dbg("reject 400: json parse error: %s", e)
        raise HTTPException(400, "Bad json")

    status = data.get("status")
    invoice_id = data.get("invoiceId")
    amount = int(data.get("amount", 0) or 0)   # копійки
    ccy = int(data.get("ccy", 980) or 980)

    _dbg("payload: offer_id=%s status=%s invoice_id=%s amount=%s ccy=%s",
         offer_id, status, invoice_id, amount, ccy)

    # 3) бізнес-умови
    async with async_session() as session:
        off = await session.get(Offer, offer_id)
        if not off:
            _dbg("noop: offer not found (id=%s)", offer_id)
            return "ok"

        if off.status == OfferStatus.PAID:
            _dbg("noop: offer already PAID (id=%s)", off.id)
            return "ok"

        if off.invoice_id and invoice_id and off.invoice_id != invoice_id:
            _dbg("noop: invoice mismatch expected=%s got=%s", off.invoice_id, invoice_id)
            return "ok"

        expected_amount = off.offered_price * 100
        if ccy != 980 or amount != expected_amount:
            _dbg("noop: amount/ccy mismatch expected_amount=%s got_amount=%s expected_ccy=980 got_ccy=%s",
                 expected_amount, amount, ccy)
            return "ok"

        if status != "success":
            _dbg("noop: status is not success: %s", status)
            return "ok"

        # 4) зараховуємо оплату
        off.status = OfferStatus.PAID
        off.paid_at = datetime.utcnow()
        await session.flush()
        lot = await session.get(Lot, off.lot_id)

        # 5) якщо кількість вибухла — скасувати інші і видалити пост
        paid_cnt = (await session.execute(
            _select(_func.count()).select_from(Offer)
            .where(Offer.lot_id == lot.id, Offer.status == OfferStatus.PAID)
        )).scalar_one()
        _dbg("paid counter for lot %s: %s of %s", lot.public_id, paid_cnt, lot.quantity)

        if paid_cnt >= lot.quantity:
            others = (await session.execute(
                _select(Offer).where(
                    Offer.lot_id == lot.id,
                    Offer.status.in_([OfferStatus.OFFERED, OfferStatus.POSTPONED])
                )
            )).scalars().all()
            for o in others:
                o.status = OfferStatus.CANCELED
            if lot.channel_id and lot.channel_message_id:
                try:
                    await bot.delete_message(lot.channel_id, lot.channel_message_id)
                    _dbg("channel message deleted chat=%s msg=%s", lot.channel_id, lot.channel_message_id)
                except Exception as e:
                    _dbg("channel delete failed: %s", e)

        # 6) ставимо юзеру стан одношагової форми
        me = await bot.get_me()
        key = StorageKey(chat_id=off.user_tg_id, user_id=off.user_tg_id, bot_id=me.id)
        await dp.fsm.storage.set_state(key, ContactOneSG.ONE)
        await dp.fsm.storage.set_data(key, {"offer_id": off.id})

        try:
            await bot.send_message(
                off.user_tg_id,
                "✅ Оплату зараховано!\n\n"
                "Відповідай одним повідомленням: ПІБ / місто / відділення (або адреса) / телефон.",
            )
            _dbg("user %s prompted for contacts (offer=%s)", off.user_tg_id, off.id)
        except Exception as e:
            _dbg("send_message failed: %s", e)

        await session.commit()

    _dbg("webhook processing OK for offer_id=%s", offer_id)
    return "ok"

@app.post("/monopay/webhook", response_class=PlainTextResponse)
async def monopay_webhook(request: Request):
    return await _handle_monopay_webhook(request)

# альтернативний шлях, якщо десь випадково у BASE_URL був префікс
@app.post("/telegram/webhook/monopay/webhook", response_class=PlainTextResponse)
async def monopay_webhook_alt(request: Request):
    return await _handle_monopay_webhook(request)

