from fastapi.testclient import TestClient
from server import app

client = TestClient(app)
try:
    response = client.get("/")
    print(response.status_code)
    print(response.text)
except Exception as e:
    import traceback
    traceback.print_exc()
