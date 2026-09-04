import os, sqlite3, json

DB_PATH = os.getenv("DB_PATH", "./rayboost.db")

PRODUCTS = [
    {"id":"p1","name":"CodePro Laptop","description":"14-inch development laptop for coding, college and everyday work.","category":"laptops","price":65000,"stock":12,"use_cases":["coding","college","development"],"compatible_with":["p2","p3","p6"],"frequently_bought_with":["p2","p3"],"margin":0.18},
    {"id":"p2","name":"DevMouse Wireless","description":"Comfortable wireless mouse for coding and office work.","category":"accessories","price":1200,"stock":45,"use_cases":["coding","office","gaming"],"compatible_with":["p1","p4"],"frequently_bought_with":["p1","p4"],"margin":0.32},
    {"id":"p3","name":"Ergo Laptop Stand","description":"Adjustable laptop stand for a more comfortable development setup.","category":"accessories","price":1800,"stock":27,"use_cases":["coding","office"],"compatible_with":["p1"],"frequently_bought_with":["p1"],"margin":0.38},
    {"id":"p4","name":"MechKey Keyboard","description":"Mechanical keyboard for coding and gaming.","category":"accessories","price":3200,"stock":18,"use_cases":["coding","gaming","office"],"compatible_with":["p1","p2"],"frequently_bought_with":["p1","p2"],"margin":0.29},
    {"id":"p5","name":"Studio Headphones","description":"Closed-back headphones for music, coding and gaming.","category":"audio","price":4800,"stock":21,"use_cases":["music","coding","gaming"],"compatible_with":["p1","p4"],"frequently_bought_with":["p1"],"margin":0.26},
    {"id":"p6","name":"USB-C Hub","description":"USB-C expansion hub for development and college setups.","category":"accessories","price":2200,"stock":34,"use_cases":["coding","college","development"],"compatible_with":["p1"],"frequently_bought_with":["p1","p3"],"margin":0.35},
]

SALES = [
    ("o101","p1",65000,1),("o102","p1",65000,1),("o103","p1",65000,1),
    ("o104","p1",65000,1),("o104","p2",1200,1),("o105","p1",65000,1),
    ("o105","p3",1800,1),("o106","p2",1200,1),("o107","p4",3200,1),
    ("o108","p1",65000,1),("o108","p2",1200,1),("o109","p5",4800,1),
]

