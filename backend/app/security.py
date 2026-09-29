import hashlib
import hmac
import secrets
import string

from app.config import settings

_ALPHABET = string.ascii_letters + string.digits
_KEY_BODY_LENGTH = 40  # ~238 bit entropi
VALID_ENVIRONMENTS = ("live", "test")


def generate_api_key(environment: str) -> str:
    if environment not in VALID_ENVIRONMENTS:
        raise ValueError(f"environment must be one of {VALID_ENVIRONMENTS}")
    body = "".join(secrets.choice(_ALPHABET) for _ in range(_KEY_BODY_LENGTH))
    return f"mk_{environment}_{body}"


def hash_api_key(api_key: str) -> str:
    # Key'ler yüksek entropili olduğu için bcrypt/argon2 gerekmez;
    # HMAC + gizli pepper, DB sızsa bile key'lerin geri üretilmesini engeller
    # ve hash üzerinden doğrudan index lookup yapılabilir.
    return hmac.new(
        settings.api_key_pepper.encode(), api_key.encode(), hashlib.sha256
    ).hexdigest()


def api_key_prefix(api_key: str) -> str:
    return api_key[:12]  # "mk_live_Ab3d"


def looks_like_api_key(value: str) -> bool:
    return value.startswith(("mk_live_", "mk_test_")) and len(value) == 8 + _KEY_BODY_LENGTH


def new_public_id(prefix: str = "mod") -> str:
    return f"{prefix}_{secrets.token_hex(12)}"
