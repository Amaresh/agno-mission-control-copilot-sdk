"""
Notification helpers for Vision Healer.

Sends alerts via Telegram message + GitHub Issue for audit trail.
"""

import asyncio
from datetime import datetime, timezone
from typing import Optional

import httpx
import structlog

from agents.config import settings

logger = structlog.get_logger()


async def send_telegram(message: str) -> bool:
    """Send a Telegram message to the configured chat."""
    chat_id = settings.telegram_chat_id
    bot_token = settings.telegram_bot_token

    if not chat_id or not bot_token:
        logger.warning("No Telegram credentials for Vision alert")
        return False

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": message,
                    "parse_mode": "Markdown",
                },
                timeout=15,
            )
            resp.raise_for_status()
            return True
    except Exception as e:
        logger.error("Vision Telegram notification failed", error=str(e))
        return False


async def create_github_issue(title: str, body: str) -> Optional[str]:
    """Create a GitHub Issue on the configured repo for audit trail."""
    token = settings.github_token
    if not token:
        logger.warning("No GitHub token for Vision issue creation")
        return None

    repo = settings.vision_issue_repo
    if not repo:
        logger.warning("No vision_issue_repo configured — skipping GitHub issue creation")
        return None
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.github.com/repos/{repo}/issues",
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github.v3+json",
                },
                json={
                    "title": title,
                    "body": body,
                    "labels": ["vision-healer", "automated"],
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("html_url")
    except Exception as e:
        logger.error("Vision GitHub issue creation failed", error=str(e))
        return None


async def notify_human(title: str, details: str, severity: str = "warning"):
    """Send alert to human via both Telegram and GitHub Issue."""
    emoji = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(severity, "⚠️")
    timestamp = datetime.now(timezone.utc).strftime("%H:%M UTC")

    # Telegram message (concise)
    telegram_msg = (
        f"{emoji} *Vision Healer Alert*\n\n"
        f"*{title}*\n"
        f"{details}\n\n"
        f"_{timestamp}_"
    )

    # GitHub Issue (detailed)
    issue_title = f"[Vision Healer] {title}"
    issue_body = (
        f"## {emoji} {title}\n\n"
        f"**Severity:** {severity}\n"
        f"**Time:** {timestamp}\n\n"
        f"### Details\n\n{details}\n\n"
        f"---\n_Automatically created by Vision Healer agent_"
    )

    # Send both in parallel
    tg_result, gh_url = await asyncio.gather(
        send_telegram(telegram_msg),
        create_github_issue(issue_title, issue_body),
        return_exceptions=True,
    )

    if isinstance(tg_result, Exception):
        logger.error("Telegram notification error", error=str(tg_result))
    if isinstance(gh_url, Exception):
        logger.error("GitHub issue error", error=str(gh_url))
    elif gh_url:
        logger.info("Created GitHub issue", url=gh_url)
