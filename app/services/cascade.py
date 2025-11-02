from datetime import datetime, timedelta
from sqlalchemy import select, func, desc
from aiogram import Bot
from aiogram.types import Message
from app.db import async_session
from app.models import Lot, Bid, Offer, OfferStatus, LotStatus
from app.settings import settings
from app.services.monopay import create_invoice
from app.utils import tri_buttons

async def start_cascade(bot: Bot, lot_id: int):
    """Compute ranking, create offers for top N = quantity, DM users with 3 buttons."""
    async with async_session() as session:
        lot = await session.get(Lot, lot_id)
        if not lot:
            return
        lot.status = LotStatus.FINISHED
        await session.flush()

        # ranking by each user's MAX bid
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

        # how many items remain
        remaining = lot.quantity

        rank = 0
        for user_tg_id, mx in res:
            rank += 1
            # prepare offer row but DM only top `remaining`
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
                # notify user
                txt = (
                    f"Ви у каскаді переможців лота <b>#{lot.public_id}</b>\n"
                    f"Сума до оплати: <b>{mx} грн</b>\n"
                    f"Тримаємо за вами до: <b>{(now+hold).strftime('%d.%m %H:%M')}</b> (за Києвом)"
                )
                kb = tri_buttons(page_url, f"postpone:{offer.id}", f"decline:{offer.id}")
                try:
                    await bot.send_message(chat_id=user_tg_id, text=txt, reply_markup=kb, parse_mode="HTML")
                except Exception:
                    pass
        await session.commit()

async def advance_cascade(bot: Bot):
    """Periodic checker: expire old offers, open next ones until items sold out."""
    from sqlalchemy import and_, select
    from datetime import datetime
    async with async_session() as session:
        now = datetime.utcnow()
        # expire
        q = select(Offer).where(Offer.status.in_([OfferStatus.OFFERED, OfferStatus.POSTPONED]), Offer.hold_until < now)
        for off in (await session.execute(q)).scalars().all():
            off.status = OfferStatus.EXPIRED
        await session.flush()

        # for each finished lot, ensure number of ACTIVE offers (offered/postponed) equals remaining quantity; if less, open next by rank
        lots = (await session.execute(select(Lot).where(Lot.status == LotStatus.FINISHED))).scalars().all()
        for lot in lots:
            # count paid
            paid = (await session.execute(select(func.count()).select_from(Offer).where(Offer.lot_id == lot.id, Offer.status == OfferStatus.PAID))).scalar_one()
            remaining = max(lot.quantity - paid, 0)
            active = (await session.execute(select(func.count()).select_from(Offer).where(Offer.lot_id == lot.id, Offer.status.in_([OfferStatus.OFFERED, OfferStatus.POSTPONED])))).scalar_one()
            if remaining > active:
                need = remaining - active
                # find next candidates by rank not yet offered/paid/declined/expired/canceled
                qnext = select(Offer).where(Offer.lot_id == lot.id, Offer.status == OfferStatus.CANCELED).order_by(Offer.rank_index)
                candidates = (await session.execute(qnext)).scalars().all()
                for off in candidates[:need]:
                    off.status = OfferStatus.OFFERED
                    from datetime import timedelta
                    off.hold_until = datetime.utcnow() + timedelta(hours=settings.HOLD_HOURS)
                    # create invoice and DM
                    inv_id, page_url = await create_invoice(
                        amount_uah=off.offered_price,
                        reference=f"lot#{lot.public_id}-user{off.user_tg_id}-offer{off.id}",
                        destination=f"Оплата за лот #{lot.public_id}",
                        comment=f"Лот #{lot.public_id}",
                        offer_id=off.id,
                    )
                    off.invoice_id = inv_id
                    off.invoice_url = page_url
                    txt = (
                        f"Черга дійшла до вас по лоту <b>#{lot.public_id}</b>\n"
                        f"Сума до оплати: <b>{off.offered_price} грн</b>\n"
                        f"Тримаємо до: <b>{off.hold_until.strftime('%d.%m %H:%M')}</b>"
                    )
                    kb = tri_buttons(page_url, f"postpone:{off.id}", f"decline:{off.id}")
                    try:
                        await bot.send_message(off.user_tg_id, txt, reply_markup=kb, parse_mode="HTML")
                    except Exception:
                        pass
        await session.commit()