"""HTTP-клиент к LLM gateway (deepseek_gateway или аналог)."""

from __future__ import annotations

import logging

import httpx

from config import LLM_ENABLED, LLM_GATEWAY_URL, LLM_INTERNAL_TOKEN, LLM_TIMEOUT_SEC

logger = logging.getLogger(__name__)


async def ask_llm(prompt: str) -> str | None:
    if not LLM_ENABLED or not LLM_GATEWAY_URL:
        return None
    headers = {"Content-Type": "application/json"}
    if LLM_INTERNAL_TOKEN:
        headers["X-Internal-Token"] = LLM_INTERNAL_TOKEN
    try:
        async with httpx.AsyncClient(timeout=LLM_TIMEOUT_SEC) as client:
            resp = await client.post(
                LLM_GATEWAY_URL.rstrip("/") + "/ask",
                json={"text": prompt},
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("ok") and data.get("reply"):
                return str(data["reply"]).strip()
    except Exception as e:
        logger.warning("LLM ask failed: %s", e)
    return None
