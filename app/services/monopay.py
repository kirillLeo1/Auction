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

from app.settings import settings

dbg_logger = logging.getLogger("app.monopay")
MONO_API = "https://api.monobank.ua"

def _dbg(msg: str, *args):
    try:
        if getattr(settings, "MONOPAY_DEBUG", False):
            dbg_logger.info("MONOPAY DEBUG: " + msg, *args)
    except Exception:
        pass

def _b64decode_loose(s: str) -> bytes:
    """
    Терпимо до відсутнього паддінга, до urlsafe (+/-/_), до переносів рядків.
    """
    s = (s or "").strip().replace(" ", "").replace("\n", "").replace("\r", "")
    # привести до стандартного base64 (а не urlsafe)
    s = s.replace("-", "+").replace("_", "/")
    missing = (-len(s)) % 4
    if missing:
        s += "=" * missing
    return base64.b64decode(s, validate=False)

PUBKEY_CACHE: Dict[str, bytes] = {}

async def _get_pubkey_der() -> bytes:
    """
    Повертає DER-представлення публічного ключа мерчанта MonoPay.
    Джерела (в такому порядку):
      1) ENV MONOPAY_PUBKEY_B64 — DER у base64
      2) ENV MONOPAY_PUBKEY_PEM — повний PEM, або навіть base64 від PEM
      3) GET /api/merchant/pubkey з X-Token
    """
    if "der" in PUBKEY_CACHE:
        return PUBKEY_CACHE["der"]

    # 1) DER у base64
    b64 = getattr(settings, "MONOPAY_PUBKEY_B64", "") or ""
    if b64:
        try:
            der = _b64decode_loose(b64)
            load_der_public_key(der)  # sanity
            PUBKEY_CACHE["der"] = der
            _dbg("pubkey source=ENV_B64 (len=%s)", len(der))
            return der
        except Exception as e:
            _dbg("ENV_B64 failed: %s", e)

    # 2) PEM (або base64 від PEM)
    pem = getattr(settings, "MONOPAY_PUBKEY_PEM", "") or ""
    if pem:
        try:
            if pem.strip().startswith("LS0"):  # це base64 від PEM
                pem_bytes = _b64decode_loose(pem)
            else:
                pem_bytes = pem.encode("utf-8")
            key = load_pem_public_key(pem_bytes)
            der = key.public_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            PUBKEY_CACHE["der"] = der
            _dbg("pubkey source=ENV_PEM (len=%s)", len(der))
            return der
        except Exception as e:
            _dbg("ENV_PEM failed: %s", e)

    # 3) Тягаємо з API
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            f"{MONO_API}/api/merchant/pubkey",
            headers={"X-Token": settings.MONOPAY_TOKEN},
        )
        r.raise_for_status()
        s = r.text.strip().strip('"')  # монобанк любить обгортати лапками
        der = _b64decode_loose(s)
        load_der_public_key(der)  # sanity
        PUBKEY_CACHE["der"] = der
        _dbg("pubkey source=API (len=%s)", len(der))
        return der

async def verify_webhook_signature(raw_body: bytes, x_sign: str) -> bool:
    """
    Перевірка ECDSA(SHA256) підпису з заголовка X-Sign / X-Signature.
    """
    try:
        signature = _b64decode_loose(x_sign)
    except Exception as e:
        _dbg("verify_webhook_signature: bad X-Sign base64: %s", e)
        return False

    try:
        der = await _get_pubkey_der()
        pub = load_der_public_key(der)
        pub.verify(signature, raw_body, ec.ECDSA(hashes.SHA256()))
        return True
    except InvalidSignature:
        _dbg("verify_webhook_signature: InvalidSignature")
        return False
    except Exception as e:
        _dbg("verify_webhook_signature: exception=%r", e)
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


