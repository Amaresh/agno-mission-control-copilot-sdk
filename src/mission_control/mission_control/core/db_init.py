"""
db_init — Dynamic TaskStatus registration from workflows.yaml.

Called once at startup (scheduler / API boot) before any DB queries.
Reads `missions.*.states` from workflows.yaml and:
  1. For PostgreSQL: ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS ...
  2. Patches the Python TaskStatus enum at runtime so SQLAlchemy can
     serialize/deserialize the new values.
"""

import enum

import structlog

logger = structlog.get_logger()


async def ensure_mission_states():
    """Register any custom mission states from workflows.yaml into the DB + Python enum."""
    from mission_control.mission_control.core.workflow_loader import get_workflow_loader
    from mission_control.mission_control.core.database import TaskStatus, engine

    loader = get_workflow_loader()
    loader.ensure_loaded()
    custom_states = loader.get_all_mission_states()

    if not custom_states:
        return

    # Check which states already exist in the Python enum
    existing = {e.name for e in TaskStatus}
    new_states = {s for s in custom_states if s not in existing}

    if not new_states:
        logger.debug("All mission states already registered", states=existing)
        return

    # 1. Patch Python enum at runtime
    for state_name in new_states:
        _add_enum_member(TaskStatus, state_name, state_name.lower())

    logger.info("Patched TaskStatus enum", new_states=new_states)

    # 2. For PostgreSQL: ALTER TYPE to add new values
    db_url = str(engine.url)
    if "postgresql" in db_url:
        from sqlalchemy import text
        from mission_control.mission_control.core.database import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            for state_name in new_states:
                try:
                    await session.execute(
                        text(f"ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS '{state_name.lower()}'")
                    )
                    logger.info("Added DB enum value", state=state_name.lower())
                except Exception as e:
                    # Value may already exist in DB but not in Python enum
                    logger.debug("DB enum value exists or failed", state=state_name, error=str(e))
            await session.commit()


def _add_enum_member(enum_cls: type[enum.Enum], name: str, value: str):
    """Dynamically add a member to a str-based Enum class.

    This is a workaround for Python's Enum immutability.  We write
    directly to internal dicts the same way stdlib enum does.
    """
    if name in enum_cls.__members__:
        return  # already exists

    # Create the new member
    member = str.__new__(enum_cls, value)
    member._name_ = name
    member._value_ = value

    # Register in all the right places
    enum_cls._member_map_[name] = member
    enum_cls._value2member_map_[value] = member
    enum_cls._member_names_.append(name)

    # Make it accessible as an attribute
    setattr(enum_cls, name, member)
