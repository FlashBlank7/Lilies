"""Platform agent operations bound to one project's members and active task."""
from __future__ import annotations

import asyncio
import json
from typing import Any, Literal
from uuid import uuid4

from fastapi.encoders import jsonable_encoder
from pydantic import Field, model_validator

from .local_agent_tools import Arguments, Draft, ProjectTools, Run, TOOL_MODELS, tool_specs
from .project_conversation import ProgressTool, ProjectAction
from .project_agent_context import draft_summary, progress_summary, task_summary
from .workflow_models import DraftEdit, DraftOperation
from .modeling_models import ModelingTool
from .modeling_summary import candidate_summary, study_summary
from .modeling_workflow import submit_and_start


class DraftBatch(Arguments):
    expected_revision: int = Field(ge=0, strict=True)
    expected_content_hash: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=200)
    operations: list[DraftEdit] = Field(min_length=1, max_length=100)

    @model_validator(mode='after')
    def valid_updates(self):
        for edit in self.operations:
            Draft(operation=DraftOperation(**edit.model_dump(), expected_revision=self.expected_revision,
                                          idempotency_key=self.idempotency_key))
        return self


class MemberDraft(Draft):
    workflow_id: str = ''
    view: Literal['summary', 'nodes', 'tests', 'full'] = 'summary'
    node_ids: list[str] = Field(default_factory=list, max_length=100)
    batch: DraftBatch | None = None

    @model_validator(mode='after')
    def selection(self):
        if self.operation and self.batch:
            raise ValueError('一次调用使用operation或batch，不能同时提供')
        if self.view == 'nodes' and not self.node_ids:
            raise ValueError('view=nodes需要node_ids')
        return self


class MemberRun(Run):
    workflow_id: str = ''
    request_key: str = Field(default='', max_length=240)
    task_id: str = ''
    wait: bool = True
    wait_seconds: int = Field(default=0, ge=0, le=60, strict=True,
        description='For inspect with task_id: wait up to this many seconds for the existing workflow task. Default 0 returns immediately; timeout does not cancel or restart the task.')
    view: Literal['summary', 'full'] = 'summary'

    @model_validator(mode='after')
    def valid_wait(self):
        if self.wait_seconds and (self.action != 'inspect' or not self.task_id):
            raise ValueError('wait_seconds 仅用于 workflow_run(action="inspect", task_id="已有任务", wait_seconds=30)')
        return self


class Members(Arguments):
    action: Literal['list', 'create', 'remove', 'classify'] = 'list'
    workflow_id: str = ''
    name: str = Field(default='', max_length=100)
    description: str = Field(default='', max_length=1000)
    purpose: Literal['business', 'test'] = 'business'


class Records(Arguments):
    action: Literal['list', 'get'] = 'list'
    collection: str = ''
    key: str = ''


class TaskResult(Arguments):
    task_id: str = Field(default='', description='Unified conversation may present an existing current-project task without rerunning it. Omit to use the active task.')
    status: Literal['succeeded', 'waiting_input', 'failed']
    message: str = Field(min_length=1, max_length=8000,
                         description='Current user-facing task summary; overrides any message carried in outputs.')
    outputs: dict[str, Any] = Field(default_factory=dict)
    markdown: str = Field(default='', max_length=28000)
    artifacts: list[dict[str, str]] = Field(default_factory=list, max_length=30,
        description='Optional result files: [{label, file_path}]; relative paths in the current project.')


