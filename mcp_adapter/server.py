"""RAYBOOST U8 -> MCP bridge for Claude Desktop.

This adapter exposes the exact U8 REST/JSON Agent Commerce Gateway as MCP tools.
It does not implement a second commerce system and does not bypass U1-U7 safety
checks. All commerce operations are forwarded to the running RAYBOOST backend.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = os.getenv("RAYBOOST_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
DEFAULT_AGENT_ID = os.getenv("RAYBOOST_AGENT_ID", "claude-desktop")
DEFAULT_BUYER_ID = os.getenv("RAYBOOST_BUYER_ID", "claude-demo-buyer")
DEFAULT_SESSION_ID = os.getenv("RAYBOOST_SESSION_ID", "claude-local-session")
TIMEOUT = float(os.getenv("RAYBOOST_TIMEOUT", "20"))

mcp = FastMCP("RAYBOOST Commerce")


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT)


async def _request(method: str, path: str, *, params: dict[str, Any] | None = None,
                   body: dict[str, Any] | None = None,
                   headers: dict[str, str] | None = None) -> str:
    """Call U8 and return a Claude-friendly JSON error instead of crashing the MCP server."""
    try:
        async with _client() as client:
            response = await client.request(
                method,
                path,
                params=params,
                json=body,
                headers=headers,
            )
        try:
            payload = response.json()
        except Exception:
            payload = {"raw": response.text}

        if response.is_error:
            return _json({
                "ok": False,
                "http_status": response.status_code,
                "error": payload,
                "gateway": BASE_URL,
            })
        return _json(payload)
    except httpx.RequestError as exc:
        return _json({
            "ok": False,
            "error": "RAYBOOST backend is unreachable.",
            "detail": str(exc),
            "gateway": BASE_URL,
            "hint": "Start the RAYBOOST FastAPI server on port 8000 first.",
        })


@mcp.tool()
async def get_merchant_capabilities() -> str:
    """Discover RAYBOOST merchant capabilities, payment environment and safety policies."""
    return await _request("GET", "/api/agent/capabilities")


@mcp.tool()
async def search_products(
    query: str = "",
    category: str = "",
    limit: int = 10,
    agent_id: str = DEFAULT_AGENT_ID,
    session_id: str = DEFAULT_SESSION_ID,
) -> str:
    """Search the RAYBOOST merchant catalogue. Prices and stock come from the merchant server."""
    limit = max(1, min(int(limit), 25))
    return await _request(
        "GET",
        "/api/agent/catalog/search",
        params={
            "q": query,
            "category": category or None,
            "limit": limit,
            "agent_id": agent_id or DEFAULT_AGENT_ID,
            "session_id": session_id or DEFAULT_SESSION_ID,
        },
    )


@mcp.tool()
async def get_product(
    product_id: str,
    agent_id: str = DEFAULT_AGENT_ID,
    session_id: str = DEFAULT_SESSION_ID,
) -> str:
    """Get a single RAYBOOST product using the merchant's product ID."""
    return await _request(
        "GET",
        f"/api/agent/products/{product_id}",
        params={
            "agent_id": agent_id or DEFAULT_AGENT_ID,
            "session_id": session_id or DEFAULT_SESSION_ID,
        },
    )


@mcp.tool()
async def get_related_products(
    product_id: str,
    agent_id: str = DEFAULT_AGENT_ID,
    session_id: str = DEFAULT_SESSION_ID,
) -> str:
    """Get catalogue-related products for a RAYBOOST product."""
    return await _request(
        "GET",
        f"/api/agent/products/{product_id}/related",
        params={
            "agent_id": agent_id or DEFAULT_AGENT_ID,
            "session_id": session_id or DEFAULT_SESSION_ID,
        },
    )


@mcp.tool()
async def get_buyer_recommendations(
    buyer_id: str,
    query: str = "",
    agent_id: str = DEFAULT_AGENT_ID,
    session_id: str = DEFAULT_SESSION_ID,
) -> str:
    """Get recommendations using RAYBOOST U5 first-party buyer intelligence when available."""
    if not buyer_id.strip():
        buyer_id = DEFAULT_BUYER_ID
    return await _request(
        "GET",
        "/api/agent/recommendations",
        params={
            "buyer_id": buyer_id,
            "q": query,
            "agent_id": agent_id or DEFAULT_AGENT_ID,
            "session_id": session_id or DEFAULT_SESSION_ID,
        },
    )


@mcp.tool()
async def create_cart(
    buyer_id: str = DEFAULT_BUYER_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    session_id: str = DEFAULT_SESSION_ID,
) -> str:
    """Create a merchant-side RAYBOOST cart for an AI buyer."""
    if not buyer_id.strip():
        buyer_id = DEFAULT_BUYER_ID
    return await _request(
        "POST",
        "/api/agent/carts",
        body={"buyer_id": buyer_id, "session_id": session_id or DEFAULT_SESSION_ID},
        headers={"X-Agent-Id": agent_id or DEFAULT_AGENT_ID},
    )


