from playwright.sync_api import sync_playwright
from google import genai
from google.genai import types
import time
import json
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise ValueError("Błąd: Nie znaleziono zmiennej GEMINI_API_KEY w pliku .env!")

client = genai.Client(api_key=GEMINI_API_KEY)
OUTPUT_JSON_FILE = "gunb_live_data.json"

# =======================================================
# AUTOMATYCZNE OBLICZANIE DAT (Ostatnie 7 dni)
# =======================================================
dzisiaj = datetime.now()
siedem_dni_temu = dzisiaj - timedelta(days=7)

data_do = dzisiaj.strftime("%Y-%m-%d")           # Dzisiejsza data
data_od = siedem_dni_temu.strftime("%Y-%m-%d")   # Data sprzed 7 dni

# =======================================================
# FILTRY WYSZUKIWANIA I SŁOWA KLUCZOWE
# =======================================================
FILTRY = {
    "slowa_kluczowe": ["hala", "magazyn", "zakład", "centrum logistyczne"],
    "miejscowosc": "",
    "wojewodztwo": "",
    "data_zlozenia_od": data_od,
    "data_zlozenia_do": data_do
}

def solve_captcha_with_gemini(captcha_image_bytes):
    print("🤖 Gemini analizuje CAPTCHA...")
    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=[
            "Read the characters in this CAPTCHA image. Reply ONLY with the characters (letters/numbers) visible. No formatting, no extra text, no spaces.",
            types.Part.from_bytes(data=captcha_image_bytes, mime_type='image/png')
        ]
    )
    return response.text.strip().replace(" ", "")

