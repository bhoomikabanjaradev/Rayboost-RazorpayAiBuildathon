import json


def build_campaign_plan(opportunity, products):
    by_id={p["id"]:p for p in products}
    ids=opportunity.get("products",[])
    primary=by_id.get(ids[0]) if ids else None
    secondary=by_id.get(ids[1]) if len(ids)>1 else None
    discount=int(opportunity.get("suggested_discount",5))
    discount=min(10,max(0,discount))
    return {
        "name": f"{secondary['name'] if secondary else 'Product'} Cross-sell",
        "product_ids": ids,
        "discount": discount,
        "target": f"Customers who purchased {primary['name']}" if primary else "Relevant existing customers",
        "message": f"Complete your setup: pair {primary['name']} with {secondary['name']} and save {discount}%." if primary and secondary else "Discover a complementary product.",
        "reason": opportunity.get("reason", "Approved revenue opportunity"),
        "guardrails": {"max_discount":10,"merchant_approval_required":True,"execution_mode":"test"}
    }

def validate_campaign_plan(plan):
    errors=[]
    if not plan.get("product_ids"): errors.append("Campaign must target at least one product")
    if int(plan.get("discount",0))>10: errors.append("Discount exceeds the 10% policy")
    if not plan.get("target"): errors.append("Campaign target is required")
    return {"allowed":not errors,"errors":errors}
