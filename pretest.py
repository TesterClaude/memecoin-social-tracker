import re
import requests
from bs4 import BeautifulSoup

CHANNEL = "degenonesol"

SOLANA_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")

url = f"https://t.me/s/{CHANNEL}"
resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
print("HTTP-Status:", resp.status_code)

soup = BeautifulSoup(resp.text, "html.parser")
blocks = soup.select("div.tgme_widget_message")
messages = [b.select_one("div.tgme_widget_message_text") for b in blocks]
messages = [m for m in messages if m]
print("Gefundene Nachrichten:", len(messages))

hits = 0
for msg in messages[-20:]:
    text = msg.get_text(" ", strip=True)
    addrs = SOLANA_RE.findall(text)
    tickers = re.findall(r"\$(?![0-9.,]+[KMB]?\b)[A-Za-z][A-Za-z0-9]{1,9}", text)
    if addrs or tickers:
        hits += 1
        print("---")
        print("Text:", text[:160])
        print("Adressen:", addrs)
        print("Ticker:", tickers)

print(f"\n{hits} von {min(20, len(messages))} Nachrichten hatten Ticker oder Adressen.")