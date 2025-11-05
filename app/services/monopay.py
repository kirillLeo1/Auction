# app/services/monopay.py
import base64
import logging
from typing import Dict

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import (
    load_der_public_key,
    load_pem_public_key,
)
from typing import Optional
from app.settings import settings

log = logging.getLogger("app.monopay")
PUBKEY_CACHE: dict[str, bytes] = {}
MONO_API = "https://api.monobank.ua"

def _dbg(msg: str, *args):
    try:
        if getattr(settings, "MONOPAY_DEBUG", False):
            dbg_logger.info("MONOPAY DEBUG: " + msg, *args)
    except Exception:
        pass

def _clean_b64_header(v: str) -> str:
    """
    Mono інколи шле X-Sign без паддінгу, з пробілами або
    проксі склеює 'val1, val2'. Беремо останню частину,
    чистимо й додаємо паддінг.
    """
    if not v:
        return v
    # якщо проксі склеїла кілька значень — беремо останнє «живе»
    if "," in v:
        v = v.split(",")[-1].strip()
    v = v.strip().strip('"').replace(" ", "").replace("\r", "").replace("\n", "")
    # уніфікуємо altchars і додаємо паддінг
    v = v.replace("-", "+").replace("_", "/")
    pad = (-len(v)) % 4
    if pad:
        v += "=" * pad
    return v

def _b64decode_loose(s: str) -> bytes:
    s = _clean_b64_header(s)
    return base64.b64decode(s, validate=False)

async def get_pubkey(client: httpx.AsyncClient) -> bytes:
    if "pub" in PUBKEY_CACHE:
        return PUBKEY_CACHE["pub"]
    r = await client.get(f"{MONO_API}/api/merchant/pubkey")
    r.raise_for_status()
    der_b64 = r.text.strip().strip('"')
    der = _b64decode_loose(der_b64)
    PUBKEY_CACHE["pub"] = der
    log.info("MONOPAY DEBUG: pubkey loaded, der_len=%s", len(der))
    return der

async def verify_webhook_signature(raw_body: bytes, x_sign: str) -> bool:
    try:
        sig = _b64decode_loose(x_sign)
        log.info("MONOPAY DEBUG: x_sign_len=%s -> sig_bytes=%s", len(x_sign), len(sig))
        async with httpx.AsyncClient(timeout=10) as client:
            pub_der = await get_pubkey(client)
        pub = serialization.load_der_public_key(pub_der)
        pub.verify(sig, raw_body, ec.ECDSA(hashes.SHA256()))
        return True
    except InvalidSignature:
        log.info("MONOPAY DEBUG: ECDSA verify -> InvalidSignature")
        return False
    except Exception as e:
        log.info("MONOPAY DEBUG: verify_webhook_signature exception=%r", e)
        return False

async def create_invoice(amount_uah: int, reference: str, destination: str, comment: str, offer_id: int) -> tuple[str, str]:
    """
    Створює інвойс. Повертає (invoice_id, page_url).
    """
    payload = {
        "amount": amount_uah * 100,  # копійки
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
            headers={
                "X-Token": settings.MONOPAY_TOKEN,
                "Content-Type": "application/json",
            },
            json=payload,
        )
        r.raise_for_status()
        data = r.json()
        return data["invoiceId"], data["pageUrl"]


