"""Small, project-scoped contracts shared by the UI, agent and workflow blocks."""
from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class FieldRule(Contract):
    dtype: Literal['number', 'category', 'text'] = 'number'
    description: str = ''
    unit: str = ''
    meaning: Literal['known', 'pending'] = 'known'
    missing_tokens: dict[str, Literal['unknown', 'not_measured', 'not_applicable', 'unjudged']] = Field(default_factory=dict)
    allow_missing: list[Literal['unknown', 'not_measured', 'not_applicable']] = Field(default_factory=list)
    pending_tokens: list[str] = Field(default_factory=list)
    intervals: Literal['pending', 'midpoint', 'lower', 'upper'] = 'pending'
    categories: list[str] = Field(default_factory=list)
    ordered_values: list[str] = Field(default_factory=list)
    lower: float | None = None
    upper: float | None = None

    @model_validator(mode='after')
    def consistent(self):
        if set(self.pending_tokens) & set(self.missing_tokens):
            raise ValueError('同一符号不能同时是含义待确认和已定义缺失')
        if self.lower is not None and self.upper is not None and self.lower > self.upper:
            raise ValueError('字段下限不能大于上限')
        if self.ordered_values and (self.dtype != 'category' or len(set(self.ordered_values)) != len(self.ordered_values)
                                    or (self.categories and set(self.ordered_values) != set(self.categories))):
            raise ValueError('有序类别必须是明确的、不重复的完整类别顺序')
        return self


class DataMapping(Contract):
    kind: Literal['tabular', 'timeseries'] = 'tabular'
    target: str = Field(default='', description='Target column name; use target, not target_column. Leave empty for analysis or unlabeled prediction.')
    id_column: str = ''
    group_column: str = ''
    time_column: str = ''
    time_unit: Literal['iso', 's', 'ms', 'us', 'ns'] = 'iso'
    prediction_time_column: str = ''
    available_time_column: str = ''
    label_available_time_column: str = ''
    error_group_columns: list[str] = Field(default_factory=list, max_length=5, description='Columns used only for error breakdown (e.g. production line); they remain usable as features and do not change the split.')
    sheet: str | int = Field(default=0, description='Sheet name or zero-based index for XLSX. Omit for CSV/TSV; null is not valid.')
    header_row: int = Field(default=0, ge=0, le=10000, description='Zero-based row containing column names.')
    skip_after_header: int = Field(default=0, ge=0, le=10000, description='Rows immediately below the header to omit, such as a second heading row.')
    field_rules: dict[str, FieldRule] = Field(default_factory=dict, description='Per-column meaning, missing reasons and parsing rules, saved with datasets and models. Unknown tokens stay unresolved; they are never automatically imputed.')
    invalid_rows: Literal['error', 'exclude'] = Field(default='error', description='Training only: explicitly exclude rows with unresolved or invalid ruled features before fixing the split, or fail with their locations. Prediction retains all rows and gives reasons for unavailable predictions.')

    @model_validator(mode='after')
    def time_keys(self):
        if self.field_rules and self.kind != 'tabular':
            raise ValueError('字段含义规则当前适用于单张表格')
        if self.kind == 'timeseries' and not all([self.id_column, self.time_column, self.prediction_time_column]):
            raise ValueError('工艺时序需要样本标识、测量时间及标签表中的预测时点列')
        return self


class DatasetRequest(Contract):
    name: str = Field(default='', max_length=160)
    source_path: str
    labels_path: str = ''
    state_path: str = Field(default='', description='Optional data-state.json returned by a platform transformation; preserve it when registering its processed.csv.')
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
    acceptance_accuracy: float | None = Field(default=None, gt=0, le=1, description='Optional validation accuracy target among automatically accepted predictions; not a production guarantee.')
    acceptance_min_samples: int = Field(default=10, ge=1)
    gap_seconds: int = Field(default=0, ge=0, description='Fixed temporal separation. For overlapping time-series windows, set at least the largest candidate window; zero conservatively purges shared subjects.')

    @model_validator(mode='after')
    def compatible(self):
        allowed = {'regression': {'mae', 'rmse', 'r2'}, 'classification': {'macro_f1', 'accuracy', 'roc_auc'}}
        if self.metric not in allowed[self.problem]:
            raise ValueError('评价指标与分类／回归任务不匹配')
        if self.acceptance_accuracy is not None and self.problem != 'classification':
            raise ValueError('自动采纳阈值只适用于分类任务')
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