def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c = conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS products(id TEXT PRIMARY KEY, data TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sales(id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT, product_id TEXT, amount INTEGER, qty INTEGER);
    CREATE TABLE IF NOT EXISTS campaigns(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, product_ids TEXT, discount INTEGER, status TEXT, reason TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT, action TEXT, reason TEXT, amount INTEGER DEFAULT 0, status TEXT, meta TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS carts(id TEXT PRIMARY KEY, items TEXT NOT NULL, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS checkout_orders(id INTEGER PRIMARY KEY AUTOINCREMENT, cart_id TEXT NOT NULL, internal_order_id TEXT UNIQUE NOT NULL, razorpay_order_id TEXT UNIQUE NOT NULL, amount INTEGER NOT NULL, currency TEXT NOT NULL, status TEXT NOT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS buyer_events(id INTEGER PRIMARY KEY AUTOINCREMENT, buyer_id TEXT NOT NULL, event_type TEXT NOT NULL, product_id TEXT, query TEXT, meta TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS purchase_intents(id INTEGER PRIMARY KEY AUTOINCREMENT, buyer_id TEXT NOT NULL, cart_id TEXT NOT NULL, fingerprint TEXT NOT NULL, state TEXT NOT NULL, status TEXT NOT NULL, subtotal INTEGER NOT NULL, discount_percent INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL, items TEXT NOT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP);

    """)
    # Upgrade 04 migration: keep the opportunity that created each campaign draft.
    try:
        c.execute("ALTER TABLE campaigns ADD COLUMN opportunity_id TEXT")
    except sqlite3.OperationalError:
        pass
    for stmt in [
        "ALTER TABLE checkout_orders ADD COLUMN buyer_id TEXT",
        "ALTER TABLE checkout_orders ADD COLUMN items TEXT",
        "ALTER TABLE checkout_orders ADD COLUMN intent_id INTEGER"
    ]:
        try: c.execute(stmt)
        except sqlite3.OperationalError: pass
    if c.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0:
        c.executemany("INSERT INTO products(id,data) VALUES (?,?)", [(p["id"], json.dumps(p)) for p in PRODUCTS])
    if c.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 0:
        c.executemany("INSERT INTO sales(order_id,product_id,amount,qty) VALUES (?,?,?,?)", SALES)
    c.commit(); c.close()

def products():
    c=conn(); rows=c.execute("SELECT data FROM products").fetchall(); c.close(); return [json.loads(r[0]) for r in rows]

def get_product_by_id(product_id):
    c=conn(); row=c.execute("SELECT data FROM products WHERE id=?",(product_id,)).fetchone(); c.close(); return json.loads(row[0]) if row else None

def audit(actor, action, reason, amount=0, status="INFO", meta=None):
    c=conn(); c.execute("INSERT INTO audit(actor,action,reason,amount,status,meta) VALUES (?,?,?,?,?,?)", (actor,action,reason,amount,status,json.dumps(meta or {}))); c.commit(); c.close()

def audits(limit=100):
    c=conn(); rows=c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?",(limit,)).fetchall(); c.close(); return [dict(r) for r in rows]

def campaigns():
    c=conn(); rows=c.execute("SELECT * FROM campaigns ORDER BY id DESC").fetchall(); c.close(); return [dict(r) for r in rows]

def create_campaign_draft(name, product_ids, discount, reason, opportunity_id):
    if discount < 0 or discount > 10: raise ValueError("Policy allows a maximum 10% discount")
    c=conn(); cur=c.execute("INSERT INTO campaigns(name,product_ids,discount,status,reason,opportunity_id) VALUES (?,?,?,?,?,?)",(name,json.dumps(product_ids),discount,"DRAFT",reason,opportunity_id)); c.commit(); cid=cur.lastrowid; c.close()
    audit("campaign_agent","campaign.draft_created",f"Draft campaign created from approved opportunity: {name}",status="DRAFT",meta={"campaign_id":cid,"opportunity_id":opportunity_id,"discount":discount})
    return cid

def get_campaign_by_id(campaign_id):
    c=conn(); row=c.execute("SELECT * FROM campaigns WHERE id=?",(campaign_id,)).fetchone(); c.close(); return dict(row) if row else None

def update_campaign_status(campaign_id, status, reason=None):
    c=conn()
    if reason is None:
        c.execute("UPDATE campaigns SET status=? WHERE id=?",(status,campaign_id))
    else:
        c.execute("UPDATE campaigns SET status=?, reason=? WHERE id=?",(status,reason,campaign_id))
    c.commit(); c.close()

def create_campaign(name, product_ids, discount, reason):
    if discount < 0 or discount > 10: raise ValueError("Policy allows a maximum 10% discount")
    c=conn(); cur=c.execute("INSERT INTO campaigns(name,product_ids,discount,status,reason) VALUES (?,?,?,?,?)",(name,json.dumps(product_ids),discount,"ACTIVE",reason)); c.commit(); cid=cur.lastrowid; c.close()
    audit("merchant","campaign.create",reason,status="APPROVED",meta={"campaign_id":cid,"discount":discount})
    return cid

def get_cart(cart_id):
    c=conn(); row=c.execute("SELECT items FROM carts WHERE id=?",(cart_id,)).fetchone(); c.close()
    return json.loads(row[0]) if row else []

def save_cart(cart_id, items):
    c=conn(); c.execute("INSERT INTO carts(id,items) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET items=excluded.items, updated_at=CURRENT_TIMESTAMP", (cart_id, json.dumps(items))); c.commit(); c.close()

def clear_cart(cart_id):
    save_cart(cart_id, [])


def get_checkout_by_cart(cart_id):
    c=conn(); row=c.execute("SELECT * FROM checkout_orders WHERE cart_id=? AND status IN ('CREATED','PENDING') ORDER BY id DESC LIMIT 1",(cart_id,)).fetchone(); c.close(); return dict(row) if row else None

def get_checkout_by_razorpay(razorpay_order_id):
    c=conn(); row=c.execute("SELECT * FROM checkout_orders WHERE razorpay_order_id=?",(razorpay_order_id,)).fetchone(); c.close(); return dict(row) if row else None

def create_checkout(cart_id, internal_order_id, razorpay_order_id, amount, currency='INR', buyer_id=None, items=None, intent_id=None):
    c=conn(); c.execute("INSERT INTO checkout_orders(cart_id,internal_order_id,razorpay_order_id,amount,currency,status,buyer_id,items,intent_id) VALUES (?,?,?,?,?,?,?,?,?)",(cart_id,internal_order_id,razorpay_order_id,amount,currency,'CREATED',buyer_id,json.dumps(items or []),intent_id)); c.commit(); c.close()

def update_checkout_status(razorpay_order_id, status):
    c=conn(); c.execute("UPDATE checkout_orders SET status=?, updated_at=CURRENT_TIMESTAMP WHERE razorpay_order_id=?",(status,razorpay_order_id)); c.commit(); c.close()

def checkout_orders(limit=50):
    c=conn(); rows=c.execute("SELECT * FROM checkout_orders ORDER BY id DESC LIMIT ?",(limit,)).fetchall(); c.close(); return [dict(r) for r in rows]


# Upgrade 05 — Buyer Intelligence event store
def record_buyer_event(buyer_id, event_type, product_id=None, query=None, meta=None):
    if not buyer_id or not event_type:
        return
    c=conn(); c.execute("INSERT INTO buyer_events(buyer_id,event_type,product_id,query,meta) VALUES (?,?,?,?,?)", (buyer_id,event_type,product_id,query,json.dumps(meta or {}))); c.commit(); c.close()

def buyer_events(buyer_id, limit=100):
    c=conn(); rows=c.execute("SELECT * FROM buyer_events WHERE buyer_id=? ORDER BY id DESC LIMIT ?",(buyer_id,limit)).fetchall(); c.close(); return [dict(r) for r in rows]


def create_purchase_intent(buyer_id, cart_id, fingerprint, state, subtotal, discount_percent, total, items):
    c=conn(); cur=c.execute("INSERT INTO purchase_intents(buyer_id,cart_id,fingerprint,state,status,subtotal,discount_percent,total,items) VALUES (?,?,?,?,?,?,?,?,?)",(buyer_id,cart_id,fingerprint,state,'PENDING',subtotal,discount_percent,total,json.dumps(items))); c.commit(); iid=cur.lastrowid; c.close(); return iid

def get_purchase_intent(intent_id):
    c=conn(); row=c.execute("SELECT * FROM purchase_intents WHERE id=?",(intent_id,)).fetchone(); c.close();
    if not row: return None
    d=dict(row); d['items']=json.loads(d.get('items') or '[]'); return d

def latest_purchase_intent(buyer_id, cart_id):
    c=conn(); row=c.execute("SELECT * FROM purchase_intents WHERE buyer_id=? AND cart_id=? ORDER BY id DESC LIMIT 1",(buyer_id,cart_id)).fetchone(); c.close();
    if not row: return None
    d=dict(row); d['items']=json.loads(d.get('items') or '[]'); return d

def confirm_purchase_intent(intent_id):
    c=conn(); c.execute("UPDATE purchase_intents SET state='CHECKOUT_READY', status='CONFIRMED', updated_at=CURRENT_TIMESTAMP WHERE id=?",(intent_id,)); c.commit(); c.close()

def update_purchase_intent_state(intent_id, state, status=None):
    c=conn()
    if status is None: c.execute("UPDATE purchase_intents SET state=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",(state,intent_id))
    else: c.execute("UPDATE purchase_intents SET state=?, status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",(state,status,intent_id))
    c.commit(); c.close()
