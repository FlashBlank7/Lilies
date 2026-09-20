"""Adapt persisted, measured candidates to AIDE's upstream search policy.

No model client or generated code executes here. Local Codex proposes the selected
draft/debug/improve step; the existing workflow evaluator supplies all scores.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import random

from .vendor.aide_policy import AidePolicy

AIDE_REVISION = '60b3978ddf65b71f86eb7c64506965048a1398cf'
AIDE_SOURCE = f'https://github.com/WecoAI/aideml/blob/{AIDE_REVISION}/aide/agent.py'
STAGE_LABELS = {'draft': '提出独立方案', 'debug': '修复失败方案', 'improve': '改进当前最佳方案'}
INSTRUCTIONS = {
    'draft': '提出一个简单且不同于已有草案的方案，说明依据；保持评价不变。使用一个模型、batch_size=1，提交后运行现有训练工作流。',
    'debug': '读取指定父候选的实际错误，仅修复该错误；保持评价不变，保留失败记录。提交单次子候选并运行训练工作流。',
    'improve': '读取指定父候选的配置、训练笔记和验证误差；提出一个可单独验证的特征或模型改动，提交单次子候选并运行训练工作流。',
}


@dataclass
class CandidateNode:
    id: str
    score: float | None
    parent: CandidateNode | None = None
    children: list = field(default_factory=list)

    @property
    def is_buggy(self):
        return self.score is None

    @property
    def is_leaf(self):
        return not self.children

    @property
    def debug_depth(self):
        return self.parent.debug_depth + 1 if self.parent and self.parent.is_buggy else 0


class CandidateJournal:
    """Read-only journal view; rankings use the platform's fixed metric direction."""
    def __init__(self, nodes, maximize):
        self.nodes, self.maximize = nodes, maximize

    @property
    def draft_nodes(self):
        return [n for n in self.nodes if n.parent is None]

    @property
    def buggy_nodes(self):
        return [n for n in self.nodes if n.is_buggy]

    @property
    def good_nodes(self):
        return [n for n in self.nodes if not n.is_buggy]

    def get_best_node(self):
        return max(self.good_nodes, key=lambda n: n.score if self.maximize else -n.score, default=None)


def choose_step(study, candidates):
    metric = study['evaluation']['metric']
    maximize = metric not in {'mae', 'rmse'}
    ordered = sorted(candidates, key=lambda c: (c['created_at'], c['id']))
    nodes, memory = {}, []
    for candidate in ordered:
        values = [t['metrics'][metric] for t in candidate.get('trials', [])
                  if t['status'] == 'completed' and isinstance(t.get('metrics', {}).get(metric), (int, float))
                  and math.isfinite(t['metrics'][metric])]
        score = (max(values) if maximize else min(values)) if values else None
        parent = nodes.get(candidate.get('parent_id'))
        node = CandidateNode(candidate['id'], score, parent)
        if parent:
            parent.children.append(node)
        nodes[node.id] = node
        memory.append({'candidate_id': node.id, 'parent_id': parent.id if parent else '',
                       'hypothesis': candidate.get('hypothesis', ''), 'score': score,
                       'error': candidate.get('error') or next((t.get('error', '') for t in candidate.get('trials', []) if t['status'] == 'failed'), ''),
                       'models': candidate.get('models'), 'engine': candidate.get('engine')})
    # Same persisted history yields the same branch after retries/restarts. Do
    # not use a process-global RNG or let a page refresh consume a search step.
    history = json.dumps(memory, sort_keys=True, ensure_ascii=False)
    seed = hashlib.sha256((str(study['evaluation']['seed']) + history).encode()).hexdigest()
    config = study['aide']
    parent = AidePolicy(CandidateJournal(list(nodes.values()), maximize), config, random.Random(seed)).search_policy()
    stage = 'draft' if parent is None else ('debug' if parent.is_buggy else 'improve')
    return {'strategy': 'aide', 'stage': stage, 'label': STAGE_LABELS[stage],
            'parent_id': parent.id if parent else '', 'parent_score': parent.score if parent else None,
            'metric': metric, 'direction': 'maximize' if maximize else 'minimize',
            'source': AIDE_SOURCE, 'revision': AIDE_REVISION, 'configuration': config,
            'history_sha256': hashlib.sha256(history.encode()).hexdigest(),
            'instructions': INSTRUCTIONS[stage], 'history': memory}
