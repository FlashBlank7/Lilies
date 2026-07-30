#!/usr/bin/env python3
"""Provision controlled ERPNext planning master data through public Frappe APIs."""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class FrappeClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def call(
        self,
        method: str,
        path: str,
        body: Any | None = None,
        *,
        form: bool = False,
    ) -> Any:
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            if form:
                data = urllib.parse.urlencode(body).encode()
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
                headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers=headers,
        )
        try:
            with self.opener.open(request, timeout=120) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {path}: HTTP {error.code}: {detail}") from error
        return json.loads(payload) if payload else None

    def list_docs(
        self, doctype: str, filters: list[list[Any]], fields: list[str]
    ) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode(
            {
                "filters": json.dumps(filters, separators=(",", ":")),
                "fields": json.dumps(fields, separators=(",", ":")),
                "limit_page_length": 100,
            }
        )
        encoded = urllib.parse.quote(doctype, safe="")
        return self.call("GET", f"/api/resource/{encoded}?{query}")["data"]

    def create(self, doctype: str, body: dict[str, Any]) -> dict[str, Any]:
        encoded = urllib.parse.quote(doctype, safe="")
        return self.call("POST", f"/api/resource/{encoded}", body)["data"]


def ensure_document(
    client: FrappeClient,
    doctype: str,
    filters: list[list[Any]],
    body: dict[str, Any],
) -> dict[str, Any]:
    existing = client.list_docs(doctype, filters, ["name"])
    if existing:
        return existing[0]
    return client.create(doctype, body)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18060")
    parser.add_argument(
        "--administrator-password",
        default=os.environ.get("EXP006_SITE_ADMIN_PASSWORD", ""),
    )
    args = parser.parse_args()
    api_key = os.environ.get("EXP006_ERPNEXT_API_KEY", "")
    api_secret = os.environ.get("EXP006_ERPNEXT_API_SECRET", "")
    if not args.administrator_password:
        raise RuntimeError("--administrator-password or EXP006_SITE_ADMIN_PASSWORD is required")
    if len(api_key) < 12 or len(api_secret) < 20:
        raise RuntimeError("EXP006_ERPNEXT_API_KEY and EXP006_ERPNEXT_API_SECRET are required")
    client = FrappeClient(args.base_url)
    client.call(
        "POST",
        "/api/method/login",
        {"usr": "Administrator", "pwd": args.administrator_password},
        form=True,
    )
    for warehouse_type in (
        "Transit",
        "Stores",
        "Work In Progress",
        "Finished Goods",
        "Scrap",
    ):
        ensure_document(
            client,
            "Warehouse Type",
            [["Warehouse Type", "name", "=", warehouse_type]],
            {"name": warehouse_type},
        )
    ensure_document(
        client,
        "Company",
        [["Company", "company_name", "=", "Lilies Planning"]],
        {
            "company_name": "Lilies Planning",
            "abbr": "L",
            "default_currency": "USD",
            "country": "United States",
        },
    )
    warehouses = client.list_docs(
        "Warehouse",
        [["Warehouse", "warehouse_name", "=", "Stores"]],
        ["name"],
    )
    warehouse_name = (
        next(
            (row["name"] for row in warehouses if str(row["name"]).endswith(" - L")),
            None,
        )
        or "Stores - L"
    )
    if not client.list_docs(
        "Warehouse",
        [["Warehouse", "name", "=", warehouse_name]],
        ["name"],
    ):
        ensure_document(
            client,
            "Warehouse",
            [["Warehouse", "warehouse_name", "=", "Stores"]],
            {
                "warehouse_name": "Stores",
                "company": "Lilies Planning",
                "is_group": 0,
            },
        )
    ensure_document(
        client,
        "UOM",
        [["UOM", "name", "=", "Nos"]],
        {"uom_name": "Nos", "must_be_whole_number": 1},
    )
    ensure_document(
        client,
        "Item Group",
        [["Item Group", "name", "=", "All Item Groups"]],
        {"item_group_name": "All Item Groups", "is_group": 1},
    )
    ensure_document(
        client,
        "Fiscal Year",
        [["Fiscal Year", "name", "=", "2026"]],
        {
            "year": "2026",
            "year_start_date": "2026-01-01",
            "year_end_date": "2026-12-31",
            "companies": [{"company": "Lilies Planning"}],
        },
    )
    for item_code in ("ITEM-A", "ITEM-B", "ITEM-C"):
        ensure_document(
            client,
            "Item",
            [["Item", "item_code", "=", item_code]],
            {
                "item_code": item_code,
                "item_name": f"Planning {item_code}",
                "item_group": "All Item Groups",
                "stock_uom": "Nos",
                "is_stock_item": 1,
                "include_item_in_manufacturing": 0,
            },
        )
    bins = client.list_docs(
        "Bin",
        [["Bin", "warehouse", "=", warehouse_name]],
        ["item_code", "actual_qty"],
    )
    actual = {row["item_code"]: float(row["actual_qty"]) for row in bins}
    expected = {"ITEM-A": 30.0, "ITEM-B": 20.0, "ITEM-C": 50.0}
    if actual != expected:
        accounts = client.list_docs(
            "Account",
            [
                ["Account", "company", "=", "Lilies Planning"],
                ["Account", "is_group", "=", 0],
            ],
            ["name", "root_type", "account_type"],
        )
        difference_account = next(
            (
                str(row["name"])
                for row in accounts
                if row.get("root_type") in {"Asset", "Liability"}
                and (
                    row.get("account_type") == "Temporary"
                    or "opening" in str(row["name"]).casefold()
                )
            ),
            None,
        )
        if difference_account is None:
            raise RuntimeError("ERPNext has no opening-entry difference account")
        reconciliation = client.create(
            "Stock Reconciliation",
            {
                "company": "Lilies Planning",
                "purpose": "Opening Stock",
                "posting_date": "2026-07-30",
                "posting_time": "08:00:00",
                "set_posting_time": 1,
                "expense_account": difference_account,
                "items": [
                    {
                        "item_code": item_code,
                        "warehouse": warehouse_name,
                        "qty": quantity,
                        "valuation_rate": 100,
                    }
                    for item_code, quantity in expected.items()
                ],
            },
        )
        full = client.call(
            "GET",
            (
                "/api/resource/Stock%20Reconciliation/"
                f"{urllib.parse.quote(str(reconciliation['name']), safe='')}"
            ),
        )["data"]
        client.call("POST", "/api/method/frappe.client.submit", {"doc": full})
    client.call(
        "PUT",
        "/api/resource/User/Administrator",
        {"api_key": api_key, "api_secret": api_secret},
    )
    final_bins = client.list_docs(
        "Bin",
        [["Bin", "warehouse", "=", warehouse_name]],
        ["item_code", "warehouse", "actual_qty"],
    )
    final = {row["item_code"]: float(row["actual_qty"]) for row in final_bins}
    if final != expected:
        raise RuntimeError(f"ERPNext inventory seed mismatch: {final}")
    print(
        json.dumps(
            {
                "company": "Lilies Planning",
                "warehouse": warehouse_name,
                "inventory": final,
                "api_credential_configured": True,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
