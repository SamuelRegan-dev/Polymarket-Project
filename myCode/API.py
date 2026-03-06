# The following code calls market closing times from polymarket and searches for very large sales made close to the closing time
# It's next goal is to search through every market, and then store that data on my laptop via. docker
import requests
from datetime import datetime, timezone, timedelta
import time

active_markets = []
current_time = time.time()
UTCtime = datetime.fromtimestamp(current_time, timezone.utc)

all_markets = []
offset = 0
limit = 100  # Adjust based on API limits


tradeResponse = requests.get("https://clob.polymarket.com/data/trades")
trades = tradeResponse.json()

response = requests.get("https://gamma-api.polymarket.com/markets")
markets = response.json()
# Look at the first market to see what fields exist
if len(markets) > 0:
    print("\nFirst market's fields:")
    print(markets[0].keys())

    print("\nFirst market full data:")
    print(markets[0])

print(f"Total trades found: {len(trades)}")

# Look at the first market to see what fields exist
if len(trades) > 0:
    print("\nFirst trades's fields:")
    print(trades[0].keys())

    print("\nFirst trade full data:")
    print(trades[0])

counter1 = 0
while True:
    params = {
        'limit': limit,
        'offset': offset
    }
    # Get all markets
    tradeResponse = requests.get("https://data-api.polymarket.com/trades")
    trades = tradeResponse.json()

    response = requests.get("https://gamma-api.polymarket.com/markets", params=params)
    markets = response.json()

    if not markets:  # No more data
        break

    all_markets.extend(markets)
    offset += limit

    for market in markets:
        if market.get("closed") or not market.get("endDate"):
            continue
        print(f"Fetched {len(all_markets)} markets so far...")
        print(f"Total markets: {len(all_markets)}")
        print(f"Total trades of interest found: {counter1}")
        endDate = datetime.fromisoformat(market["endDate"].replace("Z", "+00:00"))
        unixEndDate = endDate.timestamp()
        if UTCtime-endDate <= timedelta(hours=48):
            print("Found a market closing within 48 hours.")
            for t in trades:
                if not all(k in t for k in ["timestamp", "size", "price"]):
                    continue

                ts = int(t["timestamp"])
                size = float(t["size"])
                price = float(t["price"])
                volume = float(size*price)

                if ts>(unixEndDate-(48*60*60)):
                    if volume>20000:
                        print(f"FOUND YOU!")
                        counter1 += 1
                        continue
                    else:
                        print(f"found trades closing within 48 hours, but no large sale yet")
        else:
            print("not closing anytime soon...")




"""
# Loop through markets closing soon
for market in markets:
    if market.get('endDateIso'):
        end_time = datetime.fromisoformat(market['endDateIso'].replace('Z', '+00:00')).timestamp()
        current_time = time.time()

        #ignore closed markets
        if market.get('closed') or end_time < current_time:
            continue

        hours_until_close = (end_time - current_time) / 3600
        active_markets.append({
            'question': market['question'],
            'hours': hours_until_close,
            'days': hours_until_close / 24 })

        if (end_time - current_time) < 48 * 3600:
            print(f"Closing soon: {market['question']}")



            # NOW get trades for THIS market (inside the loop)
            response = requests.get(
                "https://data-api.polymarket.com/trades",
                params={
                    "market": market['market'],
                    "limit": 100
                }
            )

            trades = response.json()

            # Filter for large bets
            whales = [t for t in trades if t['size'] * t['price'] > 5000]

            if whales:
                print(f"  Found {len(whales)} large bets on this market!")
                for whale in whales:
                    print(f"    ${whale['size'] * whale['price']:,.2f} bet")
            else:
                print(f"  No large bets on this market!")

active_markets.sort(key=lambda x: x['hours'])

print(f"Total active markets: {len(active_markets)}\n")
print("Next 20 markets to close:")
for m in active_markets[:20]:
    print(f"{m['days']:.1f} days ({m['hours']:.0f} hours) - {m['question'][:80]}")
"""


## threads
## write to an excel