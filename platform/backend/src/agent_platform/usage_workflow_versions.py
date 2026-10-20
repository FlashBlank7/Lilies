"""Workflow version metadata for usage observations; never returns graph bodies."""
import hashlib
import json

from .project_capabilities import effective_block_type


def read_members(db, project_id, task_id=None):
    if task_id:
        source = '''SELECT s.key, json_extract(s.value,'$.content_hash'),
            json_extract(s.value,'$.snapshot') FROM project_tasks t, json_each(t.snapshots_json) s
            WHERE t.project_id=? AND t.id=?'''
        parameters = (project_id, task_id)
    else:
        source = '''SELECT d.application_id,d.content_hash,d.snapshot_json
            FROM application_drafts d JOIN project_members m ON m.application_id=d.application_id
            WHERE m.project_id=?'''
        parameters = (project_id,)
    # Descend actual nested graphs, not arbitrary config dictionaries or strings.
    # SQLite reads the stored snapshot; only types, tool names and hashes leave it.
    rows = db.execute(f'''WITH RECURSIVE members(id,hash,snapshot) AS ({source}),
        nodes(member,node) AS (
            SELECT m.id,n.value FROM members m,json_each(m.snapshot,'$.workflow.nodes') n
            UNION ALL
            SELECT p.member,n.value FROM nodes p,json_each(p.node,'$.config.workflow.nodes') n
            WHERE json_extract(p.node,'$.type') IN ('iteration','loop'))
        SELECT m.id,m.hash,json_extract(n.node,'$.type') AS kind,
            CASE WHEN json_extract(n.node,'$.type')='soft_block'
                THEN json_extract(n.node,'$.config.strategy') END AS strategy,
            CASE WHEN json_extract(n.node,'$.type')='tool'
                THEN json_extract(n.node,'$.config.tool_name')
                WHEN json_extract(n.node,'$.type') IN ('tool_executor','soft_block')
                AND json_type(n.node,'$.config.settings.tool_name')='text'
                THEN json_extract(n.node,'$.config.settings.tool_name') END AS tool_name
        FROM members m LEFT JOIN nodes n ON n.member=m.id''', parameters).fetchall()
    members = {}
    for row in rows:
        member = members.setdefault(row['id'], {'hash': row['hash'] or '', 'refs': set(), 'dynamic': False})
        kind = effective_block_type({'type': row['kind'], 'config': {'strategy': row['strategy'] or ''}})
        if kind in {'tool', 'tool_executor'}:
            name = row['tool_name']
            if isinstance(name, str) and name.startswith('workflow:'):
                member['refs'].add(name.removeprefix('workflow:'))
            elif not name:
                member['dynamic'] = True
        elif kind in {'agent', 'claude_agent', 'subagent_spawn'}:
            member['dynamic'] = True
    return members


def reachable_versions(members, root):
    versions, pending, dynamic = {}, [root], False
    while pending:
        ident = pending.pop()
        if ident in versions:
            continue
        member = members.get(ident)
        versions[ident] = member['hash'] if member else None
        if member:
            pending.extend(member['refs'] - versions.keys())
            if member['dynamic']:
                # A routed target cannot be known before execution. Its possible
                # project members are frozen together, including uncalled branches.
                dynamic = True
                pending.extend(members.keys() - versions.keys())
    return versions, dynamic


def version_hash(versions, root):
    # Preserve existing finding IDs for workflows without member calls.
    if set(versions) == {root}:
        return versions[root] or ''
    return hashlib.sha256(json.dumps(versions, sort_keys=True).encode()).hexdigest()
