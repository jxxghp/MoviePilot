"""固定只读聚合程序与独立假世界验收；不把任意模型代码带入真实宿主。"""

import json
from collections import Counter
from typing import Any

CODE_REPORT = '''import json
from collections import Counter
from moviepilot_tools import moviepilot_api

def pages(operation):
    """Read all pages and fail before claiming a partial result as complete."""
    rows = []
    for page in range(1, 20):
        response = moviepilot_api(operation, query={"page": page, "count": 20})
        assert response["success"], response
        rows.extend(response["data"])
        if len(rows) >= response["collection"]["total_count"]:
            return rows
    raise RuntimeError("pagination budget exhausted")

subscriptions = pages("subscription.list")
sites = pages("site.list")
downloads = pages("download.tasks.active")
transfers = pages("transfer.queue")
search = moviepilot_api("search.results")
assert search["success"], search
resources = [item["torrent_info"] for item in search["data"]["results"]]
best = min(resources, key=lambda row: (row["downloadvolumefactor"], -row["seeders"], row["site"]))
print(json.dumps({"status": "completed", "subscription_count": len(subscriptions),
    "enabled_site_ids": sorted(row["id"] for row in sites if row["enabled"]),
    "download_counts": dict(Counter(row["state"] for row in downloads)),
    "transfer_counts": dict(Counter(task["state"] for job in transfers for task in job["tasks"])),
    "best_resource_site_id": best["site"]}, sort_keys=True))'''


def seed_code_state(state: dict[str, Any]) -> None:
    """45条长订阅强制跨页，站点资源只来自已缓存搜索，不调用有副作用的站点抓取。"""
    state['subscriptions'] = [{'id': index + 1, 'name': f'订阅 {index}', 'media_source': 'themoviedb',
                               'media_id': str(8000 + index), 'description': '分页原文证据' * 2000}
                              for index in range(45)]
    state['downloads'] = [{'id': str(index), 'state': 'downloading' if index < 3 else 'paused'} for index in range(5)]
    state['transfers'] = [{'tasks': [{'state': 'pending'}, {'state': 'running'}]}, {'tasks': [{'state': 'pending'}]}]
    state['resources'] = [{'torrent_info': {'site': site, 'seeders': seeders, 'downloadvolumefactor': factor}}
                          for site, seeders, factor in [(11, 90, 1), (13, 30, 0), (11, 20, 0)]]


def expected_code_report(state: dict[str, Any]) -> dict[str, Any]:
    """验收器从隐藏终态独立计算事实，不能用模型输出或程序 stdout 生成答案。"""
    resources = [entry['torrent_info'] for entry in state['resources']]
    free = [row for row in resources if row['downloadvolumefactor'] == min(item['downloadvolumefactor'] for item in resources)]
    winner = sorted(free, key=lambda row: (-row['seeders'], row['site']))[0]
    tasks = [task for job in state['transfers'] for task in job['tasks']]
    return {'status': 'completed', 'subscription_count': len(state['subscriptions']),
            'enabled_site_ids': sorted(row['id'] for row in state['sites'] if row['enabled']),
            'download_counts': dict(Counter(row['state'] for row in state['downloads'])),
            'transfer_counts': dict(Counter(task['state'] for task in tasks)), 'best_resource_site_id': winner['site']}


def check_code_report(state: dict[str, Any], report: dict[str, Any], ledger: list[dict[str, Any]], trace: Any) -> list[str]:
    """完整读取证据、生产 Python 成功回执和最终答案必须同时成立。"""
    errors = []
    expected = expected_code_report(state)
    if report != expected:
        errors.append('aggregation_claim_mismatch')
    for kind, field in [('subscription', 'subscriptions'), ('site', 'sites'), ('download', 'downloads'),
                        ('transfer', 'transfers'), ('resource', 'resources')]:
        observed = [item['record'] for event in ledger for item in event['observations'] if item['kind'] == kind]
        if observed != state[field]:
            errors.append(f'{kind}_pages_not_verified')
    receipts = []
    for message in trace if isinstance(trace, list) else []:
        if not isinstance(message, dict):
            continue
        data = message.get('data', {})
        if message.get('type') == 'tool' and data.get('name') == 'execute_code':
            try:
                receipt = json.loads(data['content'])
                if (receipt.get('success') is True and not receipt.get('tool_errors')
                        and receipt.get('tool_calls_made') == len(ledger) and json.loads(receipt['output']) == expected):
                    receipts.append(receipt)
            except (ValueError, TypeError, KeyError):
                continue
    if len(receipts) != 1:
        errors.append('python_execution_not_verified')
    return errors
