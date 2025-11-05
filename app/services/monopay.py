import base64
import httpx
from typing import Tuple
from app.settings import settings

MONO_API = "https://api.monobank.ua"
# офіційний endpoint з доки checkout/webhooks:
PUBKEY_URL = f"{MONO_API}/personal/checkout/signature/public/key"

_PUBKEY_CACHE: bytes | None = None


def _b64decode_loose(s: str) -> bytes:
    """Base64 із автопаддінгом + urlsafe fallback."""
    s = (s or "").strip().replace("\n", "").replace("\r", "")
    for func in (base64.b64decode, base64.urlsafe_b64decode):
        t = s
        pad = (-len(t)) % 4
        if pad:
            t += "=" * pad
        try:
            return func(t)
        except Exception:
            continue
    raise ValueError("bad base64")


async def _get_pubkey() -> bytes:
    """
    1) Якщо в .env задано MONOPAY_PUBKEY (DER/base64) — беремо його.
    2) Інакше тягнемо через офіційний endpoint (DER/base64) і кешуємо.
    """
    global _PUBKEY_CACHE
    if settings.__dict__.get("MONOPAY_PUBKEY"):
        # у .env зберігаємо ПУБЛІЧНИЙ ключ у base64 (DER)
        return base64.b64decode(settings.MONOPAY_PUBKEY)

    if _PUBKEY_CACHE:
        return _PUBKEY_CACHE

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(PUBKEY_URL, headers={"X-Token": settings.MONOPAY_TOKEN})
        r.raise_for_status()
        der_b64 = r.text.strip().strip('"')
        _PUBKEY_CACHE = base64.b64decode(der_b64)
        return _PUBKEY_CACHE

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
    Перевірка підпису MonoPay (ECDSA P-256 + SHA-256).
    Підпис у заголовку X-Sign, дані — НЕРОЗПАРСЕНЕ тіло (raw bytes).
    """
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.exceptions import InvalidSignature
    except Exception:
        return False

    try:
        pub_der = await _get_pubkey()
        pub = serialization.load_der_public_key(pub_der)
        sig = base64.b64decode(x_sign)
        pub.verify(sig, raw_body, ec.ECDSA(hashes.SHA256()))
        return True
    except InvalidSignature:
        return False
    except Exception:
        return False
