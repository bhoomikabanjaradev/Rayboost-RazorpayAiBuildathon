"""RAYBOOST Upgrade 08 — Agent Commerce Gateway.

A thin, agent-facing interaction layer over the already-built RAYBOOST
catalogue, cart, buyer-intelligence, purchase-intent and checkout services.
It deliberately does not bypass the Upgrade 02/06 policy and confirmation gates.
"""
import uuid
import json
from .db import (
    products, get_product_by_id, get_cart, save_cart, get_checkout_by_cart,
    get_purchase_intent, checkout_orders, audit,
)
from .buyer_intelligence import build_buyer_profile, personalized_recommendations, track_event
from .checkout_agent import build_purchase_summary, cart_fingerprint
from .policy import check_discount, check_order

GATEWAY_VERSION = "1.0"
MERCHANT_NAME = "RAYBOOST Demo Store"


def capability_manifest():
    return {
        "gateway": "RAYBOOST Agent Commerce Gateway",
        "version": GATEWAY_VERSION,
        "protocol": "REST/JSON",
        "interop": "MCP-compatible tool semantics; this is RAYBOOST gateway code, not the official Razorpay MCP server.",
        "merchant": MERCHANT_NAME,
        "description": "Machine-readable commerce capabilities for AI buyers.",
        "environment": "test",
        "capabilities": [
            {"name": "product_search", "method": "GET", "path": "/api/agent/catalog/search", "money_action": False},
            {"name": "product_details", "method": "GET", "path": "/api/agent/products/{product_id}", "money_action": False},
            {"name": "related_products", "method": "GET", "path": "/api/agent/products/{product_id}/related", "money_action": False},
            {"name": "buyer_recommendations", "method": "GET", "path": "/api/agent/recommendations", "money_action": False},
            {"name": "create_cart", "method": "POST", "path": "/api/agent/carts", "money_action": False},
            {"name": "add_to_cart", "method": "POST", "path": "/api/agent/carts/{cart_id}/items", "money_action": False},
            {"name": "get_cart", "method": "GET", "path": "/api/agent/carts/{cart_id}", "money_action": False},
            {"name": "create_purchase_intent", "method": "POST", "path": "/api/agent/purchase-intents", "money_action": True, "requires": "buyer_confirmation_before_checkout"},
            {"name": "get_purchase_summary", "method": "GET", "path": "/api/agent/purchase-intents/{intent_id}", "money_action": True, "requires": "buyer_confirmation_before_checkout"},
            {"name": "confirm_purchase", "method": "POST", "path": "/api/agent/purchase-intents/{intent_id}/confirm", "money_action": True, "requires": "explicit_buyer_confirmation"},
            {"name": "prepare_checkout", "method": "POST", "path": "/api/agent/checkout/prepare", "money_action": True, "requires": "confirmed_purchase_intent"},
            {"name": "order_status", "method": "GET", "path": "/api/agent/orders/{internal_order_id}", "money_action": False},
            {"name": "merchant_capabilities", "method": "GET", "path": "/api/agent/capabilities", "money_action": False},
        ],
        "payment": {
            "provider": "Razorpay",
            "environment": "test",
            "checkout": "Razorpay Test Checkout when keys are configured; safe demo fallback otherwise",
        },
        "policies": {
            "max_discount_percent": 10,
            "max_order_amount_inr": 100000,
            "buyer_confirmation_required": True,
            "payment_signature_verification_required": True,
            "campaign_execution_requires_merchant_approval": True,
        },
        "safety": {
            "autonomous_payment": False,
            "server_side_price_recalculation": True,
            "cart_fingerprint_lock": True,
            "audit_every_gateway_action": True,
        },
    }


def _clean_query(q: str) -> str:
    return (q or "").strip().lower()


def search_products(query: str = "", category: str | None = None, limit: int = 10):
    q = _clean_query(query)
    category_q = _clean_query(category or "")
    rows = []
    for p in products():
        haystack = " ".join([
            p.get("name", ""), p.get("description", ""), p.get("category", ""),
            " ".join(p.get("use_cases", [])), " ".join(p.get("compatible_with", [])),
        ]).lower()
        if q and not all(token in haystack for token in q.split()):
            continue
        if category_q and category_q != str(p.get("category", "")).lower():
            continue
        rows.append({k: p[k] for k in p.keys() if k != "margin"})
    return rows[:max(1, min(int(limit), 25))]


