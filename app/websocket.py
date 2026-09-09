from fastapi import WebSocket

from app.models import DeviceStateChanged


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.append(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self._connections:
            self._connections.remove(websocket)

    async def broadcast(self, event: DeviceStateChanged) -> None:
        message = event.model_dump()
        stale: list[WebSocket] = []
        for connection in tuple(self._connections):
            try:
                await connection.send_json(message)
            except RuntimeError:
                stale.append(connection)
        for connection in stale:
            self.disconnect(connection)
