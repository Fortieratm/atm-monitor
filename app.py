from flask import Flask, jsonify, request, Response
from flask_cors import CORS
import threading
import os
import re
from datetime import datetime
from playwright.sync_api import sync_playwright

app = Flask(__name__)
CORS(app)

atm_data = {"terminals": [], "alerts": [], "last_updated": None, "errors": []}

def get_credentials():
    return {
        "myterminals": {"username": os.environ.get("MT_USER",""), "password": os.environ.get("MT_PASS","")},
        "perativ":     {"username": os.environ.get("PV_USER",""), "password": os.environ.get("PV_PASS","")},
        "threshold":   float(os.environ.get("THRESHOLD","500"))
    }

def find_amount(text):
    cleaned = re.sub(r'[$,\s]', '', str(text))
    try:
        v = float(cleaned)
        if 0 <= v < 10_000_000: return v
    except: pass
    return None

def scrape_myterminals(user, pwd):
    terminals = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage"])
        page = browser.new_page()
        try:
            # Login
            page.goto("https://secure.myterminals.com/SPS/login/login.aspx?ReturnUrl=%2FSPS%2Fdefault.aspx", timeout=30000)
            page.wait_for_load_state("networkidle", timeout=15000)
            page.fill("input[type=text]", user)
            page.fill("input[type=password]", pwd)
            page.click("input[type=submit]")
            page.wait_for_load_state("networkidle", timeout=15000)
            print(f"MT after login: {page.url}")

            # Click Terminal Management
            page.click("text=Terminal Management")
            page.wait_for_load_state("networkidle", timeout=15000)
            page.wait_for_timeout(2000)
            print(f"MT after click: {page.url}")

            # Find the table
            rows = page.query_selector_all("table tr")
            print(f"MT rows found: {len(rows)}")

            if rows:
                # Get headers from first row
                headers = [th.inner_text().strip().lower() for th in rows[0].query_selector_all("th,td")]
                print(f"MT headers: {headers}")

                # Find column indices
                name_idx = next((i for i,h in enumerate(headers) if "location" in h), 1)
                amount_idx = next((i for i,h in enumerate(headers) if "total cassette" in h or "cassette value" in h), None)
                id_idx = next((i for i,h in enumerate(headers) if "terminal id" in h or "terminal" == h), 0)

                print(f"MT cols - id:{id_idx} name:{name_idx} amount:{amount_idx}")

                for row in rows[1:]:
                    cells = [td.inner_text().strip() for td in row.query_selector_all("td")]
                    if not cells or len(cells) < 2: continue
                    name = cells[name_idx] if name_idx < len(cells) else cells[0]
                    terminal_id = cells[id_idx] if id_idx < len(cells) else ""
                    amount = None
                    if amount_idx is not None and amount_idx < len(cells):
                        amount = find_amount(cells[amount_idx])
                    
                    terminals.append({
                        "source": "myterminals",
                        "name": name,
                        "terminal_id": terminal_id,
                        "amount": amount
                    })

            print(f"MT found: {len(terminals)}")
        except Exception as e:
            print(f"MT error: {e}")
        finally:
            browser.close()
    return terminals

def scrape_perativ(user, pwd):
    terminals = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage"])
        page = browser.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36")
        try:
            page.goto("https://webapps.perativ.com/Account/Login", timeout=30000)
            page.wait_for_load_state("networkidle", timeout=10000)
            page.wait_for_timeout(2000)
            print(f"PV login: {page.url}")

            # Try different input selectors
            for selector in ["input[name='UserName']", "input[type='text']", "input[id='UserName']", "#UserName"]:
                try:
                    page.fill(selector, user, timeout=5000)
                    print(f"PV filled username with: {selector}")
                    break
                except: continue

            page.fill("input[type='password']", pwd)

            for selector in ["button[type='submit']", "input[type='submit']", "button:has-text('Login')", "button:has-text('Sign')"]:
                try:
                    page.click(selector, timeout=3000)
                    break
                except: continue

            page.wait_for_load_state("networkidle", timeout=15000)
            page.wait_for_timeout(3000)
            print(f"PV after login: {page.url} title: {page.title()}")

            # Get all table rows
            rows = page.query_selector_all("table tr")
            print(f"PV rows: {len(rows)}")

            if rows:
                headers = [th.inner_text().strip().lower() for th in rows[0].query_selector_all("th,td")]
                print(f"PV headers: {headers}")

                name_idx = next((i for i,h in enumerate(headers) if "location" in h or "name" in h), 0)
                amount_idx = next((i for i,h in enumerate(headers) if "available" in h or "cash" in h or "balance" in h), None)

                for row in rows[1:]:
                    cells = [td.inner_text().strip() for td in row.query_selector_all("td")]
                    if not cells or len(cells) < 2: continue
                    name = cells[name_idx] if name_idx < len(cells) else cells[0]
                    amount = None
                    if amount_idx is not None and amount_idx < len(cells):
                        amount = find_amount(cells[amount_idx])
                    terminals.append({"source":"perativ","name":name,"amount":amount})

            print(f"PV found: {len(terminals)}")
        except Exception as e:
            print(f"PV error: {e}")
        finally:
            browser.close()
    return terminals

def refresh():
    global atm_data
    creds = get_credentials()
    all_t, errors = [], []
    mt = creds["myterminals"]; pv = creds["perativ"]; th = creds["threshold"]
    print(f"=== Refresh MT:{mt['username']} PV:{pv['username']} ===")

    if mt.get("username"):
        try: all_t += scrape_myterminals(mt["username"], mt["password"])
        except Exception as e: errors.append(f"MT:{e}"); print(f"MT error:{e}")

    if pv.get("username"):
        try: all_t += scrape_perativ(pv["username"], pv["password"])
        except Exception as e: errors.append(f"PV:{e}"); print(f"PV error:{e}")

    alerts = []
    for t in all_t:
        amt = t.get("amount")
        if amt is not None:
            if amt == 0: alerts.append({"type":"empty","name":t["name"],"amount":amt,"msg":f"🚨 VIDE: {t['name']}"})
            elif amt < th: alerts.append({"type":"low","name":t["name"],"amount":amt,"msg":f"⚠️ BAS: {t['name']} — ${amt:,.0f}"})

    atm_data = {"terminals":all_t,"alerts":alerts,"last_updated":datetime.now().isoformat(),"errors":errors}
    print(f"=== Done:{len(all_t)} terminals,{len(alerts)} alerts ===")

@app.route("/")
def index():
    with open("index.html") as f:
        return Response(f.read(), mimetype="text/html")

@app.route("/api/data")
def get_data():
    return jsonify(atm_data)

@app.route("/api/credentials", methods=["POST"])
def set_creds():
    data = request.json
    mt = data.get("myterminals",{}); pv = data.get("perativ",{})
    if mt.get("username"): os.environ["MT_USER"] = mt["username"]
    if mt.get("password"): os.environ["MT_PASS"] = mt["password"]
    if pv.get("username"): os.environ["PV_USER"] = pv["username"]
    if pv.get("password"): os.environ["PV_PASS"] = pv["password"]
    if data.get("threshold"): os.environ["THRESHOLD"] = str(data["threshold"])
    threading.Thread(target=refresh, daemon=True).start()
    return jsonify({"status":"ok"})

@app.route("/api/refresh", methods=["POST"])
def do_refresh():
    threading.Thread(target=refresh, daemon=True).start()
    return jsonify({"status":"started"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    if os.environ.get("MT_USER") or os.environ.get("PV_USER"):
        threading.Thread(target=refresh, daemon=True).start()
    app.run(host="0.0.0.0", port=port)
