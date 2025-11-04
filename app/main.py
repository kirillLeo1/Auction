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

# ─────────────────────────────────────────────────────────
# Єдина реалізація логіки вебхука (працює і для /monopay/webhook,
# і для /telegram/webhook/monopay/webhook як "страховка")
async def _handle_monopay_webhook(request: Request) -> str:
    from datetime import datetime  # локальний імпорт, щоб не чіпати верхи

    raw = await request.body()
    x_sign = request.headers.get("X-Sign")

    # 0) Перевірка криптопідпису MonoPay (обовʼязково для продакшену)
    if not x_sign or not await verify_webhook_signature(raw, x_sign):
        raise HTTPException(400, "Bad signature")

    # Параметри
    offer_id = int(request.query_params.get("offer_id", "0") or 0)
    data = await request.json()
    status = data.get("status")            # created / processing / success / failure
    invoice_id = data.get("invoiceId")
    amount = int(data.get("amount", 0) or 0)  # у копійках
    ccy = int(data.get("ccy", 980) or 980)    # 980 = UAH

    async with async_session() as session:
        off = await session.get(Offer, offer_id)
        if not off:
            # незнайома пропозиція — ігноруємо тихо
            return "ok"

        # 1) Ідемпотентність: якщо вже оплачене — нічого не робимо
        if off.status == OfferStatus.PAID:
            return "ok"

        # 2) invoiceId має збігатися з тим, що ми створили
        if off.invoice_id and invoice_id and off.invoice_id != invoice_id:
            return "ok"

        # 3) Валюта й сума мають збігатися (UAH і рівно offered_price * 100 коп.)
        if ccy != 980 or amount != off.offered_price * 100:
            return "ok"

        # 4) Тільки 'success' зараховуємо як оплату
        if status != "success":
            return "ok"

        # ==== Все ок: фіксуємо оплату ====
        off.status = OfferStatus.PAID
        off.paid_at = datetime.utcnow()
        await session.flush()

        # Якщо кількість вичерпана — скасовуємо інші активні офери і видаляємо пост
        lot = await session.get(Lot, off.lot_id)
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
            if lot.channel_id and lot.channel_message_id:
                try:
                    await bot.delete_message(lot.channel_id, lot.channel_message_id)
                except Exception:
                    pass

        # Без жодних диплінків — прямо просимо дані одним повідомленням,
        # та ставимо користувачу FSM-стан ContactOneSG.ONE
        me = await bot.get_me()
        key = StorageKey(chat_id=off.user_tg_id, user_id=off.user_tg_id, bot_id=me.id)
        await dp.fsm.storage.set_state(key, ContactOneSG.ONE)
        await dp.fsm.storage.set_data(key, {"offer_id": off.id})

        prompt = (
            "✅ Оплату зараховано!\n\n"
            "Введіть ОДНИМ повідомленням:\n"
            "ПІБ / місто / відділення (або адреса) / телефон\n\n"
            "Приклад:\n"
            "Іван Іванов\nКиїв\nНП Відділення 12\n+380XXXXXXXXX"
        )
        try:
            await bot.send_message(off.user_tg_id, prompt)
        except Exception:
            pass

        await session.commit()

    return "ok"

@app.post("/monopay/webhook", response_class=PlainTextResponse)
async def monopay_webhook(request: Request):
    return await _handle_monopay_webhook(request)

# альтернативний шлях, якщо десь випадково у BASE_URL був префікс
@app.post("/telegram/webhook/monopay/webhook", response_class=PlainTextResponse)
async def monopay_webhook_alt(request: Request):
    return await _handle_monopay_webhook(request)

