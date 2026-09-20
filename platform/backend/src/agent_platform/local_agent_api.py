"""HTTP v1 contract for selecting and operating a project's local agent."""
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from .codex_app_server import discover_agents
from .local_agent_tools import ProjectTools, tool_specs
from .requirement_discussion import load_discussion
from .model_connections import ModelConnection


class SelectAgent(ModelConnection):
    # Apply the length limit in manager.select after SecretStr conversion too.
    # Request validation includes raw input values in FastAPI's 422 response.
    api_key: SecretStr | None = None

    @model_validator(mode='after')
    def valid_connection(self):
        # Validate cross-field choices in manager.select, after api_key has become
        # SecretStr. FastAPI's raw-body validation error would echo the credential.
        return self


class AgentMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=8000)
    intent: Literal["discuss", "build"] = "discuss"


class AgentToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


def local_agent_router(services, require_token) -> APIRouter:
    router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_token)])
    manager = services.local_agents

    async def project(application_id):
        try:
            await services.workflow_store.get_application(application_id)
        except KeyError as error:
            raise HTTPException(404, "没有找到这个项目") from error

    @router.get("/local-agents")
    async def list_local_agents():
        return {"contract_version": 1, "agents": await discover_agents()}

    @router.get("/applications/{application_id}/agent-session")
    async def get_project_agent_session(application_id: str):
        await project(application_id)
        state = manager.load(application_id)
        state["requirements"] = load_discussion(services.settings.workspace_root.resolve() / application_id)
        return state

    @router.put("/applications/{application_id}/agent-session")
    async def select_project_agent(application_id: str, body: SelectAgent):
        await project(application_id)
        try:
            return await manager.select(application_id, **body.model_dump())
        except (ValueError, RuntimeError, OSError) as error:
            raise HTTPException(422, str(error)) from error

    @router.post("/applications/{application_id}/agent-session/messages", status_code=202)
    async def send_project_agent_message(application_id: str, body: AgentMessage):
        await project(application_id)
        if not body.message.strip():
            raise HTTPException(422, "请输入消息")
        try:
            return await manager.message(application_id, body.message.strip(), intent=body.intent)
        except (ValueError, RuntimeError) as error:
            raise HTTPException(409, str(error)) from error

    @router.post("/applications/{application_id}/agent-session/stop")
    async def stop_project_agent(application_id: str):
        await project(application_id)
        return await manager.stop(application_id)

    @router.get("/applications/{application_id}/agent-tools")
    async def get_project_agent_tools(application_id: str):
        await project(application_id)
        return {"contract_version": 1, "tools": tool_specs()}

    @router.post("/applications/{application_id}/agent-tools")
    async def call_project_agent_tool(application_id: str, body: AgentToolCall):
        await project(application_id)
        # The owner/API can inspect the same tools. Native Codex receives only
        # bound dynamic tools, never this API's owner credential or another ID.
        try:
            return await ProjectTools(services, application_id, manager).call(body.name, body.arguments)
        except (ValueError, RuntimeError, KeyError, OSError) as error:
            raise HTTPException(422, str(error)) from error

    return router
