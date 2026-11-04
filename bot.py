import os, json, time, statistics, hashlib
from pathlib import Path
import requests

API_BASE = "https://api.getgems.io/public-api"
API_KEY = os.getenv("GETGEMS_API_KEY", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
COLLECTIONS = [x.strip() for x in os.getenv("COLLECTION_ADDRESSES", "").split(",") if x.strip()]
DISCOUNT = float(os.getenv("DISCOUNT_PERCENT", "20"))
STATE_FILE = Path("state.json")
HEADERS = {"accept": "application/json"}
if API_KEY:
    HEADERS["Authorization"] = API_KEY

def get_json(url, params=None):
    r = requests.get(url, headers=HEADERS, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("success") is False:
        raise RuntimeError(str(data))
    return data.get("response", data) if isinstance(data, dict) else data

def items_from(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "nfts", "results", "data"):
            if isinstance(data.get(key), list):
                return data[key]
    return []

def amount_ton(value, currency="TON"):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    # Getgems examples represent TON values in nanotons (1e9).
    if n > 100000:
        n /= 1_000_000_000
    return n if currency.upper() == "TON" else None

def listing_price(item):
    sale = item.get("sale") or item.get("price") or {}
    if isinstance(sale, (int, float, str)):
        return amount_ton(sale)
    currency = str(sale.get("currency", "TON"))
    for key in ("fullPrice", "price", "amount", "minBid", "value"):
        if sale.get(key) is not None:
            return amount_ton(sale[key], currency)
    return None

def nft_id(item):
    return str(item.get("address") or item.get("nftAddress") or item.get("id") or "")

def item_name(item):
    return str(item.get("name") or item.get("title") or "Telegram Gift NFT")

def item_url(item):
    addr = nft_id(item)
    return str(item.get("url") or (f"https://getgems.io/nft/{addr}" if addr else "https://getgems.io"))

def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"seen": []}

def save_state(state):
    # Keep a bounded history so the repository state stays small.
    state["seen"] = state.get("seen", [])[-5000:]
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))

def telegram_send(text):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Set BOT_TOKEN and CHAT_ID in GitHub repository secrets.")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, json={
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True
    }, timeout=30)
    r.raise_for_status()

def main():
    if not API_KEY:
        raise RuntimeError(
            "GETGEMS_API_KEY is missing. Getgems Read API currently requires an API key "
            "(the official docs state 10 GRAM/month)."
        )
    if not COLLECTIONS:
        raise RuntimeError("Add one or more collection addresses to COLLECTION_ADDRESSES.")
    state = load_state()
    seen = set(state.get("seen", []))
    fresh = []
    errors = []

    for collection in COLLECTIONS:
        try:
            # Official Getgems Read API endpoint for currently listed NFTs.
            url = f"{API_BASE}/v1/nfts/on-sale/{collection}"
            data = get_json(url, params={"limit": 100})
            rows = items_from(data)
            priced = [(x, listing_price(x)) for x in rows]
            values = sorted(p for _, p in priced if p is not None and p > 0)
            # A low ask is flagged relative to the median of the cheapest 20
            # listings in this collection, not as a guaranteed profit signal.
            baseline = statistics.median(values[:20]) if len(values) >= 5 else None

            for item, price in priced:
                address = nft_id(item)
                if not address or address in seen:
                    continue
                seen.add(address)
                fresh.append((item, price, baseline, collection))
        except Exception as e:
            errors.append(f"{collection[:10]}…: {e}")

    messages = []
    for item, price, baseline, collection in fresh:
        name = item_name(item)
        url = item_url(item)
        attrs = item.get("attributes") or []
        attr_text = ", ".join(
            f"{a.get('trait_type') or a.get('name')}: {a.get('value')}"
            for a in attrs[:5] if isinstance(a, dict)
        )
        text = f"🆕 Новое объявление NFT\n{name}\n"
        if price is not None:
            text += f"Цена: {price:.4f} TON\n"
        if baseline is not None and price is not None:
            discount = (1 - price / baseline) * 100
            if discount >= DISCOUNT:
                text += f"⚡ На {discount:.1f}% ниже медианы 20 самых дешёвых объявлений\n"
            else:
                text += f"Ориентир (медиана дешёвых объявлений): {baseline:.4f} TON\n"
        if attr_text:
            text += f"Атрибуты: {attr_text}\n"
        text += f"Коллекция: {collection[:10]}…\n{url}"
        messages.append(text)

    # Notify only on newly observed listings; no spam for unchanged items.
    for msg in messages[:30]:
        telegram_send(msg)
        time.sleep(0.4)

    state["seen"] = list(seen)
    save_state(state)

    if errors:
        print("Some collection scans failed:", " | ".join(errors))
    print(f"Scanned {len(COLLECTIONS)} collection(s); new listings: {len(fresh)}; alerts sent: {min(len(messages), 30)}")

if __name__ == "__main__":
    main()