MODELING_MANUAL = 'Project-scoped CPU modeling. Register CSV/TSV/XLSX from requirement-package, solution or results, or revise_dataset to create a new immutable version with clarified mapping. profile(sampled=true) gives a quick preview; false scans fully. If a file-based preparation workflow needs an uploaded dataset, use export_dataset(dataset_id) in build phase. It returns source_path, optional labels_path and files with SHA-256/bytes in current-project results/datasets/<id>/. Pass those paths to existing workflow inputs, never guess an internal /data path. Originals remain unchanged; retries reuse a verified independent copy, modified copies conflict. No arbitrary destination is accepted. create_study freezes evaluation/data/image; submit_candidate snapshots feature code and creates a batch of at most 5 trials. submit_candidate only registers. Prefer submit_and_run(study_id, workflow_id, candidate, inputs={}, wait=true): registers and runs through an existing workflow, never edits it. Declare string inputs study_id/candidate_id; model_train config must reference $inputs with those paths. One training node only, no finalize. Additional inputs allowed except the two platform-bound identifiers. Same candidate request_key and content/workflow/inputs return the original task, even after draft edits. Changed content conflicts. Interrupted tasks require project_action resume. Every calculation has real task/run records. Default view=summary; view=full for complete records. candidates(candidate_id=...) reads one candidate; lists use offset/limit. submit_and_run returns project_task_id, workflow_id, study_id, candidate_id, status, error, task, candidate and study. It requires build phase. Task purpose is build_test and links the study item and feedback_task_id; cancellation follows workflow_run. Use workflow_run inspect for complete outputs. Read candidates for measured scores, baseline, per-group errors, failed trials and parent hypotheses. training_note(study_id, candidate_id, slot) reads the per-trial note with actual fitted parameters, fixed split, search distributions and same-study comparisons; slot is zero-based. Each completed or failed trial is saved automatically. Downloadable model bundles include training-note.md/json, comparison.csv and curve.svg. Missing historical fields remain unknown. For a study with search_strategy="aide", call next_step before EACH candidate. It invokes pinned upstream AIDE branch selection using only this study measured validation history. Follow returned stage draft/debug/improve and parent_id; use batch_size=1 and one models entry. stage=run means execute/resume the existing candidate first; stage=stop means finish or explicitly resume the original study. Read the selected parent note, propose one concrete change, submit and run the existing training workflow. Search choice, upstream revision and parent score are recorded automatically. This is AIDE search-policy + platform agent + fixed platform evaluator, not the full upstream AIDE agent. Do not claim unbiased cross-study wins when budgets, context or warm starts differ. Run ordinary parameter search with Optuna; after a batch propose ONE error-driven feature/model change and submit a new candidate. Candidates support search_space keyed by sklearn model and actual parameter (float/int low/high/log or categorical choices); explicit model space replaces its default search, fixed parameters remain fixed. Default SVM search includes C/kernel/gamma and regression epsilon; tree leaf defaults use the smallest training fold. AutoGluon accepts autogluon_hyperparameters for GBM/RF/XT/KNN/LR (default GBM/RF/XT); explicit values are preserved. Read trial warnings and diagnostics: constant predictions suggest checking sample size/features/parameters, not rejecting the algorithm. All-failed batches consume budget but not quality patience; read study repair_candidate_id and submit a fixed child candidate. Three identical failed batches interrupt only that study. Do not repeat the same failed code or change evaluation to improve scores. No supplied metrics are accepted. budget adjusts total seconds/trials. finish(study_id, reason) ends a completed round of search and stops its clock WITHOUT evaluating holdout; use it when delivering before budget/patience ends. It requires no active computation, keeps trials/models, and budget can reopen it. Do not use finalize when holdout evaluation is not authorized. Model tools never access other projects. Runtime: network disabled, 4 CPU/4GB, one modeling computation, immutable data/code/image. Worker files are internal; download via modeling API. Use model_predict with a completed candidate and unlabeled dataset; preserve training mapping. Targets cannot be guaranteed; report validation vs untouched test status.'