@mcp.tool()
async def get_cart(
    cart_id: str,
    agent_id: str = DEFAULT_AGENT_ID,
    session_id: str = DEFAULT_SESSION_ID,
) -> str:
    """Read a RAYBOOST merchant-side cart."""
    return await _request(
        "GET",
        f"/api/agent/carts/{cart_id}",
        params={
            "agent_id": agent_id or DEFAULT_AGENT_ID,
            "session_id": session_id or DEFAULT_SESSION_ID,
        },
    )


@mcp.tool()
async def add_to_cart(
    cart_id: str,
    product_id: str,
    quantity: int = 1,
    buyer_id: str = DEFAULT_BUYER_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    session_id: str = DEFAULT_SESSION_ID,
) -> str:
    """Add a product to a RAYBOOST cart. Server-side stock and quantity limits remain enforced."""
    if not buyer_id.strip():
        buyer_id = DEFAULT_BUYER_ID
    quantity = max(1, min(int(quantity), 20))
    return await _request(
        "POST",
        f"/api/agent/carts/{cart_id}/items",
        body={
            "buyer_id": buyer_id,
            "agent_id": agent_id or DEFAULT_AGENT_ID,
            "session_id": session_id or DEFAULT_SESSION_ID,
            "product_id": product_id,
            "qty": quantity,
        },
    )


@mcp.tool()
async def create_purchase_intent(
    buyer_id: str,
    cart_id: str,
    discount_percent: int = 0,
    agent_id: str = DEFAULT_AGENT_ID,
    session_id: str = DEFAULT_SESSION_ID,
) -> str:
    """Create or reuse a purchase intent. This NEVER confirms or charges the buyer."""
    if not buyer_id.strip():
        buyer_id = DEFAULT_BUYER_ID
    discount_percent = max(0, min(int(discount_percent), 10))
    return await _request(
        "POST",
        "/api/agent/purchase-intents",
        body={
            "buyer_id": buyer_id,
            "cart_id": cart_id,
            "discount_percent": discount_percent,
            "agent_id": agent_id or DEFAULT_AGENT_ID,
            "session_id": session_id or DEFAULT_SESSION_ID,
        },
    )


@mcp.tool()
async def get_purchase_summary(
    intent_id: int,
    agent_id: str = DEFAULT_AGENT_ID,
    session_id: str = DEFAULT_SESSION_ID,
) -> str:
    """Read the exact RAYBOOST purchase summary and confirmation state."""
    return await _request(
        "GET",
        f"/api/agent/purchase-intents/{int(intent_id)}",
        params={
            "agent_id": agent_id or DEFAULT_AGENT_ID,
            "session_id": session_id or DEFAULT_SESSION_ID,
        },
    )


@mcp.tool()
async def confirm_purchase(
    intent_id: int,
    buyer_id: str,
    confirmed_by_buyer: bool,
    agent_id: str = DEFAULT_AGENT_ID,
    session_id: str = DEFAULT_SESSION_ID,
) -> str:
    """Record explicit buyer confirmation. False is rejected by U8; the agent cannot silently confirm."""
    if not buyer_id.strip():
        buyer_id = DEFAULT_BUYER_ID
    return await _request(
        "POST",
        f"/api/agent/purchase-intents/{int(intent_id)}/confirm",
        body={
            "buyer_id": buyer_id,
            "confirmed_by_buyer": bool(confirmed_by_buyer),
            "agent_id": agent_id or DEFAULT_AGENT_ID,
            "session_id": session_id or DEFAULT_SESSION_ID,
        },
    )


@mcp.tool()
async def prepare_checkout(
    intent_id: int,
    buyer_id: str,
    agent_id: str = DEFAULT_AGENT_ID,
    session_id: str = DEFAULT_SESSION_ID,
) -> str:
    """Prepare bounded Razorpay Test Checkout. U8 requires a confirmed purchase intent first."""
    if not buyer_id.strip():
        buyer_id = DEFAULT_BUYER_ID
    return await _request(
        "POST",
        "/api/agent/checkout/prepare",
        params={"intent_id": int(intent_id)},
        body={
            "buyer_id": buyer_id,
            "agent_id": agent_id or DEFAULT_AGENT_ID,
            "session_id": session_id or DEFAULT_SESSION_ID,
        },
    )


@mcp.tool()
async def get_order_status(
    internal_order_id: str,
    agent_id: str = DEFAULT_AGENT_ID,
    session_id: str = DEFAULT_SESSION_ID,
) -> str:
    """Read the status of a RAYBOOST internal checkout order."""
    return await _request(
        "GET",
        f"/api/agent/orders/{internal_order_id}",
        params={
            "agent_id": agent_id or DEFAULT_AGENT_ID,
            "session_id": session_id or DEFAULT_SESSION_ID,
        },
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
