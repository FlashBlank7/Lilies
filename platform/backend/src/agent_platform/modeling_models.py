"""Small, project-scoped contracts shared by the UI, agent and workflow blocks."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class DataMapping(Contract):
    kind: Literal['tabular', 'timeseries'] = 'tabular'
    target: str = ''
    id_column: str = ''
    group_column: str = ''
    time_column: str = ''
    time_unit: Literal['iso', 's', 'ms', 'us', 'ns'] = 'iso'
    prediction_time_column: str = ''
    available_time_column: str = ''
    label_available_time_column: str = ''
    error_group_columns: list[str] = Field(default_factory=list, max_length=5, description='Columns used only for error breakdown (e.g. production line); they remain usable as features and do not change the split.')
    sheet: str | int = 0

    @model_validator(mode='after')
    def time_keys(self):
        if self.kind == 'timeseries' and not all([self.id_column, self.time_column, self.prediction_time_column]):
            raise ValueError('工艺时序需要样本标识、测量时间及标签表中的预测时点列')
        return self


class DatasetRequest(Contract):
    name: str = Field(default='', max_length=160)
    source_path: str
    labels_path: str = ''
    replaces_id: str = ''
    mapping: DataMapping = Field(default_factory=DataMapping)


class Evaluation(Contract):
    problem: Literal['regression', 'classification'] = 'regression'
    metric: Literal['mae', 'rmse', 'r2', 'macro_f1', 'accuracy', 'roc_auc'] = 'mae'
    split: Literal['random', 'group', 'time'] = 'random'
    folds: int = Field(default=3, ge=2, le=5)
    seed: int = 42
    holdout_fraction: float = Field(default=0.2, ge=0, le=0.4)
    target_score: float | None = None
    gap_seconds: int = Field(default=0, ge=0, description='Fixed temporal separation. For overlapping time-series windows, set at least the largest candidate window; zero conservatively purges shared subjects.')

    @model_validator(mode='after')
    def compatible(self):
        allowed = {'regression': {'mae', 'rmse', 'r2'}, 'classification': {'macro_f1', 'accuracy', 'roc_auc'}}
        if self.metric not in allowed[self.problem]:
            raise ValueError('评价指标与分类／回归任务不匹配')
        return self


class Budget(Contract):
    seconds: int = Field(default=1800, ge=10, le=86400)
    trials: int = Field(default=30, ge=1, le=1000)
    patience: int = Field(default=3, ge=1, le=30)
    trial_seconds: int = Field(default=300, ge=5, le=1800)


class AideSearch(Contract):
    num_drafts: int = Field(default=5, ge=1, le=20)
    debug_prob: float = Field(default=0.5, ge=0, le=1)
    max_debug_depth: int = Field(default=3, ge=0, le=10)


class StudyRequest(Contract):
    dataset_id: str
    name: str = Field(default='自主建模', max_length=160)
    request_key: str = Field(min_length=1, max_length=160)
    item_id: str = ''
    parent_study_id: str = ''
    evaluation: Evaluation = Field(default_factory=Evaluation)
    budget: Budget = Field(default_factory=Budget)
    search_strategy: Literal['codex', 'aide'] = 'codex'
    aide: AideSearch = Field(default_factory=AideSearch)


class FeaturePlan(Contract):
    columns: list[str] = Field(default_factory=list, max_length=500)
    exclude: list[str] = Field(default_factory=list, max_length=500)
    timeseries: Literal['minimal', 'extended'] = 'minimal'
    window_seconds: int | None = Field(default=None, ge=1)
    select_k: int | None = Field(default=None, ge=1, le=500)
    transformer_path: str = Field(default='', description='Optional current-project Python module defining build_transformer(); returns a sklearn transformer fitted inside each training fold.')


class SearchParameter(Contract):
    type: Literal['float', 'int', 'categorical']
    low: float | None = None
    high: float | None = None
    log: bool = False
    choices: list[str | int | float | bool] = Field(default_factory=list, max_length=30)

    @model_validator(mode='after')
    def bounds(self):
        if self.type == 'categorical':
            if not self.choices or self.low is not None or self.high is not None or self.log:
                raise ValueError('分类搜索需要 choices，不能同时设置数值范围或 log')
        elif self.low is None or self.high is None or self.low > self.high or self.choices:
            raise ValueError('数值搜索需要有效 low/high，不能设置 choices')
        elif (self.log and self.low <= 0) or (self.type == 'int' and (not self.low.is_integer() or not self.high.is_integer())):
            raise ValueError('对数范围必须为正，整数搜索边界必须是整数')
        return self


class CandidateRequest(Contract):
    request_key: str = Field(min_length=1, max_length=160)
    parent_id: str = ''
    hypothesis: str = Field(default='建立初步模型并比较效果', min_length=1, max_length=2000)
    engine: Literal['sklearn', 'optuna', 'autogluon'] = 'optuna'
    models: list[Literal['linear', 'forest', 'hist_gradient', 'svm']] = Field(default_factory=lambda: ['linear', 'forest', 'hist_gradient'], min_length=1, max_length=4)
    batch_size: int = Field(default=5, ge=1, le=5)
    features: FeaturePlan = Field(default_factory=FeaturePlan)
    parameters: dict[str, Any] = Field(default_factory=dict)
    search_space: dict[Literal['linear', 'forest', 'hist_gradient', 'svm'], dict[str, SearchParameter]] = Field(default_factory=dict, description='Optuna distributions keyed by model and estimator parameter. An explicit model space replaces that model default search. Fixed parameters are not searched.')
    autogluon_hyperparameters: dict[Literal['GBM', 'RF', 'XT', 'KNN', 'LR'], dict[str, Any] | list[dict[str, Any]]] | None = Field(default=None, description='CPU AutoGluon model configurations. Default: GBM, RF and XT, with small-sample leaf defaults. Explicit values are preserved; outer evaluation stays fixed.')
    feedback_task_id: str = ''

    @model_validator(mode='after')
    def search_engine(self):
        if self.search_space and (self.engine != 'optuna' or not set(self.search_space) <= set(self.models)):
            raise ValueError('search_space 仅用于 Optuna 且必须属于本候选的 models')
        if any(set(space) & set(self.parameters) for space in self.search_space.values()):
            raise ValueError('同一参数不能同时固定和搜索')
        if self.autogluon_hyperparameters is not None and (self.engine != 'autogluon' or not self.autogluon_hyperparameters):
            raise ValueError('autogluon_hyperparameters 需要 AutoGluon 引擎和至少一种模型')
        if self.autogluon_hyperparameters and any(isinstance(v, list) and not v for v in self.autogluon_hyperparameters.values()):
            raise ValueError('AutoGluon 模型配置列表不能为空')
        return self


class ModelingBlock(Contract):
    model_ref: Any = ''
    slot: Any = None
    dataset_id: Any = ''
    study_id: Any = ''
    candidate_id: Any = ''
    features: Any = Field(default_factory=dict)
    finalize: bool = False


class FinishStudy(Contract):
    reason: str = Field(default='本轮方案比较已完成，保留当前结果', min_length=1, max_length=1000)


class ModelingTool(Contract):
    action: Literal['datasets', 'register_dataset', 'revise_dataset', 'export_dataset', 'profile', 'studies', 'create_study', 'read_study', 'next_step', 'submit_candidate', 'submit_and_run', 'train', 'candidates', 'training_note', 'budget', 'finish']
    view: Literal['summary', 'full'] = 'summary'
    workflow_id: str = ''
    inputs: dict[str, Any] = Field(default_factory=dict, description='Additional workflow inputs. study_id and candidate_id are bound by submit_and_run; do not supply them here.')
    wait: bool = True
    reason: str = Field(default='本轮方案比较已完成，保留当前结果', min_length=1, max_length=1000)
    dataset_id: str = ''
    study_id: str = ''
    candidate_id: str = ''
    slot: int = Field(default=0, ge=0)
    sampled: bool = False
    dataset: DatasetRequest | None = None
    mapping: DataMapping | None = None
    study: StudyRequest | None = None
    candidate: CandidateRequest | None = None
    budget: Budget | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)

    @model_validator(mode='after')
    def submission(self):
        if self.action == 'train' and (not self.study_id or self.candidate is None):
            raise ValueError('train 需要 study_id 和 candidate 配置')
        if self.action == 'submit_and_run':
            if not self.study_id or not self.workflow_id or self.candidate is None:
                raise ValueError('submit_and_run 需要 study_id、workflow_id 和 candidate 配置')
            if {'study_id', 'candidate_id'} & self.inputs.keys():
                raise ValueError('study_id 和 candidate_id 由平台绑定，请勿在 inputs 中提供')
        return self


def register_modeling_blocks(registry):
    from .blocks import _definition
    from .workflow_models import ValueType
    for kind, title, description in [
        ('data_analysis', '数据分析', '扫描项目数据集，查看质量、标签分布和时序概况。'),
        ('feature_extract', '特征提取', '按预测时点提取时序特征或选择表格字段；需要拟合的转换留在训练折内。'),
        ('model_train', '训练评估', '运行已登记的建模候选，保存逐次试验、最佳模型和真实评价。'),
        ('model_predict', '模型预测', '通过 model_ref 使用项目模型预测无标签 dataset_id 数据。自动保存 CSV，output.artifact 是项目内下载路径；output.model_version 是固定模型版本。无需额外导出积木。模型可在保存后绑定。'),
    ]:
        definition = _definition(kind, title, description, 'integration', ModelingBlock,
            inputs=[('input', ValueType.any)], outputs=[('output', ValueType.object)], error_branch=True,
            manual={'summary': description + ' 必须从项目任务运行。用 project_modeling 登记数据和研究、提交候选；填写返回的标识。'
                '所有输出位于 output。data_analysis/feature_extract 需要 dataset_id；model_train 需要 study_id/candidate_id；'
                'model_predict 使用无标签 dataset_id 和 model_ref（也兼容旧 study_id/candidate_id），返回 output.artifact 的 CSV 下载路径。资源可稍后绑定。model_train 的 finalize=true 在研究结束后评价保留测试集，随后不再允许搜索。模型、评估器及数据快照不可由节点覆盖。',
                'when_to_use': ['表格分类回归与工艺时序建模'],
                'examples': [{'description': title, 'config': {'dataset_id': '当前项目数据集标识'}}],
                'anti_patterns': ['不要改变已固定的划分，不要把训练成功当成精度达标。'],
                'common_errors': ['请先登记数据集／提交实验候选。'],
                'claude_architecture_mapping': 'Project modeling runtime',
                'composability_constraints': ['项目身份由运行上下文绑定。']})
        definition.editor['i18n']['zh'].update(title=title, description=description)
        fields = [('dataset_id', '数据集')]
        if kind == 'model_predict':
            fields += [('model_ref', '调用模型')]
        elif kind == 'model_train':
            fields += [('study_id', '训练记录'), ('candidate_id', '候选方案')]
        if kind == 'feature_extract':
            fields += [('features', '特征设置')]
        definition.editor['fields'] = [{'path': key, 'label': label, 'label_zh': label,
            'control': 'json' if key == 'features' else 'reference_or_text'} for key, label in fields]
        registry.register(definition, ModelingBlock)
