from __future__ import annotations

from collections.abc import Iterable

from .connector_sdk import (
    ConnectorDeploymentProfile,
    ConnectorManifest,
    ConnectorObjectSchema,
    ConnectorOperation,
    ConnectorParameterBinding,
    ConnectorRequestBody,
    ConnectorSchemaField,
)


_CATALOG_CREATED_AT = "2026-07-26T00:00:00+00:00"


def _open_object(schema_id: str) -> ConnectorObjectSchema:
    return ConnectorObjectSchema(
        schema_id=schema_id,
        fields=[],
        additional_properties=True,
    )


def _list_request(schema_id: str) -> ConnectorObjectSchema:
    return ConnectorObjectSchema(
        schema_id=schema_id,
        fields=[
            ConnectorSchemaField(
                name="limit",
                value_type="integer",
                required=False,
            ),
            ConnectorSchemaField(
                name="offset",
                value_type="integer",
                required=False,
            ),
            ConnectorSchemaField(
                name="page",
                value_type="integer",
                required=False,
            ),
            ConnectorSchemaField(
                name="page_size",
                value_type="integer",
                required=False,
            ),
            ConnectorSchemaField(
                name="search",
                value_type="string",
                required=False,
            ),
            ConnectorSchemaField(
                name="query",
                value_type="string",
                required=False,
            ),
        ],
        additional_properties=False,
    )


def _list_operation(
    *,
    operation_id: str,
    title: str,
    path: str,
    response_root_type: str = "object",
    response_alternate_root_type: str | None = None,
) -> ConnectorOperation:
    parameters = [
        ConnectorParameterBinding(
            input_key=name,
            wire_name=name,
            location="query",
            required=False,
        )
        for name in ("limit", "offset", "page", "page_size", "search", "query")
    ]

    def response_shape(root_type: str) -> dict[str, object]:
        result: dict[str, object] = {"type": root_type}
        if root_type == "array":
            result["items"] = {
                "type": "object",
                "additionalProperties": True,
            }
        else:
            result["additionalProperties"] = True
        return result

    primary_response = response_shape(response_root_type)
    response_json_schema: dict[str, object] = primary_response
    if response_alternate_root_type is not None:
        response_json_schema = {
            "oneOf": [
                primary_response,
                response_shape(response_alternate_root_type),
            ]
        }
    return ConnectorOperation(
        id=operation_id,
        title=title,
        kind="read",
        method="GET",
        path=path,
        request_schema=_list_request(f"{operation_id}.request"),
        response_schema=_open_object(f"{operation_id}.response"),
        response_json_schema=response_json_schema,
        response_root_type=response_root_type,
        parameters=parameters,
        required_roles=["operator"],
    )


def _document_patch(
    *,
    operation_id: str,
    title: str,
    path: str,
    kind: str,
    compensation_operation_id: str | None = None,
) -> ConnectorOperation:
    return ConnectorOperation(
        id=operation_id,
        title=title,
        kind=kind,
        method="PATCH",
        path=path,
        request_schema=ConnectorObjectSchema(
            schema_id=f"{operation_id}.request",
            fields=[
                ConnectorSchemaField(name="id", value_type="integer"),
                ConnectorSchemaField(name="body", value_type="object"),
            ],
            additional_properties=False,
        ),
        response_schema=_open_object(f"{operation_id}.response"),
        parameters=[
            ConnectorParameterBinding(
                input_key="id",
                wire_name="id",
                location="path",
                required=True,
            )
        ],
        request_body=ConnectorRequestBody(
            input_key="body",
            required=True,
            content_type="application/json",
        ),
        required_roles=["operator"],
        compensation_operation_id=compensation_operation_id,
    )


def _profile(
    *,
    profile_id: str,
    base_url: str,
) -> ConnectorDeploymentProfile:
    return ConnectorDeploymentProfile(
        id=profile_id,
        environment="private",
        base_url=base_url,
        auth_type="api_key",
        auth_location="header",
        auth_wire_name="Authorization",
        auth_prefix="Token ",
        allowed_hosts=["127.0.0.1"],
        available=True,
        timeout_seconds=20,
        claim_ceiling="H3",
        excluded_claims=[
            "automatic business-field alignment",
            "multipart or binary upload",
            "customer-production availability",
        ],
    )


