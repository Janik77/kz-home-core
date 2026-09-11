from app.schemas import (
    AutomationCreate,
    AutomationTrigger,
    DeviceAction,
    DeviceCreate,
    FloorCreate,
    HouseCreate,
    RoomCreate,
    SceneCreate,
)


def houses():
    return [HouseCreate(id="home1", name="KZ Home")]


def floors():
    return [
        FloorCreate(id="floor1", house_id="home1", name="Первый этаж", order=1),
        FloorCreate(id="floor2", house_id="home1", name="Второй этаж", order=2),
    ]


def rooms():
    return [
        RoomCreate(id="living_room", floor_id="floor1", name="Гостиная", icon="sofa"),
        RoomCreate(id="hall", floor_id="floor1", name="Коридор", icon="door"),
        RoomCreate(id="bathroom", floor_id="floor1", name="Ванная", icon="bath"),
        RoomCreate(id="bedroom", floor_id="floor2", name="Спальня", icon="bed"),
    ]


def devices():
    metadata = {"protocol": "virtual"}
    return [
        DeviceCreate(
            id="living_room_light",
            name="Свет в гостиной",
            room_id="living_room",
            type="light",
            state={"on": False, "brightness": 70},
            capabilities=["on_off", "brightness"],
            metadata=metadata,
        ),
        DeviceCreate(
            id="hall_motion",
            name="Движение в коридоре",
            room_id="hall",
            type="motion_sensor",
            state={"motion": False},
            capabilities=["motion"],
            metadata=metadata,
        ),
        DeviceCreate(
            id="bedroom_temperature",
            name="Температура в спальне",
            room_id="bedroom",
            type="temperature_sensor",
            state={"temperature": 22.5},
            capabilities=["temperature"],
            metadata=metadata,
        ),
        DeviceCreate(
            id="living_room_curtain",
            name="Шторы в гостиной",
            room_id="living_room",
            type="curtain",
            state={"position": 100},
            capabilities=["open_close", "position"],
            metadata=metadata,
        ),
        DeviceCreate(
            id="main_leak_sensor",
            name="Датчик протечки",
            room_id="bathroom",
            type="leak_sensor",
            state={"leak": False},
            capabilities=["leak"],
            metadata=metadata,
        ),
    ]


def scenes():
    actions = [
        DeviceAction(device_id="living_room_light", state={"on": False}),
        DeviceAction(device_id="living_room_curtain", state={"position": 0}),
    ]
    return [
        SceneCreate(
            id="leave_home", name="Ушёл из дома", house_id="home1", actions=actions
        ),
        SceneCreate(
            id="good_night", name="Спокойной ночи", house_id="home1", actions=actions
        ),
    ]


def automations():
    return [
        AutomationCreate(
            id="hall_motion_light",
            name="Свет в коридоре по движению",
            house_id="home1",
            trigger=AutomationTrigger(
                device_id="hall_motion", field="motion", operator="eq", value=True
            ),
            actions=[DeviceAction(device_id="living_room_light", state={"on": True})],
        )
    ]
