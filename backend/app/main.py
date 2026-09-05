import os, uuid, json, math
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv
load_dotenv()

from .db import init_db, products, audits, campaigns, create_campaign, create_campaign_draft, get_campaign_by_id, update_campaign_status, audit, get_cart, save_cart, clear_cart, get_product_by_id, get_checkout_by_cart, get_checkout_by_razorpay, create_checkout, update_checkout_status, checkout_orders, create_purchase_intent, get_purchase_intent, latest_purchase_intent, confirm_purchase_intent, update_purchase_intent_state
from .policy import check_discount, check_order
from .razorpay_service import create_order, verify_signature, configured
from .agent import root_agent
from .agent import search_catalogue, build_buyer_plan, get_product, get_related_products
from .growth_agent import explain_growth_opportunity
from .campaign_agent import build_campaign_plan, validate_campaign_plan
from .buyer_intelligence import build_buyer_profile, build_personalized_plan, personalized_recommendations, track_event
from .checkout_agent import cart_fingerprint, build_purchase_summary, validate_confirmed_intent
from .agent_control import build_control_center
from .commerce_gateway import (capability_manifest, search_products as gateway_search_products, product_detail as gateway_product_detail, related_products as gateway_related_products, new_agent_cart, cart_snapshot_gateway, add_cart_item as gateway_add_cart_item, gateway_recommendations, purchase_intent_view, order_status as gateway_order_status, gateway_safety_summary)
from .learning_agent import (learning_overview, list_experiments, get_experiment, create_experiment, start_experiment, record_exposure, record_conversion, evaluate_experiment, promote_winner, learned_strategies, learning_signal_for_opportunity)

# Keep existing revenue logic in this module so the original UI remains compatible.
def revenue_opportunities():
    """Backward-compatible endpoint data, now generated from the merchant sales table."""
    return {"opportunities": build_growth_opportunities()}

def _growth_data():
    c = conn()
    try:
        rows = c.execute("SELECT order_id, product_id, amount, qty FROM sales ORDER BY order_id").fetchall()
    finally:
        c.close()
    catalog = {p["id"]: p for p in products()}
    orders = {}
    units = {}
    revenue = 0.0
    for r in rows:
        oid, pid, amount, qty = r["order_id"], str(r["product_id"]), float(r["amount"] or 0), int(r["qty"] or 0)
        orders.setdefault(oid, set()).add(pid)
        units[pid] = units.get(pid, 0) + qty
        revenue += amount * qty
    return rows, orders, units, revenue, catalog

def build_growth_summary():
    rows, orders, units, revenue, catalog = _growth_data()
    order_count = len(orders)
    return {
        "revenue": round(revenue, 2),
        "orders": order_count,
        "units_sold": sum(units.values()),
        "average_order_value": round(revenue / order_count, 2) if order_count else 0,
        "products_sold": len(units),
        "data_source": "merchant sales ledger"
    }

def build_growth_opportunities():
    rows, orders, units, revenue, catalog = _growth_data()
    pair_counts = {}
    for ids in orders.values():
        ids = sorted(ids)
        for i, a in enumerate(ids):
            for b in ids[i+1:]:
                pair_counts[(a,b)] = pair_counts.get((a,b), 0) + 1
    out=[]
    for (a,b), co in sorted(pair_counts.items(), key=lambda x:x[1], reverse=True):
        pa, pb = catalog.get(a), catalog.get(b)
        if not pa or not pb: continue
        base = units.get(a,0)
        attached = sum(1 for ids in orders.values() if a in ids and b in ids)
        source_orders = sum(1 for ids in orders.values() if a in ids)
        rate = attached/source_orders*100 if source_orders else 0
        target = min(25.0, max(rate+8, rate*1.5))
        additional = max(0.0, source_orders*(target-rate)/100)
        potential = round(additional*float(pb['price'])*0.75,2)
        discount = min(10, max(3, round((1-float(pb.get('margin',0.25)))*4)))
        out.append({
            "id": f"cross-{a}-{b}", "type":"cross_sell",
            "title": f"{pa['name']} → {pb['name']}",
            "products":[a,b],
            "potential_monthly_revenue": potential,
            "suggested_discount": discount,
            "reason": f"{attached} of {source_orders} orders containing {pa['name']} also contain {pb['name']} ({rate:.1f}% attachment).",
            "metrics": {"source_orders":source_orders,"attached_orders":attached,"attachment_rate":round(rate,1),"target_attachment_rate":round(target,1),"estimated_monthly_revenue":potential,"co_purchases":co},
            "recommendation": {"primary_product":pa['name'],"secondary_product":pb['name'],"suggested_discount_percent":discount}
        })
        if len(out)>=5: break
    # If the small demo ledger has no pair, use catalogue relationships but label the estimate clearly.
    if not out:
        for p in catalog.values():
            for rid in p.get('frequently_bought_with',[]):
                q=catalog.get(rid)
                if q:
                    fallback_id=f"catalog-{p['id']}-{q['id']}"
                    out.append({"id":fallback_id,"type":"catalog_cross_sell","title":f"{p['name']} → {q['name']}","products":[p['id'],q['id']],"potential_monthly_revenue":round(float(q['price'])*2,2),"suggested_discount":5,"reason":f"Catalogue marks {q['name']} as frequently bought with {p['name']}; this is a test-store opportunity, not a historical sales claim.","metrics":{"source_orders":0,"attached_orders":0,"attachment_rate":0,"target_attachment_rate":10,"estimated_monthly_revenue":round(float(q['price'])*2,2)},"recommendation":{"primary_product":p['name'],"secondary_product":q['name'],"suggested_discount_percent":5},"learning_signal":learning_signal_for_opportunity({"id":fallback_id,"title":f"{p['name']} → {q['name']}","products":[p['id'],q['id']]})})
                    if len(out)>=5: return out
    return out

def conn():
    from .db import conn as _conn
    return _conn()


init_db()
app=FastAPI(title='RAYBOOST API', version='1.5.0')
app.add_middleware(CORSMiddleware, allow_origins=[os.getenv('FRONTEND_URL','http://localhost:5173')], allow_credentials=True, allow_methods=['*'], allow_headers=['*'])

