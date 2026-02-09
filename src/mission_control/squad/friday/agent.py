"""
Friday — Developer Agent.

DEPRECATED: Friday now uses GenericAgent via workflows.yaml config.
This file kept for backward compatibility only.
"""


def create_friday():
    """Create Friday via GenericAgent — delegates to AgentFactory."""
    from mission_control.mission_control.core.factory import AgentFactory
    return AgentFactory.get_agent("friday")


# Backward-compat alias
FridayAgent = None  # Use AgentFactory.get_agent("friday") instead
