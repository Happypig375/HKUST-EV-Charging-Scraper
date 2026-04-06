"""Diagnostic endpoint probe for HKUST portal API.

Rationale:
- Keeps API discovery reproducible without browser automation.
- Helps remap reachable endpoints when vendor routes or permissions change.

This script is intentionally not part of production collector flow.
"""

import json, os
from urllib.request import Request, urlopen
from urllib.error import HTTPError

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


def get(path):
    req = Request(base + path, headers=headers)
    with urlopen(req, timeout=15) as r:
        return r.getcode(), json.loads(r.read().decode('utf-8'))


def all_paths(node, prefix='', depth=0):
    if depth > 4:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            p = f'{prefix}.{k}' if prefix else k
            yield p
            yield from all_paths(v, p, depth + 1)
    elif isinstance(node, list) and node:
        yield f'{prefix}[]'
        yield from all_paths(node[0], f'{prefix}[]', depth + 1)


candidates = [
    '/v2/quickInfo',
    '/v2/charger', '/v2/charger/list',
    '/v2/session', '/v2/session/list',
    '/v2/dashboard',
    '/v2/cp', '/v2/transaction',
    '/v2/chargepoint', '/v2/liveData', '/v2/live', '/v2/telemetry',
    '/chargepoint', '/chargepoint/list',
]

for path in candidates:
    try:
        code, data = get(path)
        resp = data.get('response', data) if isinstance(data, dict) else data
        n = len(resp) if isinstance(resp, list) else -1
        p = list(dict.fromkeys(all_paths(data)))[:25]
        print(f'OK   {path}  HTTP={code}  list_len={n}')
        print(f'     paths={p}')
        if isinstance(resp, list) and resp:
            print(f'     first_item={json.dumps(resp[0], ensure_ascii=True)[:400]}')
        elif isinstance(resp, dict):
            print(f'     data={json.dumps(resp, ensure_ascii=True)[:400]}')
    except HTTPError as e:
        print(f'ERR  {path}  HTTP={e.code}')
    except Exception as e:
        print(f'ERR  {path}  {type(e).__name__}: {str(e)[:80]}')
