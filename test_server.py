import requests
try:
    r = requests.get("http://localhost:8000/")
    print("GET / :", r.status_code)
    r = requests.post("http://localhost:8000/api/run", json={"ticket_text": "Need to configure Contacts app.", "db_url": "https://sane1.odoo.com/odoo"})
    print("POST /api/run :", r.status_code)
    print(r.text[:500])
except Exception as e:
    print(e)
