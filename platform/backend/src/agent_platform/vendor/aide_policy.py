"""AIDE Agent.search_policy from WecoAI/aideml (MIT).

Pinned commit: 60b3978ddf65b71f86eb7c64506965048a1398cf
Source: https://github.com/WecoAI/aideml/blob/60b3978ddf65b71f86eb7c64506965048a1398cf/aide/agent.py
Changes: dependency-free host constructor; per-decision RNG replaces global random.
The search method itself otherwise retains upstream behavior. See AIDE_LICENSE.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..modeling_search import CandidateNode as Node

logger = logging.getLogger(__name__)


class AidePolicy:
    def __init__(self, journal, search, rng):
        self.journal = journal
        self.acfg = SimpleNamespace(search=SimpleNamespace(**search))
        self.rng = rng

    def search_policy(self) -> Node | None:
        """Select a node to work on (or None to draft a new node)."""
        search_cfg = self.acfg.search

        # initial drafting
        if len(self.journal.draft_nodes) < search_cfg.num_drafts:
            logger.debug("[search policy] drafting new node (not enough drafts)")
            return None

        # debugging
        if self.rng.random() < search_cfg.debug_prob:
            # nodes that are buggy + leaf nodes + debug depth < max debug depth
            debuggable_nodes = [
                n
                for n in self.journal.buggy_nodes
                if (n.is_leaf and n.debug_depth <= search_cfg.max_debug_depth)
            ]
            if debuggable_nodes:
                logger.debug("[search policy] debugging")
                return self.rng.choice(debuggable_nodes)
            logger.debug("[search policy] not debugging by chance")

        # back to drafting if no nodes to improve
        good_nodes = self.journal.good_nodes
        if not good_nodes:
            logger.debug("[search policy] drafting new node (no good nodes)")
            return None

        # greedy
        greedy_node = self.journal.get_best_node()
        logger.debug("[search policy] greedy node selected")
        return greedy_node
