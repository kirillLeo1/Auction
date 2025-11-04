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
    async with async_session() as session:
        lot = await session.get(Lot, lot_id)
        if not lot:
            return
        lot.status = LotStatus.FINISHED
        await session.flush()

        # рейтинг по MAX ставки кожного
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
            status = OfferStatus.OFFERED if rank <= remaining else OfferStatus.CANCELED
            offer = Offer(
                lot_id=lot.id,
                user_tg_id=user_tg_id,
                offered_price=mx,
                rank_index=rank,
                status=status,
                hold_until=(now + hold) if status == OfferStatus.OFFERED else None,
            )
            session.add(offer)
            await session.flush()

            if status == OfferStatus.OFFERED:
                inv_id, page_url = await create_invoice(
                    amount_uah=mx,
                    reference=f"lot#{lot.public_id}-user{user_tg_id}-offer{offer.id}",
                    destination=f"Оплата за лот #{lot.public_id}",
                    comment=f"Лот #{lot.public_id}",
                    offer_id=offer.id,
                )
                offer.invoice_id = inv_id
                offer.invoice_url = page_url

                txt = (
                    f"Ви у каскаді переможців лота <b>#{lot.public_id}</b>\n"
                    f"Сума до оплати: <b>{mx} грн</b>\n"
                    f"Оплатити потрібно протягом {settings.HOLD_HOURS} годин.\n"
                    f"Посилання на оплату нижче."
                )
                kb = tri_buttons(page_url, f"postpone:{offer.id}", f"decline:{offer.id}")
                try:
                    await bot.send_message(chat_id=user_tg_id, text=txt, reply_markup=kb, parse_mode="HTML")
                except Exception:
                    pass
        await session.commit()

async def advance_cascade(bot: Bot):
    from datetime import datetime
    async with async_session() as session:
        now = datetime.utcnow()

        # ── 1) Нагадування за 5 годин до дедлайну (разово)
        remind_from = now
        q_rem = select(Offer).where(
            Offer.status.in_([OfferStatus.OFFERED, OfferStatus.POSTPONED]),
            Offer.hold_until.isnot(None),
            Offer.reminder_sent.is_(False),
            Offer.hold_until - timedelta(hours=5) <= now
        )
        for off in (await session.execute(q_rem)).scalars().all():
            try:
                await bot.send_message(
                    off.user_tg_id,
                    "Ваша бронь ще дійсна протягом 5 годин, після чого буде скасована. "
                    "За скасовані броні блокуємо. Дякуємо за розуміння."
                )
            except Exception:
                pass
            off.reminder_sent = True
        await session.flush()

        # ── 2) Прострочили дедлайн → EXPIRED
        q_exp = select(Offer).where(
            Offer.status.in_([OfferStatus.OFFERED, OfferStatus.POSTPONED]),
            Offer.hold_until.isnot(None),
            Offer.hold_until < now
        )
        for off in (await session.execute(q_exp)).scalars().all():
            off.status = OfferStatus.EXPIRED
        await session.flush()

        # ── 3) Дотриматись кількості активних «бронь» == залишку
        lots = (await session.execute(select(Lot).where(Lot.status == LotStatus.FINISHED))).scalars().all()
        for lot in lots:
            paid = (await session.execute(
                select(func.count()).select_from(Offer).where(
                    Offer.lot_id == lot.id, Offer.status == OfferStatus.PAID
                )
            )).scalar_one()
            remaining = max(lot.quantity - paid, 0)
            active = (await session.execute(
                select(func.count()).select_from(Offer).where(
                    Offer.lot_id == lot.id, Offer.status.in_([OfferStatus.OFFERED, OfferStatus.POSTPONED])
                )
            )).scalar_one()

            if remaining > active:
                need = remaining - active
                # наступні кандидати з CANCELED за рангом
                qnext = select(Offer).where(
                    Offer.lot_id == lot.id, Offer.status == OfferStatus.CANCELED
                ).order_by(Offer.rank_index)
                for off in (await session.execute(qnext)).scalars().all()[:need]:
                    off.status = OfferStatus.OFFERED
                    off.hold_until = datetime.utcnow() + timedelta(hours=settings.HOLD_HOURS)
                    off.reminder_sent = False  # нове вікно → нове нагадування
                    inv_id, page_url = await create_invoice(
                        amount_uah=off.offered_price,
                        reference=f"lot#{lot.public_id}-user{off.user_tg_id}-offer{off.id}",
                        destination=f"Оплата за лот #{lot.public_id}",
                        comment=f"Лот #{lot.public_id}",
                        offer_id=off.id,
                    )
                    off.invoice_id = inv_id
                    off.invoice_url = page_url
                    try:
                        await bot.send_message(
                            off.user_tg_id,
                            (f"Черга дійшла до вас по лоту <b>#{lot.public_id}</b>\n"
                             f"Сума до оплати: <b>{off.offered_price} грн</b>\n"
                             f"Оплатити потрібно протягом {settings.HOLD_HOURS} годин."),
                            reply_markup=tri_buttons(page_url, f"postpone:{off.id}", f"decline:{off.id}"),
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
        await session.commit()
