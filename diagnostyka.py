import os
import requests
from dotenv import load_dotenv

load_dotenv()
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")

domain = "perlipol.com.pl"
url = "https://api.tavily.com/search"
payload = {
    "api_key": TAVILY_API_KEY,
    "query": f"site:{domain} kontakt",
    "include_raw_content": True,
    "max_results": 2
}

print(f"Pobieranie surowego kodu dla {domain}...")
try:
    res = requests.post(url, json=payload, timeout=15)
    if res.status_code == 200:
        results = res.json().get("results", [])
        raw_html = " ".join([r.get("raw_content", "") for r in results if r.get("raw_content")])

        # Zapisujemy do pliku, żeby łatwo było przeszukać
        with open("perlipol_raw_dump.txt", "w", encoding="utf-8") as f:
            f.write(raw_html)

        print(f"Sukces! Zapisano {len(raw_html)} znaków do perlipol_raw_dump.txt")

        # Szukamy "ręcznie" znaku małpy
        if "@" in raw_html:
            print("ZNAK @ ZNALEZIONY W KODZIE! (Gemini zawiodło)")
        else:
            print("BRAK ZNAKU @ W KODZIE! (Tavily jest ślepe / strona blokuje bota)")

    else:
        print(f"Błąd API: {res.status_code} - {res.text}")
except Exception as e:
    print(f"Błąd: {e}")