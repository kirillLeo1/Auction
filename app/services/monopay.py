# app/services/monopay.py
import base64
import hashlib
import logging
import httpx

from app.settings import settings

# crypto
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization as ser

MONO_API = "https://api.monobank.ua"

# Кеш ключа (зберігаємо суто байти; це може бути DER або PEM)
PUBKEY_CACHE: dict[str, bytes] = {}


def _dbg(msg: str) -> None:
    logging.getLogger("app.monopay").info("MONOPAY DEBUG: " + msg)


_BASE64_CHARS = re.compile(r"[A-Za-z0-9+/_=-]+")

def _b64decode_loose(s: str | bytes) -> bytes:
    """
    Над-терпимий декодер Base64:
    - прибирає все, що не з base64-алфавіту
    - конвертить urlsafe '-_' -> '+/'
    - додає '=' до кратності 4
    - декодує без strict-режиму
    """
    if isinstance(s, bytes):
        s = s.decode("utf-8", "ignore")
    s = "".join(_BASE64_CHARS.findall(s))
    s = s.replace("-", "+").replace("_", "/")
    missing = (-len(s)) % 4
    if missing:
        s += "=" * missing
    return base64.b64decode(s, validate=False)


async def _get_pubkey_der() -> bytes:
    """
    Повертає DER bytes публічного ключа мерчанта.
    Джерела: MONOPAY_PUBKEY (PEM або base64 DER) або API /api/merchant/pubkey.
    """
    try:
        from cryptography.hazmat.primitives import serialization as ser
    except Exception:
        raise RuntimeError("cryptography не встановлено")

    if getattr(settings, "MONOPAY_PUBKEY", None):
        pk_raw = settings.MONOPAY_PUBKEY.strip()
        if "BEGIN PUBLIC KEY" in pk_raw:
            # PEM
            logging.info("MONOPAY DEBUG: pubkey source=ENV(PEM)")
            pub = ser.load_pem_public_key(pk_raw.encode("utf-8"))
            return pub.public_bytes(ser.Encoding.DER, ser.PublicFormat.SubjectPublicKeyInfo)
        else:
            # base64 DER у env
            logging.info("MONOPAY DEBUG: pubkey source=ENV(DER)")
            return _b64decode_loose(pk_raw)

    # Інакше — тягнемо з API
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            "https://api.monobank.ua/api/merchant/pubkey",
            headers={"X-Token": settings.MONOPAY_TOKEN},
        )
        r.raise_for_status()
        # інколи це рядок в лапках
        der_b64 = r.text.strip().strip('"')
        logging.info("MONOPAY DEBUG: pubkey source=API len=%s", len(der_b64))
        return _b64decode_loose(der_b64)


def _load_pubkey_object(pub_bytes: bytes):
    """
    Пробуємо спочатку DER, якщо не вийшло — PEM.
    """
    try:
        return serialization.load_der_public_key(pub_bytes)
    except Exception:
        pass
    try:
        return serialization.load_pem_public_key(pub_bytes)
    except Exception as e:
        _dbg(f"load_pubkey: cannot deserialize ({type(e).__name__}: {e})")
        raise


async def create_invoice(amount_uah: int, reference: str, destination: str, comment: str, offer_id: int) -> tuple[str, str]:
    """Створює інвойс. Повертає (invoice_id, page_url)."""
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
            headers={"X-Token": settings.MONOPAY_TOKEN, "Content-Type": "application/json"},
            json=payload,
        )
        r.raise_for_status()
        data = r.json()
        return data["invoiceId"], data["pageUrl"]


async def verify_webhook_signature(raw_body: bytes, x_sign: str) -> bool:
    """
    Перевіряє X-Sign ECDSA над сирим тілом webhook.
    Повертає True/False; усі помилки — у лог, без винятків.
    """
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization as ser
        from cryptography.exceptions import InvalidSignature
    except Exception as e:
        logging.info("MONOPAY DEBUG: cryptography import error: %r", e)
        return False

    try:
        sig = _b64decode_loose(x_sign)  # X-Sign теж буває urlsafe
    except Exception as e:
        logging.info("MONOPAY DEBUG: x_sign base64 decode error: %r", e)
        return False

    try:
        der = await _get_pubkey_der()
        pub = ser.load_der_public_key(der)
        pub.verify(sig, raw_body, ec.ECDSA(hashes.SHA256()))
        logging.info("MONOPAY DEBUG: verify_webhook_signature=True")
        return True
    except InvalidSignature:
        logging.info("MONOPAY DEBUG: verify_webhook_signature=False (invalid signature)")
        return False
    except Exception as e:
        logging.info("MONOPAY DEBUG: verify_webhook_signature exception: %r", e)
        return False

