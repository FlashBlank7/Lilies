"""授权里钉死的那几个值，调用时不许改——这道闸此前零测试。

`enforce_connector_operation_request_constraint` 是"发出去的请求必须和
授权时说好的一样"那一关：授权可以把某几个路径/查询/正文字段**钉成固定值**，
也可以规定正文里**只准出现哪些字段**。

变异验证（全量，2026-08-29）：把"固定值漂了"和"正文多出字段"两处的
拒绝去掉，2273 条测试**一条都不红**。也就是说这道闸松了没人知道，
而它松掉的后果是：拿着"只准给 A 转 100 块"的授权，可以给 B 转 10000。

每条都配反向（合规的请求要放行），否则"一律拒"也能全绿。
"""

from __future__ import annotations

import pytest

from agent_platform.connector_sdk import (
    ConnectorDenied,
    ConnectorObjectSchema,
    ConnectorOperation,
    ConnectorOperationRequestConstraint,
    ConnectorRequestBody,
    enforce_connector_operation_request_constraint,
)


def _operation(**extra) -> ConnectorOperation:
    fields = {
        "id": "transfer",
        "title": "转账",
        "kind": "write",
        "method": "POST",
        "path": "/v1/transfer",
        "request_schema": ConnectorObjectSchema(schema_id="req"),
        "response_schema": ConnectorObjectSchema(schema_id="res"),
        "request_body": ConnectorRequestBody(input_key="body"),
    }
    fields.update(extra)
    return ConnectorOperation(**fields)


def _check(operation, constraint, payload) -> None:
    enforce_connector_operation_request_constraint(operation, constraint, payload)


class TestFixedValuesCannotDrift:
    def test_a_pinned_body_value_must_match(self):
        constraint = ConnectorOperationRequestConstraint(
            operation_id="transfer", fixed_body_values={"to": "A"})
        _check(_operation(), constraint, {"body": {"to": "A"}})      # 合规
        with pytest.raises(ConnectorDenied, match="drifted"):
            _check(_operation(), constraint, {"body": {"to": "B"}})

    def test_a_pinned_body_value_cannot_be_dropped(self):
        """删掉那个字段也是一种"改"——不拦的话钉死等于没钉。"""
        constraint = ConnectorOperationRequestConstraint(
            operation_id="transfer", fixed_body_values={"to": "A"})
        with pytest.raises(ConnectorDenied, match="missing"):
            _check(_operation(), constraint, {"body": {}})

    def test_a_pinned_query_value_must_match(self):
        constraint = ConnectorOperationRequestConstraint(
            operation_id="transfer", fixed_query_values={"env": "prod"})
        _check(_operation(), constraint, {"env": "prod"})
        with pytest.raises(ConnectorDenied, match="drifted"):
            _check(_operation(), constraint, {"env": "staging"})

    def test_a_pinned_path_value_must_match(self):
        constraint = ConnectorOperationRequestConstraint(
            operation_id="transfer", fixed_path_values={"account": "42"})
        _check(_operation(), constraint, {"account": "42"})
        with pytest.raises(ConnectorDenied, match="drifted"):
            _check(_operation(), constraint, {"account": "43"})

    def test_a_number_that_looks_the_same_is_the_same(self):
        """100 和 100.0 是同一个值——这类比较要按 JSON 语义，不是按类型。"""
        constraint = ConnectorOperationRequestConstraint(
            operation_id="transfer", fixed_body_values={"amount": 100})
        _check(_operation(), constraint, {"body": {"amount": 100.0}})

    def test_a_different_amount_is_refused(self):
        """上一条不能宽到"数字都算一样"。"""
        constraint = ConnectorOperationRequestConstraint(
            operation_id="transfer", fixed_body_values={"amount": 100})
        with pytest.raises(ConnectorDenied, match="drifted"):
            _check(_operation(), constraint, {"body": {"amount": 10000}})


class TestOnlyAllowedBodyFieldsGetThrough:
    def test_an_extra_field_is_refused(self):
        constraint = ConnectorOperationRequestConstraint(
            operation_id="transfer", allowed_body_fields=["to"])
        _check(_operation(), constraint, {"body": {"to": "A"}})
        with pytest.raises(ConnectorDenied, match="policy-denied"):
            _check(_operation(), constraint, {"body": {"to": "A", "amount": 10000}})

    def test_the_denied_field_is_named(self):
        """只说"有不该有的字段"没法改——得说是哪个。"""
        constraint = ConnectorOperationRequestConstraint(
            operation_id="transfer", allowed_body_fields=["to"])
        with pytest.raises(ConnectorDenied, match="amount"):
            _check(_operation(), constraint, {"body": {"to": "A", "amount": 1}})

    def test_fewer_fields_than_allowed_is_fine(self):
        """白名单是上界不是清单——少给不算越权。"""
        constraint = ConnectorOperationRequestConstraint(
            operation_id="transfer", allowed_body_fields=["to", "memo"])
        _check(_operation(), constraint, {"body": {"to": "A"}})

    def test_a_body_that_is_not_an_object_is_refused(self):
        constraint = ConnectorOperationRequestConstraint(
            operation_id="transfer", allowed_body_fields=["to"])
        with pytest.raises(ConnectorDenied, match="must be an object"):
            _check(_operation(), constraint, {"body": ["not", "an", "object"]})


class TestTheConstraintMustBelongToThisOperation:
    def test_a_constraint_for_another_operation_is_refused(self):
        """拿转账的授权去调退款——两边都合法，凑一起就是越权。"""
        constraint = ConnectorOperationRequestConstraint(
            operation_id="refund", fixed_body_values={"to": "A"})
        with pytest.raises(ConnectorDenied, match="does not match"):
            _check(_operation(id="transfer"), constraint, {"body": {"to": "A"}})

    def test_no_constraint_means_this_gate_does_not_apply(self):
        """None 表示这次调用没有请求约束这项限制——别当成"什么都不许"。"""
        _check(_operation(), None, {"body": {"anything": 1}})
