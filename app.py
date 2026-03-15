from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import threading
import time
import os
from datetime import datetime

app = Flask(__name__)
CORS(app)

atm_data = {"terminals": [], "alerts": [], "last_updated": None, "errors": []}
credentials = {"myterminals": {}, "perativ": {}}
THRESHOLD = 500

def parse_tables(html, source):
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        headers = [c.get_text(strip=True).lower() for c in rows[0].find_all(["th","td"])]
        for row in rows[1:]:
            cells = [c.get_text(strip=True) for c in row.find_all("td")]
            if not cells or all(c == "" for c in cells):
                continue
            obj = {"source": source, "name": cells[0], "amount": find_amount(cells)}
            for i, h in enumerate(headers):
                if i < len(cells):
                    obj[h] = cells[i]
            results.append(obj)
    return results

def find_amount(cells):
    import re
    for cell in cells:
        cleaned = re.sub(r'[$,\s]', '', str(cell))
        try:
            v = float(cleaned)
            if 0 <= v < 10_000_000:
                return v
        except:
            continue
    return None

def scrape_myterminals(user, pwd):
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0"
    url = "https://secure.myterminals.com/SPS/login/login.aspx?ReturnUrl=%2FSPS%2Fdefault.aspx"
    r = s.get(url, timeout=15)
    soup = BeautifulSoup(r.text, "html.parser")
    payload = {}
    for inp in soup.find_all("input"):
        if inp.get("name"):
            payload[inp["name"]] = inp.get("value", "")
    ufield = soup.find("input", {"type": "text"})
    pfield = soup.find("input", {"type": "password"})
    bfield = soup.find("input", {"type": "submit"})
    if ufield: payload[ufield["name"]] = user
    if pfield: payload[pfield["name"]] = pwd
    if bfield: payload[bfield["name"]] = bfield.get("value", "Login")
    r2 = s.post(url, data=payload, timeout=15, allow_redirects=True)
    return parse_tables(r2.text, "myterminals")

def scrape_perativ(user, pwd):
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0"
    url = "https://webapps.perativ.com/Account/Login"
    r = s.get(url, timeout=15)
    soup = BeautifulSoup(r.text, "html.parser")
    payload = {"UserName": user, "Password": pwd, "RememberMe": "false"}
    token = soup.find("input", {"name": "__RequestVerificationToken"})
    if token:
        payload["__RequestVerificationToken"] = token["value"]
    r2 = s.post(url, data=payload, timeout=15, allow_redirects=True)
    terminals = parse_tables(r2.text, "perativ")
    if not terminals:
        r3 = s.get("https://webapps.perativ.com/", timeout=10)
        terminals = parse_tables(r3.text, "perativ")
    return terminals

def refresh():
    global atm_data
    all_t, errors = [], []
    mt = credentials.get("myterminals", {})
    pv = credentials.get("perativ", {})
    if mt.get("username"):
        try:
            all_t += scrape_myterminals(mt["username"], mt["password"])
        except Exception as e:
            errors.append(f"MyTerminals: {str(e)}")
    if pv.get("username"):
        try:
            all_t += scrape_perativ(pv["username"], pv["password"])
        except Exception as e:
            errors.append(f"Perativ: {str(e)}")
    threshold = float(os.environ.get("THRESHOLD", THRESHOLD))
    alerts = []
    for t in all_t:
        amt = t.get("amount")
        if amt is not None:
            if amt == 0:
                alerts.append({"type":"empty","name":t["name"],"amount":amt,"msg":f"🚨 VIDE: {t['name']}"})
            elif amt < threshold:
                alerts.append({"type":"low","name":t["name"],"amount":amt,"msg":f"⚠️ BAS: {t['name']} — ${amt:,.0f}"})
    atm_data = {"terminals": all_t, "alerts": alerts, "last_updated": datetime.now().isoformat(), "errors": errors}
    print(f"Refreshed: {len(all_t)} terminals, {len(alerts)} alerts, {len(errors)} errors")

@app.route("/")
def index():
    return jsonify({"status": "ok", "terminals": len(atm_data["terminals"])})

@app.route("/api/data")
def get_data():
    return jsonify(atm_data)

@app.route("/api/credentials", methods=["POST"])
def set_creds():
    global credentials
    data = request.json
    if "myterminals" in data: credentials["myterminals"] = data["myterminals"]
    if "perativ" in data: credentials["perativ"] = data["perativ"]
    if "threshold" in data: os.environ["THRESHOLD"] = str(data["threshold"])
    threading.Thread(target=refresh, daemon=True).start()
    return jsonify({"status": "ok"})

@app.route("/api/refresh", methods=["POST"])
def do_refresh():
    threading.Thread(target=refresh, daemon=True).start()
    return jsonify({"status": "started"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
