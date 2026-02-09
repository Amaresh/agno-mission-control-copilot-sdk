"""
Repo-scoped wrapper for GitHub MCP tools.

Subclasses agno MCPTools to intercept write-capable GitHub tool calls
(create_pull_request, push_files, create_or_update_file, create_branch)
and reject any that target the wrong repository.
"""

from typing import Optional

import structlog
from agno.tools.mcp import MCPTools

logger = structlog.get_logger()

# GitHub MCP tools that mutate a specific owner/repo
_WRITE_TOOLS = {
    "create_pull_request",
    "push_files",
    "create_or_update_file",
    "create_branch",
    "create_repository",
}


class RepoScopedMCPTools(MCPTools):
    """MCPTools wrapper that enforces a target repository constraint.

    Set ``allowed_repo`` (e.g. ``"{owner}/{repo}"``) before
    each work cycle.  Any GitHub write tool whose ``owner``/``repo`` params
    don't match will return an error string instead of executing.

    When ``allowed_repo`` is None (default), all calls pass through.
    """

    def __init__(self, *args, **kwargs):
        kwargs.pop("allowed_repo", None)
        super().__init__(*args, **kwargs)
        self._allowed_owner: Optional[str] = None
        self._allowed_repo: Optional[str] = None

    def set_allowed_repo(self, allowed_repo: Optional[str]) -> None:
        """Set (or clear) the target repo constraint."""
        if allowed_repo and "/" in allowed_repo:
            parts = allowed_repo.split("/", 1)
            self._allowed_owner = parts[0].lower()
            self._allowed_repo = parts[1].lower()
            logger.info("Repo scope set", allowed=allowed_repo)
        else:
            self._allowed_owner = None
            self._allowed_repo = None

    async def build_tools(self) -> None:
        """Build tools then wrap write-capable ones with a repo guard."""
        await super().build_tools()

        prefix = (self.tool_name_prefix or "") + "_" if self.tool_name_prefix else ""

        for tool_name in _WRITE_TOOLS:
            prefixed = f"{prefix}{tool_name}" if prefix else tool_name
            func = self.functions.get(prefixed)
            if func is None:
                continue

            original_entrypoint = func.entrypoint
            _tool = tool_name
            _self = self

            async def guarded_entrypoint(
                *args,
                _orig=original_entrypoint,
                _tname=_tool,
                _scope=_self,
                **kwargs,
            ):
                aowner = _scope._allowed_owner
                arepo = _scope._allowed_repo
                if aowner and arepo:
                    call_owner = str(kwargs.get("owner", "")).lower()
                    call_repo = str(kwargs.get("repo", "")).lower()
                    if call_owner and call_repo:
                        if call_owner != aowner or call_repo != arepo:
                            msg = (
                                f"BLOCKED: {_tname} targeted {call_owner}/{call_repo} "
                                f"but this task's allowed repository is "
                                f"{aowner}/{arepo}. "
                                f"Re-run the tool with owner='{aowner}' and repo='{arepo}'."
                            )
                            logger.warning(msg)
                            return msg
                return await _orig(*args, **kwargs)

            func.entrypoint = guarded_entrypoint
            logger.debug("Repo-guard wrapped", tool=prefixed)
