import os
import re
from typing import Dict, List

from .db import products, get_product_by_id, audit
from .policy import check_discount

try:
    from google.adk.agents import Agent
    from google.adk.models import Gemini
    from google.genai import types
    ADK_AVAILABLE = True
except Exception:
    ADK_AVAILABLE = False


def _tokens(text: str) -> List[str]:
    return [x for x in re.findall(r"[a-zA-Z0-9]+", text.lower()) if len(x) > 1]


def search_catalogue(query: str, budget: int = 0) -> Dict:
    """Tool: search the merchant's AI-readable catalogue using intent, use-cases and budget."""
    q = query.lower()
    tokens = _tokens(query)
    scored = []

    for p in products():
        text = " ".join([
            p["name"], p["category"], p.get("description", ""),
            " ".join(p.get("use_cases", []))
        ]).lower()
        score = sum(2 for t in tokens if t in text)

        if any(x in q for x in ["laptop", "coding", "developer", "development", "college"]):
            if p["category"] == "laptops":
                score += 6
        if any(x in q for x in ["setup", "kit", "bundle"]):
            if p["id"] in {"p1", "p2", "p3", "p6"}:
                score += 2
        if budget:
            if p["price"] <= budget:
                score += 2
            else:
                score -= 1
        if p.get("stock", 0) <= 0:
            score -= 100

        if score > 0:
            scored.append((score, p))

    scored.sort(key=lambda x: (-x[0], x[1]["price"]))
    result = [p for _, p in scored[:8]]
    audit("catalog_agent", "catalog.search", f"Searched AI-readable catalogue for: {query}", status="SUCCESS", meta={"result_count": len(result)})
    return {"query": query, "budget": budget, "products": result}


def get_product(product_id: str) -> Dict:
    """Tool: retrieve one exact catalogue product."""
    p = get_product_by_id(product_id)
    if not p:
        return {"found": False, "product_id": product_id}
    return {"found": True, "product": p}


def get_related_products(product_id: str, limit: int = 3) -> Dict:
    """Tool: find products frequently bought with or compatible with a product."""
    p = get_product_by_id(product_id)
    if not p:
        return {"products": []}
    ids = list(dict.fromkeys(p.get("frequently_bought_with", []) + p.get("compatible_with", [])))
    related = [get_product_by_id(x) for x in ids]
    related = [x for x in related if x and x.get("stock", 0) > 0]
    return {"source_product_id": product_id, "products": related[:limit]}


def extract_budget(query: str) -> int:
    """Extract common INR budget expressions such as 70k, 70000, or ₹70,000."""
    q = query.lower().replace(",", "")
    m = re.search(r"(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)\s*(k|thousand|lakh|l)?", q)
    if not m:
        return 0
    value = float(m.group(1))
    unit = m.group(2) or ""
    if unit in {"k", "thousand"}:
        value *= 1000
    elif unit in {"lakh", "l"}:
        value *= 100000
    return int(value)


def build_buyer_plan(message: str) -> Dict:
    """Deterministic buyer-agent tool pipeline. It produces grounded recommendations before the LLM writes prose."""
    budget = extract_budget(message)
    search = search_catalogue(message, budget)
    matches = search["products"]

    if not matches:
        return {"products": [], "budget": budget, "intent": "purchase", "bundle": [], "total": 0}

    primary = matches[0]
    related = get_related_products(primary["id"], 3)["products"]

    bundle = [primary]
    running = primary["price"]
    for p in related:
        if p["id"] in {x["id"] for x in bundle}:
            continue
        if budget and running + p["price"] > budget:
            continue
        bundle.append(p)
        running += p["price"]
        if len(bundle) >= 3:
            break

    audit(
        "buyer_agent", "recommendation.plan",
        f"Built grounded recommendation for buyer request: {message}",
        amount=running, status="SUCCESS",
        meta={"budget": budget, "product_ids": [x["id"] for x in bundle]},
    )
    return {
        "products": matches,
        "budget": budget,
        "intent": "purchase",
        "primary": primary,
        "bundle": bundle,
        "total": running,
    }


def policy_check(discount: int) -> Dict:
    """Tool: check a proposed discount against merchant policy."""
    return check_discount(discount)


def log_agent_action(action: str, reason: str) -> str:
    """Tool: write an explainable action to the audit trail."""
    audit("buyer_agent", action, reason, status="SUCCESS")
    return "Audit event recorded."


def build_agent():
    if not ADK_AVAILABLE or not os.getenv("GOOGLE_API_KEY"):
        return None
    model = os.getenv("MODEL", "gemini-2.5-flash")
    return Agent(
        name="rayboost_buyer_agent",
        model=Gemini(model=model, retry_options=types.HttpRetryOptions(attempts=2)),
        instruction=(
            "You are RAYBOOST Buyer Agent. Ground every product fact in catalogue tools. "
            "Never invent products, prices, stock, discounts or payment status. "
            "The deterministic buyer plan is the source of truth for product selection. "
            "Explain recommendations briefly and never claim an order or payment occurred unless the backend confirms it."
        ),
        tools=[search_catalogue, get_product, get_related_products, policy_check, log_agent_action],
    )


root_agent = build_agent()