class NumericRules(Contract):
    missing_tokens: list[str] = Field(default_factory=list, max_length=50)
    intervals: Literal['reject', 'midpoint', 'lower', 'upper', 'missing'] = 'reject'


class NumericPreparation(NumericRules):
    columns: list[str] = Field(default_factory=list, max_length=500)


class TrainingPreprocessing(Contract):
    numeric_missing: Literal['auto', 'median', 'mean', 'constant', 'error'] = 'auto'
    categorical_missing: Literal['auto', 'preserve'] = Field(default='auto', description='Non-numeric category columns only. preserve uses fold-fitted one-hot encoding without mode imputation; unseen values, including unseen missing, encode as all zeros. Numeric-coded columns remain numeric.')
    fill_value: float = 0
    scaling: Literal['auto', 'none', 'standard', 'robust'] = 'auto'
    outliers: Literal['none', 'iqr_clip'] = 'none'
    outlier_columns: list[str] = Field(default_factory=list, max_length=500)
    iqr_factor: float = Field(default=1.5, gt=0, le=100)


class FeaturePlan(Contract):
    columns: list[str] = Field(default_factory=list, max_length=500)
    exclude: list[str] = Field(default_factory=list, max_length=500)
    timeseries: Literal['minimal', 'extended'] = 'minimal'
    window_seconds: int | None = Field(default=None, ge=1)
    select_k: int | None = Field(default=None, ge=1, le=500)
    numeric: NumericPreparation = Field(default_factory=NumericPreparation)
    preprocessing: TrainingPreprocessing = Field(default_factory=TrainingPreprocessing)
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


class DataAnalysisPlan(Contract):
    operation: Literal['profile', 'spearman', 'outliers', 'numeric'] = 'profile'
    columns: list[str] = Field(default_factory=list, max_length=50)
    missing: Literal['pairwise', 'complete', 'raise'] = 'pairwise'
    method: Literal['iqr', 'bounds'] = 'iqr'
    handling: Literal['flag', 'drop', 'clip'] = 'flag'
    iqr_factor: float = Field(default=1.5, gt=0)
    lower: float | None = None
    upper: float | None = None
    numeric: NumericRules = Field(default_factory=NumericRules)

    @model_validator(mode='after')
    def valid_columns_and_bounds(self):
        if len(self.columns) != len(set(self.columns)):
            raise ValueError('分析列不能重复')
        if self.lower is not None and self.upper is not None and self.lower > self.upper:
            raise ValueError('异常值下限不能大于上限')
        return self


class ModelingBlock(Contract):
    # Old drafts may contain every modeling field. Only unused default
    # values may be discarded; a configured option must never be silently lost.
    legacy_config_defaults: ClassVar[dict[str, Any]] = {
        'source_path': '', 'labels_path': '', 'mapping': {}, 'evaluation': {},
        'budget': {}, 'candidate': {}, 'model_ref': '', 'slot': None,
        'dataset_id': '', 'study_id': '', 'candidate_id': '', 'features': {},
        'finalize': False, 'analysis': DataAnalysisPlan().model_dump(),
    }

    @model_validator(mode='before')
    @classmethod
    def remove_unused_legacy_defaults(cls, value):
        if not isinstance(value, dict):
            return value
        result = dict(value)
        for key in value.keys() - cls.model_fields.keys():
            if key not in cls.legacy_config_defaults:
                continue
            supplied = value[key]
            if key == 'analysis' and isinstance(supplied, dict):
                supplied = DataAnalysisPlan.model_validate(supplied).model_dump()
            if supplied != cls.legacy_config_defaults[key]:
                raise ValueError(f'此积木不使用 {key} 配置；请将其放到对应用途的积木中')
            del result[key]
        return result


