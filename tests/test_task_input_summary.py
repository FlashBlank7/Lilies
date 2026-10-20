from agent_platform.task_input_summary import input_parameters


def test_business_summary_omits_secrets_freeform_content_and_complex_values():
    values = {
        'api_key': 'fake-private-key', 'sessionToken': 'fake-private-token',
        'connection': 'Bearer fake-private-credential', 'credentials_url': 'https://user:password@example.test',
        'access': 'sk-fake-secret', 'connection_config': 'token=fake-secret', 'request': 'private full prompt',
        'files': ['requirement-package/private.csv'], 'rows': [{'private': 'business rows'}],
        'object': {'secret': 'hidden'}, 'hidden': 'confidential', 'custom': 'private password',
        'region': '东京', 'report_title': '报表' * 100,
    }
    fields = [{'name': name, 'type': 'string'} for name in values]
    fields[-4]['sensitive'] = True
    fields[-3]['label'] = '访问密码'
    snapshot = {'workflow': {'nodes': [{'type': 'start', 'config': {'inputs': fields}}]}}
    result = input_parameters(values, snapshot)
    assert result == [
        {'name': 'region', 'label': 'region', 'value': '东京'},
        {'name': 'report_title', 'label': 'report_title', 'value': ('报表' * 100)[:79] + '…'},
    ]
    assert input_parameters({'undeclared': 'private'}, {'workflow': {'nodes': []}}) == []


def test_business_summary_keeps_false_and_zero_and_bounds_card_size():
    fields = [{'name': 'enabled', 'type': 'boolean', 'default': True},
              {'name': 'limit', 'type': 'number', 'default': 100}]
    fields += [{'name': 'option_' + str(i), 'label': '标题' * 100, 'default': str(i)} for i in range(10)]
    snapshot = {'workflow': {'nodes': [{'type': 'start', 'config': {'inputs': fields}}]}}
    result = input_parameters({'enabled': False, 'limit': 0}, snapshot)
    assert result[:2] == [{'name': 'enabled', 'label': 'enabled', 'value': '否'},
                          {'name': 'limit', 'label': 'limit', 'value': '0'}]
    assert len(result) == 6
    assert all(len(parameter['label']) <= 48 and len(parameter['value']) <= 80 for parameter in result)


def test_choices_remain_visible_after_long_field_lists():
    fields = [{'name': f'columns_{i}', 'default': 'length load'} for i in range(8)]
    fields += [{'name': 'invalid_policy', 'label': '转换问题', 'type': 'string',
                'options': ['保留', '排除'], 'default': '保留'}]
    snapshot = {'workflow': {'nodes': [{'type': 'start', 'config': {'inputs': fields}}]}}
    first = input_parameters({}, snapshot)
    changed = input_parameters({'invalid_policy': '排除'}, snapshot)
    assert first[0] == {'name': 'invalid_policy', 'label': '转换问题', 'value': '保留'}
    assert changed[0]['value'] == '排除'
    assert len(changed) == 6
