"""Threshold choice, ties, infeasibility and fixed-policy evaluation."""
import pytest
from agent_platform.modeling_worker import select_acceptance, acceptance_result
from agent_platform.modeling_models import Evaluation


def test_selects_largest_feasible_tied_score_group_and_keeps_test_separate():
    policy = select_acceptance(['a','a','a','b'], ['a']*4,
                               [[.9,.1],[.8,.2],[.7,.3],[.7,.3]], ['a','b'], .9, 2)
    assert policy['threshold'] == .8
    assert policy['validation']['accepted'] == 2
    assert policy['curve'][-1]['accepted'] == 4  # Cannot select just one tied sample.
    accepted, test = acceptance_result(policy, ['a','a'], [[.95,.05],[.5,.5]], ['a','b'], ['b','a'])
    assert accepted == [True, False]
    assert test['accuracy'] == 0 and test['review'] == 1
    assert policy['threshold'] == .8  # Test labels cannot tune the threshold.


def test_unattainable_target_reviews_all_without_lowering_target():
    policy = select_acceptance(['b','a'], ['a','a'], [[.9,.1],[.8,.2]], ['a','b'], 1, 2)
    assert policy['status'] == 'unavailable' and policy['threshold'] is None
    accepted, result = acceptance_result(policy, ['a'], [[1,0]], ['a','b'], ['a'])
    assert accepted == [False] and result['accuracy'] is None
    with pytest.raises(ValueError, match='有效'):
        select_acceptance(['a'], ['a'], [[float('nan'),0]], ['a','b'], .9, 1)
    with pytest.raises(ValueError, match='分类'):
        Evaluation(acceptance_accuracy=.9)
