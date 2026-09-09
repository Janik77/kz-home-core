from app.models import (
    Automation,
    AutomationTrigger,
    Device,
    DeviceAction,
    Floor,
    House,
    Room,
    Scene,
)


def houses() -> list[House]:
    return [House(id="home1", name="KZ Home")]


def floors() -> list[Floor]:
    return [
        Floor(id="floor1", house_id="home1", name="Первый этаж", order=1),
        Floor(id="floor2", house_id="home1", name="Второй этаж", order=2),
    ]


def rooms() -> list[Room]:
    return [
        Room(id="living_room", floor_id="floor1", name="Гостиная", icon="sofa"),
        Room(id="hall", floor_id="floor1", name="Коридор", icon="door"),
        Room(id="bathroom", floor_id="floor1", name="Ванная", icon="bath"),
        Room(id="bedroom", floor_id="floor2", name="Спальня", icon="bed"),
    ]


def devices() -> list[Device]:
    virtual = {"protocol": "virtual"}
    return [
        Device(
            id="living_room_light", name="Свет в гостиной", room_id="living_room",
            type="light", state={"on": False, "brightness": 70},
            capabilities=["on_off", "brightness"], metadata=virtual,
        ),
        Device(
            id="hall_motion", name="Движение в коридоре", room_id="hall",
            type="motion_sensor", state={"motion": False},
            capabilities=["motion"], metadata=virtual,
        ),
        Device(
            id="bedroom_temperature", name="Температура в спальне", room_id="bedroom",
            type="temperature_sensor", state={"temperature": 22.5},
            capabilities=["temperature"], metadata=virtual,
        ),
        Device(
            id="living_room_curtain", name="Шторы в гостиной", room_id="living_room",
            type="curtain", state={"position": 100},
            capabilities=["open_close", "position"], metadata=virtual,
        ),
        Device(
            id="main_leak_sensor", name="Датчик протечки", room_id="bathroom",
            type="leak_sensor", state={"leak": False},
            capabilities=["leak"], metadata=virtual,
        ),
    ]


def scenes() -> list[Scene]:
    return [
        Scene(
            id="leave_home", name="Ушёл из дома", house_id="home1",
            actions=[
                DeviceAction(device_id="living_room_light", state={"on": False}),
                DeviceAction(device_id="living_room_curtain", state={"position": 0}),
            ],
        ),
        Scene(
            id="good_night", name="Спокойной ночи", house_id="home1",
            actions=[
                DeviceAction(device_id="living_room_light", state={"on": False}),
                DeviceAction(device_id="living_room_curtain", state={"position": 0}),
            ],
        ),
    ]


def automations() -> list[Automation]:
    return [
        Automation(
            id="hall_motion_light",
            name="Свет в коридоре по движению",
            trigger=AutomationTrigger(device_id="hall_motion", field="motion", equals=True),
            actions=[DeviceAction(device_id="living_room_light", state={"on": True})],
        )
    ]
