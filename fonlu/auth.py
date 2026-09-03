"""Supabase Auth JWT dogrulama.

Proje ES256 (asimetrik) imzaliyor, yani dogrulama icin paylasilan bir sir
gerekmiyor: acik anahtarlar JWKS ucundan geliyor ve PyJWKClient onlari
cache'liyor, istek basina ag turu yok.
"""

import functools
import os

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

_bearer = HTTPBearer(auto_error=False)


def _issuer() -> str:
    return os.environ["SUPABASE_URL"].rstrip("/") + "/auth/v1"


@functools.lru_cache(maxsize=1)
def _jwks() -> PyJWKClient:
    # Tembel: import aninda kurulsa SUPABASE_URL'i her import edende zorunlu
    # kilardi ve testler ag'a cikmayan sahte bir URL'e takilirdi.
    return PyJWKClient(f"{_issuer()}/.well-known/jwks.json")


def current_user(cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> str:
    """Dogrulanmis kullanicinin id'si. Frontend bunu Authorization basliginda yolluyor."""
    if cred is None:
        raise HTTPException(401, "Giris gerekli.")
    try:
        key = _jwks().get_signing_key_from_jwt(cred.credentials).key
        claims = jwt.decode(
            cred.credentials,
            key,
            algorithms=["ES256"],
            audience="authenticated",
            issuer=_issuer(),
            # Konteynerin saati Supabase'inkinden birkac saniye geri kalinca
            # taze token "The token is not yet valid (iat)" ile reddediliyordu.
            # 60 sn tolerans normal saat kaymasini yutuyor; gercek suresi
            # dolmus token hala reddediliyor.
            leeway=60,
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(401, f"Oturum gecersiz: {exc}")
    return claims["sub"]
