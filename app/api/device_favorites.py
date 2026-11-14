from fastapi import APIRouter, Body, Depends, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session
from app.models import UserORM
from app.repositories.device_favorite_repository import DeviceFavoriteRepository
from app.security import Permission


class EmptyFavoriteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeviceFavoritesRead(BaseModel):
    device_ids: list[str]


def build_device_favorites_router(get_session, get_current_user, require):
    router = APIRouter(prefix="/v1/houses/{house_id}/me/device-favorites")

    @router.get("", response_model=DeviceFavoritesRead)
    def list_favorites(
        house_id: str,
        response: Response,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(session, user, house_id, Permission.DEVICE_READ)
        response.headers["Cache-Control"] = "no-store"
        return DeviceFavoritesRead(
            device_ids=DeviceFavoriteRepository(session).list(user.id, house_id)
        )

    @router.put("/{device_id}", status_code=204)
    def put_favorite(
        house_id: str,
        device_id: str,
        body: EmptyFavoriteBody | None = Body(default=None),
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(session, user, house_id, Permission.DEVICE_READ)
        DeviceFavoriteRepository(session).put(user.id, house_id, device_id)
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    @router.delete("/{device_id}", status_code=204)
    def delete_favorite(
        house_id: str,
        device_id: str,
        body: EmptyFavoriteBody | None = Body(default=None),
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(session, user, house_id, Permission.DEVICE_READ)
        DeviceFavoriteRepository(session).delete(user.id, house_id, device_id)
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    return router
