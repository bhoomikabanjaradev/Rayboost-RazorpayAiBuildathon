"""Upgrade 07 — Agent Control Center & Decision Intelligence.

Read-only analytics over the existing audit, sales, buyer-event, campaign and
checkout tables. No money action is performed here.
"""
import json
from collections import Counter
from .db import conn

AGENTS = {
    "AI Buyer": {"actors": ["buyer_agent", "order_agent"], "description": "Discovery, recommendation and cart flow"},
    "Buyer Intelligence": {"actors": ["buyer_intelligence"], "description": "First-party preference and personalization"},
    "Growth Agent": {"actors": ["growth_agent"], "description": "Merchant revenue analysis and opportunities"},
    "Campaign Agent": {"actors": ["campaign_agent"], "description": "Approved campaign planning and test execution"},
    "Checkout Agent": {"actors": ["checkout_agent", "razorpay"], "description": "Purchase confirmation, payment and safe failure handling"},
    "Learning Agent": {"actors": ["learning_agent"], "description": "Experiment measurement, winner evaluation and reusable decision signals"},
}

ACTION_WHY = {
    "buyer.search": "The buyer asked for a product or use case, so the agent searched the merchant catalogue.",
    "buyer.recommendations": "Recommendations were generated from catalogue relevance and available first-party buyer activity.",
    "cart.add": "The buyer explicitly added a catalogue product to the cart.",
    "purchase.intent": "A purchase review was created so the exact cart, price and policy could be checked before payment.",
    "purchase.confirm": "The buyer confirmed the exact purchase summary before the money action was unlocked.",
    "growth.summary": "The Growth Agent analyzed the merchant sales ledger instead of inventing revenue numbers.",
    "growth.opportunities": "The Growth Agent looked for evidence-backed product relationships in the sales ledger.",
    "growth.proposal_decision": "The merchant approved or rejected the proposed growth action; no automatic spend was triggered by the decision.",
    "campaign.draft_created": "A campaign draft was created only after an approved growth opportunity was found.",
    "campaign.execute": "Campaign execution was gated by merchant approval, the 10% discount cap and test-mode execution.",
    "checkout.prepare": "A checkout order was prepared only after server-side cart validation and buyer confirmation.",
    "payment.verify": "The payment result was accepted only after Razorpay signature verification.",
    "payment.failed": "The payment failed safely; the cart remains available for retry.",
    "payment.success": "The test payment completed and the order was recorded as paid.",
    "learning.experiment.create": "A bounded experiment was created to compare strategies using measured outcomes rather than agent guesses.",
    "learning.experiment.start": "The merchant started an experiment; this records analytics only and does not spend money.",
    "learning.exposure": "A buyer or test participant was assigned an experiment variant so its performance can be measured.",
    "learning.conversion": "A measured conversion outcome was attached to a variant to calculate performance.",
    "learning.experiment.promote": "The merchant promoted a winner after the minimum evidence threshold was met, making it a future decision signal.",
}


def _loads(value, default=None):
    try:
        return json.loads(value) if value else (default if default is not None else {})
    except Exception:
        return default if default is not None else {}