class DatasetBlock(ModelingBlock):
    source_path: Any = ''
    labels_path: Any = ''
    state_path: Any = ''
    mapping: Any = Field(default_factory=dict)
    dataset_id: Any = ''


class DataAnalysisBlock(DatasetBlock):
    analysis: DataAnalysisPlan | dict[str, Any] = Field(default_factory=DataAnalysisPlan,
        description='Analysis settings may contain workflow references; the resolved values are validated as DataAnalysisPlan at execution.')

    @field_validator('analysis', mode='before')
    @classmethod
    def validate_literal_analysis(cls, value):
        def deferred(item):
            if isinstance(item, dict):
                return '$ref' in item or '$formula' in item or any(deferred(v) for v in item.values())
            return isinstance(item, list) and any(deferred(v) for v in item)
        return value if deferred(value) else DataAnalysisPlan.model_validate(value)


class FeatureExtractBlock(ModelingBlock):
    dataset_id: Any = ''
    features: Any = Field(default_factory=dict)


class ModelTrainBlock(ModelingBlock):
    dataset_id: Any = ''
    features: Any = Field(default_factory=dict)
    evaluation: Any = Field(default_factory=dict)
    budget: Any = Field(default_factory=dict)
    candidate: Any = Field(default_factory=dict)
    study_id: Any = ''
    candidate_id: Any = ''
    finalize: bool = False


class ModelPredictBlock(DatasetBlock):
    model_ref: Any = ''
    slot: Any = None
    study_id: Any = ''
    candidate_id: Any = ''


class FinishStudy(Contract):
    reason: str = Field(default='本轮方案比较已完成，保留当前结果', min_length=1, max_length=1000)


