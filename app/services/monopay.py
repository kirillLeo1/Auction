# app/services/monopay.py
import base64
import httpx
import logging

from app.settings import settings

PUBKEY_CACHE: dict[str, bytes] = {}
MONO_API = "https://api.monobank.ua"

log = logging.getLogger("app.monopay")
def _dbg(fmt: str, *args):
    """
    Легка обгортка для логів. Підтримує як "fmt % args", так і просто строки.
    Приклад: _dbg("reject 400: headers=%s", list(request.headers.keys()))
    """
    try:
        msg = fmt % args if args else str(fmt)
        _log.info("MONOPAY DEBUG: %s", msg)
    except Exception:
        # без фанатизму: дебаг не має роняти сервіс
        pass

__all__ = ["_dbg"]  # щоб імпорт з інших модулів був явним
def _b64decode_loose(s: str) -> bytes:
    """Безпечне base64 (додає '=' паддінг, підтримує urlsafe)."""
    if not isinstance(s, str):
        s = str(s or "")
    s = s.strip().replace("\n", "")
    pad = (-len(s)) % 4
    s = s + ("=" * pad)
    try:
        return base64.urlsafe_b64decode(s.encode("ascii"))
    except Exception:
        return base64.b64decode(s.encode("ascii"))

def _pubkey_from_env() -> bytes | None:
    if settings.MONOPAY_PUBKEY:
        try:
            der = _b64decode_loose(settings.MONOPAY_PUBKEY)
            log.info("app.monopay:MONOPAY DEBUG: pubkey source=ENV len=%s", len(der))
            return der
        except Exception as e:
            log.warning("app.monopay:MONOPAY DEBUG: env pubkey decode error: %r", e)
    return None

async def _pubkey_from_api() -> bytes:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            f"{MONO_API}/api/merchant/pubkey",
            headers={"X-Token": settings.MONOPAY_TOKEN},
        )
        r.raise_for_status()
        b64 = (r.json() or {}).get("key", "")
        der = _b64decode_loose(b64)
        log.info("app.monopay:MONOPAY DEBUG: pubkey source=API len=%s", len(der))
        return der

async def get_pubkey_der() -> bytes:
    # 1) кеш
    if "pub" in PUBKEY_CACHE:
        return PUBKEY_CACHE["pub"]

    # 2) ENV
    env_der = _pubkey_from_env()
    if env_der:
        PUBKEY_CACHE["pub"] = env_der
        return env_der

    # 3) API (як запасний варіант)
    der = await _pubkey_from_api()
    PUBKEY_CACHE["pub"] = der
    return der

async def verify_webhook_signature(raw_body: bytes, x_sign: str | None) -> bool:
    """Перевірка ECDSA(SHA-256) підпису з X-Sign (base64)."""
    if settings.MONOPAY_SKIP_SIGNATURE:
        log.info("app.monopay:MONOPAY DEBUG: signature check skipped by flag")
        return True

    if not x_sign:
        log.info("app.monopay:MONOPAY DEBUG: no X-Sign header")
        return False

    sig = _b64decode_loose(x_sign)
    log.info("app.monopay:MONOPAY DEBUG: x_sign_len=%s -> sig_bytes=%s", len(x_sign), len(sig))

    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.exceptions import InvalidSignature

        der = await get_pubkey_der()
        pub = serialization.load_der_public_key(der)

        pub.verify(sig, raw_body, ec.ECDSA(hashes.SHA256()))
        log.info("app.monopay:MONOPAY DEBUG: verify_webhook_signature=True")
        return True

    except Exception as e:
        log.info("app.monopay:MONOPAY DEBUG: verify_webhook_signature=False (%r)", e)
        return False

async def create_invoice(
    amount_uah: int,
    reference: str,
    destination: str,
    comment: str,
    offer_id: int,
) -> tuple[str, str]:
    """
    Створює інвойс у MonoPay і повертає (invoice_id, page_url).
    """
    payload = {
        "amount": amount_uah * 100,   # копійки
        "ccy": 980,
        "merchantPaymInfo": {
            "reference": reference,
            "destination": destination,
            "comment": comment,
            "basketOrder": [],
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
