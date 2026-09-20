"""Legacy local-agent sessions, deliberately separate from raw LLM inference."""
from __future__ import annotations

import asyncio
import json
import os
from uuid import uuid4

from .codex_app_server import CodexAppServer
from .connected_model import ConnectedModel, completion_events
from .providers.base import ProviderError


class ConnectedAgent(ConnectedModel):
    async def stream(self, *, system, messages, tools, **kwargs):
        if self.connection.provider not in {'codex', 'claude', 'kimi'}:
            raise ProviderError('请选择本机智能体会话')
        prompt = json.dumps([m.model_dump(mode='json', exclude_none=True) for m in messages], ensure_ascii=False)
        if tools:
            system += ('\nReturn ONLY a JSON object {"text": "message", "tool_calls": '
                '[{"name": "tool name", "arguments": {}}]}. Use tool_calls=[] when done. '
                'These are platform tools: request them in JSON; do not use native tools. '
                'Tool results arrive in the next message. Available tools:\n' +
                json.dumps([t.model_dump(mode='json') for t in tools], ensure_ascii=False))
        text = await self.local_completion(system, prompt)
        blocks, stop = [{'type': 'text', 'text': text}], 'end_turn'
        if tools:
            try:
                raw = text.strip()
                if raw.startswith('```'):
                    raw = raw.split('\n', 1)[1].rsplit('```', 1)[0]
                response = json.loads(raw)
                if not isinstance(response, dict) or not isinstance(response.get('tool_calls'), list):
                    raise ValueError('missing tool_calls')
                blocks = [{'type': 'text', 'text': str(response.get('text', ''))}]
                names = {t.name for t in tools}
                for call in response['tool_calls']:
                    if call.get('name') not in names or not isinstance(call.get('arguments'), dict):
                        raise ValueError('invalid tool call')
                    blocks.append({'type': 'tool_use', 'id': str(uuid4()), 'name': call['name'], 'input': call['arguments']})
                stop = 'tool_use' if response['tool_calls'] else 'end_turn'
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                raise ProviderError('本机智能体未返回有效的项目工具调用；会话已保留，可继续重试') from error
        for event in completion_events(blocks, stop_reason=stop):
            yield event

    async def local_completion(self, system, prompt):
        c = self.connection
        folder = self.runtime_dir / str(uuid4())
        folder.mkdir(parents=True, mode=0o700)
        if c.provider == 'codex':
            client = CodexAppServer(c.executable or 'codex', folder, model=c.model, thinking=c.thinking)
            parts = []
            async def event(method, data):
                if method == 'item/completed' and data.get('item', {}).get('type') == 'agentMessage':
                    parts.append(data['item'].get('text', ''))
            async def tool(*args):
                raise ValueError('此会话仅允许返回文本和平台工具请求')
            try:
                await client.start([], system)
                result = await client.turn(prompt, event, tool)
                if result.get('status') != 'completed':
                    raise ProviderError('Codex 智能体会话未完成')
                return '\n'.join(parts)
            finally:
                await client.close()
        (folder / 'system.md').write_text(system, encoding='utf-8')
        allowed_env = {'HOME', 'USER', 'LOGNAME', 'PATH', 'TMPDIR', 'LANG', 'LC_ALL',
            'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy',
            'SSL_CERT_FILE', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_OAUTH_TOKEN'}
        env = {k: v for k, v in os.environ.items() if k in allowed_env}
        if c.provider == 'claude':
            argv = [c.executable or 'claude', '-p', '--output-format', 'json', '--tools', '',
                '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--setting-sources', '',
                '--settings', '{"disableAllHooks":true,"autoMemoryEnabled":false}', '--disable-slash-commands',
                '--no-chrome', '--permission-mode', 'dontAsk', '--system-prompt-file', str(folder / 'system.md'),
                '--session-id', folder.name]
            if c.thinking == 'off':
                env['MAX_THINKING_TOKENS'] = '0'
            elif c.thinking != 'default':
                argv += ['--effort', c.thinking]
        else:
            (folder / 'system.md').write_text('The input is JSON with system and conversation fields. '
                'Follow system and answer the conversation. You have no native tools.\n', encoding='utf-8')
            prompt = json.dumps({'system': system, 'conversation': json.loads(prompt)}, ensure_ascii=False)
            (folder / 'agent.yaml').write_text('version: 1\nagent:\n  name: lilies\n  system_prompt_path: ./system.md\n  tools: []\n', encoding='utf-8')
            argv = [c.executable or 'kimi', '--print', '--output-format', 'stream-json',
                '--agent-file', str(folder / 'agent.yaml'), '--mcp-config', '{"mcpServers":{}}']
            if c.thinking != 'default':
                argv += ['--no-thinking' if c.thinking == 'off' else '--thinking']
        if c.model:
            argv += ['--model', c.model]
        process = await asyncio.create_subprocess_exec(*argv, cwd=folder, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, _ = await asyncio.wait_for(process.communicate(prompt.encode()), 900)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        if process.returncode:
            raise ProviderError(f'{c.provider} 智能体调用失败，请检查本机登录与配置')
        try:
            if c.provider == 'claude':
                response = json.loads(out)
                if response.get('is_error'):
                    raise ValueError('agent error')
                text = response['result']
            else:
                parts = []
                for line in out.decode().splitlines():
                    response = json.loads(line)
                    if response.get('role') == 'assistant':
                        parts.extend(b['text'] for b in response.get('content', []) if b.get('type') == 'text')
                text = '\n'.join(parts)
            if not isinstance(text, str) or not text.strip():
                raise ValueError('empty result')
            return text
        except (ValueError, TypeError, KeyError) as error:
            raise ProviderError(f'{c.provider} 智能体未返回有效文本') from error
