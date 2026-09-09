from app.models import DeviceStateChanged
from app.services.device_service import DeviceService


class AutomationEngine:
    def __init__(self, devices: DeviceService) -> None:
        self._devices = devices

    async def handle(self, event: DeviceStateChanged) -> None:
        if event.device_id == "motion1" and event.state is True:
            await self._devices.set_state("light1", "on")
