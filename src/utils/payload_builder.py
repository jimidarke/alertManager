"""Alert payload builder ensuring CLIENTNODE.md compliance"""

import time
from typing import Optional, Dict, Any


def build_alert_payload(
    level: str,
    category: str,
    title: str,
    message: str,
    source: str,
    client: str,
    metadata: Optional[Dict[str, Any]] = None
) -> dict:
    """
    Build compliant alert payload per CLIENTNODE.md specification

    Args:
        level: Alert level (critical, warning, notice, info, debug)
        category: Alert category (security, system, weather, automation, personal, network, application, news, business, hardware, environmental)
        title: Short alert title
        message: Detailed alert message
        source: Source identifier (must be string)
        client: Client name/identifier
        metadata: Optional additional data

    Returns:
        dict: Compliant alert payload
    """

    # Build base payload with required fields
    payload = {
        "timestamp": int(time.time()),
        "level": level.strip().lower(),
        "category": category.strip().lower(),
        "title": title,
        "message": message,
        "source": source,
        "client": client.strip().lower()
    }

    # Add optional metadata if provided
    if metadata:
        payload["metadata"] = metadata

    return payload


def build_error_alert(
    feed_type: str,
    error_message: str,
    client: str,
    error_details: Optional[Dict[str, Any]] = None
) -> dict:
    """
    Build error alert when feed fetch fails

    Args:
        feed_type: Type of feed that failed (news, jokes, facts, trivia)
        error_message: Human-readable error message
        client: Client name/identifier
        error_details: Optional error metadata

    Returns:
        dict: Error alert payload
    """

    metadata = {
        "feed_type": feed_type,
        "error_type": "fetch_failed"
    }

    if error_details:
        metadata.update(error_details)

    return build_alert_payload(
        level="warning",
        category="system",
        title="Feed Fetch Failed",
        message=f"Failed to fetch {feed_type} feed: {error_message}",
        source="feed-aggregator/error",
        client=client,
        metadata=metadata
    )
