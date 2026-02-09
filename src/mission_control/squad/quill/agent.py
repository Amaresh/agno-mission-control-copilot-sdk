"""
Quill — Infrastructure Ops / DigitalOcean Monitor.

DEPRECATED: Quill now uses GenericAgent with `always_run` config in workflows.yaml.
This file kept for backward compatibility only.
"""


def create_quill_agent():
    """Create Quill via GenericAgent — delegates to AgentFactory."""
    from mission_control.mission_control.core.factory import AgentFactory
    return AgentFactory.get_agent("quill")
