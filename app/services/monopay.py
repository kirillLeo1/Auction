import base64
import json
import hashlib
from datetime import datetime
import httpx
from aiogram import Bot
from app.settings import settings

PUBKEY_CACHE: dict[str, bytes] = {}

MONO_API = "https://api.monobank.ua"

async def get_pubkey(client: httpx.AsyncClient) -> bytes:
    if "pub" in PUBKEY_CACHE:
        return PUBKEY_CACHE["pub"]
    resp = await client.get(f"{MONO_API}/api/merchant/pubkey", headers={"X-Token": settings.MONOPAY_TOKEN})
    resp.raise_for_status()
    b64 = resp.text.strip().strip('"')
    PUBKEY_CACHE["pub"] = base64.b64decode(b64)
    return PUBKEY_CACHE["pub"]

async def create_invoice(amount_uah: int, reference: str, destination: str, comment: str, offer_id: int) -> tuple[str, str]:
    """Returns (invoice_id, page_url)"""
    payload = {
        "amount": amount_uah * 100,  # kopiyky
        "ccy": 980,
        "merchantPaymInfo": {
            "reference": reference,
            "destination": destination,
            "comment": comment,
            "basketOrder": []
        },
        "redirectUrl": settings.MONOPAY_REDIRECT_URL,
        "webHookUrl": f"{settings.BASE_URL}/monopay/webhook?offer_id={offer_id}",
        "validity": settings.HOLD_HOURS * 3600,
        "paymentType": "debit",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            f"{MONO_API}/api/merchant/invoice/create",
            headers={"X-Token": settings.MONOPAY_TOKEN, "Content-Type": "application/json"},
            json=payload,
        )
        r.raise_for_status()
        data = r.json()
        return data["invoiceId"], data["pageUrl"]

async def verify_webhook_signature(raw_body: bytes, x_sign: str) -> bool:
    """ECDSA signature of raw_body using pubkey from /api/merchant/pubkey."""
    try:
        import cryptography.hazmat.primitives.serialization as ser
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.exceptions import InvalidSignature
    except Exception:
        return False
    async with httpx.AsyncClient(timeout=10) as client:
        pub_der = await get_pubkey(client)
    pub = ser.load_der_public_key(pub_der)
    try:
        sig = base64.b64decode(x_sign)
        pub.verify(sig, raw_body, ec.ECDSA(hashes.SHA256()))
        return True
    except Exception:
        return False