def paperless_manifest(
    *,
    base_url: str,
    profile_id: str = "paperless-private",
) -> ConnectorManifest:
    custom_fields_restore = "paperless.document.custom_fields.restore"
    tags_restore = "paperless.document.tags.restore"
    operations = [
        _list_operation(
            operation_id="paperless.documents",
            title="List Paperless documents and OCR metadata",
            path="/api/documents/",
        ),
        _list_operation(
            operation_id="paperless.tasks",
            title="List Paperless background tasks",
            path="/api/tasks/",
            response_root_type="array",
        ),
        _list_operation(
            operation_id="paperless.custom_fields",
            title="List Paperless custom-field definitions",
            path="/api/custom_fields/",
        ),
        _document_patch(
            operation_id="paperless.document.custom_fields.update",
            title="Update Paperless document custom fields",
            path="/api/documents/{id}/",
            kind="write",
            compensation_operation_id=custom_fields_restore,
        ),
        _document_patch(
            operation_id=custom_fields_restore,
            title="Restore Paperless document custom fields",
            path="/api/documents/{id}/",
            kind="compensate",
        ),
        _document_patch(
            operation_id="paperless.document.tags.update",
            title="Update Paperless document tags",
            path="/api/documents/{id}/",
            kind="write",
            compensation_operation_id=tags_restore,
        ),
        _document_patch(
            operation_id=tags_restore,
            title="Restore Paperless document tags",
            path="/api/documents/{id}/",
            kind="compensate",
        ),
    ]
    return ConnectorManifest(
        connector_id="paperless",
        version=1,
        title="Paperless-ngx",
        description=(
            "Reusable governed connector projection for Paperless-ngx document, "
            "OCR metadata, task and custom-field APIs."
        ),
        domain="document_management",
        operations=operations,
        deployment_profiles=[_profile(profile_id=profile_id, base_url=base_url)],
        source_provenance={
            "kind": "versioned_standard_connector_preset",
            "official_schema_path": "/api/schema/",
            "operation_selection": [item.id for item in operations],
        },
        created_at=_CATALOG_CREATED_AT,
    )


def inventree_manifest(
    *,
    base_url: str,
    profile_id: str = "inventree-private",
) -> ConnectorManifest:
    metadata_restore = "inventree.purchase_order.metadata.restore"
    operations = [
        _list_operation(
            operation_id="inventree.companies",
            title="List InvenTree companies",
            path="/api/company/",
            response_root_type="array",
            response_alternate_root_type="object",
        ),
        _list_operation(
            operation_id="inventree.parts",
            title="List InvenTree parts",
            path="/api/part/",
            response_root_type="array",
            response_alternate_root_type="object",
        ),
        _list_operation(
            operation_id="inventree.purchase_orders",
            title="List InvenTree purchase orders",
            path="/api/order/po/",
            response_root_type="array",
            response_alternate_root_type="object",
        ),
        _list_operation(
            operation_id="inventree.purchase_order_lines",
            title="List InvenTree purchase-order lines",
            path="/api/order/po-line/",
            response_root_type="array",
            response_alternate_root_type="object",
        ),
        _document_patch(
            operation_id="inventree.purchase_order.metadata.update",
            title="Update InvenTree purchase-order metadata",
            path="/api/order/po/{id}/",
            kind="write",
            compensation_operation_id=metadata_restore,
        ),
        _document_patch(
            operation_id=metadata_restore,
            title="Restore InvenTree purchase-order metadata",
            path="/api/order/po/{id}/",
            kind="compensate",
        ),
    ]
    return ConnectorManifest(
        connector_id="inventree",
        version=1,
        title="InvenTree",
        description=(
            "Reusable governed connector projection for InvenTree company, part "
            "and purchase-order APIs."
        ),
        domain="inventory_management",
        operations=operations,
        deployment_profiles=[_profile(profile_id=profile_id, base_url=base_url)],
        source_provenance={
            "kind": "versioned_standard_connector_preset",
            "official_schema_path": "/api/schema/",
            "operation_selection": [item.id for item in operations],
        },
        created_at=_CATALOG_CREATED_AT,
    )


def standard_connector_manifests(
    *,
    paperless_base_url: str,
    inventree_base_url: str,
) -> Iterable[ConnectorManifest]:
    return (
        paperless_manifest(base_url=paperless_base_url),
        inventree_manifest(base_url=inventree_base_url),
    )
