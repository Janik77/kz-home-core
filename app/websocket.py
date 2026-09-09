from fastapi import WebSocket

from app.events import Event


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.append(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self._connections:
            self._connections.remove(websocket)

    async def handle_event(self, event: Event) -> None:
        for connection in tuple(self._connections):
            try:
                await connection.send_json(event.message())
            except RuntimeError:
                self.disconnect(connection)
