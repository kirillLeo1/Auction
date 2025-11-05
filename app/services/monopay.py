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


def _b64decode_loose(s: str) -> bytes:
    """
    Максимально толерантний декод Base64:
    - прибирає пробіли/переноси
    - уніфікує алфавіт (перетворює urlsafe на стандартний)
    - додає паддінг '='
    - декодує без strict-валидації
    """
    if isinstance(s, bytes):
        s = s.decode("utf-8", "ignore")
    # прибрати всі whitespace
    s = "".join(s.split())
    # привести urlsafe до стандартного алфавіту
    s = s.replace("-", "+").replace("_", "/")
    # додати '=' до кратності 4
    missing = (-len(s)) % 4
    if missing:
        s += "=" * missing
    try:
        return base64.b64decode(s, validate=False)
    except Exception as e:
        logging.info("MONOPAY DEBUG: b64decode(loose) failed: %r | head=%s...", e, s[:20])
        raise


async def _get_pubkey_bytes() -> bytes:
    """
    Дістає публічний ключ:
    1) з кешу
    2) з ENV (MONOPAY_PUBKEY) — може бути PEM або base64(PEM/DER)
    3) з API /api/merchant/pubkey (повертає base64(DER))
    Повертає СИРІ байти (DER або PEM). Десеріалізацію робимо нижче.
    """
    if "pub" in PUBKEY_CACHE:
        return PUBKEY_CACHE["pub"]

    env_val = getattr(settings, "MONOPAY_PUBKEY", None)
    if env_val:
        raw = env_val.strip().strip('"')
        # В ENV могли покласти:
        #  - чистий PEM (-----BEGIN PUBLIC KEY-----),
        #  - або base64(PEM/DER)
        if raw.startswith("-----BEGIN"):
            pub_bytes = raw.encode("utf-8")
        else:
            decoded = _b64decode_loose(raw)
            pub_bytes = decoded
        PUBKEY_CACHE["pub"] = pub_bytes
        _dbg(f"pubkey source=ENV fp={hashlib.sha256(pub_bytes).hexdigest()[:16]}")
        return pub_bytes

    # Беремо з офіційного ендпоінта (потрібен X-Token), це base64(DER)
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{MONO_API}/api/merchant/pubkey",
            headers={"X-Token": settings.MONOPAY_TOKEN},
        )
        r.raise_for_status()
        b64 = r.text.strip().strip('"')
        pub_bytes = _b64decode_loose(b64)
        PUBKEY_CACHE["pub"] = pub_bytes
        _dbg(f"pubkey source=API fp={hashlib.sha256(pub_bytes).hexdigest()[:16]}")
        return pub_bytes


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
    Перевірка підпису MonoPay Webhook:
    - публічний ключ: з ENV MONOPAY_PUBKEY (PEM або base64 DER) або з /api/merchant/pubkey по X-Token
    - підпис: DER-encoded ECDSA, приходить у заголовку X-Sign (base64/urlsafe)
    """
    # 1) Публічний ключ
    pub_src = "API"
    pub_der: bytes | None = None

    # Якщо в ENV задано MONOPAY_PUBKEY — приймаємо і PEM, і base64 DER
    from app.settings import settings
    if getattr(settings, "MONOPAY_PUBKEY", None):
        s = settings.MONOPAY_PUBKEY.strip()
        pub_src = "ENV"
        if "BEGIN PUBLIC KEY" in s:       # PEM
            pub_key = ser.load_pem_public_key(s.encode("utf-8"))
        else:                              # base64 DER
            pub_der = _b64decode_loose(s)
            pub_key = ser.load_der_public_key(pub_der)
    else:
        # Інакше беремо з API по X-Token
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.monobank.ua/api/merchant/pubkey",
                headers={"X-Token": settings.MONOPAY_TOKEN},
            )
            r.raise_for_status()
            # API повертає b64-рядок у лапках
            der_b64 = r.text.strip().strip('"')
            pub_der = _b64decode_loose(der_b64)
            pub_key = ser.load_der_public_key(pub_der)

    # 2) Декодуємо X-Sign «поблажливо»
    try:
        sig = _b64decode_loose(x_sign)
    except Exception as e:
        logging.info("app.monopay:MONOPAY DEBUG: bad base64 in X-Sign: %r", e)
        return False

    # 3) Перевіряємо ECDSA(SHA256)
    try:
        pub_key.verify(sig, raw_body, ec.ECDSA(hashes.SHA256()))
        logging.info("app.monopay:MONOPAY DEBUG: verify_webhook_signature=True (pubkey_source=%s, x_sign_len=%d)",
                     pub_src, len(x_sign))
        return True
    except InvalidSignature:
        logging.info("app.monopay:MONOPAY DEBUG: verify_webhook_signature=False (InvalidSignature)")
        return False
    except Exception as e:
        logging.info("app.monopay:MONOPAY DEBUG: verify_webhook_signature: exception=%s", repr(e))
        return False

