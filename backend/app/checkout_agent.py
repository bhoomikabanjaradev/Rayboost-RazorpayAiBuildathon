import hashlib, json, os
from .policy import check_discount, check_order

STATES = ('DISCOVERING','RECOMMENDING','CART_READY','AWAITING_CONFIRMATION','CHECKOUT_READY','PAYMENT_PENDING','PAID','FAILED')


def cart_fingerprint(items, total, discount_percent=0):
    normalized = sorted([
        {'product_id': str(x['product_id']), 'qty': int(x['qty']), 'unit_price': int(x['unit_price'])}
        for x in items
    ], key=lambda x: x['product_id'])
    payload = json.dumps({'items': normalized, 'total': int(total), 'discount_percent': int(discount_percent)}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def build_purchase_summary(items, subtotal, discount_percent=0, buyer_profile=None):
    discount = int(discount_percent)
    discount_policy = check_discount(discount)
    amount = max(1, round(subtotal * (1 - discount / 100)))
    order_policy = check_order(amount)
    reasons = []
    if buyer_profile and buyer_profile.get('events'):
        reasons.append('uses the buyer\'s first-party activity when available')
    if len(items) > 1:
        reasons.append('combines the selected products into one cart')
    reasons.append('uses the merchant catalogue as the source of product and price truth')
    return {
        'state': 'AWAITING_CONFIRMATION',
        'items': items,
        'subtotal': subtotal,
        'discount_percent': discount,
        'discount_amount': max(0, subtotal - amount),
        'total': amount,
        'currency': 'INR',
        'reasons': reasons,
        'policy': {'discount': discount_policy, 'order': order_policy},
        'requires_confirmation': True,
        'can_checkout': bool(discount_policy.get('allowed') and order_policy.get('allowed')),
    }


def validate_confirmed_intent(intent, items, subtotal, discount_percent):
    if not intent:
        return False, 'No purchase confirmation was found. Review and confirm the purchase first.'
    if intent.get('status') != 'CONFIRMED':
        return False, 'Purchase confirmation is required before checkout.'
    expected = cart_fingerprint(items, subtotal, discount_percent)
    if intent.get('fingerprint') != expected:
        return False, 'The cart changed after confirmation. Please review the updated purchase summary.'
    return True, None