def _audit_rows(limit=250):
    c = conn()
    try:
        return [dict(r) for r in c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
    finally:
        c.close()


def _buyer_events(limit=1000):
    c = conn()
    try:
        return [dict(r) for r in c.execute("SELECT * FROM buyer_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
    finally:
        c.close()


def _checkout_rows(limit=500):
    c = conn()
    try:
        return [dict(r) for r in c.execute("SELECT * FROM checkout_orders ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
    finally:
        c.close()


def _sales_metrics():
    c = conn()
    try:
        rows = c.execute("SELECT order_id, product_id, amount, qty FROM sales ORDER BY id").fetchall()
    finally:
        c.close()
    orders = {}
    revenue = 0
    units = 0
    for r in rows:
        oid = str(r["order_id"])
        amount = float(r["amount"] or 0) * int(r["qty"] or 0)
        orders.setdefault(oid, 0)
        orders[oid] += amount
        revenue += amount
        units += int(r["qty"] or 0)
    count = len(orders)
    return {"revenue": round(revenue, 2), "orders": count, "units_sold": units,
            "average_order_value": round(revenue / count, 2) if count else 0}


def _paid_ai_metrics(checkouts):
    paid = [x for x in checkouts if x.get("status") == "PAID"]
    ai = [x for x in paid if x.get("buyer_id")]
    revenue = sum(float(x.get("amount") or 0) for x in ai)
    return {
        "orders": len(ai),
        "revenue": round(revenue, 2),
        "buyers": len({x.get("buyer_id") for x in ai if x.get("buyer_id")}),
    }


def _campaign_performance(campaigns, buyer_events, checkouts):
    out = []
    for c in campaigns:
        ids = _loads(c.get("product_ids"), [])
        ids = {str(x) for x in ids}
        views = [e for e in buyer_events if e.get("event_type") == "recommendation_view" and str(e.get("product_id")) in ids]
        carts = [e for e in buyer_events if e.get("event_type") == "cart_add" and str(e.get("product_id")) in ids]
        purchases = [e for e in buyer_events if e.get("event_type") == "purchase"]
        matching_purchase_buyers = set()
        attributed_revenue = 0.0
        purchased_orders = 0
        for order in checkouts:
            if order.get("status") != "PAID":
                continue
            items = _loads(order.get("items"), [])
            matching = [i for i in items if str(i.get("product_id")) in ids]
            if not matching:
                continue
            purchased_orders += 1
            if order.get("buyer_id"):
                matching_purchase_buyers.add(order.get("buyer_id"))
            total_lines = sum(float(i.get("line_total") or (i.get("unit_price", 0) * i.get("qty", 0))) for i in items)
            matched_lines = sum(float(i.get("line_total") or (i.get("unit_price", 0) * i.get("qty", 0))) for i in matching)
            share = (matched_lines / total_lines) if total_lines else 1
            attributed_revenue += float(order.get("amount") or 0) * share
        purchase_events = [e for e in purchases if e.get("buyer_id") in matching_purchase_buyers]
        targeted_buyers = {e.get("buyer_id") for e in views + carts if e.get("buyer_id")}
        out.append({
            "id": c.get("id"), "name": c.get("name"), "status": c.get("status"), "discount": c.get("discount", 0),
            "targeted": len(targeted_buyers), "viewed": len(views), "cart_adds": len(carts),
            "purchased": purchased_orders, "purchase_buyers": len(matching_purchase_buyers),
            "purchase_events": len(purchase_events), "attributed_revenue": round(attributed_revenue, 2),
            "products": sorted(ids),
            "note": "Test-store attribution inferred from buyer events and paid checkout line items."
        })
    return out


def _agent_activity(audits):
    result = []
    for name, spec in AGENTS.items():
        rows = [a for a in audits if a.get("actor") in spec["actors"]]
        counts = Counter(a.get("action") for a in rows)
        result.append({
            "name": name,
            "description": spec["description"],
            "status": "ACTIVE" if rows else "READY",
            "events": len(rows),
            "last_action": rows[0].get("action") if rows else "—",
            "last_status": rows[0].get("status") if rows else "—",
            "top_actions": [{"action": k, "count": v} for k, v in counts.most_common(4)],
        })
    return result


def _timeline(audits, limit=24):
    result = []
    for a in audits[:limit]:
        meta = _loads(a.get("meta"), {})
        action = a.get("action") or "unknown"
        result.append({
            "id": a.get("id"), "actor": a.get("actor"), "action": action,
            "status": a.get("status"), "reason": a.get("reason"), "amount": a.get("amount", 0),
            "created_at": a.get("created_at"), "why": ACTION_WHY.get(action, "The action was recorded so the agent decision remains explainable."),
            "meta": meta,
        })
    return result


def _guardrails(audits):
    blocked = [a for a in audits if a.get("status") == "BLOCKED"]
    failed = [a for a in audits if a.get("status") == "FAILED"]
    return {
        "rules": [
            {"key": "discount", "label": "Maximum discount", "value": "10%", "state": "ENFORCED"},
            {"key": "order", "label": "Maximum automatic order", "value": "₹1,00,000", "state": "ENFORCED"},
            {"key": "confirmation", "label": "Buyer confirmation", "value": "Required before payment", "state": "ENFORCED"},
            {"key": "payment", "label": "Payment verification", "value": "Razorpay signature required", "state": "ENFORCED"},
            {"key": "campaign", "label": "Campaign approval", "value": "Merchant approval required", "state": "ENFORCED"},
        ],
        "blocked_actions": len(blocked),
        "failed_actions": len(failed),
        "recent_blocks": _timeline(blocked, 6),
    }


def _feedback_loop(audits, buyer_events, checkouts, campaigns):
    approved = sum(1 for a in audits if a.get("action") == "growth.proposal_decision" and a.get("status") == "APPROVED")
    campaign_runs = sum(1 for a in audits if a.get("action") == "campaign.execute" and a.get("status") == "SUCCESS")
    paid = sum(1 for x in checkouts if x.get("status") == "PAID" and x.get("buyer_id"))
    return [
        {"stage": "Opportunity", "count": sum(1 for a in audits if a.get("action") == "growth.opportunities")},
        {"stage": "Merchant decision", "count": approved},
        {"stage": "Campaign execution", "count": campaign_runs},
        {"stage": "Buyer interaction", "count": sum(1 for e in buyer_events if e.get("event_type") in ("recommendation_view", "cart_add"))},
        {"stage": "AI-assisted purchase", "count": paid},
        {"stage": "Experimentation", "count": sum(1 for a in audits if a.get("action") in ("learning.experiment.create", "learning.experiment.start"))},
        {"stage": "Performance data", "count": len(campaigns) + len(checkouts) + sum(1 for a in audits if a.get("action") in ("learning.exposure", "learning.conversion"))},
    ]


def build_control_center():
    audits = _audit_rows()
    events = _buyer_events()
    checkouts = _checkout_rows()
    c = conn()
    try:
        campaigns = [dict(r) for r in c.execute("SELECT * FROM campaigns ORDER BY id DESC").fetchall()]
    finally:
        c.close()
    sales = _sales_metrics()
    ai = _paid_ai_metrics(checkouts)
    checkout_starts = sum(1 for e in events if e.get("event_type") == "checkout_start")
    conversion = (ai["orders"] / checkout_starts * 100) if checkout_starts else 0
    return {
        "agents": _agent_activity(audits),
        "revenue": {
            **sales,
            "ai_assisted_orders": ai["orders"],
            "ai_assisted_revenue": ai["revenue"],
            "ai_buyers": ai["buyers"],
            "checkout_starts": checkout_starts,
            "ai_checkout_conversion": round(conversion, 1),
            "campaign_count": len(campaigns),
        },
        "campaigns": _campaign_performance(campaigns, events, checkouts),
        "guardrails": _guardrails(audits),
        "timeline": _timeline(audits),
        "feedback_loop": _feedback_loop(audits, events, checkouts, campaigns),
        "generated_from": "existing RAYBOOST audit, sales, buyer-event, campaign and checkout data",
    }
