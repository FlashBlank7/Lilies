"""Resolve generated file defaults only against files explicitly selected by the user."""
from collections import defaultdict
from copy import deepcopy
from pathlib import PurePosixPath
import re


def bind_selected_files(workflow: dict, selected_files: list[dict]) -> dict:
    aliases = defaultdict(set)
    for item in selected_files:
        path = item['path']
        aliases[PurePosixPath(path).name].add(path)

    def resolve(value):
        if not isinstance(value, str):
            return value
        # Full paths, expressions, unbound fields and ambiguous names remain
        # explicit. Do not search other projects or reinterpret text/code.
        return next(iter(aliases[value])) if len(aliases.get(value, ())) == 1 else value

    result = deepcopy(workflow)

    def walk(graph):
        nodes = graph.get('nodes', [])
        for node in nodes if isinstance(nodes, list) else []:
            if not isinstance(node, dict):
                continue
            config = node.get('config', {})
            if not isinstance(config, dict):
                continue
            if node.get('type') in {'start', 'schedule_trigger'}:
                fields = config.get('inputs', [])
                for field in fields if isinstance(fields, list) else []:
                    if not isinstance(field, dict):
                        continue
                    if field.get('type') == 'file' or re.search(r'(?:path|file|document|attachment)$', str(field.get('name', '')), re.I):
                        if 'default' in field:
                            field['default'] = resolve(field['default'])
                    if field.get('type') == 'array' and isinstance(field.get('default'), list):
                        definitions = field.get('columns', [])
                        columns = {c.get('name') for c in definitions if isinstance(c, dict) and c.get('type') == 'file'} if isinstance(definitions, list) else set()
                        for row in field['default']:
                            if isinstance(row, dict):
                                for key in columns & row.keys():
                                    row[key] = resolve(row[key])
            if node.get('type') in {'loop', 'iteration'} and isinstance(config.get('workflow'), dict):
                walk(config['workflow'])

    walk(result)
    return result
