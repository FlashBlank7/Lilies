"""The same project block selection serves manuals, editing and execution."""
from __future__ import annotations

# Whole-agent macros are optional. Model turns, tools and loops remain available
# so a project can compose its own agent from a trusted raw model.
AGENT_BLOCK_TYPES = frozenset({'claude_agent', 'subagent_spawn'})


def effective_block_type(node):
    if node.get('type') == 'soft_block':
        from .soft_block import get_discrete_block_type
        return get_discrete_block_type(node.get('config', {}).get('strategy', ''))
    return node.get('type', '')


class ProjectBlocks:
    def __init__(self, registry, *, agent_modules_enabled: bool):
        self.registry = registry
        self.agent_modules_enabled = agent_modules_enabled

    def available(self, block_type: str) -> bool:
        return self.agent_modules_enabled or block_type not in AGENT_BLOCK_TYPES

    def require(self, block_type: str):
        if not self.available(block_type):
            raise KeyError('此积木不在本项目的可用能力中')

    def list(self):
        return [b for b in self.registry.list() if self.available(b.type)]

    def get(self, block_type):
        self.require(block_type)
        return self.registry.get(block_type)

    def manual(self, block_type):
        self.require(block_type)
        return self.registry.manual(block_type)

    def manuals(self, **kwargs):
        return [m for m in self.registry.manuals(**kwargs) if self.available(m['type'])]

    def claude_architecture_blueprint(self):
        result = self.registry.claude_architecture_blueprint()
        if not self.agent_modules_enabled:
            result.pop('legacy_macro', None)
            result['groups'] = {name: [m for m in manuals if self.available(m['type'])]
                                for name, manuals in result['groups'].items()}
        return result

    def validate_workflow(self, workflow):
        # Validate even disconnected nodes and nested loop/iteration graphs.
        value = workflow.model_dump(mode='json') if hasattr(workflow, 'model_dump') else workflow
        for node in value.get('nodes', []):
            if not self.available(effective_block_type(node)):
                raise ValueError(f"本项目禁止使用智能体积木（节点：{node.get('id', '')}）")
            if node.get('type') in {'iteration', 'loop'}:
                self.validate_workflow(node.get('config', {}).get('workflow', {}))

    def supports_workflow(self, workflow):
        try:
            self.validate_workflow(workflow)
            return True
        except ValueError:
            return False
