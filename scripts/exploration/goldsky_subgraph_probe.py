import requests
import json

endpoint = "https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/subgraphs/activity-subgraph/0.0.4/gn"

query = """
{
  redemptions(
    first: 5
    where: { condition: "0xe3b423dfad8c22ff75c9899c4e8176f628cf4ad4caa00481764d320e7415f7a9" }
  ) {
    condition
    indexSets
    payout
    timestamp
  }
}
"""

r = requests.post(endpoint, json={"query": query})
print(json.dumps(r.json(), indent=2))