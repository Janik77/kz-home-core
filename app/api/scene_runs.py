from fastapi import APIRouter, Depends, Header
from uuid import uuid4
from sqlalchemy.orm import Session
from app.models import UserORM
from app.schemas.scene_runs import SceneRunCreate, SceneRunRead
from app.security import Permission
from app.services.scene_run_service import SceneRunService


def build_scene_run_router(
    get_session, get_current_user, require, transport, executor=None
):
    router = APIRouter(prefix="/v1/houses/{house_id}/scene-runs")

    @router.post("", response_model=SceneRunRead, status_code=201)
    def create(
        house_id: str,
        data: SceneRunCreate,
        idempotency_key: str = Header(
            ..., min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
        ),
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(session, user, house_id, Permission.SCENE_RUN)
        require(session, user, house_id, Permission.DEVICE_CONTROL)
        return SceneRunService(session, transport).create(
            house_id, user.id, idempotency_key, data
        )

    @router.post("/execute", response_model=SceneRunRead, status_code=201)
    async def execute(
        house_id: str,
        data: SceneRunCreate,
        idempotency_key: str = Header(
            ..., min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
        ),
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(session, user, house_id, Permission.SCENE_RUN)
        require(session, user, house_id, Permission.DEVICE_CONTROL)
        from app.core.errors import UnavailableError

        if executor is None:
            raise UnavailableError("Scene executor unavailable")
        token = str(uuid4())
        service = SceneRunService(session, transport)
        view = await service.create_server(
            house_id, user.id, idempotency_key, data, token
        )
        row = service.row(house_id, view.id)
        if row.executor_token == token:
            executor.start(house_id, view.id, token)
        return view

    @router.get("/by-key/{key}", response_model=SceneRunRead)
    def by_key(
        house_id: str,
        key: str,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        from fastapi import HTTPException
        import re

        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", key):
            raise HTTPException(422, "Invalid idempotency key")
        require(session, user, house_id, Permission.SCENE_READ)
        require(session, user, house_id, Permission.DEVICE_READ)
        return SceneRunService(session, transport).by_key(house_id, user.id, key)

    @router.get("/{run_id}", response_model=SceneRunRead)
    def get(
        house_id: str,
        run_id: str,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(session, user, house_id, Permission.SCENE_READ)
        require(session, user, house_id, Permission.DEVICE_READ)
        return SceneRunService(session, transport).get(house_id, run_id)

    @router.post(
        "/{run_id}/actions/{position}/dispatch",
        response_model=SceneRunRead,
        status_code=202,
    )
    async def dispatch(
        house_id: str,
        run_id: str,
        position: int,
        session: Session = Depends(get_session),
        user: UserORM = Depends(get_current_user),
    ):
        require(session, user, house_id, Permission.SCENE_RUN)
        require(session, user, house_id, Permission.DEVICE_CONTROL)
        return await SceneRunService(session, transport).dispatch(
            house_id, run_id, position, user.id
        )

    return router
