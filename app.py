from flask import Flask, jsonify, request, Response
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import threading
import os
from datetime import datetime

app = Flask(__name__)
CORS(app)

atm_data = {"terminals": [], "alerts": [], "last_updated": None, "errors": []}

HTML = open("index.html").read() if os.path.exists("index.html") else "<h1>Loading...</h1>"

def get_credentials():
    return {
        "myterminals": {"username": os.environ.get("MT_USER",""), "password": os.environ.get("MT_PASS","")},
        "perativ":     {"username": os.environ.get("PV_USER",""), "password": os.environ.get("PV_PASS","")},
        "threshold":   float(os.environ.get("THRESHOLD","500"))
    }

def parse_tables(html, source):
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2: continue
        headers = [c.get_text(strip=True).lower() for c in rows[0].find_all(["th","td"])]
        for row in rows[1:]:
            cells = [c.get_text(strip=True) for c in row.find_all("td")]
            if not cells or all(c=="" for c in cells): continue
            obj = {"source":source,"name":cells[0],"amount":find_amount(cells)}
            for i,h in enumerate(headers):
                if i<len(cells): obj[h]=cells[i]
            results.append(obj)
    return results

def find_amount(cells):
    import re
    for cell in cells:
        cleaned = re.sub(r'[$,\s]','',str(cell))
        try:
            v=float(cleaned)
            if 0<=v<10_000_000: return v
        except: continue
    return None

def scrape_myterminals(user,pwd):
    s=requests.Session()
    s.headers["User-Agent"]="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    url="https://secure.myterminals.com/SPS/login/login.aspx?ReturnUrl=%2FSPS%2Fdefault.aspx"
    r=s.get(url,timeout=20)
    soup=BeautifulSoup(r.text,"html.parser")
    payload={}
    for inp in soup.find_all("input"):
        if inp.get("name"): payload[inp["name"]]=inp.get("value","")
    uf=soup.find("input",{"type":"text"})
    pf=soup.find("input",{"type":"password"})
    bf=soup.find("input",{"type":"submit"})
    if uf: payload[uf["name"]]=user
    if pf: payload[pf["name"]]=pwd
    if bf: payload[bf["name"]]=bf.get("value","Login")
    r2=s.post(url,data=payload,timeout=20,allow_redirects=True)
    print(f"MT status:{r2.status_code} url:{r2.url}")
    t=parse_tables(r2.text,"myterminals")
    print(f"MT found:{len(t)}")
    return t

def scrape_perativ(user,pwd):
    s=requests.Session()
    s.headers["User-Agent"]="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    url="https://webapps.perativ.com/Account/Login"
    r=s.get(url,timeout=20)
    soup=BeautifulSoup(r.text,"html.parser")
    payload={"UserName":user,"Password":pwd,"RememberMe":"false"}
    token=soup.find("input",{"name":"__RequestVerificationToken"})
    if token: payload["__RequestVerificationToken"]=token["value"]
    r2=s.post(url,data=payload,timeout=20,allow_redirects=True)
    print(f"PV status:{r2.status_code} url:{r2.url}")
    t=parse_tables(r2.text,"perativ")
    if not t:
        r3=s.get("https://webapps.perativ.com/",timeout=15)
        t=parse_tables(r3.text,"perativ")
    print(f"PV found:{len(t)}")
    return t

def refresh():
    global atm_data
    creds=get_credentials()
    all_t,errors=[],[]
    mt=creds["myterminals"]; pv=creds["perativ"]; th=creds["threshold"]
    print(f"Refresh MT:{mt['username']} PV:{pv['username']}")
    if mt.get("username"):
        try: all_t+=scrape_myterminals(mt["username"],mt["password"])
        except Exception as e: errors.append(f"MT:{e}"); print(f"MT error:{e}")
    if pv.get("username"):
        try: all_t+=scrape_perativ(pv["username"],pv["password"])
        except Exception as e: errors.append(f"PV:{e}"); print(f"PV error:{e}")
    alerts=[]
    for t in all_t:
        amt=t.get("amount")
        if amt is not None:
            if amt==0: alerts.append({"type":"empty","name":t["name"],"amount":amt,"msg":f"VIDE: {t['name']}"})
            elif amt<th: alerts.append({"type":"low","name":t["name"],"amount":amt,"msg":f"BAS: {t['name']} - ${amt:,.0f}"})
    atm_data={"terminals":all_t,"alerts":alerts,"last_updated":datetime.now().isoformat(),"errors":errors}
    print(f"Done:{len(all_t)} terminals,{len(alerts)} alerts,{len(errors)} errors")

@app.route("/")
def index():
    return Response(HTML, mimetype="text/html")

@app.route("/api/data")
def get_data():
    return jsonify(atm_data)

@app.route("/api/credentials", methods=["POST"])
def set_creds():
    data=request.json
    mt=data.get("myterminals",{}); pv=data.get("perativ",{})
    if mt.get("username"): os.environ["MT_USER"]=mt["username"]
    if mt.get("password"): os.environ["MT_PASS"]=mt["password"]
    if pv.get("username"): os.environ["PV_USER"]=pv["username"]
    if pv.get("password"): os.environ["PV_PASS"]=pv["password"]
    if data.get("threshold"): os.environ["THRESHOLD"]=str(data["threshold"])
    threading.Thread(target=refresh,daemon=True).start()
    return jsonify({"status":"ok"})

@app.route("/api/refresh", methods=["POST"])
def do_refresh():
    threading.Thread(target=refresh,daemon=True).start()
    return jsonify({"status":"started"})

if __name__=="__main__":
    port=int(os.environ.get("PORT",5000))
    if os.environ.get("MT_USER") or os.environ.get("PV_USER"):
        threading.Thread(target=refresh,daemon=True).start()
    app.run(host="0.0.0.0",port=port)
