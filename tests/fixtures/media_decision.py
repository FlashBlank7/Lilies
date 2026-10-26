"""Offline adapter example: explicit API field mapping, fixed targets on repair."""

def main(inputs):
    from collections import Counter, defaultdict
    from datetime import datetime

    grouped = defaultdict(list)
    totals, seen, page_totals = {}, set(), {}
    schema = inputs.get('field_map', {'id': 'Id', 'title': 'Name', 'items': 'Items', 'total': 'TotalRecordCount'})
    for page in inputs['first_pages'] + inputs['other_pages']:
        service, payload = page['service'], page['payload']
        number, pages = int(page['headers']['x-test-page']), int(page['headers']['x-test-pages'])
        if (service, number) in seen or not 1 <= number <= pages:
            raise ValueError('duplicate/invalid page: ' + service)
        seen.add((service, number))
        if service in page_totals and page_totals[service] != pages:
            raise ValueError('page count changed: ' + service)
        page_totals[service] = pages
        if service == 'emby':
            records, total = payload[schema['items']], payload[schema['total']]
            records = [{**r, 'Id': r[schema['id']], 'Name': r[schema['title']]} for r in records]
        elif service in ('radarr', 'sonarr'):
            records, total = payload['records'], payload['totalRecords']
        elif service == 'qbittorrent':
            records, total = payload, None
        else:
            records, total = payload['data'], payload['total']
        if not isinstance(records, list):
            raise ValueError('invalid records: ' + service)
        grouped[service].extend(records)
        if total is not None:
            if service in totals and totals[service] != total:
                raise ValueError('changed total: ' + service)
            totals[service] = total
    for service, pages in page_totals.items():
        if any((service, page) not in seen for page in range(1, pages + 1)):
            raise ValueError('incomplete pages: ' + service)
    for service, total in totals.items():
        if len(grouped[service]) != total:
            raise ValueError('incomplete records: ' + service)
    managers, torrents, files = defaultdict(list), defaultdict(list), defaultdict(list)
    for service in ('radarr', 'sonarr'):
        for row in grouped[service]:
            managers[str(row.get('tmdbId'))].append((service, row))
    for row in grouped['qbittorrent']:
        torrents[row.get('content_path')].append(row)
    for row in grouped['nas']:
        files[row.get('path')].append(row)
    keep_ids = {r['tag_id'] for r in grouped['maintainer'] if r['tag_label'].casefold() == 'keep'}
    if not keep_ids:
        raise ValueError('Keep definition missing')
    now = datetime.fromisoformat(inputs['now'].replace('Z', '+00:00'))
    if now.tzinfo is None:
        raise ValueError('now must include timezone')
    counts = Counter(movie['Id'] for movie in grouped['emby'])
    decisions, operations = [], []
    for movie in grouped['emby']:
        reason, manager, download, file = 'candidate', None, None, None
        match = managers.get(str(movie.get('ProviderIds', {}).get('Tmdb')), [])
        if counts[movie['Id']] != 1:
            reason = 'duplicate_media_id'
        elif len(match) != 1:
            reason = 'ambiguous_or_missing_match'
        else:
            manager_service, manager = match[0]
            if manager.get('file', {}).get('path') != movie.get('Path'):
                reason = 'path_mismatch'
            elif 'tags' not in manager:
                reason = 'unknown_tags'
            elif keep_ids.intersection(manager['tags']):
                reason = 'keep_tag'
            elif movie.get('MediaSource') != 'nas':
                reason = 'cloud_pointer'
            else:
                users = defaultdict(list)
                for user in movie.get('Users', []):
                    users[user.get('UserId')].append(user)
                relevant = [users[u][0] if len(users[u]) == 1 else None for u in inputs['users']]
                if not relevant or any(u is None for u in relevant):
                    reason = 'unknown_user_state'
                elif any(u.get('Played') is not True for u in relevant):
                    reason = 'not_all_watched'
                elif any(not u.get('CompletedAt') for u in relevant):
                    reason = 'unknown_completion_time'
                else:
                    try:
                        completed = [datetime.fromisoformat(u['CompletedAt'].replace('Z', '+00:00')) for u in relevant]
                        age = min((now - d).total_seconds() / 3600 for d in completed)
                    except (ValueError, TypeError):
                        reason = 'invalid_completion_time'
                    else:
                        if age <= inputs['retention_hours']:
                            reason = 'retention_period'
                if reason == 'candidate':
                    ds, fs = torrents[movie.get('Path')], files[movie.get('Path')]
                    if len(ds) != 1 or len(fs) != 1:
                        reason = 'ambiguous_or_missing_file'
                    else:
                        download, file = ds[0], fs[0]
                        if download.get('seed_goal_met') is not True:
                            reason = 'seeding'
                        elif file.get('exists') is not True:
                            reason = 'file_missing'
        decisions.append({'media_id': movie['Id'], 'title': movie['Name'], 'decision': reason})
        if reason == 'candidate' and inputs['execute']:
            operations.append({'media_id': movie['Id'], 'path': movie['Path'], 'etag': file['etag'],
                'manager_id': manager['id'], 'manager_service': manager_service, 'hash': download['hash'],
                'delete_path': '/nas/delete', 'unmonitor_path': '/' + manager_service + '/unmonitor',
                'remove_path': '/qbittorrent/remove', 'log_path': '/yamtrack/log'})
    # A repair retains ALL original targets, including files already deleted.
    # Current observations never replace the original planned target list.
    recovery = inputs.get('recovery', {})
    while recovery:
        previous = next((r['steps']['decide']['output'] for r in recovery.get('runs', []) if 'decide' in r.get('steps', {})), None)
        if previous is not None:
            operations = previous['operations']
            break
        recovery = recovery.get('original', {})
    return {'decisions': decisions, 'operations': operations, 'service_counts': {k: len(v) for k, v in grouped.items()},
            'summary': f'检查 {len(decisions)} 条影片，固定处理 {len(operations)} 个原目标。'}
