"""沙箱的两条资源闸：输出要截断、超时要真把进程杀掉。

变异验证（2026-08-29，禁写字节码后重验）：这两条各自删掉，
**全套 1269 条测试全绿**——沙箱是跑「生成出来的代码」的地方，
它的资源闸没人看着。

· 输出不截断：一个跑飞的命令能把几百 MB 塞进内存、事件流和搭建记录。
  200KB 这个数是刻意的——够看清出了什么事，又不至于把库撑爆。
· 超时不杀进程：`wait_for` 只是不再等它，**进程还在跑**。
  留下来的那个会继续占 CPU 和内存，而且它手里还攥着容器。
  超时的本意是"停下来"，不是"别看了"。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_platform.config import Settings
from agent_platform.sandbox import CommandResult, NetworkPolicy, SandboxError, SandboxSession, SandboxManager


def _session(tmp_path: Path) -> SandboxSession:
    settings = Settings(api_token="t", data_dir=tmp_path / "d",
                        workspace_root=tmp_path / "w")
    settings.prepare()
    return SandboxSession(
        settings=settings, session_id="s1", workspace=tmp_path / "w",
        mount_source=tmp_path / "w", network_policy=NetworkPolicy.none,
        network_allowlist=[])


@pytest.mark.asyncio
async def test_huge_output_is_cut(tmp_path):
    """把 _host_command 换成"吐一大坨"，看 run 有没有截。"""
    session = _session(tmp_path)
    session.started = True                       # 别真去开容器

    async def flood(argv, *, stdin=None, timeout=None):
        return CommandResult(stdout="x" * 500_000, stderr="y" * 500_000, exit_code=0)

    session._host_command = flood
    result = await session.run(["echo"], max_output=1000)
    assert len(result.stdout) == 1000
    assert len(result.stderr) == 1000
    assert result.output_truncated


@pytest.mark.asyncio
async def test_the_default_cap_is_not_unlimited(tmp_path):
    """不传 max_output 时也得有上限——默认值才是真正会生效的那个。"""
    session = _session(tmp_path)
    session.started = True

    async def flood(argv, *, stdin=None, timeout=None):
        return CommandResult(stdout="x" * 500_000, stderr="", exit_code=0)

    session._host_command = flood
    result = await session.run(["echo"])
    assert len(result.stdout) < 500_000, "默认没有上限"


@pytest.mark.asyncio
async def test_output_under_the_cap_is_untouched(tmp_path):
    """别把闸关死：没超限的输出一个字都不能少。"""
    session = _session(tmp_path)
    session.started = True

    async def small(argv, *, stdin=None, timeout=None):
        return CommandResult(stdout="正常输出", stderr="", exit_code=0)

    session._host_command = small
    result = await session.run(["echo"])
    assert result.stdout == "正常输出"
    assert not result.output_truncated


@pytest.mark.asyncio
async def test_bash_does_not_pass_truncated_output_as_complete_json(tmp_path):
    from types import SimpleNamespace
    from agent_platform.tools.core import BashTool
    session = _session(tmp_path)
    session.started = True

    async def flood(argv, *, stdin=None, timeout=None):
        return CommandResult('{"partial":true}' + ' ' * 200_001, '', 0)

    session._host_command = flood
    result = await BashTool().execute({'command': 'diagnostic'}, SimpleNamespace(sandbox=session))
    assert result.is_error
    assert '已截断' in result.content
    assert result.structured_output['output_truncated']
    assert 'json' not in result.structured_output


@pytest.mark.asyncio
async def test_a_timeout_actually_kills_the_process(tmp_path):
    """超时的本意是"停下来"，不是"别看了"。

    这条用真进程：`wait_for` 超时后如果不 kill，那个 sleep 会一直活着。
    """
    session = _session(tmp_path)
    started: list = []
    real_exec = asyncio.create_subprocess_exec

    async def remember(*argv, **kwargs):
        process = await real_exec(*argv, **kwargs)
        started.append(process)
        return process

    asyncio.create_subprocess_exec = remember
    try:
        with pytest.raises(SandboxError) as caught:
            await session._host_command(["sleep", "30"], timeout=0.2)
    finally:
        asyncio.create_subprocess_exec = real_exec
    assert "timed out" in str(caught.value)
    assert started, "没起进程，这条测试等于没测"
    # 给内核一点时间收尸
    for _ in range(20):
        if started[0].returncode is not None:
            break
        await asyncio.sleep(0.05)
    assert started[0].returncode is not None, "超时之后进程还活着"


@pytest.mark.asyncio
async def test_a_command_that_finishes_in_time_is_not_killed(tmp_path):
    """正常跑完的不该被当成超时。"""
    session = _session(tmp_path)
    result = await session._host_command(["echo", "好了"], timeout=10)
    assert result.exit_code == 0
    assert "好了" in result.stdout


@pytest.mark.asyncio
async def test_imported_inputs_are_mounted_readonly_while_outputs_stay_writable(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'requirement-package').mkdir()
    manager = SandboxManager(Settings(workspace_root=workspace))
    manager.protect_inputs(workspace, ['requirement-package'])
    commands = []

    async def host_command(self, argv, **kwargs):
        commands.append(argv)
        return CommandResult('container', '', 0)

    original = SandboxSession._host_command
    SandboxSession._host_command = host_command
    try:
        session = await manager.get_or_create('project', str(workspace), NetworkPolicy.none, [])
        assert session.readonly_paths == ('requirement-package',)
        command = commands[0]
        assert f'{workspace}:/workspace:rw' in command
        assert f'{workspace / "requirement-package"}:/workspace/requirement-package:ro' in command
    finally:
        await manager.close()
        SandboxSession._host_command = original


def test_readonly_input_registration_rejects_paths_outside_project(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (workspace / 'linked').symlink_to(outside)
    manager = SandboxManager(Settings(workspace_root=workspace))
    for path in ('../outside', str(outside), 'linked'):
        with pytest.raises(SandboxError):
            manager.protect_inputs(workspace, [path])
