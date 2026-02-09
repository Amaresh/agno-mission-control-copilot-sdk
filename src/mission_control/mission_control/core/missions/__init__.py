from mission_control.mission_control.core.missions.base import BaseMission
from mission_control.mission_control.core.missions.build import BuildMission
from mission_control.mission_control.core.missions.verify import VerifyMission

MISSION_REGISTRY: dict[str, type[BaseMission]] = {
    "build": BuildMission,
    "verify": VerifyMission,
}


def get_mission(mission_type: str) -> type[BaseMission]:
    """Return mission class for the given type, using workflow config."""
    from mission_control.mission_control.core.workflow_loader import get_workflow_loader
    loader = get_workflow_loader()
    return loader.get_mission_class(mission_type)
