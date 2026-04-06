"""Diagnostic quickInfo payload probe for HKUST portal API.

Rationale:
- Detects shape drift (new/removed fields, count changes, type shifts).
- Supports safe extractor updates in collector.py before production rollout.

This script is intentionally not part of production collector flow.
"""

import json, os
from urllib.request import Request, urlopen

env = {}
with open(os.path.expanduser('~/hkust-ev-collector/.env'), 'r', encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        env[k.strip()] = v.strip()

base = 'https://ust-ev.cstl.com.hk/portal/api/api'

body = json.dumps({'username': env['PORTAL_USERNAME'], 'password': env['PORTAL_PASSWORD']}).encode('utf-8')
req = Request(base + '/authenticate', data=body, headers={'Content-Type': 'application/json', 'Accept': 'application/json'})
with urlopen(req, timeout=15) as r:
    auth = json.loads(r.read().decode('utf-8'))
token = auth['response']['token']
headers = {'Authorization': token, 'Accept': 'application/json', 'Content-Type': 'application/json'}

req = Request(base + '/v2/quickInfo', headers=headers)
with urlopen(req, timeout=15) as r:
    data = json.loads(r.read().decode('utf-8'))

print('=== LOCATIONS ===')
for loc in data.get('cpLocQuickInfoDTOS', []):
    print(f"  locId={loc.get('locId')}  address={loc.get('address')}")

print('\n=== ENERGY SUMMARY ===')
summary = data.get('cpSummaryByDayRangeQuickInfoDTO', {})
for period in ['yesterdaySummary', 'todaySummary', 'last7daysSummary']:
    s = summary.get(period, {})
    print(f"  {period}: duration={s.get('duration')}  kwh={s.get('kwh')}")

print('\n=== FLEET USAGE ===')
usage = data.get('cpUsage', {})
for k, v in usage.items():
    print(f"  {k}={v}")

print('\n=== CONNECTOR QUICK INFO (cpQuickInfoDTOS) ===')
cps = data.get('cpQuickInfoDTOS', [])
print(f"  Total entries: {len(cps)}")
# Print all keys from first entry
if cps:
    print(f"  All keys: {list(cps[0].keys())}")
    # Print all entries with non-null kw (currently charging)
    charging = [c for c in cps if c.get('kw') is not None and c.get('kw') != 0]
    print(f"  Currently delivering power: {len(charging)}")
    for c in charging:
        print(f"    {c}")
    print(f"\n  First 5 entries (all fields):")
    for c in cps[:5]:
        print(f"    {json.dumps(c)}")
