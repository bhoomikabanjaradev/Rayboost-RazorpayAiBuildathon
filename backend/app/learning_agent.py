"""RAYBOOST Upgrade 09 — Agent Learning & Experimentation Engine.

Audit-backed experimentation layer. It does not create campaigns, spend money,
or change prices. It records experiment hypotheses, variant exposure and
conversion outcomes, evaluates results, and promotes a learned strategy as a
merchant decision signal for future Growth Agent reasoning.
"""
import json
import uuid
from collections import defaultdict
from .db import conn, audit, checkout_orders

MAX_VARIANTS = 4
MIN_EXPOSURES_FOR_WINNER = 5


def _loads(value, default=None):
    try:
        return json.loads(value) if value else (default if default is not None else {})
    except Exception:
        return default if default is not None else {}


def _audit_rows(limit=3000):
    c = conn()
    try:
        return [dict(r) for r in c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
    finally:
        c.close()


def _learning_rows():
    return [a for a in _audit_rows() if str(a.get("action", "")).startswith("learning.")]


def _new_id():
    return "exp-" + uuid.uuid4().hex[:12]


def _experiment_from_create(row):
    meta = _loads(row.get("meta"), {})
    return {
        "id": meta.get("experiment_id"),
        "name": meta.get("name", "Unnamed experiment"),
        "hypothesis": meta.get("hypothesis", ""),
        "opportunity_id": meta.get("opportunity_id"),
        "campaign_id": meta.get("campaign_id"),
        "variants": meta.get("variants", []),
        "status": meta.get("status", "DRAFT"),
        "created_at": row.get("created_at"),
        "created_by": meta.get("created_by", "merchant"),
    }


def _latest_experiment_rows():
    creates = {}
    latest_status = {}
    for row in reversed(_learning_rows()):
        action = row.get("action")
        meta = _loads(row.get("meta"), {})
        eid = meta.get("experiment_id")
        if not eid:
            continue
        if action == "learning.experiment.create":
            creates[eid] = _experiment_from_create(row)
        elif action in {"learning.experiment.start", "learning.experiment.complete", "learning.experiment.promote"}:
            latest_status[eid] = meta.get("status") or row.get("status")
    out = []
    for eid, exp in creates.items():
        if eid in latest_status:
            exp["status"] = latest_status[eid]
        out.append(exp)
    return out


def list_experiments():
    experiments = _latest_experiment_rows()
    metrics = {x["experiment_id"]: x for x in _all_metrics()}
    for exp in experiments:
        exp["metrics"] = metrics.get(exp["id"], _empty_metrics(exp))
    return sorted(experiments, key=lambda x: x.get("created_at") or "", reverse=True)


def _empty_metrics(exp):
    return {
        "experiment_id": exp["id"],
        "variants": [
            {"id": v["id"], "name": v.get("name", v["id"]), "exposures": 0, "conversions": 0,
             "conversion_rate": 0, "revenue": 0, "revenue_per_exposure": 0}
            for v in exp.get("variants", [])
        ],
        "total_exposures": 0,
        "total_conversions": 0,
        "total_revenue": 0,
        "winner": None,
        "evaluation": "INSUFFICIENT_DATA",
    }


def _event_rows(action):
    return [x for x in _learning_rows() if x.get("action") == action]


def _all_metrics():
    experiments = {x["id"]: x for x in _latest_experiment_rows()}
    exposures = defaultdict(list)
    conversions = defaultdict(list)
    for row in _event_rows("learning.exposure"):
        m = _loads(row.get("meta"), {})
        if m.get("experiment_id") and m.get("variant_id"):
            exposures[m["experiment_id"]].append(m)
    for row in _event_rows("learning.conversion"):
        m = _loads(row.get("meta"), {})
        if m.get("experiment_id") and m.get("variant_id"):
            conversions[m["experiment_id"]].append(m)

    out = []
    for eid, exp in experiments.items():
        exp_exposures = exposures.get(eid, [])
        exp_conversions = conversions.get(eid, [])
        variant_stats = []
        for v in exp.get("variants", []):
            vid = v["id"]
            ev = [x for x in exp_exposures if x.get("variant_id") == vid]
            cv = [x for x in exp_conversions if x.get("variant_id") == vid]
            # A buyer is counted once per variant for exposure; duplicate conversion
            # events from the same buyer are ignored for the metric.
            buyers = set()
            unique_exposure = []
            for x in ev:
                b = x.get("buyer_id") or ("anon-" + str(x.get("exposure_id")))
                if b not in buyers:
                    buyers.add(b)
                    unique_exposure.append(x)
            converted_buyers = set()
            unique_conversions = []
            for x in cv:
                b = x.get("buyer_id") or ("anon-" + str(x.get("conversion_id")))
                if b not in converted_buyers:
                    converted_buyers.add(b)
                    unique_conversions.append(x)
            revenue = sum(float(x.get("revenue", 0) or 0) for x in unique_conversions)
            n = len(unique_exposure)
            c = len(unique_conversions)
            rate = c / n * 100 if n else 0
            variant_stats.append({
                "id": vid, "name": v.get("name", vid), "exposures": n,
                "conversions": c, "conversion_rate": round(rate, 2),
                "revenue": round(revenue, 2),
                "revenue_per_exposure": round(revenue / n, 2) if n else 0,
                "discount_percent": v.get("discount_percent", 0),
            })
        winner = None
        evaluation = "INSUFFICIENT_DATA"
        eligible = [x for x in variant_stats if x["exposures"] >= MIN_EXPOSURES_FOR_WINNER]
        if len(eligible) >= 2:
            winner = max(eligible, key=lambda x: (x["conversion_rate"], x["revenue_per_exposure"], x["conversions"]))
            evaluation = "WINNER_IDENTIFIED"
        elif variant_stats and all(x["exposures"] > 0 for x in variant_stats):
            evaluation = "COLLECTING_DATA"
        out.append({
            "experiment_id": eid,
            "variants": variant_stats,
            "total_exposures": sum(x["exposures"] for x in variant_stats),
            "total_conversions": sum(x["conversions"] for x in variant_stats),
            "total_revenue": round(sum(x["revenue"] for x in variant_stats), 2),
            "winner": winner,
            "evaluation": evaluation,
        })
    return out


def get_experiment(experiment_id):
    exp = next((x for x in _latest_experiment_rows() if x["id"] == experiment_id), None)
    if not exp:
        return None
    metrics = next((x for x in _all_metrics() if x["experiment_id"] == experiment_id), _empty_metrics(exp))
    exp["metrics"] = metrics
    return exp


def create_experiment(name, hypothesis, variants, opportunity_id=None, campaign_id=None):
    if not name.strip():
        raise ValueError("Experiment name is required")
    if not hypothesis.strip():
        raise ValueError("Experiment hypothesis is required")
    if not isinstance(variants, list) or len(variants) < 2 or len(variants) > MAX_VARIANTS:
        raise ValueError(f"An experiment must have 2 to {MAX_VARIANTS} variants")
    normalized = []
    seen = set()
    for i, v in enumerate(variants):
        vid = str(v.get("id") or f"v{i+1}").strip()
        if vid in seen:
            raise ValueError("Variant IDs must be unique")
        seen.add(vid)
        discount = int(v.get("discount_percent", 0) or 0)
        if discount < 0 or discount > 10:
            raise ValueError("Experiment discounts must stay within the 10% policy")
        normalized.append({
            "id": vid,
            "name": str(v.get("name") or vid),
            "discount_percent": discount,
            "message": str(v.get("message") or ""),
        })
    eid = _new_id()
    audit("learning_agent", "learning.experiment.create", "Created a bounded merchant experiment draft", status="DRAFT",
          meta={"experiment_id": eid, "name": name.strip(), "hypothesis": hypothesis.strip(),
                "variants": normalized, "opportunity_id": opportunity_id, "campaign_id": campaign_id,
                "status": "DRAFT", "created_by": "merchant"})
    return get_experiment(eid)


def start_experiment(experiment_id):
    exp = get_experiment(experiment_id)
    if not exp:
        raise ValueError("Experiment not found")
    if exp["status"] not in ("DRAFT", "RUNNING"):
        raise ValueError(f"Experiment cannot start from status {exp['status']}")
    if exp["status"] == "RUNNING":
        return exp
    audit("learning_agent", "learning.experiment.start", "Merchant started the experiment; no money action was triggered", status="ACTIVE",
          meta={"experiment_id": experiment_id, "status": "RUNNING"})
    return get_experiment(experiment_id)


def record_exposure(experiment_id, variant_id, buyer_id=None, agent_id="manual", session_id=None, source="manual"):
    exp = get_experiment(experiment_id)
    if not exp:
        raise ValueError("Experiment not found")
    if exp["status"] != "RUNNING":
        raise ValueError("Experiment must be RUNNING before exposure can be recorded")
    if variant_id not in {v["id"] for v in exp["variants"]}:
        raise ValueError("Unknown experiment variant")
    exposure_id = "ex-" + uuid.uuid4().hex[:12]
    audit("learning_agent", "learning.exposure", "Recorded an experiment variant exposure", status="SUCCESS",
          meta={"experiment_id": experiment_id, "variant_id": variant_id, "buyer_id": buyer_id,
                "agent_id": agent_id, "session_id": session_id, "source": source, "exposure_id": exposure_id})
    return get_experiment(experiment_id)


def record_conversion(experiment_id, variant_id, buyer_id=None, revenue=0, internal_order_id=None, source="manual"):
    exp = get_experiment(experiment_id)
    if not exp:
        raise ValueError("Experiment not found")
    if exp["status"] != "RUNNING":
        raise ValueError("Experiment must be RUNNING before conversion can be recorded")
    if variant_id not in {v["id"] for v in exp["variants"]}:
        raise ValueError("Unknown experiment variant")
    amount = float(revenue or 0)
    if internal_order_id:
        found = next((x for x in checkout_orders(limit=500) if x.get("internal_order_id") == internal_order_id), None)
        if not found:
            raise ValueError("Internal order not found")
        if found.get("status") != "PAID":
            raise ValueError("Only PAID orders can be recorded as experiment conversions")
        if buyer_id and found.get("buyer_id") and found.get("buyer_id") != buyer_id:
            raise ValueError("Order belongs to another buyer")
        buyer_id = buyer_id or found.get("buyer_id")
        amount = float(found.get("amount") or 0)
    if amount < 0:
        raise ValueError("Revenue cannot be negative")
    conversion_id = "cv-" + uuid.uuid4().hex[:12]
    audit("learning_agent", "learning.conversion", "Recorded an experiment conversion outcome", status="SUCCESS", amount=amount,
          meta={"experiment_id": experiment_id, "variant_id": variant_id, "buyer_id": buyer_id,
                "revenue": round(amount, 2), "internal_order_id": internal_order_id,
                "source": source, "conversion_id": conversion_id})
    return get_experiment(experiment_id)


def evaluate_experiment(experiment_id):
    exp = get_experiment(experiment_id)
    if not exp:
        raise ValueError("Experiment not found")
    metrics = exp["metrics"]
    if metrics["evaluation"] == "WINNER_IDENTIFIED":
        winner = metrics["winner"]
        explanation = (f"{winner['name']} leads on conversion rate at {winner['conversion_rate']}% "
                       f"with {winner['exposures']} exposures and {winner['conversions']} conversions.")
    elif metrics["evaluation"] == "COLLECTING_DATA":
        explanation = f"All variants have activity, but each needs at least {MIN_EXPOSURES_FOR_WINNER} exposures before a winner is declared."
    else:
        explanation = f"Collect at least {MIN_EXPOSURES_FOR_WINNER} exposures per variant before declaring a winner."
    return {"experiment": exp, "evaluation": metrics["evaluation"], "winner": metrics["winner"], "explanation": explanation,
            "minimum_exposures_per_variant": MIN_EXPOSURES_FOR_WINNER}


def promote_winner(experiment_id):
    exp = get_experiment(experiment_id)
    if not exp:
        raise ValueError("Experiment not found")
    winner = exp["metrics"].get("winner")
    if not winner:
        raise ValueError("A statistically useful winner is not available yet")
    audit("learning_agent", "learning.experiment.promote", "Merchant promoted the observed experiment winner as a future decision signal", status="PROMOTED",
          meta={"experiment_id": experiment_id, "variant_id": winner["id"], "variant_name": winner["name"],
                "conversion_rate": winner["conversion_rate"], "revenue_per_exposure": winner["revenue_per_exposure"],
                "status": "PROMOTED"})
    return get_experiment(experiment_id)


def learned_strategies(limit=8):
    out = []
    seen = set()
    for row in _learning_rows():
        if row.get("action") != "learning.experiment.promote":
            continue
        m = _loads(row.get("meta"), {})
        key = (m.get("experiment_id"), m.get("variant_id"))
        if key in seen:
            continue
        seen.add(key)
        exp = get_experiment(m.get("experiment_id")) if m.get("experiment_id") else None
        variant = next((v for v in (exp or {}).get("variants", []) if v.get("id") == m.get("variant_id")), {})
        out.append({"experiment_id": m.get("experiment_id"), "variant_id": m.get("variant_id"),
                    "experiment": (exp or {}).get("name"), "strategy": variant.get("name", m.get("variant_name")),
                    "discount_percent": variant.get("discount_percent", 0),
                    "conversion_rate": m.get("conversion_rate", 0),
                    "revenue_per_exposure": m.get("revenue_per_exposure", 0),
                    "reason": "Promoted by merchant after the experiment met the minimum evidence threshold."})
        if len(out) >= limit:
            break
    return out


def learning_overview():
    experiments = list_experiments()
    strategies = learned_strategies()
    running = sum(1 for x in experiments if x["status"] == "RUNNING")
    winners = sum(1 for x in experiments if x["metrics"]["winner"])
    total_exposure = sum(x["metrics"]["total_exposures"] for x in experiments)
    total_revenue = round(sum(x["metrics"]["total_revenue"] for x in experiments), 2)
    return {
        "summary": {"experiments": len(experiments), "running": running, "winners": winners,
                    "exposures": total_exposure, "experiment_revenue": total_revenue, "learned_strategies": len(strategies)},
        "experiments": experiments,
        "learned_strategies": strategies,
        "guardrails": {"max_discount_percent": 10, "minimum_exposures_per_variant": MIN_EXPOSURES_FOR_WINNER,
                       "automatic_spend": False, "merchant_promotion_required": True},
    }


def learning_signal_for_opportunity(opportunity):
    strategies = learned_strategies()
    title = str(opportunity.get("title", "")).lower()
    products = {str(x) for x in opportunity.get("products", [])}
    matches = []
    for s in strategies:
        exp = get_experiment(s["experiment_id"])
        if not exp:
            continue
        if exp.get("opportunity_id") == opportunity.get("id"):
            matches.append(s)
            continue
        if title and s.get("strategy", "").lower() in title:
            matches.append(s)
    if not matches:
        return {"available": False, "message": "No promoted experiment directly matches this opportunity yet."}
    best = max(matches, key=lambda x: (float(x.get("conversion_rate", 0)), float(x.get("revenue_per_exposure", 0))))
    return {"available": True, "message": "A previously promoted experiment provides a positive decision signal.", "strategy": best}
