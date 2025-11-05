# app/services/monopay.py
import base64
import hashlib
import logging
import httpx

from app.settings import settings

# crypto
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.exceptions import InvalidSignature

MONO_API = "https://api.monobank.ua"

# Кеш ключа (зберігаємо суто байти; це може бути DER або PEM)
PUBKEY_CACHE: dict[str, bytes] = {}


def _dbg(msg: str) -> None:
    logging.getLogger("app.monopay").info("MONOPAY DEBUG: " + msg)


def _b64decode_loose(s: str) -> bytes:
    """
    Base64/urlsafe Base64 з автопаддінгом.
    Не падає на відсутніх '=' і пробілах/переносах.
    """
    t = (s or "").strip().replace("\n", "").replace("\r", "")
    for func in (base64.b64decode, base64.urlsafe_b64decode):
        cur = t
        pad = (-len(cur)) % 4
        if pad:
            cur += "=" * pad
        try:
            return func(cur)
        except Exception:
            continue
    raise ValueError("bad base64")


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
    Перевірка X-Sign (ECDSA P-256 + SHA-256) по сирому body.
    Повертає True/False без виключень (щоб вебхук завжди відповідав 200/400 у handler’і).
    """
    if str(getattr(settings, "MONOPAY_SKIP_SIGNATURE", "")).lower() in {"1", "true", "yes"}:
        _dbg("verify_webhook_signature: SKIPPED by MONOPAY_SKIP_SIGNATURE")
        return True

    try:
        # X-Sign може прийти без '=' або urlsafe — декодуємо ліберально
        sig = _b64decode_loose(x_sign)
    except Exception as e:
        _dbg(f"verify_webhook_signature: bad x_sign base64 ({type(e).__name__}: {e})")
        return False

    try:
        pub_bytes = await _get_pubkey_bytes()
        pub = _load_pubkey_object(pub_bytes)

        body_sha = hashlib.sha256(raw_body).hexdigest()
        _dbg(f"verify_webhook_signature=True? x_sign_len={len(x_sign)} body_sha256={body_sha}")

        pub.verify(sig, raw_body, ec.ECDSA(hashes.SHA256()))
        return True
    except InvalidSignature:
        _dbg("verify_webhook_signature: InvalidSignature")
        return False
    except Exception as e:
        _dbg(f"verify_webhook_signature: exception={type(e).__name__}: {e}")
        return False

