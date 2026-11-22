from flask import Flask, render_template, request, jsonify, send_from_directory, url_for, Response
import sqlite3, os, json, socket, threading, time, secrets
import requests
from datetime import datetime, date

app = Flask(__name__)
DB = os.getenv("VALA_DB_PATH", os.path.join(os.path.dirname(__file__), "vala.db"))
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "cloud_menu.json")
CLOUD_MODE = os.getenv("VALA_CLOUD_MENU", "0") == "1"
SYNC_TOKEN = os.getenv("VALA_MENU_SYNC_TOKEN", "")
_sync_started = False
_sync_lock = threading.Lock()


def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS products(
      id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, category TEXT NOT NULL,
      price INTEGER NOT NULL DEFAULT 0, stock REAL NOT NULL DEFAULT 0, unit TEXT DEFAULT 'عدد',
      active INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS orders(
      id INTEGER PRIMARY KEY AUTOINCREMENT, table_no INTEGER NOT NULL, status TEXT DEFAULT 'open',
      created_at TEXT NOT NULL, total INTEGER DEFAULT 0, note TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS order_items(
      id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL, product_id INTEGER,
      name TEXT NOT NULL, category TEXT, qty REAL NOT NULL, price INTEGER NOT NULL,
      FOREIGN KEY(order_id) REFERENCES orders(id)
    );
    CREATE TABLE IF NOT EXISTS payments(
      id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL, amount INTEGER NOT NULL,
      method TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS expenses(
      id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, amount INTEGER NOT NULL,
      category TEXT DEFAULT 'عمومی', created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS public_menu_items(
      id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
      price INTEGER NOT NULL DEFAULT 0, active INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS app_settings(
      key TEXT PRIMARY KEY, value TEXT NOT NULL
    );
    INSERT OR IGNORE INTO app_settings(key,value) VALUES('table_count','12');
    """)
    count = c.execute("SELECT COUNT(*) n FROM products").fetchone()["n"]
    if count == 0:
        seed = [
          ("چای", "کافه", 45000, 0, "عدد"), ("اسپرسو", "کافه", 85000, 0, "عدد"),
          ("لاته", "کافه", 125000, 0, "عدد"), ("موهیتو", "کافه", 135000, 0, "عدد"),
          ("چیزبرگر", "غذا", 290000, 0, "عدد"), ("پاستا آلفردو", "غذا", 320000, 0, "عدد"),
          ("سیب‌زمینی", "غذا", 150000, 0, "عدد"), ("قلیان معمولی", "قلیان", 280000, 0, "عدد"),
          ("قلیان ویژه", "قلیان", 390000, 0, "عدد")
        ]
        c.executemany("INSERT INTO products(name,category,price,stock,unit) VALUES(?,?,?,?,?)", seed)
    c.commit(); c.close()

def stamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

@app.route("/")
def home():
    return render_template("index.html")

@app.get("/api/tables")
def get_tables():
    c=db()
    row=c.execute("SELECT value FROM app_settings WHERE key='table_count'").fetchone()
    c.close()
    return jsonify(count=int(row["value"]) if row else 12)

@app.put("/api/tables")
def update_tables():
    d=request.json or {}
    try:
        count=int(d.get("count",0))
    except (TypeError, ValueError):
        return jsonify(error="تعداد میز معتبر نیست."),400
    if count < 1 or count > 200:
        return jsonify(error="تعداد میز باید بین ۱ تا ۲۰۰ باشد."),400
    c=db()
    highest=c.execute("SELECT COALESCE(MAX(table_no),0) n FROM orders WHERE status='open'").fetchone()["n"]
    if count < highest:
        c.close()
        return jsonify(error=f"میزهای بالاتر از {count} سفارش باز دارند؛ ابتدا آن سفارش‌ها را تسویه کنید."),400
    c.execute("INSERT INTO app_settings(key,value) VALUES('table_count',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(count),))
    c.commit(); c.close()
    return jsonify(ok=True,count=count)

@app.get("/public-menu")
def public_menu():
    return render_template("public_menu.html")

@app.get("/api/public-menu")
def public_menu_data():
    c=db()
    table = "public_menu_items" if CLOUD_MODE else "products"
    rows=c.execute(f"SELECT id,name,category,price FROM {table} WHERE active=1 ORDER BY category,name").fetchall()
    c.close()
    response = jsonify([dict(x) for x in rows])
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response

def read_cloud_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data=json.load(f)
            return {"url": str(data.get("url", "")).strip().rstrip("/"), "token": str(data.get("token", "")).strip()}
    except (OSError, ValueError, TypeError):
        return {"url":"", "token":""}

def sync_menu_once():
    if CLOUD_MODE:
        return False, "این نسخه سرور منوی آنلاین است."
    cfg=read_cloud_config()
    if not cfg["url"] or not cfg["token"]:
        return False, "آدرس سرور و کلید همگام‌سازی هنوز تنظیم نشده است."
    with _sync_lock:
        c=db()
        rows=c.execute("SELECT id,name,category,price FROM products WHERE active=1 ORDER BY category,name").fetchall()
        c.close()
        payload={"items":[dict(r) for r in rows]}
        try:
            r=requests.post(cfg["url"]+"/api/sync-menu", json=payload,
                headers={"Authorization":"Bearer "+cfg["token"]}, timeout=12)
            r.raise_for_status()
            return True, "منوی آنلاین به‌روز شد."
        except requests.RequestException as e:
            return False, "همگام‌سازی انجام نشد: "+str(e)

def sync_worker():
    while True:
        sync_menu_once()
        time.sleep(60)

def start_sync_worker():
    global _sync_started
    if not CLOUD_MODE and not _sync_started:
        _sync_started=True
        threading.Thread(target=sync_worker, daemon=True, name="vala-menu-sync").start()

@app.post("/api/sync-menu")
def receive_menu_sync():
    if not CLOUD_MODE:
        return jsonify(error="این نشانی برای دریافت همگام‌سازی پیکربندی نشده است."),404
    supplied=request.headers.get("Authorization", "")
    if not SYNC_TOKEN or not secrets.compare_digest(supplied, "Bearer "+SYNC_TOKEN):
        return jsonify(error="دسترسی غیرمجاز."),401
    data=request.get_json(silent=True) or {}
    items=data.get("items")
    if not isinstance(items,list) or len(items)>5000:
        return jsonify(error="اطلاعات منو معتبر نیست."),400
    cleaned=[]
    try:
        for item in items:
            name=str(item.get("name", "")).strip()
            category=str(item.get("category", "")).strip()
            price=int(item.get("price", 0))
            pid=int(item.get("id"))
            if not name or not category or price<0 or pid<1:
                raise ValueError
            cleaned.append((pid,name,category,price,1))
    except (AttributeError,TypeError,ValueError):
        return jsonify(error="یکی از محصولات اطلاعات نامعتبر دارد."),400
    c=db()
    c.execute("BEGIN IMMEDIATE")
    c.execute("DELETE FROM public_menu_items")
    c.executemany("INSERT INTO public_menu_items(id,name,category,price,active) VALUES(?,?,?,?,?)",cleaned)
    c.commit(); c.close()
    return jsonify(ok=True, count=len(cleaned), updated_at=stamp())

@app.get("/api/cloud-config")
def cloud_config_get():
    cfg=read_cloud_config()
    return jsonify(url=cfg["url"], configured=bool(cfg["url"] and cfg["token"]))

@app.put("/api/cloud-config")
def cloud_config_save():
    if CLOUD_MODE:
        return jsonify(error="تنظیمات از نسخه صندوق قابل انجام است."),400
    data=request.get_json(silent=True) or {}
    url=str(data.get("url", "")).strip().rstrip("/")
    token=str(data.get("token", "")).strip()
    if url and not url.startswith("https://"):
        return jsonify(error="آدرس سرور باید با https:// شروع شود."),400
    if bool(url) != bool(token):
        return jsonify(error="برای فعال‌سازی، هم آدرس سرور و هم کلید لازم است."),400
    try:
        with open(CONFIG_FILE,"w",encoding="utf-8") as f:
            json.dump({"url":url,"token":token},f,ensure_ascii=False)
    except OSError:
        return jsonify(error="ذخیره تنظیمات ممکن نشد."),500
    if url:
        ok,msg=sync_menu_once()
        return jsonify(ok=ok,message=msg,configured=True)
    return jsonify(ok=True,message="همگام‌سازی ابری غیرفعال شد.",configured=False)

def public_menu_url():
    cfg=read_cloud_config()
    if not CLOUD_MODE and cfg["url"]:
        return cfg["url"]+"/public-menu"
    if CLOUD_MODE:
        return request.url_root.rstrip("/")+"/public-menu"
    host=request.host.split(":",1)[0]
    port=request.environ.get("SERVER_PORT", "5000")
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        try:
            probe=socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect(("192.0.2.1",80)); host=probe.getsockname()[0]; probe.close()
        except OSError:
            host="127.0.0.1"
    return f"http://{host}:{port}/public-menu"

@app.get("/api/menu-url")
def menu_url():
    cfg=read_cloud_config()
    return jsonify(url=public_menu_url(), online_configured=bool(CLOUD_MODE or (cfg["url"] and cfg["token"])))

@app.get("/menu-qr.png")
def menu_qr():
    try:
        import qrcode
        from io import BytesIO
        target=public_menu_url()
        qr=qrcode.QRCode(box_size=9,border=3)
        qr.add_data(target); qr.make(fit=True)
        image=qr.make_image(fill_color="#173b32",back_color="white")
        output=BytesIO(); image.save(output,format="PNG")
        return Response(output.getvalue(),mimetype="image/png",headers={"Cache-Control":"no-store"})
    except ImportError:
        return jsonify(error="برای ساخت QR، بسته qrcode را نصب کنید."),500

@app.get("/api/products")
def products():
    c=db()
    rows=c.execute("SELECT * FROM products WHERE active=1 ORDER BY category,name").fetchall()
    c.close()
    return jsonify([dict(x) for x in rows])

@app.post("/api/products")
def add_product():
    d=request.json or {}
    c=db()
    c.execute("INSERT INTO products(name,category,price,stock,unit) VALUES(?,?,?,?,?)",
      (d.get("name","").strip(), d.get("category","غذا"), int(d.get("price",0)), float(d.get("stock",0)), d.get("unit","عدد")))
    c.commit(); c.close()
    sync_menu_once()
    return jsonify(ok=True)

@app.put("/api/products/<int:pid>")
def update_product(pid):
    d=request.json or {}
    name=str(d.get("name", "")).strip()
    category=str(d.get("category", "غذا")).strip()
    try:
        price=int(d.get("price", 0))
        stock=float(d.get("stock", 0))
    except (TypeError, ValueError):
        return jsonify(error="قیمت یا موجودی معتبر نیست."), 400
    if not name or price < 0 or stock < 0:
        return jsonify(error="نام، قیمت و موجودی را درست وارد کنید."), 400
    c=db()
    cur=c.execute("UPDATE products SET name=?,category=?,price=?,stock=?,unit=? WHERE id=? AND active=1",
      (name,category,price,stock,str(d.get("unit", "عدد")).strip() or "عدد",pid))
    c.commit(); c.close()
    if cur.rowcount == 0:
        return jsonify(error="محصول پیدا نشد."),404
    sync_menu_once()
    return jsonify(ok=True)

@app.delete("/api/products/<int:pid>")
def delete_product(pid):
    # Soft-delete: keep old order history intact while hiding the product from the menu.
    c=db()
    cur=c.execute("UPDATE products SET active=0 WHERE id=? AND active=1",(pid,))
    c.commit(); c.close()
    if cur.rowcount == 0:
        return jsonify(error="محصول پیدا نشد."),404
    sync_menu_once()
    return jsonify(ok=True)

@app.get("/api/orders")
def orders():
    c=db()
    rows=c.execute("SELECT * FROM orders WHERE status='open' ORDER BY id DESC").fetchall()
    out=[]
    for r in rows:
        items=c.execute("SELECT * FROM order_items WHERE order_id=?", (r["id"],)).fetchall()
        out.append({**dict(r), "items":[dict(i) for i in items]})
    c.close()
    return jsonify(out)

@app.post("/api/orders")
def create_order():
    d=request.json or {}
    table=int(d.get("table_no",1))
    c=db()
    setting=c.execute("SELECT value FROM app_settings WHERE key='table_count'").fetchone()
    table_count=int(setting["value"]) if setting else 12
    if table < 1 or table > table_count:
        c.close()
        return jsonify(error="این شماره میز در تنظیمات فعال نیست."),400
    row=c.execute("SELECT id FROM orders WHERE table_no=? AND status='open'",(table,)).fetchone()
    if row:
        oid=row["id"]
        # The client submits the complete current cart; replace prior lines to avoid duplicates.
        c.execute("DELETE FROM order_items WHERE order_id=?", (oid,))
    else:
        cur=c.execute("INSERT INTO orders(table_no,created_at,note) VALUES(?,?,?)",(table,stamp(),d.get("note","")))
        oid=cur.lastrowid
    for item in d.get("items",[]):
        pid=item.get("product_id")
        p=c.execute("SELECT * FROM products WHERE id=?",(pid,)).fetchone() if pid else None
        if not p: continue
        qty=float(item.get("qty",1))
        c.execute("INSERT INTO order_items(order_id,product_id,name,category,qty,price) VALUES(?,?,?,?,?,?)",
          (oid,p["id"],p["name"],p["category"],qty,p["price"]))
    total=c.execute("SELECT COALESCE(SUM(qty*price),0) n FROM order_items WHERE order_id=?",(oid,)).fetchone()["n"]
    c.execute("UPDATE orders SET total=? WHERE id=?",(int(total),oid))
    c.commit(); c.close()
    return jsonify(ok=True,order_id=oid)

@app.post("/api/orders/<int:oid>/pay")
def pay(oid):
    d=request.json or {}
    amount=int(d.get("amount",0)); method=d.get("method","کارت")
    c=db()
    order=c.execute("SELECT * FROM orders WHERE id=? AND status='open'",(oid,)).fetchone()
    if not order:
        c.close(); return jsonify(error="سفارش پیدا نشد"),404
    paid=c.execute("SELECT COALESCE(SUM(amount),0) n FROM payments WHERE order_id=?",(oid,)).fetchone()["n"]
    due=max(0,int(order["total"])-int(paid))
    amount=due if amount<=0 else min(amount,due)
    c.execute("INSERT INTO payments(order_id,amount,method,created_at) VALUES(?,?,?,?)",(oid,amount,method,stamp()))
    newpaid=paid+amount
    if newpaid>=order["total"]:
        c.execute("UPDATE orders SET status='closed' WHERE id=?",(oid,))
    c.commit(); c.close()
    return jsonify(ok=True,paid=amount,remaining=max(0,int(order["total"])-int(newpaid)),closed=newpaid>=order["total"])

@app.post("/api/expenses")
def expense():
    d=request.json or {}
    c=db()
    c.execute("INSERT INTO expenses(title,amount,category,created_at) VALUES(?,?,?,?)",
      (d.get("title","هزینه"),int(d.get("amount",0)),d.get("category","عمومی"),stamp()))
    c.commit(); c.close()
    return jsonify(ok=True)

@app.get("/api/summary")
def summary():
    today=date.today().isoformat()
    c=db()
    sales=c.execute("SELECT COALESCE(SUM(amount),0) n FROM payments WHERE substr(created_at,1,10)=?",(today,)).fetchone()["n"]
    expenses=c.execute("SELECT COALESCE(SUM(amount),0) n FROM expenses WHERE substr(created_at,1,10)=?",(today,)).fetchone()["n"]
    open_orders=c.execute("SELECT COUNT(*) n FROM orders WHERE status='open'").fetchone()["n"]
    closed_count=c.execute("SELECT COUNT(*) n FROM orders WHERE status='closed' AND substr(created_at,1,10)=?",(today,)).fetchone()["n"]
    c.close()
    return jsonify(sales=int(sales),expenses=int(expenses),net=int(sales)-int(expenses),open_orders=open_orders,closed_count=closed_count)

@app.get("/api/report")
def report():
    start=request.args.get("from", "").strip()
    end=request.args.get("to", "").strip()
    if start and end and start>end:
        return jsonify(error="تاریخ شروع نباید بعد از تاریخ پایان باشد."),400
    where_pay=[]; args_pay=[]; where_exp=[]; args_exp=[]
    if start:
        where_pay.append("substr(created_at,1,10)>=?"); args_pay.append(start)
        where_exp.append("substr(created_at,1,10)>=?"); args_exp.append(start)
    if end:
        where_pay.append("substr(created_at,1,10)<=?"); args_pay.append(end)
        where_exp.append("substr(created_at,1,10)<=?"); args_exp.append(end)
    pay_clause=(" WHERE "+" AND ".join(where_pay)) if where_pay else ""
    exp_clause=(" WHERE "+" AND ".join(where_exp)) if where_exp else ""
    c=db()
    rows=c.execute("SELECT substr(created_at,1,10) day, SUM(amount) amount FROM payments"+pay_clause+" GROUP BY substr(created_at,1,10) ORDER BY day DESC",args_pay).fetchall()
    exp=c.execute("SELECT substr(created_at,1,10) day, SUM(amount) amount FROM expenses"+exp_clause+" GROUP BY substr(created_at,1,10) ORDER BY day DESC",args_exp).fetchall()
    c.close()
    return jsonify(sales=[dict(x) for x in rows],expenses=[dict(x) for x in exp])

@app.get("/api/report/orders")
def report_orders():
    """Paid orders for report history; supports payment-date range filtering."""
    start=request.args.get("from", "").strip(); end=request.args.get("to", "").strip()
    if start and end and start>end:
        return jsonify(error="تاریخ شروع نباید بعد از تاریخ پایان باشد."),400
    filters=[]; args=[]
    if start: filters.append("substr(p.created_at,1,10)>=?"); args.append(start)
    if end: filters.append("substr(p.created_at,1,10)<=?"); args.append(end)
    clause=(" AND "+" AND ".join(filters)) if filters else ""
    c=db()
    rows=c.execute("""SELECT o.*, COALESCE(SUM(p.amount),0) paid,
      MAX(p.created_at) paid_at, GROUP_CONCAT(DISTINCT p.method) methods
      FROM orders o JOIN payments p ON p.order_id=o.id
      WHERE 1=1"""+clause+" GROUP BY o.id ORDER BY paid_at DESC",args).fetchall()
    result=[]
    for row in rows:
        order=dict(row)
        order["items"]=[dict(x) for x in c.execute(
            "SELECT name,category,qty,price FROM order_items WHERE order_id=? ORDER BY id",(row["id"],)).fetchall()]
        result.append(order)
    c.close()
    return jsonify(result)

@app.get("/manifest.json")
def manifest():
    return send_from_directory("static","manifest.json")

@app.get("/sw.js")
def sw():
    return send_from_directory("static","sw.js",mimetype="application/javascript")

if __name__ == "__main__":
    init_db()
    start_sync_worker()
    mode = "CLOUD PUBLIC MENU" if CLOUD_MODE else "LOCAL POS"
    print(f"\nVALA {mode} is running: http://127.0.0.1:5000\n")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
