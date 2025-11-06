import base64
import logging
import httpx

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import (
    load_pem_public_key, load_der_public_key
)
from cryptography.exceptions import InvalidSignature

from app.settings import settings

PUBKEY_CACHE: dict[str, bytes] = {}
MONO_API = "https://api.monobank.ua"

_log = logging.getLogger("app.monopay")
def _dbg(fmt: str, *args):
    try:
        msg = fmt % args if args else str(fmt)
        _log.info("MONOPAY DEBUG: %s", msg)
    except Exception:
        pass

__all__ = ["_dbg"]  # щоб імпорт з інших модулів був явним
def _b64_loose_to_bytes(s: str) -> bytes:
    """
    М'який декодер: прибирає пробіли/переноси, нормалізує URL-safe та додає паддінг.
    Підходить і для X-Sign, і для ключа.
    """
    s = (s or "").strip()
    s = s.replace("\n", "").replace("\r", "").replace(" ", "")
    # URL-safe -> звичайний base64
    s = s.replace("-", "+").replace("_", "/")
    pad = (-len(s)) % 4
    if pad:
        s += "=" * pad
    return base64.b64decode(s, validate=False)

def _pubkey_from_env() -> bytes | None:
    if settings.MONOPAY_PUBKEY:
        try:
            der = _b64decode_loose(settings.MONOPAY_PUBKEY)
            log.info("app.monopay:MONOPAY DEBUG: pubkey source=ENV len=%s", len(der))
            return der
        except Exception as e:
            log.warning("app.monopay:MONOPAY DEBUG: env pubkey decode error: %r", e)
    return None

async def _load_pubkey() -> "Any":
    """
    1) Якщо є settings.MONOPAY_PUBKEY_B64 — використовуємо його (без запитів).
       Це саме той рядок "key" з /api/merchant/pubkey.
       Після base64 отримуємо або PEM-текст, або DER-байти.
    2) Інакше разово тягнемо з Monobank і кешуємо в памʼяті (за потреби).
    """
    # 1) з ENV
    if getattr(settings, "MONOPAY_PUBKEY", None):
        raw = _b64_loose_to_bytes(settings.MONOPAY_PUBKEY)
        is_pem = raw.startswith(b"-----BEGIN")
        _dbg("pubkey source=ENV len=%s pem=%s", len(raw), is_pem)
        try:
            return load_pem_public_key(raw) if is_pem else load_der_public_key(raw)
        except Exception as e:
            _dbg("pubkey load from ENV failed: %s", e)

    # 2) тягнемо з API (резервний шлях)
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            "https://api.monobank.ua/api/merchant/pubkey",
            headers={"X-Token": settings.MONOPAY_TOKEN},
        )
        r.raise_for_status()
        data = r.json()
        raw = _b64_loose_to_bytes(data["key"])
        is_pem = raw.startswith(b"-----BEGIN")
        _dbg("pubkey source=API len=%s pem=%s", len(raw), is_pem)
        return load_pem_public_key(raw) if is_pem else load_der_public_key(raw)

async def verify_webhook_signature(raw_body: bytes, x_sign: str) -> bool:
    """
    Перевірка підпису Monobank:
    - підпис у заголовку X-Sign (base64 url-safe від DER ECDSA-SHA256);
    - перевіряємо на публічному ключі мерчанта.
    """
    if not x_sign:
        _dbg("verify: no X-Sign")
        return False

    try:
        sig = _b64_loose_to_bytes(x_sign)
    except Exception as e:
        _dbg("verify: signature b64 decode error: %s", e)
        return False

    try:
        pub = await _load_pubkey()
        pub.verify(sig, raw_body, ec.ECDSA(hashes.SHA256()))
        return True
    except InvalidSignature:
        _dbg("verify: InvalidSignature")
        return False
    except Exception as e:
        _dbg("verify: exception=%s", e)
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
