from __future__ import annotations

import base64
import httpx

from app.settings import settings

MONO_API = "https://api.monobank.ua"

# кеш для публічного ключа мерчанта (перевірка X-Sign)
_PUBKEY_CACHE: bytes | None = None


async def _get_pubkey(client: httpx.AsyncClient) -> bytes:
    global _PUBKEY_CACHE
    if _PUBKEY_CACHE:
        return _PUBKEY_CACHE
    r = await client.get(
        f"{MONO_API}/api/merchant/pubkey",
        headers={"X-Token": settings.MONOPAY_TOKEN},
        timeout=20,
    )
    r.raise_for_status()
    # повертається base64 рядок у лапках
    b64 = r.text.strip().strip('"')
    _PUBKEY_CACHE = base64.b64decode(b64)
    return _PUBKEY_CACHE


async def create_invoice(
    amount_uah: int,
    reference: str,
    destination: str,
    comment: str,
    offer_id: int,
) -> tuple[str, str]:
    """
    Створює інвойс MonoPay.
    Повертає (invoice_id, page_url).
    """
    payload = {
        "amount": amount_uah * 100,  # копійки
        "ccy": 980,
        "merchantPaymInfo": {
            "reference": reference,
            "destination": destination,
            "comment": comment,
            "basketOrder": [],
        },
        "redirectUrl": settings.MONOPAY_REDIRECT_URL,
        "webHookUrl": f"{settings.BASE_URL}/monopay/webhook?offer_id={offer_id}",
        "validity": settings.HOLD_HOURS * 3600,  # сек
        "paymentType": "debit",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            f"{MONO_API}/api/merchant/invoice/create",
            headers={
                "X-Token": settings.MONOPAY_TOKEN,
                "Content-Type": "application/json",
            },
            json=payload,
        )
        r.raise_for_status()
        data = r.json()
        return data["invoiceId"], data["pageUrl"]


async def verify_webhook_signature(raw_body: bytes, x_sign: str) -> bool:
    """
    Перевіряє X-Sign від MonoPay (ECDSA SHA-256).
    """
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
    except Exception:
        return False

    async with httpx.AsyncClient(timeout=10) as client:
        pub_der = await _get_pubkey(client)

    try:
        pub = serialization.load_der_public_key(pub_der)
        sig = base64.b64decode(x_sign)
        pub.verify(sig, raw_body, ec.ECDSA(hashes.SHA256()))
        return True
    except Exception:
        return False
