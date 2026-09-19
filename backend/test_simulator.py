import requests

BASE_URL = "http://localhost:9898"

# -------------------------
# 1. LOGIN
# -------------------------

login_response = requests.post(
    f"{BASE_URL}/api/v1/auth/login",
    json={
        "email": "admin",
        "password": "admin"
    }
)

print("Login status:", login_response.status_code)
print("Login response:", login_response.text)

login_response.raise_for_status()

token = login_response.json()["token"]

print("\nTOKEN RECEIVED!")

# -------------------------
# 2. GET PARKING SPOTS
# -------------------------

headers = {
    "Authorization": f"Bearer {token}"
}

spots_response = requests.get(
    f"{BASE_URL}/api/v1/list-parking-spots",
    headers=headers
)

print("\nParking API status:", spots_response.status_code)

spots_response.raise_for_status()

spots = spots_response.json()

print("Objects received:", len(spots))

for spot in spots:
    print(
        spot["name"],
        "| purpose:", spot["purpose"],
        "| type:", spot["parkingForCarType"],
        "| zone:", spot["zoneParent"],
        "| cars:", spot["detectedCars"]
    )