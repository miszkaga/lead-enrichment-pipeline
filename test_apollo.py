import os
import requests
from dotenv import load_dotenv

load_dotenv()
APOLLO_API_KEY = os.environ.get("APOLLO_API_KEY")

HEADERS = {
    "Content-Type": "application/json",
    "Cache-Control": "no-cache",
    "X-Api-Key": APOLLO_API_KEY
}

# Test 1: Sprawdzamy darmowy Search
payload_search = {
    "q_organization_domains": ["noyen.com"], # W głównym skrypcie daliśmy tu stringa, testujemy tablicę
    "person_titles": ["Prezes", "CEO", "Właściciel", "Dyrektor"]
}

print("--- TEST 1: Endpoint Search ---")
res_search = requests.post("https://api.apollo.io/v1/mixed_people/search", headers=HEADERS, json=payload_search)
print(f"Status: {res_search.status_code}")
print(f"Odpowiedź: {res_search.text[:300]}...") # Drukujemy początek odpowiedzi

# Test 2: Sprawdzamy płatny Match
payload_match = {
    "first_name": "Zbigniew",
    "last_name": "Kurant",
    "domain": "noyen.com"
}

print("\n--- TEST 2: Endpoint Match ---")
res_match = requests.post("https://api.apollo.io/v1/people/match", headers=HEADERS, json=payload_match)
print(f"Status: {res_match.status_code}")
print(f"Odpowiedź: {res_match.text[:300]}...")