PROJECT_TOOL_MODELS = {
    'project_modeling': (ModelingTool, 'Project CPU modeling: analyze data, export_dataset for file-based preparation, create a study, submit_and_run a candidate through an editable workflow, compare measured results, read training notes. Default view=summary; view=full for details. Read block_catalog(tool_name="project_modeling") once for setup, inputs and examples. Reuse one input-driven training workflow. Stop/resume uses its original task. Evaluation/data stay fixed; never finalize holdout without authorization. AIDE studies require next_step before each new candidate.'),
    'project_progress': (ProgressTool, 'Default read returns a SUMMARY with current revision; item_id reads one complete item, view=full reads the complete record. Prefer action=patch, item_id, changes, expected_revision to create/update ONE item while preserving others. Without item_id patch accepts goal/summary only. For action=update, value is a COMPLETE replacement: read view=full first, never replace from a summary. Version conflicts are explicit. Preserve customer answers. Record this request deliverable/completion_criteria separately from the enterprise goal. Link only real current-project workflows, tasks and files.'),
    'project_action': (ProjectAction, 'Unified conversation: inspect progress; build a business item; trial/operate a workflow with actual inputs; resume an existing frozen task; discuss a requirement change; wait on a specific question/blocker; finish when no authorized work remains. trial/operate execute immediately and return real task results. Build feedback fixes use a NEW trial and feedback_task_id, never change an old task snapshot.'),
    'workflow_draft': (MemberDraft, 'Read the current draft SUMMARY (revision/content_hash, nodes/edges index, tests index); view=nodes with node_ids reads exact configs, view=tests reads saved tests, view=full reads the complete draft. workflow_id defaults to the project main. Prefer batch={expected_revision,expected_content_hash,idempotency_key,operations:[{op,data},...]} for related edits to ONE member: one atomic save, rollback on any error, one revision increment. A single operation using the legacy schema is also supported. Read current revision before editing, preserve human layout, and use update_node.data={node_id,changes,merge_config:true}. Mutations return a summary; full data remains readable. Project scope and build authorization still apply.'),
    'workflow_run': (MemberRun, 'Validate/start/inspect/test a member workflow. start waits by default; wait=false starts real concurrent tasks. For an existing running task, inspect(task_id=...,wait_seconds=30) waits up to 30 seconds without creating or restarting a task; maximum 60, default 0 returns immediately. Timeout returns its current status and leaves it running. Use bounded waiting instead of repeated immediate polling. Outputs default to a bounded summary; inspect(task_id=...,view=full) returns exact inputs, outputs and member traces. Small outputs remain complete; outputs_truncated explicitly marks previews. Saved tests return summary and failing cases by default; view=full returns every test. All member drafts freeze per task. Optional build request_key tests idempotency: same key/content returns existing task, changed content conflicts. Read actual failures and repair only affected code/graph, then rerun affected checks. Single terminal fields are direct; multiple terminals are grouped; workflow: calls wrap output.'),
    'project_workflows': (Members, 'List project members with ids, create a new blank member or remove an unreferenced member. Creation/removal only in build phase. Main workflow id equals project id. A Tool node with tool_name="workflow:<member-id>" and input={...} calls that member. Main canvas is the executable collaboration graph.'),
    'project_records': (Records, 'Read shared business records (get: found/revision/value; list: records). To change records, build and run a project_record node. Business operation phase cannot directly change files, graphs or records.'),
    'project_task_result': (TaskResult, 'Complete the active operate task or ask for needed input. A finished model turn does not itself finish a business task. Report actual run outputs; waiting_input lets the user update records and continue the same task.'),
}


def project_tool_specs():
    definitions = {x['name']: x for x in tool_specs()}
    for name, (model, description) in PROJECT_TOOL_MODELS.items():
        definitions[name] = {'type': 'function', 'name': name, 'description': description,
                             'inputSchema': model.model_json_schema(), 'deferLoading': False}
    return list(definitions.values())


