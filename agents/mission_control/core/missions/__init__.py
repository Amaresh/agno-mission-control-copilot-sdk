from agents.mission_control.core.missions.base import BaseMission
from agents.mission_control.core.missions.build import BuildMission
from agents.mission_control.core.missions.verify import VerifyMission

MISSION_REGISTRY: dict[str, type[BaseMission]] = {
    "build": BuildMission,
    "verify": VerifyMission,
}


def get_mission(mission_type: str) -> type[BaseMission]:
    """Return mission class for the given type, defaulting to BuildMission."""
    return MISSION_REGISTRY.get(mission_type, BuildMission)
