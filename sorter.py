import pandas as pd

FILE_NAME = "GUNB_Enriched_Scored_Leads.xlsx"

print("Sortowanie pliku...")
df = pd.read_excel(FILE_NAME)

# Ustawiamy hierarchię statusów
df['Status'] = pd.Categorical(
    df['Status'],
    categories=['POZYTYWNY', 'DO_WERYFIKACJI', 'ODRZUCONY'],
    ordered=True
)

# Sortujemy: najpierw po Statusie, potem po Potencjale (od 10 do 0)
df = df.sort_values(by=['Status', 'Potencjał (1-10)'], ascending=[True, False])

# Zapisujemy
df.to_excel(FILE_NAME, index=False)
print(f"Gotowe! Plik {FILE_NAME} został idealnie posegregowany.")