PROJECT_INSTRUCTIONS = """
数据分析与建模使用 project_modeling 和 data_analysis / feature_extract / model_train / model_predict 积木。
先给客户数据概况和朴素参照，再运行标准模型。一次候选最多 5 次参数试验，Optuna 内部不需要逐次调用智能体。
每批完成后读取真实指标、分组误差和失败原因；提出一项具体改动，以 parent_id 保留前后关系，继续同一研究。
评价规则、数据版本不可在研究中改变；字段含义变更用 revise_dataset 创建新版本，再创建关联研究。
研究默认总预算 30 分钟、30 次训练；达到目标、预算耗尽或连续 3 批未改善就交付当前最佳及差距，不再盲目重试。
依赖安装失败是平台运行环境问题，报告具体错误及恢复动作，不能改用手写机器学习训练器。
最终测试集不参与搜索。返回的 validation 指标不能称为盲测或产线达标。代码转换必须返回可序列化 sklearn transformer。
保持一条紧凑训练流程：声明 study_id/candidate_id 字符串输入，训练节点引用 $inputs 对应字段。
后续用 project_modeling(action="submit_and_run",study_id,workflow_id,candidate) 提交并运行，避免每轮改图或创建工作流。
建模工具默认摘要；只在需要参数、逐样本划分或完整笔记时读 view="full"。用 block_catalog(tool_name="project_modeling") 查完整说明和示例。
结束时用已完成候选生成独立 model_predict 工作流并用无标签输入真实试用，向客户呈现模型、结果报告和使用方式。
本项目包含多个独立工作流。主工作流就是协作拓扑，调用节点使用 tool_name=workflow:<成员id>。
当前上下文提供相关成员id及草稿修订号；缺少成员时再读 project_workflows。不能复制旧项目解法。
workflow_draft默认摘要，不返回整份代码；需要配置时用view=nodes和node_ids，需要完整图时用view=full。
编辑前取得当前revision/content_hash；将同一成员的关联修改合成batch原子保存，避免逐节点往返和重复读取整图。
返回摘要足够判断保存结果；发生版本冲突时读取当前相关节点后合并，保留人工布局和无关修改。
requirement-package/ 和 requirements/ 由所有成员共用且只读，solution/、results/用于本次产物。
用 block_catalog(tool_name="Bash") 等查询运行工具参数和环境；空参数返回积木目录。
项目共享数据使用 project_record 积木。get 未找到时 revision=0；put 做修订号比较更新，
必须检查 written/conflict。竞争失败须重新读并判断，不能不看版本强行覆盖。
工作流返回等待业务条件时，在 end 输出明确的 task_status="waiting_input"；否则为 succeeded。
单个末端节点的结果直接返回字段，跨成员引用为 output.<字段>；多个末端则按节点id分组。以真实结果为准。
build阶段：规划后自行创建成员、编辑主流程与成员，执行真实测试并修复。
operate阶段：只能读资料和当前业务记录，选择执行主流程/成员；不能改图、需求或程序。
每次执行都使用当前项目任务启动时固定的草稿。根据结果调用 project_task_result
报告 succeeded、waiting_input 或 failed；需要补充信息就提出具体问题，等待用户继续。
工作流测试可使用平台代码工具运行本次新实现的算法，不需要内置大模型或外网。
运行标识可引用 $run.run_id，项目任务标识可引用 $run.project_task_id；均使用标准 $ref 分段路径。
build阶段 workflow_run(action="start",request_key="测试唯一键") 可以验证平台任务去重，返回项目任务id和成员运行。
真正并发的测试：用 wait=false 连续发起两项任务，再按 task_id 查询结果；不要用等待结束的串行调用充当并发。
所有实际测试写入共享记录，使用独立的测试键。停止 Lilies 会取消本轮仍在运行的测试任务。
保存的测试会复制 requirement-package/、requirements/、solution/、results/ 到每个用例独立目录；
资料及确认需求保持只读，成员调用共享该用例目录。用例产物不写回项目原目录，通过运行输出检查。
"""


