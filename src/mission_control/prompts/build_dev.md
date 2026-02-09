Execute the following task by calling GitHub MCP tools NOW. Do not explain, plan, or ask questions — just call the tools.

Task: {title}
Description: {description}

**Target Repository:** `{repository}` on GitHub

{context_files_section}
{learned_context}

Branch `{branch_name}` already exists on `{source_branch}`. Do NOT call create_branch. Go directly to step 2.

STEP 2: Call `create_or_update_file` to create deliverable files. Use owner="{owner}", repo="{repo}", branch="{branch_name}". Write real, complete implementation code — NOT plan documents, NOT outlines, NOT markdown breakdowns. Deliver .py/.js/.ts/.yaml files with working logic. The path is relative to the repo root (e.g. "src/monitoring/health.py").

STEP 3: Call `create_pull_request` with owner="{owner}", repo="{repo}", head="{branch_name}", base="{source_branch}", title="{title}".

RULES: No local git/shell/filesystem commands. No Copilot skills. No update_task_status. No plan/outline/breakdown .md files. Only repo `{repository}`. No create_branch calls. Call the first tool now.
