import json
from collections import Counter, defaultdict
from .db import products, buyer_events, record_buyer_event, get_product_by_id
from .agent import build_buyer_plan

EVENT_WEIGHTS = {
    "search": 1,
    "recommendation_view": 2,
    "cart_add": 4,
    "purchase": 6,
}


def _catalog_map():
    return {p["id"]: p for p in products()}


def build_buyer_profile(buyer_id):
    catalog = _catalog_map()
    events = buyer_events(buyer_id, 250)
    product_scores = Counter()
    categories = Counter()
    use_cases = Counter()
    search_terms = Counter()
    spend = 0
    purchase_count = 0
    cart_adds = 0

    for e in events:
        weight = EVENT_WEIGHTS.get(e["event_type"], 1)
        pid = e.get("product_id")
        if pid and pid in catalog:
            product_scores[pid] += weight
            p = catalog[pid]
            categories[p["category"]] += weight
            for u in p.get("use_cases", []):
                use_cases[u] += weight
        if e.get("query"):
            for token in str(e["query"]).lower().split():
                if len(token) >= 3:
                    search_terms[token] += 1
        if e["event_type"] == "purchase":
            purchase_count += 1
            spend += int((e.get("meta") and json.loads(e["meta"]).get("amount", 0)) or 0)
        elif e["event_type"] == "cart_add":
            cart_adds += 1

    top_products = [
        {"id": pid, "name": catalog[pid]["name"], "score": score}
        for pid, score in product_scores.most_common(5) if pid in catalog
    ]
    return {
        "buyer_id": buyer_id,
        "events": len(events),
        "top_products": top_products,
        "preferred_categories": [x[0] for x in categories.most_common(3)],
        "preferred_use_cases": [x[0] for x in use_cases.most_common(5)],
        "recent_searches": [x[0] for x in search_terms.most_common(6)],
        "cart_adds": cart_adds,
        "purchases": purchase_count,
        "tracked_spend": spend,
        "data_note": "Behavior signals are based only on this buyer's RAYBOOST events.",
    }


def _affinity_from_events(buyer_id):
    # Build co-interest from products appearing in the same buyer journey.
    events = buyer_events(buyer_id, 250)
    by_type = defaultdict(set)
    for e in events:
        if e.get("product_id"):
            by_type[e["event_type"]].add(e["product_id"])
    # Stronger signals come from cart additions and purchases.
    strong = set(by_type.get("cart_add", set())) | set(by_type.get("purchase", set()))
    return strong


def personalized_recommendations(buyer_id, query=""):
    catalog = _catalog_map()
    profile = build_buyer_profile(buyer_id)
    base = build_buyer_plan(query) if query.strip() else {"products": [], "bundle": [], "budget": 0, "intent": ""}
    base_ids = {p["id"] for p in base.get("products", [])}
    history = {x["id"]: x["score"] for x in profile["top_products"]}
    preferred_categories = set(profile["preferred_categories"])
    preferred_uses = set(profile["preferred_use_cases"])
    strong = _affinity_from_events(buyer_id)

    candidates = list(catalog.values())
    scored=[]
    for p in candidates:
        score = 0.0
        reasons=[]
        if p["id"] in base_ids:
            score += 50; reasons.append("matches your current request")
        if p["id"] in history:
            score += min(18, history[p["id"]] * 2); reasons.append("matches your previous activity")
        if p["category"] in preferred_categories:
            score += 8; reasons.append("fits a category you use often")
        matching_uses = preferred_uses.intersection(set(p.get("use_cases", [])))
        if matching_uses:
            score += min(8, 2 * len(matching_uses)); reasons.append("fits your preferred use cases")
        if p["id"] in strong:
            score += 10; reasons.append("you showed strong interest in it")
        # Do not rank by merchant margin; buyer relevance stays the primary signal.
        scored.append((score, p["price"], p, reasons))

    scored.sort(key=lambda x: (-x[0], x[1]))
    selected=[]
    seen_categories=set()
    for score, price, p, reasons in scored:
        if not reasons:
            continue
        # Keep recommendations diverse unless the request explicitly produced a tight set.
        if p["category"] in seen_categories and len(selected) >= 3:
            continue
        selected.append({**p, "recommendation_score": round(score,1), "why": "; ".join(reasons[:3])})
        seen_categories.add(p["category"])
        if len(selected) >= 5:
            break
    return selected


def build_personalized_plan(buyer_id, query):
    profile = build_buyer_profile(buyer_id)
    recs = personalized_recommendations(buyer_id, query)
    base = build_buyer_plan(query)
    allowed_ids = {p["id"] for p in recs}
    bundle = [p for p in base.get("bundle", []) if p["id"] in allowed_ids]
    if not bundle and recs:
        bundle = recs[:2]
    return {
        "budget": base.get("budget", 0),
        "intent": base.get("intent", query),
        "products": recs,
        "bundle": bundle,
        "total": sum(int(p["price"]) for p in bundle),
        "personalized": bool(profile["events"]),
        "profile_signals": {
            "preferred_categories": profile["preferred_categories"],
            "preferred_use_cases": profile["preferred_use_cases"],
            "recent_searches": profile["recent_searches"],
        },
    }


def track_event(buyer_id, event_type, product_id=None, query=None, meta=None):
    record_buyer_event(buyer_id, event_type, product_id, query, meta)
    return {"ok": True}
