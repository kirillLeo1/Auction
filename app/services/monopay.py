import base64
import httpx
from typing import Tuple
from app.settings import settings

MONO_API = "https://api.monobank.ua"
# офіційний endpoint з доки checkout/webhooks:
PUBKEY_URL = f"{MONO_API}/personal/checkout/signature/public/key"

PUBKEY_CACHE: dict[str, bytes] = {}

def _dbg(msg: str):
    import logging
    logging.getLogger("app.monopay").info("MONOPAY DEBUG: " + msg)
    
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


async def get_pubkey_der() -> bytes:
    """Отримує DER публічний ключ (спочатку з env, інакше з API за X-Token)."""
    if "pub" in PUBKEY_CACHE:
        return PUBKEY_CACHE["pub"]

    if getattr(settings, "MONOPAY_PUBKEY", None):
        b64 = settings.MONOPAY_PUBKEY.strip().strip('"').replace("\n", "")
        PUBKEY_CACHE["pub"] = base64.b64decode(b64)
        _dbg(f"pubkey source=ENV fp={hashlib.sha256(PUBKEY_CACHE['pub']).hexdigest()[:16]}")
        return PUBKEY_CACHE["pub"]

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{MONO_API}/api/merchant/pubkey",
                             headers={"X-Token": settings.MONOPAY_TOKEN})
        r.raise_for_status()
        b64 = r.text.strip().strip('"')
        PUBKEY_CACHE["pub"] = base64.b64decode(b64)
        _dbg(f"pubkey source=API fp={hashlib.sha256(PUBKEY_CACHE['pub']).hexdigest()[:16]}")
        return PUBKEY_CACHE["pub"]

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
    """Перевіряє X-Sign: ECDSA P-256 + SHA-256 по сирому body."""
    try:
        pub_der = await get_pubkey_der()
        pub = serialization.load_der_public_key(pub_der)

        sig = base64.b64decode(x_sign)
        body_sha = hashlib.sha256(raw_body).hexdigest()

        _dbg(f"verify_webhook_signature: x_sign_len={len(x_sign)} body_sha256={body_sha}")

        pub.verify(sig, raw_body, ec.ECDSA(hashes.SHA256()))
        return True
    except InvalidSignature:
        _dbg("verify_webhook_signature: InvalidSignature (підпис не співпав)")
        return False
    except Exception as e:
        _dbg(f"verify_webhook_signature: exception={type(e).__name__} {e}")
        return False
