MAX_DISCOUNT_PERCENT = 10
MAX_AUTOMATIC_ORDER_INR = 100000

def check_discount(discount: int):
    return {"allowed": 0 <= discount <= MAX_DISCOUNT_PERCENT, "max_discount": MAX_DISCOUNT_PERCENT, "requested": discount}

def check_order(amount: int):
    return {"allowed": 0 < amount <= MAX_AUTOMATIC_ORDER_INR, "limit": MAX_AUTOMATIC_ORDER_INR, "amount": amount}
