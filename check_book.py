import requests, json, time
from datetime import datetime

now = time.time() * 1000
r = requests.get('https://gamma-api.polymarket.com/events?series_slug=btc-up-or-down-hourly&active=true&closed=false&limit=20')
events = r.json()
for ev in events:
    m = ev['markets'][0]
    end = m.get('endDate') or m.get('end_date')
    end_ts = datetime.fromisoformat(end.replace('Z', '+00:00')).timestamp() * 1000
    mins = (end_ts - now) / 60000
    if 120 < mins < 180:
        tokens = json.loads(m['clobTokenIds'])
        print(f"Market: {m['question'][:40]}... mins={mins:.0f}")
        for i, t in enumerate(tokens):
            # All three calls at same time
            book = requests.get(f'https://clob.polymarket.com/book?token_id={t}', timeout=10).json()
            price_buy = requests.get(f'https://clob.polymarket.com/price?token_id={t}&side=BUY', timeout=10).json()
            price_sell = requests.get(f'https://clob.polymarket.com/price?token_id={t}&side=SELL', timeout=10).json()
            
            asks = book.get('asks', [])
            bids = book.get('bids', [])
            best_ask = min(asks, key=lambda x: float(x['price'])) if asks else None
            best_bid = max(bids, key=lambda x: float(x['price'])) if bids else None
            
            print(f"  Token {i}:")
            print(f"    get_price BUY:  {price_buy}")
            print(f"    get_price SELL: {price_sell}")
            print(f"    Best ask: {best_ask}")
            print(f"    Best bid: {best_bid}")
        break