def scrape_gunb_live():
    wszystkie_leady = []
    zapisane_identyfikatory = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False) # Możesz zmienić na True, gdy system pójdzie na produkcję
        page = browser.new_page()

        print(f"Będę szukać dokumentów z ostatnich 7 dni: od {FILTRY['data_zlozenia_od']} do {FILTRY['data_zlozenia_do']}.")

        for slowo in FILTRY["slowa_kluczowe"]:
            print("\n" + "="*50)
            print(f"🔍 ROZPOCZYNAM WYSZUKIWANIE DLA SŁOWA: '{slowo.upper()}'")
            print("="*50)

            page.goto("https://wyszukiwarka.gunb.gov.pl/")
            page.wait_for_load_state("domcontentloaded")
            time.sleep(2)

            try:
                btn_akceptuj = page.locator('button:has-text("Akceptuję"), button:has-text("Rozumiem")').first
                if btn_akceptuj.is_visible(timeout=3000):
                    btn_akceptuj.click()
            except:
                pass

            print("Trwa wypełnianie formularza...")
            try:
                page.locator('#intention_name').fill(slowo)
                if FILTRY["miejscowosc"]: page.locator('#city').fill(FILTRY["miejscowosc"])
                if FILTRY["wojewodztwo"]: page.locator('#region').select_option(label=FILTRY["wojewodztwo"])
                if FILTRY["data_zlozenia_od"]: page.locator('#date_min').fill(FILTRY["data_zlozenia_od"])
                if FILTRY["data_zlozenia_do"]: page.locator('#date_max').fill(FILTRY["data_zlozenia_do"])
            except Exception as e:
                print(f"❌ Błąd podczas wypełniania formularza dla słowa '{slowo}'. Pomijam.")
                continue

            max_attempts = 5
            attempt = 1
            sukces_formularza = False

            # ETAP PRZEŁAMYWANIA CAPTCHA
            while attempt <= max_attempts:
                print(f"Podejście do CAPTCHA nr {attempt}...")

                try:
                    captcha_img_locator = page.locator('#sec_code_captcha')
                    captcha_img_locator.wait_for(state="visible", timeout=10000)
                    captcha_img_locator.scroll_into_view_if_needed()
                    time.sleep(1)
                    captcha_bytes = captcha_img_locator.screenshot()
                except Exception as e:
                    print("❌ Nie udało się zlokalizować obrazka CAPTCHA na stronie.")
                    break

                try:
                    captcha_text = solve_captcha_with_gemini(captcha_bytes)
                    print(f"🤖 Gemini odczytało kod: {captcha_text}")
                except Exception as e:
                    print(f"❌ Błąd API Gemini: {e}")
                    break

                page.locator('#sec_code').fill(captcha_text)
                page.locator('button[type="submit"], input[type="submit"], button:has-text("Szukaj")').first.click()
                time.sleep(4)

                body_text = page.locator("body").inner_text().lower()
                if "niepoprawny kod" in body_text or "nieprawidłowy kod" in body_text or "błędny kod" in body_text:
                    print("❌ Zły kod CAPTCHA! Odświeżam i próbuję ponownie...")
                    attempt += 1
                    continue

                if page.locator("table.table").is_visible():
                    print(f"✅ Znaleziono wyniki dla słowa '{slowo}'!")
                    sukces_formularza = True
                    break

                if "brak wyników" in body_text or "nie znaleziono" in body_text:
                    print(f"✅ Brak wyników dla słowa '{slowo}'.")
                    sukces_formularza = True
                    break

                attempt += 1

            if not sukces_formularza:
                print(f"Nie udało się przejść CAPTCHA dla słowa '{slowo}'. Przechodzę do następnego.")
                continue

            # ==========================================
            # ETAP POBIERANIA DANYCH I PAGINACJI
            # ==========================================
            nowe_leady_dla_slowa_lacznie = 0
            numer_strony = 1

            while True:
                if not page.locator("table.table").is_visible():
                    break

                print(f"Pobieranie danych ze strony nr {numer_strony}...")
                rows = page.query_selector_all("table.table tbody tr")
                nowe_leady_na_tej_stronie = 0

                for row in rows:
                    cells = row.query_selector_all("td")
                    if len(cells) >= 5:
                        informacje = cells[1].inner_text().strip()
                        zamierzenie = cells[2].inner_text().strip()
                        status = cells[3].inner_text().strip()
                        inwestor = cells[4].inner_text().strip()

                        if "Brak danych" in inwestor and "Brak danych" in zamierzenie:
                            continue

                        unikalne_id = informacje.replace('\n', ' ')

                        if unikalne_id not in zapisane_identyfikatory:
                            zapisane_identyfikatory.add(unikalne_id)
                            nowe_leady_na_tej_stronie += 1
                            nowe_leady_dla_slowa_lacznie += 1

                            # Formatyzujemy pod 2_analyzer.py
                            wszystkie_leady.append({
                                "source": f"GUNB_LIVE ({slowo})",
                                "title": zamierzenie.replace('\n', ' '),
                                "company_name": inwestor.replace('\n', ' '),
                                "location": "Dane w opisie",
                                "url": "https://wyszukiwarka.gunb.gov.pl/",
                                "text": f"Status: {status} | Szczegóły: {informacje.replace('\n', ' | ')}"
                            })

                if page.locator('text="Wyszukiwarka pokazuje tylko 100 najnowszych"').is_visible():
                    print("⚠️ Uwaga: Osiągnięto limit 100 wyników (10 stron) nakładany przez urząd.")

                # Zabezpieczenie przed nieskończoną pętlą wyników
                if nowe_leady_na_tej_stronie == 0 and numer_strony > 1:
                    print("⚠️ Pobrano 0 nowych leadów z tej strony (pętla). Kończę pobieranie dla tego słowa.")
                    break

                # --- NAPRAWIONA PAGINACJA ---
                next_btn_link = page.locator('a[aria-label="Następna strona"]').first

                if next_btn_link.is_visible():
                    # Pobieramy atrybut 'class' rodzica (czyli elementu <li>) za pomocą xpath
                    parent_li_class = next_btn_link.locator("xpath=..").get_attribute("class") or ""

                    if "disabled" in parent_li_class:
                        print(f"Koniec wyników dla słowa '{slowo}' (Ostatnia strona).")
                        break
                    else:
                        print(f"➡️ Przechodzę do strony {numer_strony + 1}...")
                        next_btn_link.click()
                        # Czekamy, aż nowa tabela się załaduje
                        page.wait_for_load_state("networkidle")
                        time.sleep(2)
                        numer_strony += 1
                else:
                    print(f"Koniec wyników dla słowa '{slowo}' (Brak przycisku 'Następna strona').")
                    break

            print(f"Zakończono słowo '{slowo}'. Łącznie pobrano {nowe_leady_dla_slowa_lacznie} leadów.")

        print("\n" + "="*50)
        print(f"ZAKOŃCZONO CAŁE WYSZUKIWANIE. Łącznie zebrano unikalnych leadów z GUNB: {len(wszystkie_leady)}")

        with open(OUTPUT_JSON_FILE, "w", encoding="utf-8") as f:
            json.dump(wszystkie_leady, f, ensure_ascii=False, indent=2)

        print(f"Zapisano dane do pliku {OUTPUT_JSON_FILE}. Zamykam przeglądarkę.")
        browser.close()

if __name__ == "__main__":
    scrape_gunb_live()