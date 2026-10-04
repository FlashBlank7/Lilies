"""Bounded business input labels for project run history."""
import math
import re


_PRIVATE_FIELD = re.compile(
    r'password|passwd|passphrase|secret|token|api[_ -]?key|credential|authorization|cookie|private[_ -]?key|'
    r'密码|密钥|口令|凭证|令牌', re.I)
_CONTENT_FIELD = re.compile(r'(?:^|[_ -])(?:prompt|request|message|text|content|context|body|notes?|instructions?)$', re.I)
_FILE_FIELD = re.compile(r'(?:path|file|document|attachment)s?$', re.I)
_SECRET_VALUE = re.compile(r'://|\bBearer\s|\bsk-[\w-]+|-----BEGIN|(?:password|token|secret|api[_ -]?key)\s*[:=]', re.I)


def input_parameters(inputs: dict, snapshot: dict) -> list[dict[str, str]]:
    """Use only declared scalar fields from the saved snapshot, never a live draft."""
    parameters = []
    for node in snapshot.get('workflow', {}).get('nodes', []):
        if node.get('type') != 'start':
            continue
        for field in node.get('config', {}).get('inputs', []):
            name = field.get('name', '')
            label = field.get('label') or name
            if (field.get('type', 'string') not in {'string', 'number', 'boolean'}
                    or _PRIVATE_FIELD.search(name + ' ' + label)
                    or _CONTENT_FIELD.search(name) or _FILE_FIELD.search(name)
                    or any(field.get(key) for key in ('secret', 'sensitive', 'writeOnly'))):
                continue
            value = inputs.get(name, field.get('default'))
            if isinstance(value, bool):
                display = '是' if value else '否'
            elif isinstance(value, (int, float)) and math.isfinite(value):
                display = str(value)
            elif isinstance(value, str) and value.strip() and not _SECRET_VALUE.search(value):
                display = ' '.join(value.split())
            else:
                continue
            parameters.append({'name': name[:80], 'label': label[:48],
                               'value': display if len(display) <= 80 else display[:79] + '…'})
            if len(parameters) == 6:
                return parameters
    return parameters
