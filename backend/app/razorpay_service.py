import os, uuid, hmac, hashlib
import razorpay


def configured():
    return bool(os.getenv("RAZORPAY_KEY_ID") and os.getenv("RAZORPAY_KEY_SECRET"))

def client():
    if not configured():
        return None
    return razorpay.Client(auth=(os.environ["RAZORPAY_KEY_ID"], os.environ["RAZORPAY_KEY_SECRET"]))

def create_order(amount_inr: int):
    rp = client()
    receipt = "rb_" + uuid.uuid4().hex[:24]
    if not rp:
        return {"id":"order_demo_"+uuid.uuid4().hex[:12],"amount":amount_inr*100,"currency":"INR","receipt":receipt,"demo":True}
    return rp.order.create({"amount":amount_inr*100,"currency":"INR","receipt":receipt,"notes":{"source":"rayboost"}})

def verify_signature(order_id, payment_id, signature):
    secret = os.getenv("RAZORPAY_KEY_SECRET", "")
    if not secret: return False
    msg = f"{order_id}|{payment_id}".encode()
    expected = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
