# work_platform_harness_observability_ui_2026_07_09

## 1. Goal

进入 Automatic Evolution Mode 后，从 `v0.2.3` 的 Next-stage Task Pool 选择首项任务，推进 `v0.2.4_platform_harness_observability_ui`。

目标是把 Platform Harness / task monitor boundary 从后端 API 变成 Studio 中可见、可刷新、可审计的维护面板。

## 2. Scope

实现范围：

- Studio 增加 `Monitor` tab。
- 前端读取 `/api/v1/platform/harness/tasks`。
- 展示 task kind、status、owner/resource、usage counts、error、最近 usage events。
- 支持相关任务、失败任务、全部任务三种筛选。
- 将当前 application、build、run 相关 task 标记为 related。
- 保留手动刷新，不引入 websocket、durable storage 或额外后端状态。

不实现：

- Durable Platform Harness storage。
- Account-level billing、secret policy、egress policy。
- 完整 Builder benchmark suite。
- Model-assisted natural language editing。

## 3. Plans

| Plan | Current design | Status | Acceptance |
| --- | --- | --- | --- |
| Platform Harness monitor UI | `docs/historical-designs/v0.2.4_design_platform_harness_observability_ui_v1.md` | implemented | Studio can inspect task monitor records without mutating workflows. |

## 4. Evidence

Implemented files:

- `platform/frontend/lib/platform.ts`
- `platform/frontend/lib/i18n.ts`
- `platform/frontend/app/applications/[id]/page.tsx`
- `platform/frontend/app/globals.css`

Verification:

- `/Users/zhonghaoyang/.nvm/versions/node/v24.15.0/bin/node node_modules/typescript/bin/tsc --noEmit` in `platform/frontend` -> passed.
- `.venv/bin/python -m compileall -q platform/backend/src/agent_platform tests/test_workflow.py` -> passed.
- `.venv/bin/python -m pytest -q` -> `54 passed, 1 warning`.
- Changed-file backend ruff command for v0.2.3 backend surface -> `All checks passed!`.
- `curl -I http://127.0.0.1:3000` -> `200 OK`.
- `curl http://127.0.0.1:3000/applications/smoke` -> Studio HTML rendered with `监控` tab.
- Local Platform Harness API smoke with `.env` token -> returned JSON with status ok.

Skipped / bounded verification:

- No paid model test required. This stage is UI observability over existing Platform Harness records and does not change Builder/model behavior.
- No formal `.docx` experiment report created. This is an engineering feature stage, not a completed experiment.
- Full repository ruff still has pre-existing lint debt in unrelated modules; this stage records it but does not broaden scope into lint cleanup.

## 5. Design Execution Decisions

| Design | Decision | Reason | Next action |
| --- | --- | --- | --- |
| `design_platform_harness_observability_ui_v1.md` | proceed to archive | Code, TypeScript check, backend regression, frontend smoke, and API smoke passed. | Archive v0.2.4 automatically. |

## 6. Review Before Archive

- Completion summary: Platform Harness Monitor tab implemented in Studio.
- Files changed: frontend platform types, Studio page, i18n, CSS, docs.
- Verification: TypeScript passed; backend compile/pytest passed; selected backend ruff passed; frontend and API smoke passed.
- Remaining risk: task records are still in-process and disappear after backend restart.
- Awaiting user review before archive: no, Automatic Evolution Mode is active

## 7. Archive Conditions

- Create `docs/stage-report-archives/v0.2.x/v0.2.4_platform_harness_observability_ui.md`.
- Copy current design to `docs/historical-designs/v0.2.4_design_platform_harness_observability_ui_v1.md`.
- Auto commit after verification.

## 8. Automatic Evolution

- Automatic Evolution Mode active: yes
- Current version: `v0.2.4`
- Archive automatically after verification: yes
- Next version selection source: `docs/stage-report-archives/v0.2.x/v0.2.3_platform_harness_and_development_roadmap.md`
- Continue after archive: inspect the generated `v0.2.4` next-stage pool
