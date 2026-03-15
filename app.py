from flask import Flask, jsonify, request, Response
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import threading
import os
import re
from datetime import datetime

app = Flask(__name__)
CORS(app)

atm_data = {"terminals": [], "alerts": [], "last_updated": None, "errors": []}

def get_credentials():
    return {
        "myterminals": {"username": os.environ.get("MT_USER",""), "password": os.environ.get("MT_PASS","")},
        "perativ":     {"username": os.environ.get("PV_USER",""), "password": os.environ.get("PV_PASS","")},
        "threshold":   float(os.environ.get("THRESHOLD","500"))
    }

def find_amount(cells):
    for cell in cells:
        cleaned = re.sub(r'[$,\s]', '', str(cell))
        try:
            v = float(cleaned)
            if 0 <= v < 10_000_000: return v
        except: continue
    return None

def parse_html_tables(soup, source):
    results = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2: continue
        headers = [c.get_text(strip=True).lower() for c in rows[0].find_all(["th","td"])]
        print(f"{source} headers: {headers}")
        for row in rows[1:]:
            cells = [c.get_text(strip=True) for c in row.find_all("td")]
            if not cells or all(c=="" for c in cells): continue
            obj = {"source": source, "name": cells[0], "amount": find_amount(cells)}
            for i, h in enumerate(headers):
                if i < len(cells): obj[h] = cells[i]
            results.append(obj)
    return results

def scrape_myterminals(user, pwd):
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    url = "https://secure.myterminals.com/SPS/login/login.aspx?ReturnUrl=%2FSPS%2Fdefault.aspx"
    r = s.get(url, timeout=20)
    soup = BeautifulSoup(r.text, "html.parser")
    payload = {}
    for inp in soup.find_all("input"):
        if inp.get("name"): payload[inp["name"]] = inp.get("value","")
    uf = soup.find("input", {"type":"text"})
    pf = soup.find("input", {"type":"password"})
    bf = soup.find("input", {"type":"submit"})
    if uf: payload[uf["name"]] = user
    if pf: payload[pf["name"]] = pwd
    if bf: payload[bf["name"]] = bf.get("value","Login")
    r2 = s.post(url, data=payload, timeout=20, allow_redirects=True)
    print(f"MT: {r2.status_code} -> {r2.url}")
    soup2 = BeautifulSoup(r2.text, "html.parser")
    t = parse_html_tables(soup2, "myterminals")
    # Try other pages if empty
    if not t:
        for path in ["/SPS/default.aspx", "/SPS/Terminal/List", "/SPS/ATM"]:
            try:
                r3 = s.get(f"https://secure.myterminals.com{path}", timeout=10)
                soup3 = BeautifulSoup(r3.text, "html.parser")
                t = parse_html_tables(soup3, "myterminals")
                print(f"MT {path}: {len(t)} terminals")
                if t: break
            except: pass
    print(f"MT total: {len(t)}")
    return t

def scrape_perativ(user, pwd):
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.8",
    })

    login_url = "https://webapps.perativ.com/Account/Login"
    r = s.get(login_url, timeout=20)
    print(f"PV GET: {r.status_code}")
    soup = BeautifulSoup(r.text, "html.parser")

    payload = {"UserName": user, "Password": pwd, "RememberMe": "false"}
    token = soup.find("input", {"name": "__RequestVerificationToken"})
    if token: payload["__RequestVerificationToken"] = token["value"]

    r2 = s.post(login_url, data=payload,
                headers={"Content-Type":"application/x-www-form-urlencoded","Referer":login_url,"Origin":"https://webapps.perativ.com"},
                timeout=20, allow_redirects=True)
    print(f"PV POST: {r2.status_code} -> {r2.url}")

    # Print page title to debug
    soup2 = BeautifulSoup(r2.text, "html.parser")
    title = soup2.find("title")
    print(f"PV page title: {title.text if title else 'none'}")

    terminals = []

    # Try JSON API endpoints first
    api_endpoints = [
        "/api/terminals", "/api/Terminal", "/api/terminal/list",
        "/api/dashboard", "/api/cashposition", "/api/atm",
        "/Terminal/GetAll", "/Dashboard/Terminals",
    ]
    for ep in api_endpoints:
        try:
            rj = s.get(f"https://webapps.perativ.com{ep}",
                      headers={"Accept":"application/json","X-Requested-With":"XMLHttpRequest"},
                      timeout=10)
            print(f"PV API {ep}: {rj.status_code} | {rj.text[:100]}")
            if rj.status_code == 200 and (rj.text.strip().startswith("[") or rj.text.strip().startswith("{")):
                data = rj.json()
                parsed = parse_json_terminals(data)
                if parsed:
                    terminals.extend(parsed)
                    print(f"PV got {len(parsed)} from {ep}")
                    break
        except Exception as e:
            print(f"PV {ep}: {e}")

    # Try HTML pages
    if not terminals:
        for pg in ["https://webapps.perativ.com/", "https://webapps.perativ.com/Terminal",
                   "https://webapps.perativ.com/Terminals", "https://webapps.perativ.com/Dashboard"]:
            try:
                rp = s.get(pg, timeout=15)
                print(f"PV page {pg}: {rp.status_code} len:{len(rp.text)}")
                sp = BeautifulSoup(rp.text, "html.parser")
                t = parse_html_tables(sp, "perativ")
                if t:
                    terminals.extend(t)
                    print(f"PV got {len(t)} from {pg}")
                    break
                # Log page content snippet for debugging
                print(f"PV page snippet: {rp.text[500:800]}")
            except Exception as e:
                print(f"PV page {pg}: {e}")

    print(f"PV total: {len(terminals)}")
    return terminals

def parse_json_terminals(data):
    results = []
    items = data if isinstance(data, list) else data.get("terminals") or data.get("data") or data.get("items") or []
    for item in items:
        if not isinstance(item, dict): continue
        name = item.get("name") or item.get("location") or item.get("terminalId") or item.get("id","?")
        amount = None
        for k in ["available","cashAvailable","balance","amount","cashPosition","Available","CashAvailable"]:
            if k in item:
                try: amount = float(str(item[k]).replace("$","").replace(",","")); break
                except: pass
        results.append({"source":"perativ","name":str(name),"amount":amount})
    return results

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
