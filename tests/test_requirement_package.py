import io
import json
import stat
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform import requirement_package
from tests.test_runtime import ScriptedProvider


def package(*, prefix="", manifest=None, extra=()):
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(prefix + "requirement.json", json.dumps(manifest or {
            "format": "lilies-requirement-package", "version": 1,
            "name": "工厂测试需求", "requirement_file": "需求.md",
        }))
        archive.writestr(prefix + "需求.md", "请读取数据并为企业搭建可运行的工作流。")
        archive.writestr(prefix + "数据/input.csv", "棒号,长度\nA,400\n")
        for name, data in extra:
            archive.writestr(name, data)
    return content.getvalue()


@pytest.fixture
def client(tmp_path):
    settings = Settings(api_token="package-test", data_dir=tmp_path / "data",
                        workspace_root=tmp_path / "workspaces", model_egress_enabled=False)
    with TestClient(create_app(settings, ScriptedProvider())) as value:
        value.headers["Authorization"] = "Bearer package-test"
        yield value


@pytest.mark.parametrize("prefix", ["", "企业项目/"])
def test_import_opens_empty_application_with_readable_materials(client, tmp_path, prefix):
    response = client.post("/api/v1/requirement-packages/import",
                           files={"file": ("project.zip", package(prefix=prefix), "application/zip")})
    assert response.status_code == 201, response.text
    body = response.json()
    app_id = body["application"]["id"]
    assert body["application"]["name"] == "工厂测试需求"
    assert "requirement-package/需求.md" in body["application"]["requirement"]
    assert body["package"]["file_count"] == 3
    base = f"/api/v1/applications/{app_id}"
    assert client.get(base + "/draft").json()["snapshot"]["workflow"]["nodes"] == []
    assert client.get(base + "/builds").json() == []
    assert client.get(base + "/runs").json() == []
    files = client.get(base + "/workspace/files").json()
    assert {f["path"] for f in files} == {f["path"] for f in body["package"]["files"]}
    downloaded = client.get(base + "/workspace/files/requirement-package/数据/input.csv")
    assert downloaded.status_code == 200
    assert downloaded.content == "棒号,长度\nA,400\n".encode()
    assert (tmp_path / "workspaces" / app_id / "requirement-package/数据/input.csv").read_bytes() == downloaded.content


def symlink():
    info = zipfile.ZipInfo("link")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    return info


@pytest.mark.parametrize("payload", [
    b"not a zip",
    package(extra=[("../outside.txt", "escape")]),
    package(extra=[("/absolute.txt", "escape")]),
    package(extra=[(symlink(), "../../outside")]),
    package(extra=[("A.txt", "1"), ("a.txt", "2")]),
    package(extra=[("folder", "1"), ("folder/child.txt", "2")]),
    package(manifest={"format": "lilies-requirement-package", "version": 2,
                      "name": "测试", "requirement_file": "需求.md"}),
    package(manifest={"format": "lilies-requirement-package", "version": 1,
                      "name": "测试", "requirement_file": "missing.md"}),
])
def test_rejected_package_leaves_no_application_or_workspace(client, tmp_path, payload):
    existing = set((tmp_path / "workspaces").iterdir())
    response = client.post("/api/v1/requirement-packages/import", files={"file": ("bad.zip", payload)})
    assert response.status_code == 422
    assert client.get("/api/v1/applications").json() == []
    assert set((tmp_path / "workspaces").iterdir()) == existing
    assert not (tmp_path / "outside.txt").exists()


@pytest.mark.parametrize("limit", ["MAX_UPLOAD_BYTES", "MAX_EXPANDED_BYTES", "MAX_FILES"])
def test_package_limits_fail_cleanly(client, tmp_path, monkeypatch, limit):
    existing = set((tmp_path / "workspaces").iterdir())
    monkeypatch.setattr(requirement_package, limit, 1)
    response = client.post("/api/v1/requirement-packages/import", files={"file": ("large.zip", package())})
    assert response.status_code == 422
    assert client.get("/api/v1/applications").json() == []
    assert set((tmp_path / "workspaces").iterdir()) == existing


def test_import_requires_authentication(client):
    client.headers.pop("Authorization")
    response = client.post("/api/v1/requirement-packages/import", files={"file": ("project.zip", package())})
    assert response.status_code == 401