class ChatIn(BaseModel):
    message: str
    mode: str='buyer'
    buyer_id: str | None = None

class CampaignIn(BaseModel):
    name: str
    product_ids: list[str]
    discount: int = Field(ge=0, le=100)
    reason: str

class CheckoutIn(BaseModel):
    cart_id: str
    discount_percent: int = Field(default=0, ge=0, le=10)
    buyer_id: str | None = None
    intent_id: int | None = None

class VerifyIn(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str

class PaymentFailedIn(BaseModel):
    razorpay_order_id: str
    reason: str = 'Razorpay reported payment failure'

class CartItemIn(BaseModel):
    product_id: str
    qty: int = Field(default=1, ge=1, le=20)

class CartIn(BaseModel):
    cart_id: str | None = None
    item: CartItemIn
    buyer_id: str | None = None

class CartReadIn(BaseModel):
    cart_id: str

class PurchaseIntentIn(BaseModel):
    buyer_id: str
    cart_id: str
    discount_percent: int = Field(default=0, ge=0, le=10)

class PurchaseConfirmIn(BaseModel):
    intent_id: int

class ExperimentVariantIn(BaseModel):
    id: str
    name: str
    discount_percent: int = Field(default=0, ge=0, le=10)
    message: str = ''

class ExperimentCreateIn(BaseModel):
    name: str
    hypothesis: str
    variants: list[ExperimentVariantIn]
    opportunity_id: str | None = None
    campaign_id: int | None = None

class ExposureIn(BaseModel):
    variant_id: str
    buyer_id: str | None = None
    agent_id: str = 'manual'
    session_id: str | None = None
    source: str = 'manual'

class ConversionIn(BaseModel):
    variant_id: str
    buyer_id: str | None = None
    revenue: float = Field(default=0, ge=0)
    internal_order_id: str | None = None
    source: str = 'manual'


@app.get('/api/health')
def health(): return {'ok':True,'razorpay_configured':configured(),'ai_configured':root_agent is not None}

@app.get('/api/catalog')
def catalog(): return {'products':products()}

@app.get('/api/opportunities')
def opportunities(): return revenue_opportunities()

@app.get('/api/audit')
def audit_list(): return {'events':audits()}

@app.get('/api/control-center')
def control_center():
    data=build_control_center()
    audit('control_center','control.center_view','Built unified agent decision and revenue intelligence view',status='SUCCESS')
    return data

@app.get('/api/learning/overview')
def learning_overview_api():
    data=learning_overview()
    audit('learning_agent','learning.overview','Built the experimentation and learning view',status='SUCCESS',meta=data.get('summary',{}))
    return data

@app.get('/api/learning/experiments')
def learning_experiments():
    return {'experiments':list_experiments()}

@app.post('/api/learning/experiments')
def learning_create(body: ExperimentCreateIn):
    try:
        exp=create_experiment(body.name,body.hypothesis,[v.model_dump() for v in body.variants],body.opportunity_id,body.campaign_id)
        return {'ok':True,'experiment':exp}
    except ValueError as e:
        raise HTTPException(400,str(e))

@app.get('/api/learning/experiments/{experiment_id}')
def learning_get(experiment_id: str):
    exp=get_experiment(experiment_id)
    if not exp: raise HTTPException(404,'Experiment not found')
    return {'ok':True,'experiment':exp}

@app.post('/api/learning/experiments/{experiment_id}/start')
def learning_start(experiment_id: str):
    try: return {'ok':True,'experiment':start_experiment(experiment_id)}
    except ValueError as e: raise HTTPException(400,str(e))

@app.post('/api/learning/experiments/{experiment_id}/exposure')
def learning_exposure(experiment_id: str, body: ExposureIn):
    try: return {'ok':True,'experiment':record_exposure(experiment_id,body.variant_id,body.buyer_id,body.agent_id,body.session_id,body.source)}
    except ValueError as e: raise HTTPException(400,str(e))

@app.post('/api/learning/experiments/{experiment_id}/conversion')
def learning_conversion(experiment_id: str, body: ConversionIn):
    try: return {'ok':True,'experiment':record_conversion(experiment_id,body.variant_id,body.buyer_id,body.revenue,body.internal_order_id,body.source)}
    except ValueError as e: raise HTTPException(400,str(e))

@app.post('/api/learning/experiments/{experiment_id}/evaluate')
def learning_evaluate(experiment_id: str):
    try: return {'ok':True,**evaluate_experiment(experiment_id)}
    except ValueError as e: raise HTTPException(400,str(e))

@app.post('/api/learning/experiments/{experiment_id}/promote')
def learning_promote(experiment_id: str):
    try: return {'ok':True,'experiment':promote_winner(experiment_id)}
    except ValueError as e: raise HTTPException(400,str(e))

@app.get('/api/learning/strategies')
def learning_strategies():
    return {'strategies':learned_strategies()}

@app.get('/api/campaigns')
def campaign_list(): return {'campaigns':campaigns()}

@app.get('/api/buyer/profile')
def buyer_profile(buyer_id: str):
    if not buyer_id.strip(): raise HTTPException(400,'buyer_id is required')
    profile=build_buyer_profile(buyer_id)
    audit('buyer_intelligence','buyer.profile','Built buyer preference profile',status='SUCCESS',meta={'buyer_id':buyer_id,'events':profile['events']})
    return {'profile':profile}

@app.get('/api/buyer/recommendations')
def buyer_recommendations(buyer_id: str, query: str = ''):
    if not buyer_id.strip(): raise HTTPException(400,'buyer_id is required')
    recs=personalized_recommendations(buyer_id,query)
    audit('buyer_intelligence','buyer.recommendations',f'Generated {len(recs)} personalized recommendations',status='SUCCESS',meta={'buyer_id':buyer_id,'query':query,'count':len(recs)})
    return {'recommendations':recs,'query':query,'personalized':bool(build_buyer_profile(buyer_id)['events'])}

class BuyerEventIn(BaseModel):
    buyer_id: str
    event_type: str
    product_id: str | None = None
    query: str | None = None
    meta: dict = {}

@app.post('/api/buyer/events')
def buyer_event(body: BuyerEventIn):
    allowed={'search','recommendation_view','cart_add','purchase','checkout_start'}
    if body.event_type not in allowed: raise HTTPException(400,'Unsupported buyer event')
    if body.product_id and not get_product_by_id(body.product_id): raise HTTPException(404,'Product not found')
    track_event(body.buyer_id,body.event_type,body.product_id,body.query,body.meta)
    return {'ok':True}

@app.post('/api/buyer/checkout-intent')
def create_buyer_checkout_intent(body: PurchaseIntentIn):
    if not body.buyer_id.strip(): raise HTTPException(400,'buyer_id is required')
    items, subtotal = calculate_server_cart(body.cart_id)
    profile = build_buyer_profile(body.buyer_id)
    summary = build_purchase_summary(items, subtotal, body.discount_percent, profile)
    if not summary['can_checkout']:
        audit('checkout_agent','purchase.intent.block','Purchase intent blocked by checkout policy',amount=summary['total'],status='BLOCKED',meta={'buyer_id':body.buyer_id,'cart_id':body.cart_id})
        raise HTTPException(400, detail=summary['policy'])
    fp = cart_fingerprint(items, subtotal, body.discount_percent)
    existing = latest_purchase_intent(body.buyer_id, body.cart_id)
    if existing and existing.get('status') == 'PENDING' and existing.get('fingerprint') == fp:
        summary.update({'intent_id':existing['id'],'state':existing['state'],'status':existing['status'],'fingerprint':fp})
        audit('checkout_agent','purchase.intent.reuse','Reused matching purchase intent; no payment action taken',amount=summary['total'],status='SUCCESS',meta={'intent_id':existing['id'],'buyer_id':body.buyer_id,'cart_id':body.cart_id})
        return summary
    iid=create_purchase_intent(body.buyer_id,body.cart_id,fp,summary['state'],subtotal,body.discount_percent,summary['total'],items)
    summary.update({'intent_id':iid,'fingerprint':fp,'status':'PENDING'})
    audit('checkout_agent','purchase.intent.created','Prepared explainable purchase summary; waiting for buyer confirmation',amount=summary['total'],status='PENDING',meta={'intent_id':iid,'buyer_id':body.buyer_id,'cart_id':body.cart_id})
    return summary

@app.post('/api/buyer/checkout-intent/confirm')
def confirm_buyer_checkout(body: PurchaseConfirmIn):
    intent=get_purchase_intent(body.intent_id)
    if not intent: raise HTTPException(404,'Purchase intent not found')
    items, subtotal = calculate_server_cart(intent['cart_id'])
    # Confirmation endpoint validates that the review is still current, then
    # performs the state transition. validate_confirmed_intent() is intentionally
    # reserved for /api/checkout/prepare because it requires status=CONFIRMED.
    if intent.get('status') == 'CONFIRMED' and intent.get('state') == 'CHECKOUT_READY':
        ok, error = validate_confirmed_intent(intent, items, subtotal, intent['discount_percent'])
        if not ok: raise HTTPException(409, error)
        return {'ok':True,'intent_id':intent['id'],'state':'CHECKOUT_READY','status':'CONFIRMED','message':'Purchase already confirmed.'}
    expected_fp = cart_fingerprint(items, subtotal, intent['discount_percent'])
    if intent.get('fingerprint') != expected_fp:
        raise HTTPException(409,'The cart changed after review. Please review the updated purchase summary.')
    confirm_purchase_intent(body.intent_id)
    audit('buyer_agent','purchase.confirm','Buyer confirmed the exact cart, price and policy summary',amount=intent['total'],status='APPROVED',meta={'intent_id':intent['id'],'buyer_id':intent['buyer_id'],'cart_id':intent['cart_id']})
    return {'ok':True,'intent_id':intent['id'],'state':'CHECKOUT_READY','status':'CONFIRMED','message':'Purchase confirmed. Checkout is now unlocked.'}

@app.get('/api/buyer/checkout-intent/{intent_id}')
def get_buyer_checkout_intent(intent_id: int):
    intent=get_purchase_intent(intent_id)
    if not intent: raise HTTPException(404,'Purchase intent not found')
    return {'intent':intent}


# Upgrade 08 — Agent Commerce Gateway
# These routes are intentionally thin wrappers over the already-built U1–U7
# services. They expose a machine-readable merchant surface without bypassing
# the existing policy, purchase-confirmation, Razorpay and audit gates.
from fastapi import Header

class AgentCartCreateIn(BaseModel):
    buyer_id: str
    session_id: str | None = None

class AgentCartItemIn(BaseModel):
    buyer_id: str
    agent_id: str = 'external-ai-agent'
    session_id: str | None = None
    product_id: str
    qty: int = Field(default=1, ge=1, le=20)

class AgentPurchaseIntentIn(BaseModel):
    buyer_id: str
    cart_id: str
    discount_percent: int = Field(default=0, ge=0, le=10)
    agent_id: str = 'external-ai-agent'
    session_id: str | None = None

class AgentConfirmIn(BaseModel):
    buyer_id: str
    confirmed_by_buyer: bool = False
    agent_id: str = 'external-ai-agent'
    session_id: str | None = None

class AgentCheckoutIn(BaseModel):
    buyer_id: str
    agent_id: str = 'external-ai-agent'
    session_id: str | None = None


def _agent_identity(agent_id: str, session_id: str | None):
    aid = (agent_id or '').strip() or 'external-ai-agent'
    sid = (session_id or '').strip() or 'unspecified-session'
    return aid, sid

@app.get('/api/agent/capabilities')
def agent_capabilities():
    manifest = capability_manifest()
    manifest['safety_summary'] = gateway_safety_summary()
    audit('agent_gateway','gateway.capabilities','External agent read the merchant capability manifest',status='SUCCESS',meta={'gateway_version':manifest['version']})
    return manifest

@app.get('/api/agent/catalog/search')
def agent_catalog_search(q: str = '', category: str | None = None, limit: int = 10, agent_id: str = 'external-ai-agent', session_id: str | None = None):
    aid, sid = _agent_identity(agent_id, session_id)
    result = gateway_search_products(q, category, limit)
    audit('agent_gateway','gateway.catalog.search',f'External agent searched merchant catalogue for "{q}"',status='SUCCESS',meta={'agent_id':aid,'session_id':sid,'query':q,'category':category,'count':len(result)})
    return {'ok':True,'tool':'product_search','query':q,'category':category,'products':result,'count':len(result)}

@app.get('/api/agent/products/{product_id}')
def agent_product(product_id: str, agent_id: str = 'external-ai-agent', session_id: str | None = None):
    aid, sid = _agent_identity(agent_id, session_id)
    result = gateway_product_detail(product_id)
    if not result: raise HTTPException(404,'Product not found')
    audit('agent_gateway','gateway.product.get',f'External agent read product {result["name"]}',status='SUCCESS',meta={'agent_id':aid,'session_id':sid,'product_id':product_id})
    return {'ok':True,'tool':'product_details','product':result}

@app.get('/api/agent/products/{product_id}/related')
def agent_product_related(product_id: str, agent_id: str = 'external-ai-agent', session_id: str | None = None):
    aid, sid = _agent_identity(agent_id, session_id)
    result = gateway_related_products(product_id)
    if result is None: raise HTTPException(404,'Product not found')
    audit('agent_gateway','gateway.product.related',f'External agent read related products for {product_id}',status='SUCCESS',meta={'agent_id':aid,'session_id':sid,'product_id':product_id,'count':len(result)})
    return {'ok':True,'tool':'related_products','product_id':product_id,'products':result}

@app.get('/api/agent/recommendations')
def agent_recommendations(buyer_id: str, q: str = '', agent_id: str = 'external-ai-agent', session_id: str | None = None):
    aid, sid = _agent_identity(agent_id, session_id)
    if not buyer_id.strip(): raise HTTPException(400,'buyer_id is required')
    result = gateway_recommendations(buyer_id, q)
    audit('agent_gateway','gateway.recommendations',f'External agent requested {len(result["recommendations"])} buyer recommendations',status='SUCCESS',meta={'agent_id':aid,'session_id':sid,'buyer_id':buyer_id,'query':q})
    return {'ok':True,'tool':'buyer_recommendations',**result}

@app.post('/api/agent/carts')
def agent_create_cart(body: AgentCartCreateIn, agent_id: str = Header(default='external-ai-agent', alias='X-Agent-Id')):
    aid, sid = _agent_identity(agent_id, body.session_id)
    if not body.buyer_id.strip(): raise HTTPException(400,'buyer_id is required')
    return {'ok':True,'tool':'create_cart',**new_agent_cart(body.buyer_id,aid,sid)}

@app.get('/api/agent/carts/{cart_id}')
def agent_get_cart(cart_id: str, agent_id: str = 'external-ai-agent', session_id: str | None = None):
    aid, sid = _agent_identity(agent_id, session_id)
    result=cart_snapshot_gateway(cart_id)
    audit('agent_gateway','gateway.cart.read','External agent read merchant-side cart',status='SUCCESS',meta={'agent_id':aid,'session_id':sid,'cart_id':cart_id,'count':result['count']})
    return {'ok':True,'tool':'get_cart',**result}

@app.post('/api/agent/carts/{cart_id}/items')
def agent_add_to_cart(cart_id: str, body: AgentCartItemIn):
    aid, sid = _agent_identity(body.agent_id, body.session_id)
    snap, error = gateway_add_cart_item(cart_id, body.product_id, body.qty, body.buyer_id, aid, sid)
    if error: raise HTTPException(400,error)
    return {'ok':True,'tool':'add_to_cart',**snap}

@app.post('/api/agent/purchase-intents')
def agent_create_purchase_intent(body: AgentPurchaseIntentIn):
    aid, sid = _agent_identity(body.agent_id, body.session_id)
    if not body.buyer_id.strip(): raise HTTPException(400,'buyer_id is required')
    items, subtotal = calculate_server_cart(body.cart_id)
    profile = build_buyer_profile(body.buyer_id)
    summary = build_purchase_summary(items, subtotal, body.discount_percent, profile)
    if not summary['can_checkout']:
        audit('agent_gateway','gateway.purchase_intent.block','External agent purchase intent blocked by checkout policy',amount=summary['total'],status='BLOCKED',meta={'agent_id':aid,'session_id':sid,'buyer_id':body.buyer_id,'cart_id':body.cart_id})
        raise HTTPException(400, detail=summary['policy'])
    fp = cart_fingerprint(items, subtotal, body.discount_percent)
    existing = latest_purchase_intent(body.buyer_id, body.cart_id)
    if existing and existing.get('status') in ('PENDING','CONFIRMED') and existing.get('fingerprint') == fp:
        view = purchase_intent_view(existing)
        view['source'] = 'reused'
        view['requires_buyer_confirmation'] = existing.get('status') != 'CONFIRMED'
        audit('agent_gateway','gateway.purchase_intent.reuse','External agent reused matching purchase intent; no payment action taken',amount=summary['total'],status='SUCCESS',meta={'agent_id':aid,'session_id':sid,'intent_id':existing['id'],'buyer_id':body.buyer_id})
        return {'ok':True,'tool':'create_purchase_intent',**view}
    iid=create_purchase_intent(body.buyer_id,body.cart_id,fp,summary['state'],subtotal,body.discount_percent,summary['total'],items)
    intent=get_purchase_intent(iid)
    audit('agent_gateway','gateway.purchase_intent.create','External agent created purchase intent; waiting for explicit buyer confirmation',amount=summary['total'],status='PENDING',meta={'agent_id':aid,'session_id':sid,'intent_id':iid,'buyer_id':body.buyer_id,'cart_id':body.cart_id})
    view=purchase_intent_view(intent)
    view['source']='created'
    view['requires_buyer_confirmation']=True
    return {'ok':True,'tool':'create_purchase_intent',**view}

@app.get('/api/agent/purchase-intents/{intent_id}')
def agent_get_purchase_intent(intent_id: int, agent_id: str = 'external-ai-agent', session_id: str | None = None):
    aid, sid = _agent_identity(agent_id, session_id)
    intent=get_purchase_intent(intent_id)
    if not intent: raise HTTPException(404,'Purchase intent not found')
    view=purchase_intent_view(intent)
    audit('agent_gateway','gateway.purchase_intent.read','External agent read purchase summary',status='SUCCESS',meta={'agent_id':aid,'session_id':sid,'intent_id':intent_id,'buyer_id':intent['buyer_id']})
    return {'ok':True,'tool':'get_purchase_summary',**view}

@app.post('/api/agent/purchase-intents/{intent_id}/confirm')
def agent_confirm_purchase_intent(intent_id: int, body: AgentConfirmIn):
    # This endpoint is deliberately explicit: an agent may surface the
    # confirmation action, but the request is treated as a buyer confirmation
    # only when the supplied buyer matches the intent. It never skips the
    # fingerprint check or creates a payment by itself.
    aid, sid = _agent_identity(body.agent_id, body.session_id)
    intent=get_purchase_intent(intent_id)
    if not intent: raise HTTPException(404,'Purchase intent not found')
    if intent.get('buyer_id') != body.buyer_id: raise HTTPException(403,'Purchase intent belongs to another buyer')
    if not body.confirmed_by_buyer:
        raise HTTPException(428,'Explicit buyer confirmation is required. The agent may request confirmation but cannot grant it silently.')
    items, subtotal = calculate_server_cart(intent['cart_id'])
    expected_fp=cart_fingerprint(items,subtotal,int(intent['discount_percent']))
    if intent.get('fingerprint') != expected_fp:
        raise HTTPException(409,'The cart changed after review. Please review the updated purchase summary.')
    if intent.get('status') == 'CONFIRMED' and intent.get('state') == 'CHECKOUT_READY':
        return {'ok':True,'tool':'confirm_purchase','intent_id':intent_id,'state':'CHECKOUT_READY','status':'CONFIRMED','message':'Purchase already confirmed.'}
    confirm_purchase_intent(intent_id)
    audit('agent_gateway','gateway.purchase.confirm','Explicit buyer confirmation received through the commerce gateway',amount=intent['total'],status='APPROVED',meta={'agent_id':aid,'session_id':sid,'intent_id':intent_id,'buyer_id':body.buyer_id})
    return {'ok':True,'tool':'confirm_purchase','intent_id':intent_id,'state':'CHECKOUT_READY','status':'CONFIRMED','message':'Purchase confirmed. Checkout is now unlocked.'}

@app.post('/api/agent/checkout/prepare')
def agent_prepare_checkout(intent_id: int, body: AgentCheckoutIn):
    aid, sid = _agent_identity(body.agent_id, body.session_id)
    intent=get_purchase_intent(intent_id)
    if not intent: raise HTTPException(404,'Purchase intent not found')
    if intent.get('buyer_id') != body.buyer_id: raise HTTPException(403,'Purchase intent belongs to another buyer')
    if intent.get('status') != 'CONFIRMED':
        raise HTTPException(428,'Explicit buyer confirmation is required before checkout')
    items, subtotal=calculate_server_cart(intent['cart_id'])
    fp=cart_fingerprint(items,subtotal,int(intent['discount_percent']))
    if intent.get('fingerprint') != fp:
        raise HTTPException(409,'The cart changed after confirmation. Please review the updated purchase summary.')
    track_event(body.buyer_id,'checkout_start',query='External agent gateway checkout',meta={'cart_id':intent['cart_id'],'intent_id':intent_id,'agent_id':aid,'session_id':sid})
    audit('agent_gateway','gateway.checkout.prepare','External agent requested bounded checkout after confirmed purchase intent',amount=intent['total'],status='APPROVED',meta={'agent_id':aid,'session_id':sid,'intent_id':intent_id,'buyer_id':body.buyer_id})
    return prepare_checkout(CheckoutIn(cart_id=intent['cart_id'],discount_percent=int(intent['discount_percent']),buyer_id=body.buyer_id,intent_id=intent_id))

@app.get('/api/agent/orders/{internal_order_id}')
def agent_order_status(internal_order_id: str, agent_id: str = 'external-ai-agent', session_id: str | None = None):
    aid, sid = _agent_identity(agent_id, session_id)
    order=gateway_order_status(internal_order_id)
    if not order: raise HTTPException(404,'Order not found')
    audit('agent_gateway','gateway.order.status','External agent read order status',status='SUCCESS',meta={'agent_id':aid,'session_id':sid,'internal_order_id':internal_order_id})
    return {'ok':True,'tool':'order_status','order':order}

@app.get('/api/ai/catalog')
def ai_catalog():
    return {'merchant':'RayBoost Demo Store','capabilities':['search','product_details','related_products','cart','checkout'],'products':products()}

@app.post('/api/ai/search')
def ai_search(body: ChatIn):
    return search_catalogue(body.message)

@app.get('/api/ai/product/{product_id}')
def ai_product(product_id: str):
    result = get_product(product_id)
    if not result['found']: raise HTTPException(404, 'Product not found')
    return result

@app.get('/api/ai/product/{product_id}/related')
def ai_related(product_id: str):
    return get_related_products(product_id)

@app.get('/api/growth/summary')
def growth_summary():
    summary=build_growth_summary()
    audit('growth_agent','growth.summary','Analyzed merchant sales ledger',status='SUCCESS',meta=summary)
    return {"summary":summary}

@app.get('/api/growth/opportunities')
def growth_opportunities():
    data=build_growth_opportunities()
    audit('growth_agent','growth.opportunities',f'Identified {len(data)} revenue opportunities',status='SUCCESS',meta={'count':len(data)})
    return {"opportunities":data}

@app.post('/api/growth/opportunities/{opportunity_id}/explain')
def growth_explain(opportunity_id: str):
    item=next((x for x in build_growth_opportunities() if x['id']==opportunity_id),None)
    if not item: raise HTTPException(404,'Opportunity not found')
    result=explain_growth_opportunity(item)
    audit('growth_agent','growth.explain',result.get('headline','Explained opportunity'),status='SUCCESS',meta={'opportunity_id':opportunity_id})
    return {'opportunity':item,'explanation':result}

class GrowthDecisionIn(BaseModel):
    decision: str

class CampaignExecuteIn(BaseModel):
    simulate_failure: bool = False

@app.post('/api/growth/opportunities/{opportunity_id}/decision')
def growth_decision(opportunity_id: str, body: GrowthDecisionIn):
    if body.decision not in ('approved','rejected'): raise HTTPException(400,'decision must be approved or rejected')
    item=next((x for x in build_growth_opportunities() if x['id']==opportunity_id),None)
    if not item: raise HTTPException(404,'Opportunity not found')
    audit('merchant','growth.proposal_decision',f"Merchant {body.decision} growth proposal: {item['title']}",status='APPROVED' if body.decision=='approved' else 'REJECTED',meta={'opportunity_id':opportunity_id,'decision':body.decision})
    return {'ok':True,'opportunity_id':opportunity_id,'decision':body.decision,'message':'Proposal approved for campaign planning. No campaign was launched automatically.' if body.decision=='approved' else 'Proposal rejected. No campaign was launched.'}

def _latest_growth_decision(opportunity_id):
    c=conn()
    try:
        rows=c.execute("SELECT status, meta FROM audit WHERE action='growth.proposal_decision' ORDER BY id DESC").fetchall()
    finally:
        c.close()
    for r in rows:
        try: meta=json.loads(r['meta'] or '{}')
        except Exception: meta={}
        if meta.get('opportunity_id')==opportunity_id:
            return meta.get('decision') if r['status'] in ('APPROVED','REJECTED') else None
    return None

@app.post('/api/growth/opportunities/{opportunity_id}/campaign-draft')
def campaign_draft(opportunity_id: str):
    item=next((x for x in build_growth_opportunities() if x['id']==opportunity_id),None)
    if not item: raise HTTPException(404,'Opportunity not found')
    approved = _latest_growth_decision(opportunity_id) == 'approved'
    if not approved:
        audit('campaign_agent','campaign.block','Campaign draft blocked because merchant approval was not found',status='BLOCKED',meta={'opportunity_id':opportunity_id})
        raise HTTPException(403,'Merchant approval is required before campaign planning')
    plan=build_campaign_plan(item,products())
    policy=validate_campaign_plan(plan)
    if not policy['allowed']:
        audit('policy','campaign.plan_block','; '.join(policy['errors']),status='BLOCKED',meta={'opportunity_id':opportunity_id})
        raise HTTPException(400,policy)
    cid=create_campaign_draft(plan['name'],plan['product_ids'],plan['discount'],plan['reason'],opportunity_id)
    return {'campaign':{**plan,'id':cid,'status':'DRAFT','opportunity_id':opportunity_id},'policy':policy}

@app.post('/api/campaigns/{campaign_id}/execute')
def campaign_execute(campaign_id: int, body: CampaignExecuteIn):
    campaign=get_campaign_by_id(campaign_id)
    if not campaign: raise HTTPException(404,'Campaign not found')
    if campaign.get('opportunity_id') and _latest_growth_decision(campaign['opportunity_id']) != 'approved':
        audit('policy','campaign.block','Campaign execution blocked because merchant approval is no longer active',status='BLOCKED',meta={'campaign_id':campaign_id,'opportunity_id':campaign.get('opportunity_id')})
        raise HTTPException(403,'Merchant approval is required for campaign execution')
    if campaign['status']=='ACTIVE': return {'ok':True,'campaign':campaign,'message':'Campaign is already active; duplicate execution prevented.'}
    if campaign['status']=='FAILED':
        # Retry is allowed only after a failed test execution.
        pass
    elif campaign['status']!='DRAFT':
        raise HTTPException(409,f"Campaign cannot be executed from status {campaign['status']}")
    if body.simulate_failure:
        update_campaign_status(campaign_id,'FAILED','Test execution failed safely. No campaign was activated.')
        audit('campaign_agent','campaign.execute','Campaign execution failed safely in test mode',status='FAILED',meta={'campaign_id':campaign_id,'retryable':True})
        return {'ok':False,'retryable':True,'campaign':get_campaign_by_id(campaign_id),'message':'Campaign execution failed safely. Nothing was activated and you can retry.'}
    try:
        ids=json.loads(campaign['product_ids'] or '[]')
        missing=[pid for pid in ids if not get_product_by_id(pid)]
        if missing: raise ValueError('Unknown campaign product: '+', '.join(missing))
        if int(campaign['discount'])>10: raise ValueError('Discount exceeds 10% policy')
        update_campaign_status(campaign_id,'ACTIVE')
        audit('campaign_agent','campaign.execute','Approved campaign activated in test mode',status='SUCCESS',meta={'campaign_id':campaign_id,'execution_mode':'test'})
        return {'ok':True,'campaign':get_campaign_by_id(campaign_id),'message':'Campaign activated successfully in test mode.'}
    except Exception as e:
        update_campaign_status(campaign_id,'FAILED',str(e))
        audit('campaign_agent','campaign.execute',str(e),status='FAILED',meta={'campaign_id':campaign_id,'retryable':True})
        return {'ok':False,'retryable':True,'campaign':get_campaign_by_id(campaign_id),'message':'Campaign failed safely. No activation occurred; retry is available.'}

@app.post('/api/campaigns')
def campaign_create(body: CampaignIn):
    policy=check_discount(body.discount)
    if not policy['allowed']:
        audit('policy','campaign.block',f"Requested {body.discount}% discount; policy max is {policy['max_discount']}%",status='BLOCKED')
        raise HTTPException(400, detail=policy)
    cid=create_campaign(body.name,body.product_ids,body.discount,body.reason)
    return {'id':cid,'status':'ACTIVE','policy':policy}

@app.post('/api/cart')
def cart_add(body: CartIn):
    cart_id = body.cart_id or str(uuid.uuid4())
    p = get_product_by_id(body.item.product_id)
    if not p: raise HTTPException(404, 'Unknown product')
    if body.item.qty > p['stock']: raise HTTPException(400, f"Only {p['stock']} units are available")
    items = get_cart(cart_id)
    existing = next((x for x in items if x['product_id'] == p['id']), None)
    if existing:
        new_qty = existing['qty'] + body.item.qty
        if new_qty > p['stock']: raise HTTPException(400, f"Only {p['stock']} units are available")
        existing['qty'] = new_qty
    else:
        items.append({'product_id': p['id'], 'qty': body.item.qty})
    save_cart(cart_id, items)
    audit('buyer_agent','cart.add',f"Added {p['name']} to buyer cart",amount=p['price']*body.item.qty,status='SUCCESS',meta={'cart_id':cart_id,'product_id':p['id'],'qty':body.item.qty,'buyer_id':body.buyer_id})
    if body.buyer_id: track_event(body.buyer_id,'cart_add',p['id'],meta={'cart_id':cart_id,'qty':body.item.qty,'amount':p['price']*body.item.qty})
    return cart_snapshot(cart_id)

@app.post('/api/cart/read')
def cart_read(body: CartReadIn):
    return cart_snapshot(body.cart_id)

@app.delete('/api/cart/{cart_id}')
def cart_clear(cart_id: str):
    clear_cart(cart_id)
    audit('buyer_agent','cart.clear','Buyer cart cleared',status='SUCCESS',meta={'cart_id':cart_id})
    return cart_snapshot(cart_id)

def cart_snapshot(cart_id):
    raw = get_cart(cart_id)
    by_id = {p['id']: p for p in products()}
    items=[]; total=0
    for row in raw:
        p=by_id.get(row['product_id'])
        if not p: continue
        qty=int(row['qty']); line=p['price']*qty; total += line
        items.append({**p,'qty':qty,'line_total':line})
    return {'cart_id':cart_id,'items':items,'total':total,'count':sum(x['qty'] for x in items)}

@app.get('/api/orders')
def order_list():
    return {'orders': checkout_orders()}

def calculate_server_cart(cart_id: str):
    raw=get_cart(cart_id)
    if not raw: raise HTTPException(400,'Cart is empty')
    catalog_map={p['id']:p for p in products()}
    items=[]; subtotal=0
    for row in raw:
        p=catalog_map.get(row['product_id'])
        if not p: raise HTTPException(400, f"Unknown product: {row['product_id']}")
        qty=int(row['qty'])
        if qty<1 or qty>p['stock']: raise HTTPException(400, f"Invalid quantity for {p['name']}")
        line=p['price']*qty; subtotal += line
        items.append({'product_id':p['id'],'name':p['name'],'qty':qty,'unit_price':p['price'],'line_total':line})
    return items, subtotal

@app.post('/api/checkout/prepare')
def prepare_checkout(body: CheckoutIn):
    items, subtotal = calculate_server_cart(body.cart_id)
    discount = int(body.discount_percent)
    policy = check_discount(discount)
    if not policy['allowed']:
        audit('policy_agent','checkout.block',f"Requested {discount}% discount exceeds policy max {policy['max_discount']}%",status='BLOCKED',meta={'cart_id':body.cart_id})
        raise HTTPException(400, detail=policy)
    amount=max(1, round(subtotal*(1-discount/100)))
    order_policy=check_order(amount)
    if not order_policy['allowed']: raise HTTPException(400, detail=order_policy)

    intent=None
    if body.intent_id is not None:
        intent=get_purchase_intent(body.intent_id)
        ok,error=validate_confirmed_intent(intent,items,subtotal,discount)
        if not ok: raise HTTPException(409,error)
        if intent['buyer_id'] and body.buyer_id and intent['buyer_id'] != body.buyer_id: raise HTTPException(403,'Purchase intent belongs to another buyer')
    elif body.buyer_id:
        raise HTTPException(428,'Buyer confirmation is required before checkout')

    existing=get_checkout_by_cart(body.cart_id)
    if existing:
        if intent: update_purchase_intent_state(intent['id'],'PAYMENT_PENDING','CONFIRMED')
        audit('payment_agent','checkout.reuse',f"Reusing pending Razorpay order {existing['razorpay_order_id']} to prevent duplicates",amount=existing['amount'],status='SUCCESS',meta={'cart_id':body.cart_id,'razorpay_order_id':existing['razorpay_order_id']})
        return {'internal_order_id':existing['internal_order_id'],'razorpay_order_id':existing['razorpay_order_id'],'amount_inr':existing['amount'],'currency':existing['currency'],'key_id':os.getenv('RAZORPAY_KEY_ID',''),'demo':existing['razorpay_order_id'].startswith('order_demo_'),'reused':True,'policy':order_policy,'items':items,'subtotal':subtotal,'discount_percent':discount,'intent_id':intent['id'] if intent else None,'state':'PAYMENT_PENDING'}

    rp_order=create_order(amount)
    internal_id='RB-'+uuid.uuid4().hex[:10].upper()
    create_checkout(body.cart_id, internal_id, rp_order['id'], amount, rp_order.get('currency','INR'), body.buyer_id, items, intent['id'] if intent else None)
    if intent: update_purchase_intent_state(intent['id'],'PAYMENT_PENDING','CONFIRMED')
    audit('payment_agent','checkout.create',f"Created bounded checkout order {internal_id} after buyer confirmation",amount=amount,status='PENDING',meta={'cart_id':body.cart_id,'razorpay_order_id':rp_order['id'],'internal_order_id':internal_id,'discount_percent':discount,'buyer_id':body.buyer_id,'intent_id':intent['id'] if intent else None})
    return {'internal_order_id':internal_id,'razorpay_order_id':rp_order['id'],'amount_inr':amount,'currency':rp_order.get('currency','INR'),'key_id':os.getenv('RAZORPAY_KEY_ID',''),'demo':rp_order.get('demo',False),'reused':False,'policy':order_policy,'items':items,'subtotal':subtotal,'discount_percent':discount,'intent_id':intent['id'] if intent else None,'state':'PAYMENT_PENDING'}

@app.post('/api/payments/verify')
def payment_verify(body: VerifyIn):
    record=get_checkout_by_razorpay(body.razorpay_order_id)
    if not record: raise HTTPException(404,'Checkout order not found')
    ok=verify_signature(body.razorpay_order_id,body.razorpay_payment_id,body.razorpay_signature)
    if ok:
        update_checkout_status(body.razorpay_order_id,'PAID')
        if record.get('intent_id'): update_purchase_intent_state(record['intent_id'],'PAID','CONFIRMED')
        if record.get('buyer_id'):
            track_event(record['buyer_id'],'purchase',query='Razorpay verified purchase',meta={'internal_order_id':record['internal_order_id'],'amount':record['amount'],'razorpay_order_id':body.razorpay_order_id})
        audit('razorpay','payment.verify','Payment signature verified; order marked PAID',amount=record['amount'],status='SUCCESS',meta={'order_id':body.razorpay_order_id,'payment_id':body.razorpay_payment_id,'internal_order_id':record['internal_order_id'],'buyer_id':record.get('buyer_id')})
        return {'verified':True,'internal_order_id':record['internal_order_id'],'status':'PAID'}
    audit('razorpay','payment.verify','Invalid payment signature',amount=record['amount'],status='FAILED',meta={'order_id':body.razorpay_order_id,'payment_id':body.razorpay_payment_id})
    raise HTTPException(400,'Invalid payment signature')

@app.post('/api/payments/failed')
def payment_failed(body: PaymentFailedIn):
    record=get_checkout_by_razorpay(body.razorpay_order_id)
    if not record: raise HTTPException(404,'Checkout order not found')
    update_checkout_status(body.razorpay_order_id,'FAILED')
    if record.get('intent_id'): update_purchase_intent_state(record['intent_id'],'FAILED','CONFIRMED')
    audit('razorpay','payment.failed',body.reason,amount=record['amount'],status='FAILED',meta={'order_id':body.razorpay_order_id,'internal_order_id':record['internal_order_id']})
    return {'ok':True,'status':'FAILED','cart_preserved':True,'internal_order_id':record['internal_order_id']}

@app.post('/api/payments/demo-success')
def demo_success(cart_id: str):
    record=get_checkout_by_cart(cart_id)
    if not record: raise HTTPException(404,'No pending checkout for this cart')
    update_checkout_status(record['razorpay_order_id'],'PAID')
    if record.get('intent_id'): update_purchase_intent_state(record['intent_id'],'PAID','CONFIRMED')
    payment_id='pay_demo_'+uuid.uuid4().hex[:12]
    audit('razorpay','payment.success','Simulated Test Mode success; order marked PAID',amount=record['amount'],status='SUCCESS',meta={'order_id':record['razorpay_order_id'],'payment_id':payment_id,'internal_order_id':record['internal_order_id']})
    clear_cart(cart_id)
    if record.get('buyer_id'):
        track_event(record['buyer_id'],'purchase',query='Demo Test Mode purchase',meta={'internal_order_id':record['internal_order_id'],'amount':record['amount']})
    audit('order_agent','order.complete','Order completed and cart cleared',amount=record['amount'],status='SUCCESS',meta={'cart_id':cart_id,'internal_order_id':record['internal_order_id'],'buyer_id':record.get('buyer_id')})
    return {'ok':True,'status':'PAID','internal_order_id':record['internal_order_id'],'payment_id':payment_id,'amount_inr':record['amount']}

@app.post('/api/payments/demo-failure')
def demo_failure(cart_id: str):
    record=get_checkout_by_cart(cart_id)
    if record:
        update_checkout_status(record['razorpay_order_id'],'FAILED')
        if record.get('intent_id'): update_purchase_intent_state(record['intent_id'],'FAILED','CONFIRMED')
    audit('razorpay','payment.failed','Simulated payment failure. Cart preserved and no duplicate order created.',amount=record['amount'] if record else 0,status='FAILED',meta={'cart_id':cart_id,'razorpay_order_id':record['razorpay_order_id'] if record else None})
    return {'ok':True,'message':'Payment failed safely. Cart preserved; retry is available. A new checkout will not be created if a pending order still exists.'}

@app.post('/api/chat')
def chat(body: ChatIn):
    msg=body.message.strip()
    if not msg: raise HTTPException(400,'Message required')
    if body.mode=='merchant':
        data=revenue_opportunities(); audit('growth_agent','revenue.analysis','Analyzed merchant sales patterns',status='SUCCESS')
        if not data['opportunities']: return {'text':'I could not find enough sales relationships yet. Add more paid orders to generate stronger opportunities.', 'data':data}
        top=data['opportunities'][0]
        return {'text':f"I found {len(data['opportunities'])} revenue opportunities. The strongest is {top['title']} with estimated potential of ₹{top['potential_monthly_revenue']:,}/month.", 'data':data}

    buyer_id = body.buyer_id or 'demo-buyer'
    track_event(buyer_id,'search',query=msg)
    plan = build_personalized_plan(buyer_id,msg)
    for p in plan.get('products',[])[:5]: track_event(buyer_id,'recommendation_view',p['id'],query=msg)
    if not plan['products']:
        return {'text':'I could not find a strong catalogue match. Try a budget, use case, or product category.', 'products':[], 'plan':plan}

    picks = plan['bundle'] or plan['products'][:2]
    bundle_names = ', '.join(x['name'] for x in picks)
    budget_text = f" within your ₹{plan['budget']:,} budget" if plan['budget'] else ''
    personalization = ' I also used your earlier activity to personalize the ranking.' if plan['personalized'] else ''
    text = f"I found a grounded recommendation{budget_text}: {bundle_names}. The bundle total is ₹{plan['total']:,}. I selected these from the merchant catalogue based on your request and relevance signals.{personalization} You can add any of them to your cart."
    return {'text':text,'products':picks,'estimated_total':plan['total'],'plan':{'budget':plan['budget'],'intent':plan['intent'],'product_ids':[x['id'] for x in picks],'personalized':plan['personalized'],'profile_signals':plan['profile_signals']}}
