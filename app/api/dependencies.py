from typing import Any

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.repositories import RefreshSessionRepository, UserRepository
from app.security import TokenCodec
from app.services.auth_service import AuthenticationError, AuthenticationService


def bearer_token(authorization: str | None = Header(default=None)) -> str:
    parts = authorization.split() if authorization else []
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            401,
            "Valid Bearer authentication is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return parts[1]


def build_current_user_dependency(get_session: Any, codec: TokenCodec):
    def get_current_user(
        token: str = Depends(bearer_token),
        session: Session = Depends(get_session),
    ):
        service = AuthenticationService(
            UserRepository(session), RefreshSessionRepository(session), codec
        )
        try:
            return service.access_user(token)
        except AuthenticationError as error:
            raise HTTPException(
                401, str(error), headers={"WWW-Authenticate": "Bearer"}
            ) from error

    return get_current_user
