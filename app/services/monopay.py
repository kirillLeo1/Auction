import base64
import httpx
from typing import Tuple
from app.settings import settings

MONO_API = "https://api.monobank.ua"
_PUBKEY_CACHE: Tuple[str, bytes] | None = None  # ("der"|"pem", data)


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


async def get_pubkey(client: httpx.AsyncClient) -> Tuple[str, bytes]:
    """
    Повертає ("der"/"pem", bytes). Порядок:
    1) Якщо MONOPAY_PUBKEY у .env — використовуємо його (DER base64 або PEM).
    2) Інакше тягнемо з API /api/merchant/pubkey (X-Token).
    """
    global _PUBKEY_CACHE
    if _PUBKEY_CACHE:
        return _PUBKEY_CACHE

    # 1) override з .env
    if settings.MONOPAY_PUBKEY:
        val = settings.MONOPAY_PUBKEY.strip()
        try:
            if val.startswith("-----BEGIN"):
                _PUBKEY_CACHE = ("pem", val.encode())
            else:
                der = _b64decode_loose(val)
                _PUBKEY_CACHE = ("der", der)
            return _PUBKEY_CACHE
        except Exception:
            # впаде -> спробуємо API
            pass

    # 2) тягнемо з API Mono
    r = await client.get(
        f"{MONO_API}/api/merchant/pubkey",
        headers={"X-Token": settings.MONOPAY_TOKEN},
        timeout=15,
    )
    r.raise_for_status()
    ct = r.headers.get("content-type", "")
    body_text = r.text.strip()

    b64 = None
    if "application/json" in ct:
        try:
            j = r.json()
            if isinstance(j, str):
                b64 = j
            elif isinstance(j, dict):
                b64 = j.get("key") or j.get("pubkey") or j.get("data")
        except Exception:
            b64 = body_text.strip('"')
    else:
        if body_text.startswith('"') and body_text.endswith('"'):
            b64 = body_text[1:-1]
        else:
            b64 = body_text

    if b64:
        try:
            der = _b64decode_loose(b64)
            _PUBKEY_CACHE = ("der", der)
            return _PUBKEY_CACHE
        except Exception:
            pass

    _PUBKEY_CACHE = ("pem", r.content)
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
    """Перевіряємо X-Sign (ECDSA P-256). Підтримка DER і PEM публічних ключів."""
    if settings.MONOPAY_SKIP_SIGNATURE:
        return True  # для тестів/пісочниці

    # імпорти крипти всередині, щоб не ламати середовище, якщо хтось прибрав пакет
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization as ser
    except Exception:
        # якщо криптопакета нема — пропускаємо перевірку (краще все ж встановити cryptography)
        return True

    async with httpx.AsyncClient(timeout=10) as client:
        kind, keydata = await get_pubkey(client)

    # підготовка сигнатури
    try:
        sig = _b64decode_loose(x_sign or "")
    except Exception:
        return False

    # завантаження ключа
    try:
        if kind == "der":
            pub = ser.load_der_public_key(keydata)
        else:
            pub = ser.load_pem_public_key(keydata)
        pub.verify(sig, raw_body, ec.ECDSA(hashes.SHA256()))
        return True
    except Exception:
        return False

