import urllib.request, json, sys

def list_units(bank_id, state="valid", limit=200):
    url = f"http://hindsight:8888/v1/default/banks/{bank_id}/memories/list?state={state}&limit={limit}"
    req = urllib.request.Request(url, headers={"Authorization": "Bearer Yishengaini12345"})
    data = json.loads(urllib.request.urlopen(req, timeout=10).read())
    return data.get("items", []) or []

target = "Deduplication uses an in-memory LRU"
for bank in ["hindsight-memorial", "hermes-agent", "camofox-opencli"]:
    print(f"== {bank} ==")
    for state in ["valid", "invalidated"]:
        items = list_units(bank, state=state)
        hits = [m for m in items if target.lower() in (m.get("text", "") or "").lower()]
        print(f"  state={state} scanned={len(items)} hits={len(hits)}")
        for h in hits:
            print(f"    id={h.get('id')}")
            print(f"    text={h.get('text')[:200]}")
    print()
