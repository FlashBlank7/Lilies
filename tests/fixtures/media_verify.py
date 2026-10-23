"""Compare current readback with the frozen target plan, not newly eligible rows."""

def main(inputs):
    from collections import defaultdict
    rows, seen, pages, totals = defaultdict(list), set(), {}, {}
    for page in inputs['first_pages'] + inputs['other_pages']:
        service, body = page['service'], page['payload']
        number = int(page['headers']['x-test-page'])
        if (service, number) in seen:
            raise ValueError('duplicate readback page')
        seen.add((service, number))
        pages[service] = int(page['headers']['x-test-pages'])
        if service in ('radarr', 'sonarr'):
            values, total = body['records'], body['totalRecords']
        elif service == 'qbittorrent':
            values, total = body, None
        else:
            values, total = body['data'], body['total']
        rows[service].extend(values)
        if total is not None:
            totals[service] = total
    for service, count in pages.items():
        if any((service, n) not in seen for n in range(1, count + 1)):
            raise ValueError('incomplete readback pages')
    for service, count in totals.items():
        if len(rows[service]) != count:
            raise ValueError('incomplete readback rows')
    results = []
    for target in inputs['targets']:
        files = [r for r in rows['nas'] if r['path'] == target['path']]
        managers = [r for r in rows[target['manager_service']] if r['id'] == target['manager_id']]
        torrents = [r for r in rows['qbittorrent'] if r['hash'] == target['hash']]
        results.append({'id': target['media_id'],
            'file': len(files) == 1 and files[0].get('exists') is False,
            'monitor': len(managers) == 1 and managers[0].get('monitored') is False,
            'download': len(torrents) == 1 and torrents[0].get('removed') is True,
            'log': any(r.get('media_id') == target['media_id'] and r.get('event') == 'cleanup_completed' for r in rows['yamtrack'])})
    return {'targets': results, 'target_count': len(results),
            **{key: sum(r[key] is True for r in results) for key in ('file', 'monitor', 'download', 'log')},
            'markdown': f'本次回读 {len(results)} 个原目标；各动作结果见结果检查。'}