class ModelingTool(Contract):
    action: Literal['datasets', 'register_dataset', 'revise_dataset', 'export_dataset', 'profile', 'analyze', 'studies', 'create_study', 'read_study', 'next_step', 'submit_candidate', 'submit_and_run', 'train', 'candidates', 'training_note', 'budget', 'finish']
    analysis: DataAnalysisPlan = Field(default_factory=DataAnalysisPlan)
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
    dataset: DatasetRequest | None = Field(default=None, description='register_dataset uses {source_path, mapping:{kind:"tabular", target:"label", ...}}. Mapping belongs inside dataset.')
    mapping: DataMapping | None = Field(default=None, description='For revise_dataset only. register_dataset uses dataset.mapping.')
    study: StudyRequest | None = None
    candidate: CandidateRequest | None = None
    budget: Budget | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)

    @model_validator(mode='after')
    def submission(self):
        if self.action == 'register_dataset' and self.mapping is not None:
            raise ValueError('register_dataset 的映射应放在 dataset.mapping；顶层 mapping 只用于 revise_dataset，不能在注册时忽略它。例：dataset={"source_path":"results/data.csv","mapping":{"kind":"tabular","target":"label","id_column":"row_id","group_column":"group_id"}}；CSV 省略 sheet，XLSX 用 sheet=0 或工作表名，不用 null')
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
    manuals = {
        'data_analysis': {
            'summary': '通过 source_path、可选 labels_path 和 mapping 读取当前项目的 CSV/TSV/XLSX，或传入已有 dataset_id。'
                '返回 output.dataset_id 和分析结果；清洗改变数据时输出新的数据集，原文件保留。'
                '概况扫描支持表格和已配置样本、时间及预测时点的工艺时序；相关分析和异常值处理当前只支持单表数值列。',
            'when_to_use': ['检查数据形态、明确整理数值、计算 Spearman 或处理异常值。'],
            'examples': [{'description': '两个数值字段的相关分析', 'config': {'source_path': 'requirement-package/data.csv',
                'analysis': {'operation': 'spearman', 'columns': ['pressure', 'height']}}}],
            'anti_patterns': ['不把图片、PDF 或数据库连接直接作为表格输入。不要未经业务确认将区间值取平均。'],
            'common_errors': ['数值操作遇到文字时，先明确占位符和区间规则；未指定规则时保留待配置。'],
        },
        'feature_extract': {
            'summary': '输入 dataset_id 和 features。表格数据选择/排除字段；工艺时序按已登记的样本标识、测量时间、预测时点提取窗口特征。'
                '输出 output.dataset_id、output.feature_plan、特征概况及文件，可分别连接训练节点的 dataset_id 和 features。'
                '数值整理使用显式规则；缺失值填补、特征筛选和自定义转换的拟合发生在后续训练折内，此节点不训练预测模型。',
            'when_to_use': ['整理用于训练的字段或生成按预测时点截取的时序特征。'],
            'examples': [{'description': '明确训练字段', 'config': {'dataset_id': '当前项目数据集标识',
                'features': {'columns': ['pressure', 'height']}}}],
            'anti_patterns': ['不把完整数据上拟合的特征转换当成无泄漏的模型评估。'],
            'common_errors': ['先登记或连接数据分析节点的数据集；时序数据需要时间和样本字段映射。'],
        },
        'model_train': {
            'summary': '输入已有或上游 dataset_id、features、evaluation、budget 和 candidate，训练表格分类/回归模型；'
                '工艺时序先按预测时点构造样本。数据需要明确目标列及有效标签。'
                'features 可引用特征节点的 output.feature_plan；评价、预算和候选方案分别配置。'
                '返回 output.id（候选标识）、output.study_id、output.trials 及实际评价。'
                '也可用 study_id/candidate_id 继续已有候选，沿用固定数据和划分。finalize=true 只评价结束研究的保留测试集，之后不能继续搜索。',
            'when_to_use': ['训练、比较候选模型或继续已有训练。'],
            'examples': [{'description': '对已登记数据做分类训练', 'config': {'dataset_id': '当前项目数据集标识',
                'evaluation': {'problem': 'classification', 'metric': 'macro_f1'},
                'candidate': {'engine': 'sklearn', 'models': ['forest'], 'batch_size': 1}}}],
            'anti_patterns': ['训练成功不代表精度达标。不要改变已固定研究的划分或把保留测试集用于搜索。'],
            'common_errors': ['分类和回归指标须匹配；分组划分需指定分组列，时间划分需指定时间列。'],
        },
        'model_predict': {
            'summary': '输入新数据的 source_path 或 dataset_id，以及项目 model_ref；模型可以稍后绑定。'
                '也兼容 study_id/candidate_id 和可选 slot。新文件需包含模型训练时所需字段，普通表格预测不需要目标标签。'
                'mapping.sheet、header_row、skip_after_header 描述新文件布局；字段语义、数值规则、已拟合预处理及运行环境沿用所选模型。'
                '输出 output.artifact 的 CSV 下载路径、output.project_path 及 output.model_version。此节点不重新训练模型。',
            'when_to_use': ['用已训练或已导入的模型处理新样本。'],
            'examples': [{'description': '使用稍后绑定的项目模型', 'config': {'source_path': 'requirement-package/new-data.csv', 'model_ref': 'quality-model'}}],
            'anti_patterns': ['不在预测节点重新配置训练特征、训练预算或评估划分。'],
            'common_errors': ['运行时必须绑定模型并提供所需字段；缺失字段应补齐，不能悄悄换模型。',
                '此节点只预测，不生成新表的异常与相关分析。整套分析复用示例：project_skills(action="read", skill_id="modeling", reference="analysis-prediction-workflow.json")；将本次输入的数据集分别连接分析和预测节点。'],
        },
    }
    for kind, config, title, description in [
        ('data_analysis', DataAnalysisBlock, '数据分析', '选择数据概况、数值整理、Spearman 相关分析或异常值处理；返回统计结果或新的数据集。'),
        ('feature_extract', FeatureExtractBlock, '特征提取', '按预测时点提取时序特征或选择表格字段；需要拟合的转换留在训练折内。'),
        ('model_train', ModelTrainBlock, '训练评估', '从上游数据集和表单配置训练，或继续已有候选；保存模型、预处理和真实评价。'),
        ('model_predict', ModelPredictBlock, '模型预测', '选择项目文件和已绑定模型，沿用训练时的字段语义、预处理和环境预测。生成可下载 CSV；模型可稍后绑定。'),
    ]:
        manual = manuals[kind]
        definition = _definition(kind, title, description, 'integration', config,
            inputs=[('input', ValueType.any)], outputs=[('output', ValueType.object)], error_branch=True,
            manual={**manual, 'summary': manual['summary'] + ' 从项目任务运行，所有结果位于 output；资源和列未配置时仍可保存草稿，运行时检查。',
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
        def field(path, label, control='text', **extra):
            return {'path': path, 'label': label, 'label_zh': label, 'control': control, **extra}
        def numeric_fields(prefix):
            return [field(prefix + '.missing_tokens', '视为缺失的文字（每行一个，如 /）', 'string_list'),
                field(prefix + '.intervals', '区间值处理（如 3.5-4.2）', 'enum',
                    options=['reject', 'midpoint', 'lower', 'upper', 'missing'], default_value='reject',
                    option_labels={'reject': '遇到区间值即停止', 'midpoint': '取区间中点',
                                   'lower': '取下限', 'upper': '取上限', 'missing': '视为缺失'})]
        if kind in {'data_analysis', 'model_predict'}:
            definition.editor['fields'] = [field('source_path', '项目数据文件', 'reference_or_text'),
                field('labels_path', '独立标签文件（可选）', 'reference_or_text'),
                field('dataset_id', '已有数据集（未选文件时使用）', 'reference_or_text'),
                *[field('mapping.' + k, label, 'reference_or_text') for k, label in [
                    ('target', '预测目标列'), ('id_column', '样本标识列'), ('group_column', '批次／分组列'),
                    ('time_column', '时间列'), ('prediction_time_column', '预测时点列'),
                    ('available_time_column', '数据可用时间列'), ('label_available_time_column', '标签可用时间列')]],
                field('mapping.kind', '数据形态', 'enum', options=['tabular', 'timeseries'], default_value='tabular'),
                field('mapping.time_unit', '时间格式', 'enum', options=['iso', 's', 'ms', 'us', 'ns'], default_value='iso'),
                field('mapping.sheet', '工作表序号（从 0 开始）', 'number', default_value=0, minimum=0),
                field('mapping.header_row', '列名所在行（从 0 开始）', 'number', default_value=0, minimum=0),
                field('mapping.skip_after_header', '列名下方跳过行数', 'number', default_value=0, minimum=0)]
            if kind == 'model_predict':
                definition.editor['fields'] = (definition.editor['fields'][:3] + [field('model_ref', '调用模型', 'reference_or_text')]
                                               + definition.editor['fields'][-3:])
                definition.editor['fields'].append(field('state_path', '处理结果的原值与状态文件（可选）', 'reference_or_text'))
        if kind == 'data_analysis':
            definition.editor['fields'] += [field('mapping.field_rules', '字段含义与处理规则', 'json'),
                field('mapping.invalid_rows', '训练中无法使用的记录', 'enum', options=['error', 'exclude'], default_value='error',
                      option_labels={'error': '提示具体问题', 'exclude': '排除并报告数量'}),
                field('state_path', '处理结果的原值与状态文件（可选）', 'reference_or_text')]
            definition.manual_summary += (' analysis.operation 默认为 profile，兼容已有概况分析。spearman 需要至少两列数值字段，'
                '返回 pairs、correlation、sample_counts 和可下载结果；missing 可选 pairwise、complete、raise。'
                'outliers 支持单表数值列，method=iqr 或 bounds；handling=flag（只标记）、drop（删除异常行）、clip（截到边界）。'
                'drop/clip 返回新的 output.dataset_id，原数据不变，可接下一分析或训练节点。列通过 analysis.columns 明确选择；'
                'numeric 按 analysis.numeric.missing_tokens 和 intervals（reject/midpoint/lower/upper/missing）整理指定列，'
                '返回新数据集和逐项转换文件；未知文字或布尔值报错，原始行数不变。mapping.header_row 和 skip_after_header 可跳过标题或第二层表头。'
                '异常处理保留缺失值，不接受无穷值。IQR 在本次全表上估计，仅用于探索或经业务确认的数据清洗；'
                '模型评价中的自适应预处理必须在训练折内拟合。资源和列可先留空，运行时再检查。')
            definition.editor['fields'] = [
                field('analysis.operation', '分析操作', 'enum', options=['profile', 'numeric', 'spearman', 'outliers'], default_value='profile',
                      option_labels={'profile': '数据概况', 'numeric': '数值整理', 'spearman': 'Spearman 相关分析', 'outliers': '异常值处理'}),
                *definition.editor['fields'],
                field('analysis.columns', '分析列（每行一个；相关分析至少两列）', 'string_list'),
                field('analysis.missing', '相关分析：缺失值处理', 'enum', options=['pairwise', 'complete', 'raise'], default_value='pairwise'),
                field('analysis.method', '异常值：识别方法', 'enum', options=['iqr', 'bounds'], default_value='iqr'),
                field('analysis.handling', '异常值：处理方式', 'enum', options=['flag', 'drop', 'clip'], default_value='flag'),
                field('analysis.iqr_factor', '异常值：四分位距倍数', 'number', default_value=1.5, minimum=.01),
                field('analysis.lower', '异常值：固定下限（可留空）', 'number'),
                field('analysis.upper', '异常值：固定上限（可留空）', 'number'),
                *numeric_fields('analysis.numeric'),
            ]
            for item in definition.editor['fields']:
                path = item['path']
                if path == 'analysis.columns':
                    item['visible_when'] = {'analysis.operation': ['numeric', 'spearman', 'outliers']}
                elif path.startswith('analysis.numeric.'):
                    item['visible_when'] = {'analysis.operation': ['numeric']}
                elif path == 'analysis.missing':
                    item.update(visible_when={'analysis.operation': ['spearman']}, option_labels={
                        'pairwise': '每对变量分别删除缺失样本', 'complete': '删除任一分析列缺失的样本', 'raise': '发现缺失值即停止'})
                elif path.startswith('analysis.') and path != 'analysis.operation':
                    item['visible_when'] = {'analysis.operation': ['outliers']}
                    if path == 'analysis.iqr_factor':
                        item['visible_when']['analysis.method'] = ['iqr']
                    if path in {'analysis.lower', 'analysis.upper'}:
                        item['visible_when']['analysis.method'] = ['bounds']
                elif path in {'mapping.time_unit', 'mapping.time_column',
                              'mapping.prediction_time_column', 'mapping.available_time_column', 'mapping.label_available_time_column'}:
                    item['visible_when'] = {'analysis.operation': ['profile']}
                if path == 'analysis.method':
                    item['option_labels'] = {'iqr': '四分位距（IQR）', 'bounds': '指定上下限'}
                if path == 'analysis.handling':
                    item['option_labels'] = {'flag': '只标记，保留全部数据', 'drop': '删除异常值所在行', 'clip': '将异常值截到边界'}
        if kind == 'feature_extract':
            definition.manual_summary += (' features.preprocessing 配置训练数值缺失填补、缩放及 IQR 截断；这里只传递方案，不拟合。'
                'numeric_missing=auto 保留引擎默认：sklearn/Optuna 使用中位数，AutoGluon 使用原生处理；也可选 median、mean、constant、error。'
                'categorical_missing=preserve 在各引擎中按训练折独热编码类别，保留缺失而不填成已有类别；训练未见类别编码全零。auto 保留旧行为，sklearn/Optuna 使用训练折众数，AutoGluon 使用原生处理。'
                'scaling=auto 时 sklearn/Optuna 标准化、AutoGluon 不额外缩放；可选 none、standard、robust。'
                'outliers=iqr_clip 需要 outlier_columns，边界仅从当前训练折估计；预测复用已保存边界，不删除样本。')
            definition.editor['fields'] = [field('dataset_id', '上游数据集', 'reference_or_text'),
                field('features.columns', '使用字段（每行一个，留空使用全部）', 'string_list'),
                field('features.exclude', '排除字段（每行一个）', 'string_list'),
                field('features.numeric.columns', '需要整理为数值的字段（每行一个）', 'string_list'),
                *numeric_fields('features.numeric'),
                field('features.window_seconds', '过程窗口长度（秒）', 'number'),
                field('features.timeseries', '时序特征集', 'enum', options=['minimal', 'extended'], default_value='minimal'),
                field('features.select_k', '折内筛选特征数（留空不筛选）', 'number', minimum=1, maximum=500),
                field('features.preprocessing.numeric_missing', '训练：数值缺失值处理', 'enum', options=['auto', 'median', 'mean', 'constant', 'error'], default_value='auto',
                      option_labels={'auto': '跟随训练引擎', 'median': '训练样本中位数', 'mean': '训练样本均值', 'constant': '指定固定值', 'error': '遇到缺失即停止'}),
                field('features.preprocessing.fill_value', '训练：缺失填补固定值', 'number', default_value=0, visible_when={'features.preprocessing.numeric_missing': ['constant']}),
                field('features.preprocessing.categorical_missing', '训练：类别缺失值处理', 'enum', options=['auto', 'preserve'], default_value='auto',
                      option_labels={'auto': '跟随引擎（sklearn/Optuna 填最常见类别）', 'preserve': '保留缺失，不填成已有类别'}),
                field('features.preprocessing.scaling', '训练：数值缩放', 'enum', options=['auto', 'none', 'standard', 'robust'], default_value='auto',
                      option_labels={'auto': '跟随训练引擎', 'none': '不缩放', 'standard': '均值与标准差', 'robust': '中位数与四分位距'}),
                field('features.preprocessing.outliers', '训练：异常值处理', 'enum', options=['none', 'iqr_clip'], default_value='none',
                      option_labels={'none': '保留原值', 'iqr_clip': '按训练样本 IQR 边界截断'}),
                field('features.preprocessing.outlier_columns', '训练：截断字段（每行一个，必须为数值）', 'string_list', visible_when={'features.preprocessing.outliers': ['iqr_clip']}),
                field('features.preprocessing.iqr_factor', '训练：IQR 倍数', 'number', default_value=1.5, minimum=.01, maximum=100, visible_when={'features.preprocessing.outliers': ['iqr_clip']}),
                field('features.transformer_path', '高级特征转换代码（可选）', 'reference_or_text')]
        if kind == 'model_train':
            definition.editor['fields'] = [field('dataset_id', '上游数据集', 'reference_or_text'),
                field('features', '上游特征方案', 'reference_or_text'),
                field('evaluation.problem', '预测任务', 'enum', options=['classification', 'regression'], default_value='regression'),
                field('evaluation.metric', '比较指标', 'enum', options=['macro_f1', 'accuracy', 'roc_auc', 'mae', 'rmse', 'r2'], default_value='mae'),
                field('evaluation.split', '数据划分', 'enum', options=['random', 'group', 'time'], default_value='random'),
                field('evaluation.folds', '交叉验证折数', 'number', default_value=3, minimum=2, maximum=5),
                field('evaluation.holdout_fraction', '独立测试比例', 'number', default_value=.2, minimum=0, maximum=.4, step=.05),
                field('evaluation.gap_seconds', '时间隔离间隔（秒）', 'number', default_value=0, minimum=0),
                field('evaluation.acceptance_accuracy', '自动采纳最低验证准确率（留空不选阈值）', 'number', minimum=.01, maximum=1, step=.01),
                field('evaluation.acceptance_min_samples', '阈值选择最少验证样本数', 'number', default_value=10, minimum=1),
                field('candidate.engine', '训练引擎', 'enum', options=['sklearn', 'optuna', 'autogluon'], default_value='sklearn'),
                field('candidate.models', '候选模型（linear、forest、hist_gradient、svm，每行一个）', 'string_list', default_value=['linear', 'forest']),
                field('candidate.batch_size', '本批试验数', 'number', default_value=2, minimum=1, maximum=5),
                field('budget.seconds', '计算预算（秒）', 'number', default_value=600, minimum=10),
                field('budget.trials', '最多试验数', 'number', default_value=3, minimum=1),
                field('budget.trial_seconds', '单次试验预算（秒）', 'number', default_value=120, minimum=5),
                field('study_id', '继续已有训练记录（可选）', 'reference_or_text'),
                field('candidate_id', '继续已有候选方案（可选）', 'reference_or_text'),
                field('finalize', '只评价独立测试集', 'boolean')]
        registry.register(definition, config)