def product_detail(product_id: str):
    p = get_product_by_id(product_id)
    if not p:
        return None
    return {k: p[k] for k in p.keys() if k != "margin"}


def related_products(product_id: str):
    p = get_product_by_id(product_id)
    if not p:
        return None
    ids = p.get("frequently_bought_with", []) or p.get("compatible_with", [])
    out = []
    for pid in ids:
        q = product_detail(pid)
        if q:
            out.append(q)
    return out


def new_agent_cart(buyer_id: str, agent_id: str, session_id: str | None = None):
    cart_id = f"agcart_{uuid.uuid4().hex[:18]}"
    save_cart(cart_id, [])
    audit(
        "agent_gateway", "gateway.cart.create", "External agent created a merchant-side cart",
        status="SUCCESS", meta={"cart_id": cart_id, "buyer_id": buyer_id, "agent_id": agent_id, "session_id": session_id},
    )
    return {"cart_id": cart_id, "buyer_id": buyer_id, "agent_id": agent_id, "items": [], "total": 0, "count": 0}


def cart_snapshot_gateway(cart_id: str):
    raw = get_cart(cart_id)
    catalog = {p["id"]: p for p in products()}
    items = []
    total = 0
    for row in raw:
        p = catalog.get(row.get("product_id"))
        if not p:
            continue
        qty = int(row.get("qty", 0))
        line = int(p["price"]) * qty
        total += line
        items.append({k: p[k] for k in p.keys() if k != "margin"} | {"qty": qty, "line_total": line})
    return {"cart_id": cart_id, "items": items, "total": total, "count": sum(x["qty"] for x in items)}


def add_cart_item(cart_id: str, product_id: str, qty: int, buyer_id: str, agent_id: str, session_id: str | None = None):
    p = get_product_by_id(product_id)
    if not p:
        return None, "Product not found"
    if qty < 1 or qty > 20:
        return None, "Quantity must be between 1 and 20"
    raw = get_cart(cart_id)
    existing = next((x for x in raw if x.get("product_id") == product_id), None)
    current = int(existing.get("qty", 0)) if existing else 0
    if current + qty > int(p.get("stock", 0)):
        return None, f"Only {p['stock']} units are available"
    if existing:
        existing["qty"] = current + qty
    else:
        raw.append({"product_id": product_id, "qty": qty})
    save_cart(cart_id, raw)
    track_event(buyer_id, "cart_add", product_id, meta={"cart_id": cart_id, "qty": qty, "amount": int(p["price"]) * qty, "agent_id": agent_id, "session_id": session_id})
    snap = cart_snapshot_gateway(cart_id)
    audit(
        "agent_gateway", "gateway.cart.add", f"External agent added {p['name']} to cart",
        amount=int(p["price"]) * qty, status="SUCCESS",
        meta={"cart_id": cart_id, "product_id": product_id, "qty": qty, "buyer_id": buyer_id, "agent_id": agent_id, "session_id": session_id},
    )
    return snap, None


def gateway_recommendations(buyer_id: str, query: str = ""):
    recs = personalized_recommendations(buyer_id, query)
    profile = build_buyer_profile(buyer_id)
    return {"buyer_id": buyer_id, "query": query, "personalized": bool(profile.get("events")), "recommendations": recs}


def purchase_intent_view(intent: dict):
    items = intent.get("items") or []
    subtotal = int(intent.get("subtotal", 0))
    discount = int(intent.get("discount_percent", 0))
    summary = build_purchase_summary(items, subtotal, discount)
    return {
        "intent_id": intent["id"],
        "buyer_id": intent["buyer_id"],
        "cart_id": intent["cart_id"],
        "state": intent["state"],
        "status": intent["status"],
        "summary": summary,
        "fingerprint": intent.get("fingerprint"),
        "created_at": intent.get("created_at"),
        "updated_at": intent.get("updated_at"),
    }


def order_status(internal_order_id: str):
    for row in checkout_orders(limit=200):
        if row.get("internal_order_id") == internal_order_id:
            return row
    return None


def gateway_safety_summary():
    return {
        "price_source": "server-side merchant catalogue",
        "discount": check_discount(0),
        "order_limit": check_order(1),
        "human_gate": "Purchase intent must be explicitly confirmed before checkout preparation.",
        "payment_gate": "Razorpay payment is verified server-side before PAID status is recorded.",
        "failure_behavior": "Failed payment preserves the cart and supports safe retry.",
    }
