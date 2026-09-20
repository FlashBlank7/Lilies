# 项目内数据分析与自主建模

2026-09-14 实现。范围是表格、工艺时序、传统机器学习和本机 CPU。使用现有项目、统筹对话、成员画布与运行记录；原工业工作流和运行镜像保留。

## 客户使用路径

1. 打开项目，导入 CSV、TSV、XLSX，或让统筹登记需求包内文件。导入保存数据，不自动训练。
2. 在原对话说明预测目标。统筹分析字段、缺失、标签及时间，确认预测时点、分组、指标和目标值。
3. 开始后先得到朴素参照与初步模型。Optuna 在一个特征方案内搜索参数；统筹读取真实误差，提交带父候选和改动说明的新方案。
4. 对话中的卡片持续显示最佳效果、参照、目标差距和下一步。概览、数据、特征、实验四个页签就地展开；反馈关联研究、候选和原任务。
5. 达标或预算结束时保留最佳模型。可生成独立预测工作流、下载模型及报告，继续提交无标签输入。改数据或评价规则创建关联的新研究。

当前可打开的真实应用内试用：
[平台测试 · 自主建模](http://127.0.0.1:3000/projects/a0f33411-6c1d-48b8-9902-30a1b385eecb)。
这是构造数据的平台测试，不是工业项目验收。需求沟通、方案、画布、训练和反馈后的再次预测由应用内本机 Codex 完成；外部开发代理只代理客户并核对平台行为。

## 对象和执行

| 对象 | 保存内容 |
|---|---|
| 数据集 | 只读文件副本、散列、字段映射、版本关系、抽样及完整分析 |
| 建模研究 | 数据版本、固定评价规则和样本划分、预算、最佳结果、业务事项 |
| 实验候选 | 父候选、改动理由、特征配置、模型参数、代码副本、镜像、逐次预测与指标、关联任务 |

`modeling_objects` 在平台 SQLite 中保存摘要；输入、缓存与产物在 `data/modeling/`。计算容器不挂载平台数据库、项目凭证或其他项目文件。Optuna 使用独立 SQLite 文件，在同研究、同特征与模型方向下延续参数历史。

上传文件需要交给已有文件型准备流程时，统筹在建设阶段调用 `project_modeling(action="export_dataset", dataset_id)`。平台校验当前项目的数据版本并导出独立副本到固定的 `results/datasets/{dataset_id}/`，返回 `source_path`、可选 `labels_path`、各文件字节数及 SHA-256；将返回路径交给工作流输入即可。重复调用复用内容一致的副本；副本被修改或不完整时明确冲突，不静默覆盖。原始数据集不变，需求包不变，不开放任意目标路径。此操作不运行分析或训练。

### 每个模型的训练笔记（2026-09-15）

“查看结果 → 实验 → 查看训练笔记”可打开任意一次试验，包括未被选中或失败的试验。参数相同但重新训练也属于新的试验；同一请求恢复时复用已完成记录。

- 每次试验提交结果后，平台自动保存 `training-note.md/json`。笔记来自实际训练记录，不调用模型编写指标；后续查看时补入同研究最新比较和属于该模型的最终测试结果，原始试验不变。
- 保存数据集文件散列、固定逐样本划分、特征方案与代码散列、镜像、改动理由、实际拟合参数、搜索分布、起止时间、每折指标、验证预测、任务和运行关联。分类保存预测概率，朴素参照保存逐样本预测，便于复算指标。
- 页面按试验次数或累计试验耗时展示验证指标及历次最佳；较差和失败试验保留。比较限定当前研究，最终测试不加入搜索曲线，外部 benchmark 不自动混入。
- 每次成功试验均可单独下载模型。模型 ZIP 自动包含笔记、比较 CSV、曲线 SVG 和划分；另可下载不含模型权重的笔记与原始记录 ZIP。源数据仍由项目数据集版本管理，不在每份 ZIP 重复打包。
- 中断后重试保留未提交试验的文件在 `output/interrupted-attempts/`，不覆盖已完成试验。旧镜像和旧记录仍可读；未保存的参数、搜索分布或起止时间明确显示缺口，不推测补齐。

接口：`GET .../modeling/studies/{study}/candidates/{candidate}/trials/{slot}/note` 返回结构化记录和 Markdown；追加 `/download` 下载记录包。原模型下载接口支持 `?slot=0` 指定试验，不传仍下载该候选最佳模型。所有读取沿用项目权限，`slot` 从 0 开始。统筹使用 `project_modeling(action="training_note", study_id, candidate_id, slot)` 读取同一份记录。

独立的 `scripts/benchmark_modeling.py` 也保留每次运行的完整目录，路径写在 `records_directory`，包括各模型参数、预测、分数、获胜模型及日志。历史已被清除的 benchmark 明细不能恢复；此改动不重写旧比较结果。

验证使用独立的[逐模型训练笔记测试项目](http://127.0.0.1:3000/projects/dda31422-61e4-4079-95b2-eeb789dda95b)：固定线性／森林、Optuna 森林及失败参数共四次真实容器试验，全部保留独立笔记。这是平台功能测试，没有调用 Codex 或重新训练工业项目。

四个积木均进入已有工作流运行记录，项目身份来自可信运行上下文：

| 积木 | 必要配置 | 输出 |
|---|---|---|
| 数据分析 `data_analysis` | `dataset_id` | 质量统计、分布、时序抽样、数据集标识 |
| 特征提取 `feature_extract` | `dataset_id`, `features` | 字段来源、窗口与计算方式、可传递特征方案 |
| 训练评估 `model_train` | `study_id`, `candidate_id` | 已持久化试验、指标、模型与运行关联 |
| 模型预测 `model_predict` | 无标签 `dataset_id`, `study_id`, `candidate_id` | 预测预览和可下载 CSV |

积木结果位于 `output`。`model_train` 的 `finalize=true` 在选模结束后评价保留测试集；评价前固定最佳候选，此研究随后禁止继续搜索。没有保留测试样本时仅交付验证结果。保存的画布测试可以回放已完成训练并真实执行预测，不额外改动研究。

## 精简的建模工具（2026-09-16）

`project_modeling` 默认 `view="summary"`：保留指标方向、最佳/参照、剩余预算、改动、错误、各折诊断和最差5个分组；省略的分组数明确返回。完整参数、划分、预测和训练笔记仍保存在原处，用返回的 `detail` 工具参数读取 `view="full"`。`candidates` 可用 `candidate_id` 精确读取一个候选，列表继续支持 `offset/limit`。已有HTTP详情接口语义不变。

首次搭建时，用 `block_catalog(tool_name="project_modeling")` 读取完整说明、输入契约和可直接编辑的画布示例。训练流程声明 `study_id`、`candidate_id` 两个字符串输入；唯一的 `model_train` 节点分别配置为 `{"$ref":{"node_id":"$inputs","path":["study_id"]}}` 和对应的 `candidate_id` 引用。后续复用该流程，无需每轮改节点。

```json
{"action":"submit_and_run","study_id":"当前研究","workflow_id":"当前项目训练成员",
 "candidate":{"request_key":"本轮唯一键","engine":"sklearn","models":["linear"],"batch_size":1},
 "inputs":{},"wait":true}
```

平台登记候选、绑定两个输入、校验启动时固定的画布并创建真实项目任务；不编辑画布。`inputs` 用于其余输入，不允许覆盖两个标识。返回 `project_task_id/workflow_id/study_id/candidate_id/status/error` 及 `task/candidate/study` 摘要；`wait=false` 返回已启动任务，后续通过 `workflow_run inspect` 查询。任务用途为 `build_test`，关联原业务事项及候选的 `feedback_task_id`。

同候选键、方案、工作流及输入返回原任务；改变内容需要新键。后续编辑草稿不影响旧任务，也不会让重复提交重跑。中断使用原任务的停止/继续入口。组合操作只在建设阶段开放，只支持一处输入驱动的训练节点，不接受任何成员中的 `finalize`；复杂流程仍可使用原工作流工具。候选已保存但任务尚未创建时，原请求重试接续创建，不再创建第二个候选。

固定构造数据的实际运行对照见测量JSON（本地试验记录）：本轮减少工具往返和默认返回量，未证明训练计算本身更快。模型、数据划分及训练环境没有因摘要而变化。

## 评价与特征

- 表格使用 sklearn Pipeline：训练折内缺失填补、编码、缩放、可选 SelectKBest 与模型。支持线性模型、随机森林、梯度提升、SVM 的分类和回归。
- tsfresh 默认使用 MinimalFCParameters；扩展配置增加趋势、变化、自相关和少量频域特征。每个窗口只读取预测时点前已实际可用的观测。
- 自定义特征模块必须提供 `build_transformer()`，返回 sklearn 转换器，在各训练折内拟合。候选固定该模块副本，修改工作区文件不改变已有候选；首期支持一个自包含模块，不自动收集外部模块依赖。
- 随机／分层划分、按组划分、向前时间划分均固定样本索引。同组重复记录不能随机拆开；时间戳相同的记录不跨边界。缺标签会明确计数，困难样本和缺特征样本不会因预测不好而被删去。
- 时间验证先隔离最终测试边界，再构建搜索折。`gap_seconds` 限制窗口重叠，时序候选窗口不得大于固定间隔；间隔为零时保守隔离重复主体。`label_available_time_column` 控制标签何时可用于拟合。
- 数值时间必须指定 `time_unit=s/ms/us/ns`，字符串日期默认 `iso`。时序需要独立标签／预测时点清单；没有绝对时点的资料需要先确认映射。缺失或无穷时序观测会明确报错，需要登记有业务依据的清洗版本。
- `error_group_columns` 用于设备、产线等误差拆分，独立于划分组键。重要性来自模型权重或树模型，不能解读为因果关系。
- AutoGluon 是受预算限制的 CPU 对照，当前默认启用 LightGBM、随机森林及 ExtraTrees；外层验证行不传给 fit。它支持相同原始／时序特征，不接受自定义拟合转换和 SelectKBest。该限制会明确返回，不静默忽略配置。
- AutoGluon 各验证折及开发集最终拟合均显式使用研究指定的问题类型和评价指标。2026-09-15 修复了未传指标时回归默认按 RMSE、分类默认按 accuracy 内部选模的问题；旧镜像的结果保留原口径，补做对照使用新镜像和关联研究，不覆盖已完成实验。

## 预算、停止与恢复

### 2026-09-15 小样本与实验修复

新建模镜像中，梯度提升和 AutoGluon 的默认最小叶样本数取 `max(2, min(20, 最小训练折样本数 // 5))`，仅使用训练折规模；显式指定的参数保持原值。AutoGluon 默认运行 GBM、RF、XT，可用候选的 `autogluon_hyperparameters` 指定 GBM/RF/XT/KNN/LR 的参数或配置列表，仍受单试验 CPU、内存和时间预算限制，不改变外层评价。

Optuna 默认 SVM 搜索 C、kernel，以及非线性核的 gamma；回归另搜 epsilon。梯度提升搜索包含 min_samples_leaf。候选可提供 `search_space`，按模型与实际 estimator 参数配置 float/int 的 low/high/log 或 categorical 的 choices。显式模型搜索空间替换该模型的默认空间；固定参数不参与搜索，修改空间生成新候选并使用独立参数历史。例如：

```json
{"engine":"optuna","models":["svm"],"parameters":{"kernel":"rbf"},"search_space":{"svm":{"C":{"type":"float","low":0.01,"high":100,"log":true},"gamma":{"type":"float","low":0.0001,"high":1,"log":true},"epsilon":{"type":"float","low":0.001,"high":0.5,"log":true}}}}
```

每次成功试验保存逐折样本量、预测不同值数量及回归标准差；对不同实测值给出相同预测时，在卡片、实验列表与训练笔记提示检查。该试验及分数仍保留，不自动否定算法。旧镜像的笔记不补造这些检查。切换研究时，界面丢弃上一个研究延迟返回的候选列表，避免将旧实验混入当前比较。

主动结束本轮搜索使用 `project_modeling(action="finish", study_id, reason)` 或 `POST .../studies/{study}/finish`。它停止计时并保留模型，**不运行留出评价**；仍在计算时需先等待或停止关联任务。后续可调整预算继续，已封存研究不重新开放。

全失败批次计入试验和时间预算，但不增加效果无改善次数。研究保存 `repair_candidate_id`，统筹读取实际错误后提交关联的修复候选；页面提供“修复并继续研究”。连续三批同一错误只中断该研究，修复成功后清除失败计数。原候选、原模型、固定评价和任务记录保留。混合批次包含成功试验时仍按实际效果计算无改善次数。

上述参数默认和预测检查由新镜像生效；已有研究继续使用固定旧镜像。补做和改进使用关联的新研究，已看过的留出数据不能重新作为未见测试结果。

默认总计 1,800 秒、30 次试验、每批最多 5 次、单次 300 秒，连续 3 批无改善结束。一次试验包含固定交叉验证各折和开发集重拟合，不是一次 estimator.fit。失败试验计数，完成结果即时保存，较差或失败候选不覆盖最佳模型。普通参数试验不逐次调用 Codex。

研究计时覆盖登记后分析／划分、等待资源、训练及统筹批间等待；已完成数据分析的计算耗时计入初始用量。登记研究前的需求沟通、人工等待、镜像下载和研究结束后的交付操作不计入搜索时钟，应单独报告。预算可通过对话修改。

每个平台实例默认一个活动建模计算任务，容器限制 4 CPU、4 GB 内存、无网络。单次训练用独立计算进程，超时直接终止；停止项目任务会终止计算容器。每次试验原子保存模型及结果后再发完成事件。恢复复用已完成试验，只重做没有完成提交的试验；不恢复算法内部迭代步。

服务重启标记中断并清理本数据目录所属的遗留计算容器，不自动续跑。用户继续后使用原数据、代码、镜像及固定划分。修改特征生成新候选；修改数据或评价规则生成新研究。反馈后的业务试用仍使用现有项目固定任务快照机制。

## 环境和模型交付

```sh
docker build -f Dockerfile.modeling -t lilies-modeling:20260914 .
```

`requirements-modeling.txt` 记录直接依赖，`requirements-modeling.lock` 锁定经 ARM64 安装验证的完整依赖。建模环境使用 pandas 2.3.3，避免和原工业镜像 pandas 3 的 AutoGluon 依赖冲突。`MODELING_IMAGE` 可指定标签；创建研究会解析成不可变镜像 ID，保存实际 worker、Python 版本、架构与依赖清单。已有研究所用镜像需要保留。

本机测试从已有 Python 3.12 工业镜像创建独立建模镜像；旧镜像未改变。Docker Hub 基础镜像解析曾停滞，本机完成构建的命令为：

```sh
COPYFILE_DISABLE=1 tar --no-xattrs -cf - Dockerfile.modeling requirements-modeling.txt requirements-modeling.lock platform/backend/src/agent_platform/modeling_worker.py | DOCKER_BUILDKIT=0 docker build --build-arg MODELING_BASE=lilies-modeling-base:local -f Dockerfile.modeling -t lilies-modeling:20260914 -
```

下载包包含已拟合模型、预处理、特征代码、预测示例配置、冻结的 worker 和依赖环境、指标及验证预测。README 提供使用原镜像或独立 Python 环境运行的命令。包内不复制原始训练资料。跨操作系统安装能力未作为本次验证结论。

## 接口

项目下新增：

- `/api/v1/projects/{id}/datasets`：登记、列表；`/upload` 上传，`/{dataset}/profile` 分析，`/{dataset}/files/{path}` 下载产物。
- `/api/v1/projects/{id}/modeling/studies`：创建、列表与详情；`/{study}/budget` 修改预算；`/{study}/candidates` 提交及查询；`/{study}/candidates/{candidate}/download` 下载可复用模型。
- 列表支持 `offset`、`limit`、`after` 和 `summary=true`。对话默认读摘要，阅读窗口再取详细记录。
- 统一对话可附带 `dataset_id`、`study_id`、`candidate_id`。平台检查全部关联属于当前项目。
- `project_modeling` 工具负责查询资料、字段版本、创建研究和候选；训练、特征计算、最终评价及预测由实际工作流执行。完整参数可从现有工具说明接口查询。

继续使用本机 Codex，`MODEL_EGRESS_ENABLED=false`。没有增加常驻 Agent、远程计算、审批体系或平台自行改代码的执行器。

## 验证边界

自动测试覆盖训练、预测、数据隔离和停止恢复。客户实际指标与训练笔记保留在本地项目，未纳入公开仓库。AIDE 目前是可选搜索策略适配，并非完整上游智能体。
