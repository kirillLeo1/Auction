from datetime import datetime, timedelta
from sqlalchemy import select, func, desc, and_
from sqlalchemy.orm import selectinload
from aiogram import Bot
from app.db import async_session
from app.models import Lot, Bid, Offer, OfferStatus, LotStatus
from app.settings import settings
from app.services.monopay import create_invoice
from app.utils import tri_buttons, update_channel_caption

REMINDER_BEFORE_HOURS = 5

async def start_cascade(bot: Bot, lot_id: int):
    """Формуємо рейтинґ з MAX-ставок, створюємо офери для топ-N=quantity і шлемо 3 кнопки."""
    async with async_session() as session:
        lot = await session.get(Lot, lot_id)
        if not lot:
            return
        lot.status = LotStatus.FINISHED
        await session.flush()

        # Якщо це розпродаж (min_step==0), каскад не запускаємо
        if lot.min_step == 0:
            await session.commit()
            return

        stmt = (
            select(Bid.user_tg_id, func.max(Bid.amount).label("mx"))
            .where(Bid.lot_id == lot_id)
            .group_by(Bid.user_tg_id)
            .order_by(desc("mx"))
        )
        res = (await session.execute(stmt)).all()
        if not res:
            await session.commit()
            return

        now = datetime.utcnow()
        hold = timedelta(hours=settings.HOLD_HOURS)
        remaining = lot.quantity
        rank = 0
        for user_tg_id, mx in res:
            rank += 1
            offer = Offer(
                lot_id=lot.id,
                user_tg_id=user_tg_id,
                offered_price=mx,
                rank_index=rank,
                status=OfferStatus.OFFERED if rank <= remaining else OfferStatus.CANCELED,
                hold_until=now + hold if rank <= remaining else None,
            )
            session.add(offer)
            await session.flush()
            if rank <= remaining:
                inv_id, page_url = await create_invoice(
                    amount_uah=mx,
                    reference=f"lot#{lot.public_id}-user{user_tg_id}-offer{offer.id}",
                    destination=f"Оплата за лот #{lot.public_id}",
                    comment=f"Лот #{lot.public_id}",
                    offer_id=offer.id,
                )
                offer.invoice_id = inv_id
                offer.invoice_url = page_url
                kb = tri_buttons(page_url, f"postpone:{offer.id}", f"decline:{offer.id}")
                txt = (
                    f"Ви у каскаді переможців лота <b>#{lot.public_id}</b>\n"
                    f"Сума до оплати: <b>{mx} грн</b>\n"
                    f"Тримаємо за вами до: <b>{(now+hold).strftime('%d.%m %H:%M')}</b> (Київ)"
                )
                try:
                    await bot.send_message(user_tg_id, txt, reply_markup=kb, parse_mode="HTML")
                except Exception:
                    pass
        await session.commit()

async def advance_cascade(bot: Bot):
    """Періодичний чекер: EXPIRE, нагадування за 5 год, відкриття наступних кандидатів,
    і видалення поста з каналу коли все викуплено (бекап до вебхука)."""
    now = datetime.utcnow()
    async with async_session() as session:
        # 1) Нагадування за 5 годин до дедлайну
        q_rem = select(Offer).where(
            Offer.status.in_([OfferStatus.OFFERED, OfferStatus.POSTPONED]),
            Offer.hold_until.is_not(None)
        )
        for off in (await session.execute(q_rem)).scalars().all():
            if off.hold_until and not off.reminder_sent:
                delta = off.hold_until - now
                if delta <= timedelta(hours=REMINDER_BEFORE_HOURS) and delta > timedelta(seconds=0):
                    try:
                        await bot.send_message(
                            off.user_tg_id,
                            "Ваша бронь ще дійсна протягом 5 годин, після чого буде скасована. За скасовані броні блокуємо. Дякуємо за розуміння.")
                    except Exception:
                        pass
                    off.reminder_sent = True
        await session.flush()

        # 2) Прострочені → EXPIRED
        q_exp = select(Offer).where(
            Offer.status.in_([OfferStatus.OFFERED, OfferStatus.POSTPONED]),
            Offer.hold_until.is_not(None),
            Offer.hold_until < now,
        )
        for off in (await session.execute(q_exp)).scalars().all():
            off.status = OfferStatus.EXPIRED
        await session.flush()

        # 3) Для кожного FINISHED-лоту тримаємо активних оферів рівно стільки, скільки лишилось одиниць
        lots = (await session.execute(select(Lot))).scalars().all()
        for lot in lots:
            # видалення поста якщо все викуплено
            paid_cnt = (await session.execute(select(func.count()).select_from(Offer).where(Offer.lot_id==lot.id, Offer.status==OfferStatus.PAID))).scalar_one()
            if lot.channel_id and lot.channel_message_id and paid_cnt >= lot.quantity:
                try:
                    await bot.delete_message(lot.channel_id, lot.channel_message_id)
                except Exception:
                    pass

            if lot.status != LotStatus.FINISHED:
                continue
            remaining = max(lot.quantity - paid_cnt, 0)
            active_cnt = (
                await session.execute(
                    select(func.count()).select_from(Offer).where(
                        Offer.lot_id==lot.id,
                        Offer.status.in_([OfferStatus.OFFERED, OfferStatus.POSTPONED])
                    )
                )
            ).scalar_one()
            if remaining > active_cnt:
                need = remaining - active_cnt
                qnext = select(Offer).where(Offer.lot_id==lot.id, Offer.status==OfferStatus.CANCELED).order_by(Offer.rank_index)
                for off in (await session.execute(qnext)).scalars().all()[:need]:
                    off.status = OfferStatus.OFFERED
                    off.hold_until = datetime.utcnow() + timedelta(hours=settings.HOLD_HOURS)
                    inv_id, page_url = await create_invoice(
                        amount_uah=off.offered_price,
                        reference=f"lot#{lot.public_id}-user{off.user_tg_id}-offer{off.id}",
                        destination=f"Оплата за лот #{lot.public_id}",
                        comment=f"Лот #{lot.public_id}",
                        offer_id=off.id,
                    )
                    off.invoice_id = inv_id
                    off.invoice_url = page_url
                    kb = tri_buttons(page_url, f"postpone:{off.id}", f"decline:{off.id}")
                    try:
                        await bot.send_message(off.user_tg_id, f"Черга дійшла до вас по лоту #{lot.public_id}. Сума до оплати: {off.offered_price} грн", reply_markup=kb)
                    except Exception:
                        pass
        await session.commit()