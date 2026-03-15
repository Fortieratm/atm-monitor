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

def scrape_tables(page, source):
    terminals = []
    tables = page.query_selector_all("table")
    print(f"{source} tables: {len(tables)}")
    for table in tables:
        rows = table.query_selector_all("tr")
        if len(rows) < 2: continue
        headers = [th.inner_text().strip().lower() for th in rows[0].query_selector_all("th,td")]
        print(f"{source} headers: {headers}")
        for row in rows[1:]:
            cells = [td.inner_text().strip() for td in row.query_selector_all("td")]
            if not cells or all(c=="" for c in cells): continue
            obj = {"source":source,"name":cells[0],"amount":None}
            for i,h in enumerate(headers):
                if i < len(cells): obj[h] = cells[i]
            for key in ["available","cash available","balance","amount","cash position","cassette"]:
                if key in obj:
                    amt = find_amount(obj[key])
                    if amt is not None:
                        obj["amount"] = amt
                        break
            terminals.append(obj)
    return terminals

def scrape_myterminals(user, pwd):
    terminals = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage"])
        page = browser.new_page()
        try:
            page.goto("https://secure.myterminals.com/SPS/login/login.aspx?ReturnUrl=%2FSPS%2Fdefault.aspx", timeout=30000)
            page.fill("input[type=text]", user)
            page.fill("input[type=password]", pwd)
            page.click("input[type=submit]")
            page.wait_for_load_state("networkidle", timeout=15000)
            print(f"MT after login: {page.url}")
            terminals = scrape_tables(page, "myterminals")
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
            print(f"PV login page: {page.url}")
            page.wait_for_timeout(2000)

            # Fill login
            page.fill("input[name='UserName']", user)
            page.fill("input[name='Password']", pwd)
            
            # Click login button
            for selector in ["button[type='submit']","input[type='submit']","button:has-text('Login')","button:has-text('Sign in')"]:
                try:
                    page.click(selector, timeout=3000)
                    break
                except: continue

            page.wait_for_load_state("networkidle", timeout=15000)
            page.wait_for_timeout(3000)
            print(f"PV after login: {page.url}")
            print(f"PV page title: {page.title()}")

            terminals = scrape_tables(page, "perativ")

            # Try navigation if no tables
            if not terminals:
                for nav in ["https://webapps.perativ.com/","https://webapps.perativ.com/Terminal","https://webapps.perativ.com/Dashboard"]:
                    try:
                        page.goto(nav, timeout=10000)
                        page.wait_for_timeout(2000)
                        t = scrape_tables(page, "perativ")
                        if t:
                            terminals.extend(t)
                            break
                    except Exception as e:
                        print(f"PV nav {nav}: {e}")

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
