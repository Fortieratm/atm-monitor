from flask import Flask, jsonify, request, Response, send_file
from flask_cors import CORS
import threading
import time
import os
import re
from datetime import datetime
from playwright.sync_api import sync_playwright

app = Flask(__name__, static_folder='.', static_url_path='')
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
            page.goto("https://secure.myterminals.com/SPS/login/login.aspx?ReturnUrl=%2FSPS%2Fdefault.aspx", timeout=30000)
            page.wait_for_load_state("networkidle", timeout=15000)
            page.fill("input[type=text]", user)
            page.fill("input[type=password]", pwd)
            page.click("input[type=submit]")
            page.wait_for_load_state("networkidle", timeout=15000)
            page.goto("https://secure.myterminals.com/SPS/addins/TerminalManager/Views.aspx", timeout=30000)
            page.wait_for_load_state("networkidle", timeout=15000)
            page.wait_for_timeout(3000)
            rows = page.query_selector_all("table tr")
            print(f"MT rows: {len(rows)}")
            header_row = None
            data_rows = []
            for i, row in enumerate(rows):
                tds = row.query_selector_all("td,th")
                cells = [td.inner_text().strip() for td in tds]
                if not cells or all(c=="" for c in cells): continue
                if any("terminal id" in c.lower() for c in cells):
                    header_row = cells
                elif header_row and len(cells) >= 5:
                    data_rows.append(cells)
            if header_row:
                headers_lower = [h.lower() for h in header_row]
                id_idx = next((i for i,h in enumerate(headers_lower) if "terminal id" in h), 0)
                name_idx = next((i for i,h in enumerate(headers_lower) if "location" in h), 1)
                amount_idx = next((i for i,h in enumerate(headers_lower) if "total cassette" in h or ("cassette" in h and "value" in h)), None)
                if amount_idx is None and data_rows:
                    for i, cell in enumerate(data_rows[0]):
                        if "$" in cell:
                            amount_idx = i
                            break
                for cells in data_rows:
                    if len(cells) < 3: continue
                    terminal_id = cells[id_idx] if id_idx < len(cells) else ""
                    name = cells[name_idx] if name_idx < len(cells) else cells[0]
                    amount = None
                    if amount_idx is not None and amount_idx < len(cells):
                        amount = find_amount(cells[amount_idx])
                    if amount is None:
                        for cell in cells:
                            if "$" in cell:
                                amt = find_amount(cell)
                                if amt is not None:
                                    amount = amt
                                    break
                    if terminal_id and len(terminal_id) > 3 and " " not in terminal_id:
                        terminals.append({"source":"myterminals","name":name,"terminal_id":terminal_id,"amount":amount})
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
            page.wait_for_load_state("networkidle", timeout=15000)
            page.wait_for_timeout(2000)
            for selector in ["input[name='UserName']","input[type='text']","#UserName"]:
                try:
                    page.fill(selector, user, timeout=5000)
                    break
                except: continue
            page.fill("input[type='password']", pwd)
            for selector in ["button[type='submit']","input[type='submit']","button:has-text('Login')"]:
                try:
                    page.click(selector, timeout=3000)
                    break
                except: continue
            page.wait_for_load_state("networkidle", timeout=20000)
            page.wait_for_timeout(5000)
            page.goto("https://webapps.perativ.com/portal/", timeout=30000)
            page.wait_for_load_state("networkidle", timeout=20000)
            page.wait_for_timeout(5000)
            for selector in ["text=Terminal List","button:has-text('Terminal List')","a:has-text('Terminal List')"]:
                try:
                    page.click(selector, timeout=5000)
                    break
                except: continue
            page.wait_for_timeout(4000)
            rows = page.query_selector_all("table tr, .k-grid tr, [role='row']")
            headers = []
            for i, row in enumerate(rows):
                cells_els = row.query_selector_all("td, th, [role='gridcell'], [role='columnheader']")
                cells = [c.inner_text().strip() for c in cells_els]
                if not cells or all(c=="" for c in cells): continue
                if i == 0 or any(h in " ".join(cells).lower() for h in ["terminal","location","model"]):
                    headers = [c.lower() for c in cells]
                    continue
                if not headers or len(cells) < 2: continue
                name_idx = next((j for j,h in enumerate(headers) if "location" in h or "name" in h), 1)
                amount_idx = next((j for j,h in enumerate(headers) if "txn" in h or "cash" in h or "$" in h), None)
                name = cells[name_idx] if name_idx < len(cells) else cells[0]
                amount = None
                if amount_idx is not None and amount_idx < len(cells):
                    amount = find_amount(cells[amount_idx])
                if amount is None:
                    for cell in cells:
                        if "$" in cell:
                            amt = find_amount(cell)
                            if amt is not None:
                                amount = amt
                                break
                if name and name not in ["","Location","Terminal"]:
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
            if amt == 0: alerts.append({"type":"empty","name":t["name"],"amount":amt,"msg":f"VIDE: {t['name']}"})
            elif amt < th: alerts.append({"type":"low","name":t["name"],"amount":amt,"msg":f"BAS: {t['name']} - ${amt:,.0f}"})
    atm_data = {"terminals":all_t,"alerts":alerts,"last_updated":datetime.now().isoformat(),"errors":errors}
    print(f"=== Done:{len(all_t)} terminals,{len(alerts)} alerts ===")

def auto_refresh():
    while True:
        time.sleep(900)
        try: refresh()
        except Exception as e: print(f"Auto-refresh error: {e}")

@app.route("/")
def index():
    with open("index.html") as f:
        return Response(f.read(), mimetype="text/html")

@app.route("/Cyber_25.jpg")
def logo():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    paths_to_try = [
        os.path.join(base_dir, "Cyber_25.jpg"),
        "Cyber_25.jpg",
        "/app/Cyber_25.jpg",
    ]
    for path in paths_to_try:
        if os.path.exists(path):
            print(f"Serving logo from: {path}")
            return send_file(path, mimetype="image/jpeg")
    print(f"Logo not found. Tried: {paths_to_try}")
    return Response("Logo not found", status=404)

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
        threading.Thread(target=auto_refresh, daemon=True).start()
    app.run(host="0.0.0.0", port=port)
