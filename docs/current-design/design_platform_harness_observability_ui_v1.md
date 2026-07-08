# design_platform_harness_observability_ui_v1

## 1. Goal

实现 Platform Harness observability UI v1：让维护人员和测试人员在 Studio 中直接看到平台级 task monitor boundary 的运行记录，而不是只能通过后端 API 或事件日志推断资源边界。

## 2. Module Boundary

涉及：

- `platform/frontend/lib/platform.ts`
- `platform/frontend/lib/i18n.ts`
- `platform/frontend/app/applications/[id]/page.tsx`
- `platform/frontend/app/globals.css`
- `docs/workingon/work_platform_harness_observability_ui_2026_07_09.md`

不涉及：

- 后端 Platform Harness 存储模型。
- Durable execution。
- 账号级计费或安全策略。
- 新增付费模型调用。

## 3. Data Flow / Control Flow

Studio 页面加载或用户点击刷新：

1. 调用 `api('/api/v1/platform/harness/tasks?limit=100')`。
2. 前端按当前 `application_id`、当前 `build.id`、当前 `run.id` 计算 related task。
3. Monitor tab 展示 summary、filter 和 task cards。
4. 构建、测试、运行、取消运行后刷新 monitor records。

任务来源：

- `builder_build`
- `workflow_run`
- `test_suite`
- `scheduler_trigger`
- `scheduler_manual_trigger`
- `benchmark`
- `draft_patch_preview`

## 4. Implementation Plan

1. 在 `lib/platform.ts` 定义 `PlatformTaskRecord` / `PlatformUsageRecord`。
2. Studio 中维护 `monitorTasks`、`monitorLoading`、`monitorFilter`、`monitorError`。
3. 新增 `refreshMonitorTasks()`。
4. 左侧 tabs 从 build/edit/test/run 扩展为 build/edit/test/run/monitor。
5. 增加 monitor panel，展示 related/running/failed/total summary。
6. 展示每个 task 的 kind/status/owner/resource/usage/error/latest usage。
7. 增加中英文 i18n 文案和 CSS。

## 5. Acceptance Criteria

- Monitor tab 可打开并手动刷新。
- 没有 task 时显示空状态。
- 有 task 时能看到 kind/status/owner/resource/usage/error。
- 当前 application/build/run 相关 task 有明显标记。
- TypeScript 检查通过。
- 后端 compile/ruff/pytest 不回退。

## 6. Referenced Intellectual Assets

- `docs/intellectual-assets/asset_platform_harness_task_monitor_boundary.md`
- `docs/historical-designs/v0.2.3_design_platform_harness_task_monitor_v1.md`
- `docs/stage-reports/v0.2.3_platform_harness_and_development_roadmap.md`

## 7. Risk

Platform Harness 当前仍是 in-process，所以前端展示的是当前后端进程内的 task monitor 记录。刷新后端进程会丢失历史 task，这不是 UI bug，而是 `v0.2.3` 后端边界的已知限制。

## 8. Implementation Result

Status: implemented.

Implemented modules:

- `platform/frontend/lib/platform.ts`
- `platform/frontend/lib/i18n.ts`
- `platform/frontend/app/applications/[id]/page.tsx`
- `platform/frontend/app/globals.css`

Verification:

- Frontend TypeScript check passed using `/Users/zhonghaoyang/.nvm/versions/node/v24.15.0/bin/node node_modules/typescript/bin/tsc --noEmit`.
- Frontend dev server smoke passed: `GET http://127.0.0.1:3000` returned `200 OK`.
- Application page smoke passed: `GET http://127.0.0.1:3000/applications/smoke` rendered Studio HTML containing the `监控` tab.
- Platform Harness API smoke passed using local `.env` token: `/api/v1/platform/harness/tasks?limit=3` returned JSON.
- Backend pytest passed: `54 passed, 1 warning`.
- Changed-file backend ruff passed for the v0.2.3 backend surface.

Known verification boundary:

- Full repository ruff currently reports pre-existing lint debt in unrelated modules such as `observability.py`, `meta_cognition.py`, and `orchestration_advisor.py`. This stage did not touch those files.