class WorkspaceProjectTools(ProjectTools):
    def __init__(self, services, application_id: str, manager):
        super().__init__(services, application_id, manager)
        self.projects = services.projects

    def tool_definitions(self) -> list[dict]:
        return project_tool_specs()

    async def run_build_task(self, creation, wait=True):
        """Track/cancel a real task even if the agent disconnects during creation."""
        creation = asyncio.create_task(creation)
        try:
            task = await asyncio.shield(creation)
        except asyncio.CancelledError:
            task = await creation
            self.manager.track_project_task(self.application_id, task['id'])
            await self.projects.stop(self.application_id, task['id'])
            raise
        self.manager.track_project_task(self.application_id, task['id'])
        worker = self.projects.active.get(task['id'])
        if wait and worker:
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                await self.projects.stop(self.application_id, task['id'])
                raise
        return await self.projects.task(self.application_id, task['id'])

    async def call(self, name: str, arguments: dict) -> Any:
        phase = self.manager.load(self.application_id).get('phase')
        if name == 'project_file' and arguments.get('action') == 'write':
            self.require_build()
        if name == 'requirements_submit' and phase == 'operate' and arguments.get('action') != 'read':
            raise ValueError('业务处理阶段不能修改需求，请先切回需求沟通')
        if name not in PROJECT_TOOL_MODELS:
            result = await super().call(name, arguments)
            if name == 'block_catalog' and arguments.get('tool_name') == 'project_modeling':
                result['description'] = MODELING_MANUAL
                result['examples'] = [
                    {'action': 'profile', 'dataset_id': 'current-dataset', 'sampled': True},
                    {'action': 'export_dataset', 'dataset_id': 'current-dataset'},
                    {'action': 'submit_and_run', 'study_id': 'current-study', 'workflow_id': 'current-member',
                     'candidate': {'request_key': 'first-model', 'engine': 'sklearn', 'models': ['linear'], 'batch_size': 1}},
                    {'action': 'training_note', 'study_id': 'current-study', 'candidate_id': 'current-candidate', 'slot': 0, 'view': 'full'},
                ]
                result['workflow_example'] = {'nodes': [
                    {'id': 'start', 'type': 'start', 'title': '训练输入', 'config': {'inputs': [
                        {'name': key, 'type': 'string', 'required': True} for key in ('study_id', 'candidate_id')]}},
                    {'id': 'train', 'type': 'model_train', 'title': '训练评估', 'config': {
                        key: {'$ref': {'node_id': '$inputs', 'path': [key]}} for key in ('study_id', 'candidate_id')}},
                    {'id': 'end', 'type': 'end', 'title': '模型结果', 'config': {'outputs': {
                        'result': {'$ref': {'node_id': 'train', 'path': ['output']}}}}}],
                    'edges': [{'id': 'start-train', 'source': 'start', 'target': 'train'},
                              {'id': 'train-end', 'source': 'train', 'target': 'end'}]}
            if name == 'block_catalog' and arguments.get('tool_name') == 'workflow_draft':
                result['examples'] = [
                    {'workflow_id': 'current-member-id'},
                    {'view': 'nodes', 'node_ids': ['compute']},
                    {'batch': {'expected_revision': 5, 'expected_content_hash': 'hash-from-summary',
                        'idempotency_key': 'one-related-change', 'operations': [
                            {'op': 'update_node', 'data': {'node_id': 'compute', 'changes': {'title': '计算报价'}, 'merge_config': True}},
                            {'op': 'update_node', 'data': {'node_id': 'end', 'changes': {'title': '返回报价'}, 'merge_config': True}}]}},
                ]
            if name == 'block_catalog' and arguments.get('tool_name') == 'workflow_run':
                result['examples'] = [
                    {'action': 'inspect', 'task_id': 'existing-task-id'},
                    {'action': 'inspect', 'task_id': 'existing-task-id', 'wait_seconds': 30},
                ]
            return result
        args = PROJECT_TOOL_MODELS[name][0].model_validate(arguments)
        if name == 'project_modeling':
            modeling = self.projects.services.modeling
            if args.action in {'register_dataset', 'revise_dataset', 'export_dataset', 'create_study', 'submit_candidate', 'submit_and_run', 'budget'}:
                self.require_build()
            if args.action == 'submit_and_run':
                state = self.manager.load(self.application_id)
                task = await self.run_build_task(submit_and_start(self.services, self.application_id, args,
                    state.get('active_item_id', '') if state.get('conversation_enabled') else ''), args.wait)
                candidate = await modeling.duplicate(self.application_id, 'candidate', args.study_id, args.candidate.model_dump())
                # The real task is already tracked for stop/disconnection. Patch
                # only its link so concurrent trial commits remain intact.
                await modeling.link_task(self.application_id, candidate['id'], task['id'])
                candidate = await modeling.get(self.application_id, 'candidate', candidate['id'])
                study = await modeling.get(self.application_id, 'study', args.study_id)
                result = {'project_task_id': task['id'], 'workflow_id': task['workflow_id'], 'status': task['status'], 'error': task.get('error', ''),
                          'study_id': study['id'], 'candidate_id': candidate['id']}
                if args.view == 'full':
                    return {**result, 'task': task, 'candidate': candidate, 'study': study}
                summary = task_summary(task)
                # Training output repeats the candidate (including full fitted
                # models); its measured summary is returned once below.
                summary.pop('outputs', None)
                summary['outputs_in_detail'] = True
                return {**result, 'task': summary, 'candidate': candidate_summary(candidate), 'study': study_summary(study)}
            return await modeling.tool(self.application_id, args)
        if name == 'project_progress':
            if args.action == 'read':
                progress = await self.projects.store.progress(self.application_id)
                if args.item_id:
                    return {'revision': progress['revision'], 'item': await self.projects.conversation.item(self.application_id, args.item_id)}
                if args.view == 'full':
                    return progress
                state = self.manager.load(self.application_id)
                return progress_summary(progress, state.get('active_item_id', ''))
            if args.action == 'patch':
                result = await self.projects.conversation.patch(self.application_id, args)
                return progress_summary(result, args.item_id)
            if args.value is None:
                raise ValueError('请提供完整项目进展value')
            result = await self.projects.conversation.update(self.application_id, args.value, args.expected_revision)
            return result if args.view == 'full' else progress_summary(result)
        if name == 'project_action':
            result = await self.projects.conversation.action(self.application_id, args)
            if 'id' in result and 'outputs' in result:
                return task_summary(result)
            if 'progress' in result:
                return {**result, 'progress': progress_summary(result['progress'])}
            return result
        if name == 'project_workflows':
            if args.action == 'list':
                return await self.projects.store.get(self.application_id)
            if args.action == 'classify':
                await self.projects.member(self.application_id, args.workflow_id)
                await self.projects.store.classify_member(self.application_id, args.workflow_id, args.purpose)
                return {'workflow_id': args.workflow_id, 'purpose': args.purpose}
            self.require_build()
            if args.action == 'create':
                if not args.name.strip():
                    raise ValueError('请提供工作流名称')
                if self.manager.load(self.application_id).get('conversation_enabled') and 'purpose' not in arguments:
                    raise ValueError('请明确purpose=business或test，不能按工作流名称猜用途')
                return await self.projects.add_member(self.application_id, args.name, args.description, args.purpose)
            await self.projects.remove_member(self.application_id, args.workflow_id)
            return {'removed': args.workflow_id}
        if name == 'project_records':
            if args.action == 'get':
                return await self.projects.store.get_record(self.application_id, args.collection, args.key)
            return {'records': await self.projects.store.records(self.application_id, args.collection or None)}
        if name == 'project_task_result':
            state = self.manager.load(self.application_id)
            target_id = args.task_id if state.get('conversation_enabled') and args.task_id else state.get('project_task_id')
            if not target_id or (phase != 'operate' and not (state.get('conversation_enabled') and args.task_id)):
                raise ValueError('当前没有按需统筹任务')
            task = await self.projects.store.get_task(self.application_id, target_id)
            if state.get('conversation_enabled'):
                for artifact in args.artifacts:
                    if not self.path(artifact.get('file_path', '')).is_file():
                        raise ValueError('结果文件不存在')
                if task['status'] in {'running', 'queued'} and task['mode'] == 'workflow':
                    raise ValueError('实际运行尚未结束，请等待运行结果')
                if task['status'] == 'failed' and args.status == 'succeeded':
                    raise ValueError('实际运行失败，不能作为成功试用呈现')
                if task['mode'] == 'workflow' and task['status'] in {'waiting_input', 'interrupted'} and args.status == 'succeeded':
                    raise ValueError('实际任务仍需继续，不能作为已完成业务呈现')
                await self.projects.store.present_task(task['id'], {
                    'message': args.message, 'markdown': args.markdown or args.message, 'artifacts': args.artifacts})
                if task['mode'] == 'agent' and phase == 'operate' and target_id == state.get('project_task_id'):
                    await self.projects.store.update_task(task['id'], status=args.status,
                        outputs={**args.outputs, 'message': args.message})
                self.manager.event(self.application_id, 'result', args.message, task_id=task['id'],
                                   item_id=task.get('item_id', '') or state.get('active_item_id', ''), purpose=task.get('purpose', ''))
            else:
                await self.projects.store.update_task(task['id'], status=args.status,
                    outputs={**args.outputs, 'message': args.message})
            return {'status': args.status, 'message': args.message}
        workflow_id = args.workflow_id or self.application_id
        await self.projects.member(self.application_id, workflow_id)
        if name == 'workflow_draft':
            if args.operation or args.batch:
                self.require_build()
                edits = args.batch.operations if args.batch else [args.operation]
                for edit in edits:
                    if edit.op == 'set_metadata' and 'requirement' in edit.data:
                        raise ValueError('请通过需求沟通修改需求')
                    # Include whole-graph replacements as well as incremental nodes.
                    raw = json.dumps(edit.data, ensure_ascii=False)
                    if 'workflow:' in raw:
                        import re
                        for target in re.findall(r'workflow:([a-f0-9-]{36})', raw):
                            await self.projects.member(self.application_id, target)
                if args.batch:
                    result = await self.services.applications.apply_operations_atomically(workflow_id,
                        **args.batch.model_dump(), change_context_operation='project_agent_batch')
                else:
                    result = await self.services.applications.apply_operation(workflow_id, args.operation)
                summary = draft_summary(await self.services.workflow_store.get_draft(workflow_id))
                summary['applied_revision'] = result['revision']
                summary['operations_applied'] = len(edits)
                summary['changed_operations'] = [e.op for e in edits]
                return summary
            draft = jsonable_encoder(await self.services.workflow_store.get_draft(workflow_id))
            if args.view == 'full':
                return draft
            if args.view == 'nodes':
                graph = draft['snapshot']['workflow']
                found = {n['id']: n for n in graph['nodes']}
                if set(args.node_ids) - found.keys():
                    raise ValueError('节点不存在，请读取当前草稿摘要中的节点索引')
                return {'revision': draft['revision'], 'content_hash': draft['content_hash'],
                        'nodes': [found[n] for n in args.node_ids]}
            if args.view == 'tests':
                return {'revision': draft['revision'], 'content_hash': draft['content_hash'], 'tests': draft['snapshot']['tests']}
            return draft_summary(draft)
        if name == 'workflow_run':
            if args.action == 'inspect':
                if args.task_id:
                    task = await self.projects.task(self.application_id, args.task_id)
                    if args.wait_seconds and task['status'] in {'queued', 'running'}:
                        worker = self.projects.active.get(task['id'])
                        if worker:
                            # asyncio.wait observes the existing worker without
                            # cancelling it when this bounded wait times out.
                            await asyncio.wait({worker}, timeout=args.wait_seconds)
                            task = await self.projects.task(self.application_id, args.task_id)
                    return task if args.view == 'full' else task_summary(task)
                run = await self.services.workflow_store.get_run(args.run_id)
                context = run['state'].project_context
                if not context or context['project_id'] != self.application_id:
                    raise ValueError('不能查看其他项目的运行')
                return jsonable_encoder(run)
            if phase not in {'build', 'operate'}:
                raise ValueError('需求沟通阶段不能运行工作流')
            if phase == 'build':
                self.require_build()
            if args.action == 'validate':
                return jsonable_encoder(await self.services.applications.validate_draft(workflow_id))
            if args.action == 'tests':
                self.require_build()
                # Each saved test uses a real project task with project context and frozen members.
                result = await self.projects.test_workflow(self.application_id, workflow_id)
                if args.view == 'full':
                    return result
                return {k: result[k] for k in ('passed', 'summary', 'project_task_id') if k in result} | {
                    'failed_tests': [t for t in result.get('tests', []) if not t.get('passed')],
                    'detail': 'workflow_run(action="inspect",task_id=project_task_id,view="full") includes the complete saved-test report.'}
            state = self.manager.load(self.application_id)
            if phase == 'operate':
                if args.request_key:
                    raise ValueError('业务阶段沿用当前项目任务，不能另设请求标识')
                task_id = state['project_task_id']
                result = await self.projects.execute(self.application_id, task_id, workflow_id, args.inputs,
                                                    step='agent/' + workflow_id, reuse=True)
                if result['status'] == 'failed':
                    await self.projects.cancel_runs(task_id)
                return result
            result = await self.run_build_task(self.projects.start(self.application_id,
                request_key=args.request_key or 'build-' + str(uuid4()), workflow_id=workflow_id, inputs=args.inputs,
                purpose='build_test', item_id=state.get('active_item_id', '') if state.get('conversation_enabled') else ''), args.wait)
            return result if args.view == 'full' else task_summary(result)
        raise ValueError('未实现的项目操作')
