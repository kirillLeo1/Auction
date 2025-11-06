# app/main.py
import asyncio
import hashlib
import logging
from datetime import datetime
from contextlib import asynccontextmanager
from app.handlers.user import ContactOneSG
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
    # 0) сире тіло + заголовки (для логів і підпису)
    raw = await request.body()
    body_sha = hashlib.sha256(raw).hexdigest()
    x_sign = request.headers.get("X-Sign") or request.headers.get("X-Signature")
    ctype = request.headers.get("content-type")

    client_ip = getattr(getattr(request, "client", None), "host", "?")
    _dbg(
        "hit POST /monopay/webhook | ip=%s | has X-Sign=True X-Signature=%s | ctype=%s | body_sha256=%s",
        client_ip, bool(request.headers.get("X-Signature")), ctype, body_sha[:16]
    )

    # 1) перевірка підпису
    if not x_sign:
        _dbg("reject 400: missing signature header; headers_keys=%s", list(request.headers.keys()))
        raise HTTPException(400, "Bad signature")

    ok = await verify_webhook_signature(raw, x_sign)
    _dbg("verify_webhook_signature=%s (x_sign_len=%s)", ok, len(x_sign))
    if not ok:
        _dbg("reject 400: signature invalid (body_sha256=%s)", body_sha)
        raise HTTPException(400, "Bad signature")

    # 2) JSON
    try:
        data = await request.json()
    except Exception:
        _dbg("reject 400: bad json")
        raise HTTPException(400, "Bad JSON")

    status: str = data.get("status")
    invoice_id: str = data.get("invoiceId")  # на всяк, для дебагу/аудиту

    # 3) offer_id з query (?offer_id=..)
    offer_id_raw = request.query_params.get("offer_id")
    try:
        offer_id = int(offer_id_raw) if offer_id_raw is not None else None
    except ValueError:
        offer_id = None

    if not offer_id:
        _dbg("noop: no offer_id in webhook")
        return "ok"

    # 4) дістаємо оффер і реагуємо на статус
    async with async_session() as session:
        off: Offer | None = await session.get(Offer, offer_id)
        if not off:
            _dbg("noop: offer not found id=%s", offer_id)
            return "ok"

        # тільки фінальний статус цікавить
        if status in ("created", "processing"):
            _dbg("noop: status=%s", status)
            return "ok"

        if status != "success":
            _dbg("noop: status is not success: %s", status)
            return "ok"

        # 5) success → позначаємо оплаченим
        off.status = OfferStatus.PAID
        off.paid_at = datetime.utcnow()
        await session.commit()
        await session.refresh(off)

        # тягнемо лот, бо public_id у лота
        lot = await session.get(Lot, off.lot_id)
        lot_public = getattr(lot, "public_id", off.id) if lot else off.id

        # 6) Штовхаємо юзеру форму контактів БЕЗ ЛІНКА і ставимо FSM на one-shot
        try:
            ctx = dp.fsm.get_context(bot=bot, user_id=off.user_tg_id, chat_id=off.user_tg_id)
            await ctx.set_state(ContactOneSG.ONE)
            await ctx.update_data(offer_id=off.id)

            msg = (
                f"✅ Оплату за лот #{lot_public} зараховано!\n\n"
                "Відправте одним повідомленням ваші дані у довільному форматі:\n"
                "— ПІБ\n— місто/область\n— НП/УП + відділення або адреса\n— телефон\n— коментар (за потреби)\n\n"
                "Напишіть все В ОДНОМУ повідомленні у відповідь на це."
            )
            await bot.send_message(off.user_tg_id, msg)
        except Exception as e:
            _dbg("warn: cannot push contact form: %r", e)

    return "ok"
