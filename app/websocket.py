from collections.abc import Awaitable, Callable

from fastapi import WebSocket

from app.events import Event


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[WebSocket, Callable[[str], Awaitable[bool]]] = {}

    async def connect(
        self, websocket: WebSocket, authorizes_house: Callable[[str], Awaitable[bool]]
    ) -> None:
        await websocket.accept()
        self._connections[websocket] = authorizes_house

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.pop(websocket, None)

    async def handle_event(self, event: Event) -> None:
        house_id = event.data.get("house_id")
        if house_id is None:
            return
        for connection, authorizes_house in tuple(self._connections.items()):
            if not await authorizes_house(house_id):
                continue
            try:
                await connection.send_json(event.message())
            except RuntimeError:
                self.disconnect(connection)
