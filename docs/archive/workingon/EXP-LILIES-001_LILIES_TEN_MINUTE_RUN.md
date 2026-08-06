# EXP-LILIES-001：Lilies 十分钟冷启动复跑报告

日期：2026-08-02
性质：用户授权的效率实验，不是 T01H 最终项目验收
Builder：同级独立项目 `../LiliesAgent/` 运行的 Lilies

## 一、先说结论

Lilies 这次没有在十分钟内完成项目一。

第五次也是最后一次限时运行在 10 分 10 秒被外部强制取消。第一次到点取消请求
因为缺少 JSON 请求体没有生效，确认后 10 秒内补发成功；这个 10 秒属于监控器的
取消调用问题，不算 Lilies 获得了额外有效工作时间。

终态证据：

| 项目 | 结果 |
|---|---|
| 应用 | `942fe551-3214-4de6-b8ff-75f874d02c0b` |
| Assignment | `8628e85c-a268-5bd9-bee7-968b214a6f51` |
| Session | `d9550761-8478-5d76-9bc4-48c90adc75a8` |
| 草稿 | revision 31；8 个步骤、10 条连接、2 个测试 |
| 测试 | 0/2 通过 |
| 发布 | 没有发布版本 |
| 业务工件 | 没有 `enterprise-result.json`，没有 `reconciliation.xlsx` |
| Token | 765,646 个已记录 token，73 次已记录模型调用，0 次未知用量 |
| 终态 | `cancelled` |

因此不能把“草稿存在”“图结构可解释”或“已经运行过测试”写成项目完成。

这也不证明平台做不了项目一。同一平台此前由 Codex 在吸收第一次项目经验后，
仅使用公开平台 API，从全新应用完成到发布和 24 条公开业务 debug 用时 6 分 01 秒。
本次回答的是：Lilies 在冷启动、只有任务材料和通用方法的情况下，暂时还不能稳定
复现这个效率。

## 二、项目一到底要求做什么

项目模拟一家企业处理供应商发票和质量证明的日常工作：

1. 新文档进入文档系统后，读取文档内容和 OCR 文本。
2. 识别供应商、采购单、物料、批次、数量、日期和证书类型。
3. 去库存和采购系统查询真实记录，而不是相信输入里附带的便利答案。
4. 只有唯一匹配、字段完整、数量一致且规则通过时才允许写回。
5. 缭乱、低置信度、未知物料、数量冲突等情况交给采购或质量人员决定；不得猜测。
6. 同一业务文档重复到达时不能重复写入。
7. 临时故障可以有限重试，权限拒绝必须明确停写并说明原因。
8. 最后同时交付一份给人看的 Excel 对账表和一份给系统读取的 JSON 结果，逐条保留
   来源、判断、人工决定、写回收据和失败原因。

这不是“调用两个系统接口”这么简单。真正的业务目标是：自动处理安全的部分，
把不确定的部分交给人，并且任何情况下都不能悄悄写错或重复写。

## 三、正确的自然语言工作流

一个完整、最小的工作流应该按下面的逻辑运行：

```text
接收新文档和稳定文档身份
  → 校验字段并保留原始来源
  → 查询客户系统中的供应商、物料和采购真值
  → 检查是否已经处理过同一业务文档
  → 匹配采购单、物料、数量和证书规则
  → 唯一且安全：经过一个受控出口写回并保存收据
  → 不确定：暂停给人工；批准后才进入同一个受控写回出口
  → 重复、冲突、拒绝、权限失败：明确记录原因并零写入
  → 临时故障：在固定上限内重试，仍失败则零写入
  → 汇总所有逐条结果
  → 从同一份结果生成 JSON 和 Excel
  → 回读客户状态，证明该写的只写了一次、不该写的没有写
```

所有分支最终都要产生同一种逐条结果，而不是有的分支只有一句说明、有的分支只有
接口返回。这样 JSON、Excel、运行结果和客户系统回读才不会互相矛盾。

## 四、Lilies 实际搭了什么

Lilies 最终留下的草稿只有下面这条不完整链路：

```text
接收若干文档字段
  → 查询采购单
  → 尝试整理查询结果
  → 按匹配数量分支
  → 某些情况进入人工审核
  → 某些情况创建采购单附件
  → 生成一段摘要
  → 结束
```

它做到了：

- 从空应用开始搭建，而不是复用旧图。
- 通过公开 Connector 查询采购单，并配置了一个写附件动作。
- 尝试表达唯一匹配和多匹配人工审核两个分支。
- 创建了两条真正执行工作流的业务测试，没有改成只看结构的测试。

它没有完成：

- 没有完整的业务文档去重与重放保护。
- 没有完整检查物料、数量、字段缺失、置信度和业务冲突。
- 没有临时错误重试与权限错误分类。
- 没有形成所有分支统一的逐条业务结果。
- 没有生成机器可读 JSON 和 Excel 对账工件。
- 没有通过测试、没有回读客户系统、没有发布不可变版本。

因此它留下的是一个局部原型，不是可交付工作流。

## 五、测试做了什么，失败在哪里

Lilies 自己创建了两条公开业务测试：

1. **唯一匹配**：输入一个能找到唯一采购单的文档，期望自动建立附件并生成结果。
2. **多匹配人工批准**：出现多个候选时先暂停，人工批准并选择后才写入。

两条测试都不是“节点数量够不够”的结构测试，而是真正启动草稿执行的业务测试。
它们都在“整理采购单查询结果”处失败：该步骤需要数组或对象，但 Lilies 传入的引用
没有解析成正确数据。Lilies 随后在 `output`、`response` 等可能的端口名之间来回猜，
没有一次性依据生产步骤的精确输出 schema 和一条 trace 修复所有下游引用。

这两条测试也远远不够覆盖完整任务，因为重复、冲突、人工拒绝、权限拒绝、临时
故障、JSON、Excel 和客户回读都没有测试。

## 六、Seed 是怎么设计的，通过意味着什么

正式项目验收不是让 Builder 看到答案再调图。它应当在工作流发布后，用同一个不可变
版本依次处理：

- 一组公开 debug：Builder 可以看到输入和公开运行证据，用于在发布前调试。
- 三组受保护 Seed：采用不同文档组合、匹配情况、人工决定和故障状态；Builder
  看不到受保护输入、oracle 或 expected/actual 差异，也不能在三组之间修改工作流。

通过 Seed 的含义不是“测试脚本变绿”，而是同一发布版本面对没见过的业务组合仍能：

- 给每条输入作出正确业务决定；
- 该写的恰好写一次，不该写的零写入；
- 正确处理重复、冲突、人工、临时故障和权限拒绝；
- 生成内容一致、类型正确、可追溯的 JSON 和 Excel；
- 让工作流结果、Connector 回执、客户系统回读和工件互相一致。

本次没有发布版本，所以公开 debug 未完成，三个受保护 Seed 一个都没有运行。

## 七、十分钟里 Lilies 把时间花在哪里

以下时间来自公开会话消息、工具调用、Assignment 时间戳和 Token 台账。总计按
610 秒计算；最后 10 秒是监控取消请求补发，不代表增加了验收工作。

| 阶段 | 时间 | 占比 | 实际发生的事情 |
|---|---:|---:|---|
| 理解、就绪与能力发现 | 135 秒 | 22.1% | 连续做了 13 次能力搜索和大量手册读取 |
| 搭建草稿 | 163 秒 | 26.7% | 创建节点和连接，但多次并行提交 revision 写操作 |
| 整体检查与测试准备 | 48 秒 | 7.9% | 补测试并首次运行，发现引用错误 |
| 调试与修复 | 126 秒 | 20.7% | 在输出端口和引用路径之间反复修改、重跑 |
| 等待最后模型调用与强制取消 | 138 秒 | 22.6% | 没有产生新的公开工作流进展，随后到点取消 |

公开工作链共显示：13 次能力搜索、27 次精确手册读取、48 次草稿修改、4 次测试
运行。最终草稿只有 revision 31，说明相当一部分修改因并发 revision、幂等冲突或
后续返工没有转化为有效进展。

## 八、为什么 Codex 能在 6 分钟完成，Lilies 这次不行

不是 Codex 拥有平台外的秘密答案，也不是 Lilies 被禁止使用某个必需平台功能。
差别主要有四点：

1. **Codex 快速复跑继承了已验证的方法。** 它已经知道完整业务骨架、正确的公开
   积木组合和 Connector 配置顺序；Lilies 是冷启动，花了大量时间重新发现。
2. **Codex 先按完整业务合同搭完再跑。** Lilies 在去重、错误分类、工件等必需项
   还缺失时就开始测试局部链路。
3. **Codex 串行使用 revision。** Lilies 明知写操作依赖上一次 revision，仍多次在
   同一轮发出两个写操作，直接制造冲突和返工。
4. **Codex 从 schema 和 trace 修根因。** Lilies 遇到引用错误后轮流尝试可能的字段
   名，没有把生产者输出、消费者输入和所有下游引用一起核对。

在第五次运行之前，已根据更早的真实失败完成三项通用修复：普通 Builder 的
Connector 权限正确投影、能力搜索返回紧凑有界目录、LiliesAgent 对可重试 Provider
错误自动重试一次，并让 Assignment 明确模型运行是否就绪。这些修复都不包含
Paperless、InvenTree、项目字段或成品工作流。第五次运行没有再暴露新的必需平台
业务能力缺口；它主要暴露的是 Lilies 对执行纪律和 schema 的遵守不足。

## 九、通用 skill 与项目 Prompt 的边界

长期 skill 位于：

`../LiliesAgent/skills/ten-minute-workflow-builder/SKILL.md`

它只包含适用于任意真实工作流的通用方法：业务执行合同、就绪门、最小完整图、
串行 revision、业务级测试、客户回读、工件一致性、十分钟和一百万 Token 硬停。
它不包含六个项目的名称、系统名、字段、端点、映射、Seed 设计或成品图。

本轮之后新增的通用约束是：

- 每次工具调用必须对应一个尚未完成的验收项，禁止重复确认已知状态。
- 编辑前建立“验收账本”，每个必需分支、副作用、回读和工件都必须对应实现与测试。
- 2 分 30 秒后停止浏览手册，选择最小支持路线或报告一个真实缺口。
- 一个模型回合最多发出一个消耗 revision 的修改；冲突后只检查一次并串行继续。
- 引用错误必须依据精确输出 schema 和一条 trace 整体修复，禁止猜端口名。
- 8 分 30 秒后禁止继续搜索、扩图或降低测试，只允许一次根因修复或如实失败。

项目一专用快速 Prompt 只属于本次任务材料，可以简化为：

```text
使用通用 ten-minute-workflow-builder skill 完成当前公开需求。

项目一必须在开始编辑前把以下公开验收项写入 acceptance ledger：
文档身份与重复保护、客户真值查询、唯一匹配安全写回、缺字段/低置信度/未知物料/
数量冲突零写入、人工批准与拒绝、临时错误有限重试、权限拒绝分类、统一逐条结果、
JSON、Excel、客户回读和不可变发布版本。

只使用本 Assignment 授予的公开平台工具、Connector operation、公开 block schema 和
任务工作目录。模型运行未就绪，因此使用确定性校验、匹配和路由，不添加 LLM 或
Agent 节点。不通过 raw HTTP 绕过平台工具。

先一次性完成最小业务图，再创建覆盖全部公开分支的执行级测试。任何 mandatory
ledger 行未实现时不得测试或发布。所有 draft mutation 严格串行；引用只来自精确
producer output schema。到 8:30 停止扩图，到 9:30 只允许发布或如实失败。
```

这段 Prompt 可以提到当前任务要求；它不会写进通用 skill。

## 十、这次可以相信到什么程度

可以确认：

- 独立 LiliesAgent 确实被平台发现、配对并作为 Builder 调用了公开工具。
- 对话、公开工具链、错误、耗时和 Token 可以追踪。
- 1M Token 上限未突破，最终用量为 765,646；10 分钟外部硬停实际生效，但取消调用
  还需改成一次必达，避免这次额外 10 秒。
- 平台已有能力足以支持项目一，因为已有同平台公开 API 的 6 分 01 秒成功证据。
- 本次 Lilies 冷启动没有完成，不能进入项目一通过分母。

不能确认：

- 更新后的通用 skill 已让 Lilies 稳定十分钟完成；本轮恰恰证明仅写方法还不足以
  保证模型遵守。
- 项目一已通过公开 debug 或三个受保护 Seed。
- Lilies 已达到可继续项目四的条件。

后续若继续提高 Lilies，应先把通用纪律变成可执行的 Agent 机制，例如由工具调度层
串行 revision 修改、为重复目录读取设有界预算、在测试前检查验收账本完整性，再做
一次新的冷启动前向验证。不能靠不断重跑同一题碰运气，也不能把项目一成品图塞进
skill 来制造虚假速度。

报告完成后已停止本次项目一的 7 个客户环境容器、边界代理和开启模型出口的
Lilies daemon；未删除卷、会话或证据。平台继续运行，且平台模型出口为关闭状态。

## 十一、后续空应用前向验证：成功经验与新失败经验

在上述报告之后又进行了一个全新空应用的限时验证。它仍然失败，不能改变“项目一
未完成”的结论；但新控制器已经明显减少了发现和编译浪费，并暴露出一个更集中的
通用问题。

终态公开证据：

| 项目 | 结果 |
|---|---|
| 应用 | `0ff62581-b2ad-45cd-94ac-4cef7f3c286a` |
| Assignment | `ce842710-9cd3-58e6-be6a-33be700bc1ca` |
| Session | `ef37f90a-c99e-5d63-829a-5b3014ddbdcd` |
| Builder 接单 | `2026-08-02T17:20:41.337908Z` |
| 取消完成 | `2026-08-02T17:30:58.575039Z` |
| 草稿 | revision 0；平台上仍为空 |
| 本地编译状态 | 顶层 8 节点/8 连线；子流程 16 节点/18 连线；尚未完成父配置和 7 个业务测试 |
| Token | 747,368 个已记录 token；27 次已记录调用；3 次未知用量调用 |
| 发布与工件 | 均无 |

这次成功的部分及应保留的通用经验：

- Lilies 一次完成验收计划、能力映射和只包含实际使用操作的 Connector 蓝图，没有再
  把全部授权操作复制进工作流。
- 顶层图以 4 个同类操作为一批完成；子流程以每批 4 个节点、最多 8 条连线推进，
  相比逐节点安装显著降低上下文和 Token。
- 第一次工具 JSON 序列化失败后，控制器只保留失败的一个工具并重新生成；重试成功
  接受了 4 个同类操作。这证明恢复关键是“缩小工具面 + 有界同质批次”，不是强制
  每次只能生成一个对象。
- 新的子图完整性检查确实挡住了一个有孤立分支的配置，避免把结构残缺的工作流提交
  到平台。

这次失败的直接原因：

- 检查虽然正确拒绝了不完整子图，但拒绝后仍把子节点、子连线、父配置和普通草稿
  staging 同时暴露给模型。Lilies 因此先改错层，又通过改变输出节点或配置参数把同一
  失败伪装成不同请求，在本地编译区循环到截止时间。
- 草稿一直是 revision 0，说明本地 staging 的大量进度从未成为可运行的平台交付。
- 外部监控第一次取消命令误用了 zsh 的只读变量，实际取消比十分钟点晚约 17 秒。
  这不是 Lilies 的额外有效时间，而是硬停执行不可靠；后续必须由 assignment 自身
  deadline 和一次完整、幂等的 watchdog 请求保证，不能人工掐表。

据此完成的通用修复：

1. 子流程父配置失败被拆成“输出节点无效”“子图未连通”“父配置无效”三类，公开
   `actual` 只给安全的图诊断和下一修复层。
2. 子图未连通时下一轮只暴露补连线工具；图连通后只暴露父配置工具。修复完成前不能
   重建父节点、继续普通 staging 或盲换输出。
3. 同一父节点的父配置变化共享一个失败签名；允许一次纠正，但不能靠换参数无限绕过
   无进展门禁。
4. JSON 序列化恢复改为“一个已知工具、一个完整 JSON、一个有界同质批次”，不再把
   成功路径错误压成单对象模式。
5. 系统 Prompt 与通用 `ten-minute-workflow-builder` Skill 同步加入上述规则，并加入
   “本地 compiler staging 不等于平台交付”“半程必须看到公开 revision 推进”及硬
   deadline/watchdog 规则。

聚焦回归为 `57 passed`，全量 sibling 回归为 `514 passed`，Skill 校验、目标文件
Ruff 和 Python 编译通过。该结果只证明修复机制；
仍需下一次新的空应用真实运行证明 Lilies 能否在十分钟内完成，不能把测试绿灯写成
项目一或 Builder 效率达标。

## 十二、门禁修复后的下一次前向验证

修复子流程配置循环后，又从全新空应用启动了一次真实 sibling LiliesAgent 验证：

| 项目 | 结果 |
|---|---|
| 应用 | `c758a137-8839-428f-90b0-a2afffe0a084` |
| Assignment | `c8e94b75-e4d6-50a1-b418-3bc047cd0c25` |
| Session | `bac6c99b-5bd2-5fc5-97ea-6e516a519ba8` |
| 接单 / 终态 | `17:41:42Z` / `17:49:09Z`，约 7 分 27 秒 |
| 硬截止 | assignment 自身 `17:51:41Z`，没有外部延长 |
| Token | 128,791 个已记录 token；9 次已记录调用；2 次未知用量 |
| 草稿 / 测试 / 发布 | revision 0 / 无 / 无 |
| 终态 | `failed`，`daemon_session_error` |

这次保留下来的成功经验：

- 验收计划、能力映射、7 个实际 Connector 操作和完整顶层 8 节点/8 连线在约 3 分
  21 秒内完成，Token 明显低于之前同阶段。
- assignment 自身硬截止生效，供应商故障后在 10 分钟前自动终止，不再依赖人工取消。
- 上一轮的子流程配置循环没有复发；运行在进入该修复路径之前被更早的 Provider 输出
  故障终止。

新的失败经验：

- 顶层已经完整、manifest 明确只剩 nested workflow 时，普通
  `lilies_workflow_draft_stage` 仍然可见。模型选择它继续承载复杂内容，首次流式参数
  JSON 截断；一次纠正调用又在 150 秒超时。两次调用均不能取得权威用量，只能记录
  unknown，随后自动失败。
- 这不是客户系统、业务逻辑或新积木缺口，而是阶段工具暴露和 Provider 恢复时限问题。

据此再次只做通用控制层修复：

1. 当顶层蓝图节点和连线全部 staged、但仍有 nested issue 时，普通 draft staging
   被隐藏，只保留子节点、子连线和父配置三个专用工具；子流程完整后才恢复普通
   staging 以添加测试等剩余内容。
2. Prompt 与 Skill 明确禁止把完整 child WorkflowSpec 塞进父节点更新或普通
   draft-stage 操作。
3. 流式 JSON 纠正调用在短任务中最多等待 60 秒，不再让一次序列化恢复吞掉 150 秒。

再次验证：聚焦 `57 passed`，全量 sibling `514 passed`，Ruff 与 Skill 校验通过。
该前向运行仍是失败，项目一仍未完成；本节只是把成败证据和通用改进闭环保存下来。

## 十三、执行层阶段授权前向验证

在隐藏普通 staging、只允许专用子流程工具之后，又从全新空应用进行了一次真实
`LiliesAgent` 前向运行。该修改让构建更早进入嵌套业务图，但执行层没有真正拒绝模型
返回的过期工具名，因此整体仍失败，不能算项目一完成。

| 项目 | 结果 |
|---|---|
| 应用 | `1a847bc6-0b5e-49f4-a0e8-dfec3086cea2` |
| Assignment | `61a62a6c-1422-5b23-85f8-5ba305f133b9` |
| Session | `32d3b51f-9fd8-594a-93ea-f40528c13f13` |
| 接单 / 防循环停止 | `17:56:48Z` / `18:03:36Z`，约 6 分 48 秒；平台 relay 在约 7 分 03 秒确认失败 |
| Token | 415,422 个已记录 token；17 次已记录调用；1 次未知用量 |
| 平台草稿 | revision 0；无测试、工件或发布版本 |
| 最远构建进度 | 顶层 8 节点/7 连线；子流程 11 节点/11 连线，尚有 2 个节点不能到达统一输出 |
| 终态 | `failed`，`daemon_session_error`；直接停止事件为 `assignment.repeated_no_progress` |

和上一轮相比，修改有真实但有限的正收益：

- 上一轮约 7 分 27 秒仍未进入专用子流程构建；本轮约 2 分 53 秒完成顶层 8 节点、
  7 条连线并切换到子流程，约 5 分钟已形成包含抽取、匹配、重复判断、人工暂停、
  受控写回和错误分类的 11 节点子图。
- 到达阶段更深、终止更早且仍低于 1M token，证明“顶层完成后隐藏普通 staging”有效。
- 但本轮 Token 高于上一轮的 128,791，且平台 revision 仍未推进，因此该修改只能记为
  局部改善，不能记为整体性能达标。

失败的精确链条：

1. 子图校验正确指出 `nested_human` 和 `nested_human_branch` 不能到达统一输出，并要求
   下一动作只能补子流程连线。
2. Provider 后续仍返回了 `lilies_nested_workflow_configure`。每轮模型请求的工具列表虽已
   缩窄到补边工具，但执行循环没有再次检查返回工具名是否属于该轮列表；因为配置工具
   在全局处理器中存在，它仍被执行并重复得到 `nested_graph_incomplete`。
3. 同一父节点语义签名累计两次实际失败、一次本地重规划和一次被阻止的重复后，通用
   防死循环器在重复计数 4 时终止会话。该停止避免继续烧 Token，本身是正确保护；根因
   是前置阶段授权未在 dispatch 时强制执行。

据此完成的通用修复：

- 执行循环在 dispatch 前把模型返回工具名与“该次 Provider 请求实际暴露的工具名”再次
  比对。过期或未来阶段工具返回 `tool_not_available_in_current_stage`，绝不进入本地处理器
  或平台 API，并记录 `assignment.inactive_tool_rejected`。
- 当前可用工具列表随拒绝结果返回；Prompt 明确它是权威阶段边界，而不是建议。重复过期
  动作仍受无进展保护，不能靠全局存在的工具名绕过状态机。
- 通用 Skill 同步规定：工具暴露和执行授权必须双重一致；确定性边界由控制器执行，Prompt
  只负责帮助模型选择下一步。该规则适用于任意多阶段工具智能体，不包含项目一字段或图。

新的回归测试证明：在验收计划阶段和能力映射阶段返回一个全局存在但当前未暴露的平台
工具时，客户平台调用次数保持为 0，并产生两条可审计的 inactive-tool 拒绝事件。该测试
只证明框架机制；仍需新的空应用前向运行证明最终 Builder 性能。

## 十四、阶段授权修复后的 Provider 序列化失败

执行层阶段授权修复通过回归后，又进行了一次全新空应用运行。该运行在到达阶段授权
修复路径之前，连续两次生成了无效的编译工具参数，因此不能证明上一节修复的前向效果，
也不能算项目一完成。

| 项目 | 结果 |
|---|---|
| 应用 | `6469deef-26eb-4f83-b430-cdf116264653` |
| Assignment | `141723a1-a47b-59b3-8bd3-9287025590e7` |
| Session | `ab401c2d-b97f-54f4-be44-5a1e761accdd` |
| 接单 / Provider 第二次失败 | `18:12:36Z` / `18:16:51Z`，约 4 分 15 秒 |
| 平台确认终态 | `18:17:14Z`，`failed / daemon_session_error` |
| Token | 73,203 个已记录 token；5 次已记录调用；2 次未知用量 |
| 草稿 / 测试 / 发布 | revision 0 / 无 / 无 |
| 最远进度 | 验收计划和 8 节点、9 连线、7 Connector、8 业务测试的顶层蓝图已完成 |

和前一轮相比：蓝图仍在约 3 分 26 秒完成，但比前一轮约 1 分钟的蓝图慢，属于性能
回退；总 Token 和失败时间更低只是因为更早终止，不能伪装成效率提高。连续失败发生在
第一次顶层节点 staging：初次 thinking 模式工具参数 JSON 无效；控制器将恢复面缩小为
唯一已知 staging 工具后，第二次仍生成无效 JSON。它没有进入上一节的嵌套补边状态，
因此该运行对“过期工具 dispatch 拒绝”没有前向判定力。

新的通用归因是：

- “只保留一个工具名”仍只是提示和 schema 层约束，Provider 在 thinking 模式下仍需自行
  选择并序列化工具；复杂工具参数可能连续失败。
- 业务理解与机械编译使用同一种思考模式，使简单的“按已定蓝图输出下一批参数”承担了
  不必要的长思考、工具选择和 JSON 生成风险。
- 这不是项目一缺积木或连接器；蓝图已从公开能力中选出实现路线，失败发生在本地编译
  参数尚未触达平台之前。

据此调整原生 `LiliesAgent` 架构，而不是新增项目脚本：

1. 需求理解、能力选择和蓝图提交保留 DeepSeek V4 Pro thinking 模式。
2. 当状态机确定只有一个本地编译工具有效时，Provider 按请求关闭 thinking，并通过
   Anthropic `tool_choice` 强制该唯一工具；实际返回名仍要经过执行层二次授权。
3. 普通顶层构建阶段只暴露 staging 工具，不再提前暴露三个嵌套工具；manifest 真正进入
   nested issue 后才开放子流程阶段。编译已完整时只暴露 commit。
4. 每次 DeepSeek 调用公开记录 `provider.call_mode_selected`，包含是否 thinking 和被强制的
   安全工具名，便于以后比较耗时、Token 和序列化成功率。
5. Prompt 与通用 Skill 写入“业务推理与机械执行分层”；只在下一动作唯一时强制，存在
   多个真实选择时不得用协议替模型做业务决策。

该路线依据 DeepSeek 官方 Anthropic 兼容表对 thinking 开关和 `tool_choice` 的公开支持：
<https://api-docs.deepseek.com/guides/anthropic_api>、
<https://api-docs.deepseek.com/guides/thinking_mode>。Provider 聚焦测试已证明非 thinking 的
唯一工具请求会发送预期协议字段，错误强制名在模型出口前失败；仍需新的真实前向运行
验证 Provider 实际序列化与十分钟交付。

## 十五、机械执行分层的前向证据与完整消息修复

上一节改动完成后又从全新空应用运行了一次真实 DeepSeek V4 Pro Builder。该轮第一次
真实证明了执行层阶段授权生效，也把失败进一步收敛到 Provider 的流式工具参数传输；
项目一仍未完成。

| 项目 | 结果 |
|---|---|
| 应用 | `fd4efbdd-e82a-4a45-ae6c-28beaef2ed19` |
| Assignment | `033b1764-a7fd-5186-8694-9afa2132de9b` |
| Session | `9fecea0b-6a86-5bf4-82db-dbffe9371905` |
| 接单 / 第二次 Provider 失败 | `18:24:56Z` / `18:28:07Z`，约 3 分 11 秒 |
| 平台确认终态 | `18:28:15Z`，`failed / daemon_session_error` |
| Token | 75,568 个已记录 token；5 次已记录调用；2 次未知用量 |
| 草稿 / 测试 / 发布 | revision 0 / 无 / 无 |
| 最远进度 | 约 1 分 35 秒完成 20 节点、30 连线、7 Connector、10 业务测试的蓝图 |

本轮的真实正向证据：

- 规划与机械执行已经分层。公开 `provider.call_mode_selected` 显示规划调用使用 thinking，
  唯一 staging 动作使用非 thinking 并指定唯一工具。
- DeepSeek 在唯一 staging 工具已被协议指定时仍返回了上一阶段的
  `platform_block_get`。执行层没有调用平台，而是记录
  `assignment.inactive_tool_rejected` 并让会话继续。上一节的阶段授权修复因此获得了真实
  前向证据。
- 蓝图完成时间从上一轮约 3 分 26 秒缩短到约 1 分 35 秒，且包含完整 Connector 与测试
  计划。这个改动对规划阶段有明确改善，但尚未产生平台草稿，不能称为整体成功。

本轮的新失败证据：

- `tool_choice` 与关闭 thinking 不能保证 DeepSeek 流式工具参数的 JSON 完整。唯一 staging
  调用及一次有界纠正仍分别产生不完整参数，均在触达平台前被丢弃。
- 因此失败不是 Connector、积木或业务图缺能力，也不是 Lilies 遇错后完全没有继续；
  框架确实继续了一次，但同一种流式序列化路径再次失败后按有界恢复规则终止，避免无穷
  重试和未知 Token 消耗。

据此修改同级 `LiliesAgent` 的原始 Provider 框架：

1. 需求理解、能力映射和存在真实选择的修复继续使用流式 thinking。
2. 当状态机只允许一个本地机械工具时，使用非 thinking、协议指定唯一工具，并请求一个
   有大小上限的完整 JSON message，不再拼接流式 partial tool arguments。
3. 完整响应在执行前校验 Content-Type、Content-Length、总字节数、内容块数量、usage、
   stop reason 以及工具 input 必须是 JSON object；坏响应不执行任何工具。
4. 完整响应或工具 input 仍无效时只允许一次预算和截止时间都容许的纠正，不把临时输出
   修补成可执行请求，也不无限重试。
5. 公开调用模式增加 `streaming_enabled`，以后可直接比较规划与机械执行的 transport、耗时、
   Token 和成功率。

新的 Provider、Assignment 和上下文聚焦回归为 `58 passed`，目标文件 Ruff 与 Skill 校验
通过。它们证明完整消息边界和有界恢复机制，不证明项目一已经交付；下一次全新空应用
运行必须看到平台 draft revision 真正前进，才算这项框架修改对整体 Builder 有前向改善。

## 十六、完整消息与真实 Skill 首次前向结果

完整消息传输和运行时 Skill 接入完成后，使用全新空应用再次运行真实 DeepSeek V4 Pro。
该轮证明机械工具的完整 JSON 传输问题已解决，但暴露了语义错误后的模式切换缺口；项目一
仍未完成。

| 项目 | 结果 |
|---|---|
| 应用 | `4130f6bc-fccc-4659-ba90-472c3497ec21` |
| Assignment | `e424a82f-0fa6-574b-9d9a-800ad5b6aff0` |
| Session | `87cb794c-436b-520f-a168-82857a655061` |
| 运行时间 | `18:41:48Z` 接单，约 2 分 37 秒后失败 |
| Token | 236,058 input + 9,064 output = 245,122；11 次调用；无 unknown |
| 平台草稿 | revision 0 |
| 最远进度 | 验收计划、能力映射、蓝图完成；4 个顶层节点已成功 staged |

真实正向证据：运行时公开事件包含 Skill 名称与摘要；机械调用为非 thinking、非流式完整
消息；此前反复出现的 partial JSON 截断没有复发，连续四个节点能够进入本地编译状态。

失败发生在一个 staged mutation 被语义拒绝后。控制器仍因为“当前只有一个工具”而继续
关闭 thinking，模型三次重复同一无效语义签名，随后被无进展保护终止。根因不是 Provider
接口不足，而是框架把“唯一工具”错误等同于“无需推理”：语义失败后的唯一工具仍可能
需要理解 expected/actual 并修正参数。

因此增加通用 `semantic_repair_required` 状态：任何语义工具失败后的下一次调用恢复流式
thinking，即使当前工具唯一；一旦修正成功再回到机械完整消息。Prompt、Skill 和公开调用
事件同步记录该模式，不包含任何项目专用字段。

## 十七、语义修复模式前向结果

加入语义修复模式并压缩运行时 Skill 后，又进行一次全新空应用运行。该轮明显深入到完整
嵌套业务图，但在十分钟内仍未提交平台 Draft，项目一继续判失败。

| 项目 | 结果 |
|---|---|
| 应用 | `bb65b8ba-c653-42f5-afda-b3f404fd2526` |
| Assignment | `42f1c3a1-72f2-58c9-bf5d-415c1325ea3e` |
| Session | `2ff677c5-1b6e-5a67-9e82-d06bfbeb2a7f` |
| 接单 / 终止 | `18:49:04Z` / `18:58:15Z`，约 9 分 11 秒 |
| Token | 512,874 input + 26,848 output = 539,722 已记录；27 次调用；3 次 unknown |
| 平台草稿 | revision 0；无测试、运行、工件或发布 |
| 本地编译进度 | 27 operations；顶层 11 节点/12 连线；4/7 测试；嵌套图 8 节点/12 连线且已配置 |
| 剩余工作 | 3 个 Connector 操作与 3 个业务测试 |

本轮的真实改善：压缩 Skill 使前两次调用从上一轮约 23,583 token 降到 12,780，约减少
46%；蓝图语义错误后的 reasoning 修复成功；过期平台工具被执行层拒绝后能够恢复；一次
invalid staged mutation 后，reasoning 修复了下一批节点；随后多个机械批次使用完整消息
快速成功。子图配置首次发现 3 条孤立路径后，也能恢复推理、补边并完成配置。

仍然失败的主因是子流程阶段同时暴露节点、连线、配置三个工具。模型因此在每个子批次都
使用流式 thinking，单次约消耗 25k–30k token 和几十秒；完成子图后，剩余 Connector/测试
批次又发生一次无效 mutation，修复调用遇到序列化故障并在截止前耗尽时间。另有 38 秒
发生在 assignment 创建到 daemon 接单之间，属于编排开销，不应伪装成 Builder 思考时间。

## 十八、原始智能体框架的子工作流单向编译器

根据第十七节的直接证据，本轮没有继续堆 Prompt 或反复付费试跑，而是修改同级
`../LiliesAgent/` 的原始执行框架：

1. 每个 iteration/loop 在顶层壳与连线完成后，先提交一个小型 child blueprint，只包含
   子节点身份/类型、精确连线路径、Connector 操作和统一输出节点。
2. 控制器随后按 `child blueprint → nodes → edges → parent config` 单向推进；任一时刻只
   暴露一个子流程工具。蓝图阶段保留推理，后三段在无语义错误时强制非 thinking、完整
   JSON message 和唯一工具。
3. 子节点和子连线必须来自已提交 child blueprint；不能新增、改类型、改输出或用另一个
   Connector 绕过。所有节点必须能到达统一输出，父配置完成后才恢复普通 staging。
4. 任一语义拒绝仍会临时恢复 reasoning，读取公开 expected/actual 后修一次；成功后重新
   进入机械模式。状态机负责确定性顺序，Prompt/Skill 只解释通用方法。
5. Skill 的运行时卡片同步写入该流程，但不含客户名、题号、fixture、字段映射、成品图或
   隐藏验收信息。

确定性证据：新增端到端假平台测试证明四个子阶段顺序严格且后三段每次只暴露一个工具；
相关测试 `65 passed`；完整 sibling 回归 `525 passed, 1 warning`，Ruff、Skill 校验和
`git diff --check` 全部通过。这里只能证明框架改动成立；仍需一次新的真实项目一运行证明
十分钟内平台 Draft、业务测试和发布真正完成。

## 十九、子工作流单向编译器的首次前向结果

第十八节修改通过确定性回归后，从全新空应用进行了一次真实 DeepSeek V4 Pro 运行。新
状态机按预期进入 child blueprint 和单一节点/连线阶段，但运行时上下文压缩遗漏了已提交
子蓝图的精确对象，造成不可完成的严格一致性校验；本轮被主动取消，项目一仍未完成。

| 项目 | 结果 |
|---|---|
| 应用 | `37405b02-4323-43bd-a180-14ff30805bd9` |
| Assignment | `6576da70-1512-58b4-b457-f4c69567958a` |
| Session | `7c8918fa-e000-5496-aea8-759e7c188209` |
| 接单 / 取消 | `19:16:22Z` / `19:24:01Z`，约 7 分 38 秒 |
| Token | 590,266 input + 26,780 output = 617,046 已记录；27 次已记录调用；2 次 unknown |
| 成本 | USD 0.09013564 |
| 平台草稿 | revision 0；无测试、运行、工件或发布 |
| 最远进度 | 顶层蓝图、顶层 staged graph、child blueprint 与全部 child nodes 完成；进入仅连线阶段 |
| 终态 | 通过公开 cancel API 主动取消，未越过 10 分钟或 1M token |

真实改善证据：约 1 分 18 秒完成顶层蓝图并开始唯一 staging；多个 staged mutation 语义
失败都能恢复 reasoning 后继续；child blueprint 首次失败后能够修正并提交；随后只开放
`lilies_nested_workflow_nodes_stage`，节点完成后自动切到唯一
`lilies_nested_workflow_edges_stage`。上一轮“三个子工具同时暴露、每批重新选工具”的路径
没有复发，证明单向状态机本身有效。

新的框架缺陷由公开事件精确证明：连线阶段依次返回
`nested_edge_outside_committed_blueprint`、`invalid_nested_workflow_edges` 和多个不同签名的
`nested_edge_outside_committed_blueprint`。child blueprint tool exchange 已被通用编译上下文
压缩替换，而 authoritative runtime state 只保留 `missing_edge_count`，没有保留精确边对象；
模型被要求严格照蓝图，却只能重新猜边。不同猜法产生不同语义签名，通用防重复器也不能
把它们识别为同一个状态记忆缺口。继续运行没有增量证据，因此在 617,046 token 主动取消。

据此只修通用状态记忆：`nested_build_step` 在节点阶段携带精确 missing node ID/type/purpose
和 Connector 操作，在连线阶段携带精确 missing edge 对象（含需要时的端口和分支）。每批
成功后只返回仍缺的对象；模型无需依赖已压缩的 child-blueprint 消息，也不得重新设计身份。
工具说明、Prompt 和 Skill 同步要求复制 authoritative missing objects。新增端到端断言证明
child blueprint exchange 压缩后，下一次节点和连线请求的 system state 仍包含精确对象；
完整 sibling 回归再次为 `525 passed, 1 warning`，Ruff、Skill 和 diff 检查通过。

## 二十、精确子图状态修复后的真实前向运行

第十九节的精确 missing object 修复通过回归后，又从全新空应用运行真实 DeepSeek V4 Pro。
本轮完整通过了 20 节点、25 连线的子工作流状态机，但在最终草稿提交前耗尽任务时间；
项目一仍判失败，不能因为本地编译已完成而写成“基本成功”。

| 项目 | 结果 |
|---|---|
| 应用 | `5c27b0ac-302e-4b2b-9572-2fa52d82e4e1` |
| Assignment | `f2b56aee-0ae0-5d63-86d3-dd3559a21b22` |
| Session | `0f6398cb-72d8-5b14-b439-458aef2482d8` |
| 提交 / 接单 / 失败 | `19:27:48Z` / `19:29:25Z` / `19:37:32Z` |
| 时间 | 提交至失败约 9 分 44 秒；接单后约 8 分 07 秒；接单桥接约 1 分 37 秒 |
| Token | 717,486 input + 30,715 output = 748,201 已记录；30 次已记录调用；最终 1 次 unknown |
| 平台草稿 | revision 0；无测试、运行、工件或发布 |
| 最远进度 | 顶层 11 节点/11 连线、6 业务测试、子图 20 节点/25 连线、父配置均已完成；本地 manifest 已 commit-ready |
| 终态 | `failed / daemon_session_error`；最终 commit 模型调用被 assignment call deadline 截断 |

本轮证明第十九节的修复有效：child blueprint 提交后，精确节点对象和 25 条精确边能够跨
上下文压缩继续推进；上一轮反复猜最后几条边的问题没有复发。子图完成后还发现顶层蓝图
声明了 `attachment_list`，但原子图没有代表该 Connector 的节点；Lilies 经过一次语义修复
补入 `check_existing` 后使本地 manifest 达到 commit-ready。这个补救虽然成功，但暴露出
Connector 声明与配置的一致性校验太晚。

直接失败原因是最后一次仅需调用 `lilies_workflow_draft_commit` 的确定性动作仍经过模型。
该调用在剩余时间不足时被 Provider deadline 截断，未知用量被正确记账，平台 revision 因此
仍为 0。深层原因是原框架把“关闭 thinking、强制唯一工具”当成机械执行，实际上仍要付出
完整上下文、Provider 延迟、工具名服从和 JSON 生成成本。30 个调用中，大量调用只是把已
提交蓝图中的节点、边和配置再次抄给本地状态机，不包含新的业务决策。

本轮没有修改平台业务能力，也没有读取隐藏种子、oracle、平台数据库或源码答案；失败
归属为 `LiliesAgent controller / execution architecture`，不是积木或 Connector 能力缺口。

## 二十一、可执行蓝图与无模型机械执行框架

根据第二十节的直接证据，继续修改同级 `../LiliesAgent/` 原始智能体框架，而不是新增项目
脚本或项目一专用规则：

1. child blueprint 现在必须一次性提交每个子节点的最终公开 title/config、精确边、实际
   使用的 Connector、父节点 config（不含 workflow）和输出节点。语义设计仍由模型完成。
2. child blueprint 提交时立即重建每个 connector_action 的规范 Connector 名，要求声明
   集合与配置集合一致；声明但没有节点、节点使用未声明能力、版本拆分错误都会在蓝图阶段
   被拒绝，不再拖到最终 commit。
3. 蓝图成功后，控制器直接从持久状态分批物化节点、边和父配置，不再调用模型抄写。
   语义拒绝仍不会被自动覆盖；只有精确已提交数据允许无模型执行。
4. 完整本地 manifest 的 draft commit、当前业务测试、通过后的 publish，以及最终完成消息
   也由控制器按已有证据确定性推进，不再为无决策动作调用 Provider。
5. 新增公开事件 `assignment.framework_action_selected` 和
   `assignment.framework_completion`，明确记录跳过了哪一次模型调用，便于后续计算各阶段
   时间和 Token。
6. Prompt 与通用 `ten-minute-workflow-builder` Skill 同步采用“模型负责一次语义设计，
   框架负责无模型执行已确认计划”的规则；没有客户名、项目号、fixture、字段答案或成品图。

确定性端到端测试证明：模型提交 child blueprint 后，节点、边、父配置、draft commit、
business tests 和 publish 六个动作全部由框架执行，Provider 总调用数只保留业务规划和仍需
配置的顶层阶段。首次完整回归出现一项与本改动无关的进程监督计时测试偶发失败；该测试
立即单独重跑通过，随后完整回归干净得到 `526 passed, 1 warning in 52.32s`。Ruff、Skill
校验和 `git diff --check` 均通过。

本节仍只是框架确定性证据。下一次全新项目一前向运行必须同时满足十分钟、1M token、
平台 draft revision 前进、非空业务测试通过和发布，才能证明这次架构修改真正改善交付。

## 二十二、可执行子图框架后的真实失败：顶层仍在重复生成

第二十一节的无模型子图执行通过完整回归后，从全新空应用再次运行真实 DeepSeek V4 Pro。
本轮在约 3 分 17 秒主动失败，远早于十分钟和 1M token 上限；它尚未进入子图阶段，因此
不能用来否定可执行 child blueprint，而是暴露出同一种重复生成问题仍存在于顶层。

| 项目 | 结果 |
|---|---|
| 应用 | `9eb5de22-33e3-43f6-a0b0-5bf2d10833d2` |
| Assignment | `3447ce4f-84eb-53e3-86d2-a2fa79e00abb` |
| Build | `cbc38e86-5726-58f7-927b-cf50c5b1e389` |
| Session | `3d48cebd-4847-5218-a74d-b1ab9e201071` |
| 接单 / 失败 | `19:51:29Z` / 约 `19:54:46Z`，约 3 分 17 秒 |
| Token | 130,998 input + 8,098 output = 139,096 已记录；8 次已记录调用；3 次 unknown |
| 平台草稿 | revision 0；无测试、运行、工件或发布 |
| 最远进度 | 验收计划、公开能力映射、顶层架构蓝图、草稿 inspect 完成；8 个顶层节点进入本地 staged state |
| 终态 | `failed / daemon_session_error` |

直接失败是顶层 staged mutation 的语义修复连续两次产生不完整流式工具 JSON；三个未知调用
均发生在 Provider 序列化失败边界，未执行平台写入。执行层先拒绝了一个阶段切换后的过期
inspect 调用，随后能够继续顶层节点 staging，说明阶段授权修复仍有效；真正停止点是同一
序列化指纹的有界纠正再次失败，框架按规则终止，没有无穷重试。

根因归属为 `LiliesAgent controller / top-level compiler architecture`：子图已经采用“一次
语义计划、框架机械执行”，但顶层节点、连线和测试仍通过多次模型 staging 逐批产生；同一
已知业务图被反复序列化，既耗时耗 Token，也把 Provider 流式 JSON 可靠性放到交付关键路径。
这不是平台积木、Connector 或客户环境缺口。本轮平台改动为 **无**；没有读取隐藏 Seed、
oracle、平台数据库或源码答案。

## 二十三、顶层可执行计划与完整可追踪控制器

依据第二十二节的直接证据，继续修改同级 `../LiliesAgent/` 原始框架，并把记录边界固定为
“框架改动、平台改动、每次实验、每次失败原因”四类：

1. 架构蓝图和公开积木手册准备完成、草稿 inspect 一次后，模型只提交一次完整顶层可执行
   计划：最终节点 title/config、精确连线端口/分支和每个非结构业务测试的完整输入与断言。
2. 控制器校验执行计划的节点 ID/type、边端点和测试 ID 必须与架构蓝图完全一致；Connector
   配置必须重建为已授权且已声明的规范名称。iteration/loop 在顶层只提交壳，内部仍使用
   独立可执行 child blueprint，避免把复杂嵌套 WorkflowSpec 塞进一个配置。
3. 校验通过后，控制器直接把计划转换为公开 `add_node`、`add_edge`、`add_test` 操作；后续
   子图物化、draft commit、business tests、publish 和最终完成消息全部使用已确认状态，
   不再调用模型重述已知参数。
4. 新增公开事件 `assignment.workflow_executable_plan_committed`；原有
   `assignment.workflow_draft_staged` 标注 `initiated_by=committed_executable_plan`，配合
   `assignment.framework_action_selected` 和 `assignment.framework_completion` 可重建模型
   决策与框架执行链，不记录私有思维链。
5. Prompt 与通用 Skill 同步为“验收合同 → 能力映射 → 架构蓝图 → 顶层可执行计划 → 必要的
   子图可执行计划 → 框架物化 → 业务测试 → 发布”。规则不包含项目名称、客户字段、fixture、
   隐藏答案或完成图。

本轮平台改动为 **无**：没有因智能体序列化失败而增加新积木、Connector 或项目专用接口。
当前聚焦回归为 `31 passed`，完整 sibling 回归为
`526 passed, 1 warning in 54.78s`；Ruff、Skill 校验和 `git diff --check` 均通过。新的真实
项目一前向运行仍是本节待补证据，未完成前不宣称十分钟交付成功。

## 二十四、顶层可执行计划的首次真实前向运行

第二十三节通过完整回归后，从全新空应用运行真实 DeepSeek V4 Pro。顶层一次性执行计划
获得了明确正向证据，但两个子图收口不变量没有被控制器强制，导致运行重新落回旧 staging
路径并失败。本轮在请求 deadline 后由公开 cancel API 终止，项目一仍未完成。

| 项目 | 结果 |
|---|---|
| 应用 | `86a0508e-b2a0-4c14-a6c6-6c2796cfa2d8` |
| Assignment | `663310f4-6db0-5da2-8258-ba233f26b280` |
| Build | `edfa35d7-e6bc-5086-9ef5-2f44392c3453` |
| Session | `fca2a22c-99d0-5ba5-9552-f3df6f38d41a` |
| 应用创建 / daemon 接单 | `20:05:57Z` / `20:06:17Z` |
| 请求 deadline / 公开取消终态 | `20:15:57Z` / `20:16:36Z` |
| 时间 | 创建至取消约 10 分 39 秒；公开状态在 deadline 后约 39 秒仍为 running，随后人工调用公开取消接口 |
| Token | 259,970 已记录（233,141 input + 26,829 output）；12 次已记录调用；4 次 unknown |
| 平台草稿 | revision 0；无测试、运行、工件或发布 |
| 最远进度 | 顶层 13 节点/13 连线/7 业务测试一次计划并生成 33 operations；子图 12 节点/18 连线由框架物化 |
| 终态 | `cancelled`；不满足十分钟、平台 Draft、测试或发布验收 |

本轮真实改善：约 2 分 55 秒时，`assignment.workflow_executable_plan_committed` 一次提交
13 个顶层节点、13 条精确连线和 7 个完整非结构业务测试，控制器直接生成 33 个本地操作；
上一轮逐批顶层 staging 的多次模型调用消失。子图蓝图第一次流式序列化失败后，一次有界
纠正以 4,335 token 成功提交；随后 12 个节点和 18 条边分六个
`assignment.framework_action_selected` 动作全部无模型完成。到这里累计业务计划约 110k
已记录 token，证明“模型一次设计、框架机械执行”显著降低了重复调用。

本轮失败由三个可分离事实组成：

1. 顶层 executable plan 允许 iteration 壳提前携带 `output_node_id`。子边完成后，物化视图
   因为已见到输出字段而不再报告 nested issue，但 authoritative child compiler state 仍是
   `configured=false`；控制器因此没有选择确定性的 parent-config 动作，错误回到普通 staging。
2. 架构蓝图声明了 `connector:inventree@6.attachment_list`，但顶层执行计划和唯一 child
   blueprint 都没有配置对应节点。现有校验只保证每个子图“自己声明的 Connector 都有节点”，
   没有在最后一个未规划子图处保证“全图剩余 Connector 已覆盖”，manifest 持续报告这一项。
3. 普通 staging 回退后，模型尝试用 `update_node` 拼装父配置；该操作不更新独立 child
   compiler state，又连续产生无效 mutation 和 Provider JSON 序列化失败。平台桥接公开状态
   在请求 deadline 后仍短暂显示 running，人工按硬限制调用公开 cancel；没有允许第二个并发
   会话或新的盲目实验。

根因归属均为 `LiliesAgent controller / compiler invariants`。本轮平台改动为 **无**，没有
新增积木、Connector、宿主适配器或项目专用接口；也没有读取隐藏 Seed、oracle、平台数据库
或源码答案。下一步只收紧通用控制器：iteration/loop 顶层壳不得预填 child workflow/output，
child compiler 的完成状态必须独立于物化视图决定，最后一个 child plan 必须覆盖全图剩余的
已声明 Connector。修复后先跑确定性回归，再从新的空应用前向运行。

## 二十五、子图收口不变量的一次性框架修复

第二十四节失败后没有修改工作流业务逻辑，也没有继续试跑；先在同级
`../LiliesAgent/` 原始智能体框架中一次性补齐三个通用编译不变量：

1. 顶层 executable plan 会拒绝在 iteration/loop 壳中预填 `workflow` 或
   `output_node_id`。这两个字段只能由已提交的 child plan 在子节点、精确边、父配置和输出
   全部验证后写入。
2. 每个 child compiler state 现在有独立的完整性判定：实际节点 ID 集合、实际边集合、父
   config 和 output 必须与 child blueprint 精确相等。即使平台物化视图表面上不再报告
   nested issue，未完成的 authoritative compiler state 仍会生成
   `nested_compiler_state_incomplete`，控制器继续唯一的确定性子图动作，禁止回落普通 staging。
3. 顶层与所有已提交 child plan 的 Connector 节点按全图求并集。还有其他未规划 child 时
   允许连接器暂缺；最后一个 child plan 提交时，必须覆盖架构蓝图全部已声明 Connector，
   否则直接以精确缺口拒绝，不能把错误拖到最终 commit。

通用 `ten-minute-workflow-builder` Skill 同步写入上述所有权和收口规则，不含项目一名称、
客户字段、固定节点图、Seed、fixture 或预期答案。本轮平台改动为 **无**；没有新增积木、
Connector、API、数据库字段或测试专用旁路。

确定性证据：`tests/test_assignment.py` 新增覆盖空壳拒绝、authoritative child state 收口、
多 child Connector 延迟/最终覆盖的测试，聚焦结果为 `32 passed`。第一次完整回归为
`526 passed, 1 failed`，唯一失败是未改动的进程监督 50ms 时序测试返回了另一种监督错误；
该用例随后连续两次单独通过，因此没有修改无关模块。第二次完整回归干净得到
`527 passed, 1 warning in 50.15s`；Ruff、Skill 校验和 `git diff --check` 均通过。

下一条实验只从全新空应用运行一次项目一。它将独立记录应用、Assignment、Session、阶段
耗时、公开事件、Token、平台 Draft/测试/发布结果及直接失败原因；十分钟或 1M token 任一
达到即停止，不把框架内部进度当作业务完成。

## 二十六、收口不变量修复后的真实运行：第二张大计划超时

第二十五节回归通过后，从全新空应用进行一次真实 DeepSeek V4 Pro 项目一运行。三个新增
收口不变量没有再触发上一轮的普通 staging 回退，但独立的 child executable plan 仍是第二
张完整业务图设计，单次 Provider 调用耗尽其剩余 call deadline。本轮失败，平台 Draft 未变。

| 项目 | 结果 |
|---|---|
| 应用 | `eb56e0e8-d95d-4416-b725-8e34c923b0c9` |
| Assignment | `9ee3747a-328a-5552-bca7-92eadd894827` |
| Build | `740bb392-8d29-5a7f-94dd-550dbe22c599` |
| Session | `7d8ba2c2-19f2-5663-9ccd-b42343cfd266` |
| 平台提交 / daemon 接单 / 失败 | `20:27:44Z` / `20:28:50Z` / `20:33:35Z` |
| 时间 | 平台桥接约 1 分 06 秒；接单后约 4 分 45 秒失败；未越过十分钟 |
| Token | 90,575 input + 10,376 output = 100,951 已记录；第 7 次调用用量 unknown；远低于 1M 已知上限 |
| 平台草稿 | revision 0；0 节点、0 连线、0 测试；无版本或发布 |
| 最远进度 | 顶层 blueprint；9 节点/9 连线/7 业务测试的 executable plan；控制器生成 25 个本地操作 |
| 终态 | `failed / daemon_session_error`；Provider call deadline |

正向证据：Blueprint 第一次语义拒绝后能够按公开 expected/actual 修正；约接单后 2 分 18 秒
一次提交顶层 9 节点、9 连线、7 个完整业务测试，控制器直接生成 25 个操作。iteration 顶层
壳没有提前伪造 child workflow/output，且最后 child 的全图 Connector 覆盖约束已经进入
待执行状态；上一轮“表面图通过后退回普通 staging”的错误没有复发。

直接失败证据：第 7 次调用在 `20:31:02Z` 以 thinking/streaming 模式开始生成 child
executable plan，到 `20:33:35Z` 返回 `provider_timeout`，运行时记录
`model provider request exceeded the assignment call deadline` 并安全终止；没有并发重放或
继续猜图。该调用用量未知，按硬预算保守停止。

根因归属为 `LiliesAgent planner/controller architecture`：顶层 executable plan 已完成一次
整体语义设计，但框架随后要求模型重新装载同一上下文并生成第二张完整子图计划。单次复杂
子图的推理/序列化延迟仍在交付关键路径。它不是平台积木、Connector、客户环境或授权能力
缺口。本轮平台改动为 **无**；没有读取隐藏 Seed、oracle、平台数据库或源码答案。下一步
改为一次分层 executable plan：模型一次提交顶层及必要 child plans，控制器统一做全图校验、
分批物化、commit、业务测试和发布，消除第二个大语义调用。

## 二十七、一次分层计划的智能体框架改造

依据第二十六节的 Provider timeout，修改同级 `../LiliesAgent/` 的 planner/controller，而非
平台业务能力：

1. `lilies_workflow_executable_plan_commit` 从“只提交顶层”升级为一次分层语义计划。模型在
   同一调用中提交最终顶层节点/连线/业务测试，以及每个顶层 iteration/loop 的完整 child
   plan；不再有第二个重新装载上下文的 child-planning 调用。
2. 控制器一次校验全部 child 的父节点所有权、节点/边唯一性、已知且已声明的积木类型、
   每个节点到输出的可达性、父 config、Connector 配置重建，以及顶层加全部 child 的全图
   Connector 覆盖。任一处不成立，整份计划不进入编译状态。
3. 校验成功后，控制器把每个 child blueprint 直接写入 authoritative compiler state，并
   继续原有的分批节点、分批连线、父配置、平台 Draft commit、业务测试和发布动作；这些
   机械阶段继续零模型调用。
4. 公开事件保留顶层计划统计，并为每个 child 记录
   `assignment.nested_workflow_blueprint_committed`，标注
   `initiated_by=committed_hierarchical_executable_plan`，因此仍可重建计划与执行链，但不记录
   私有思维链。
5. 通用 Skill 同步改为“验收合同 → 能力映射 → 架构蓝图 → 一次分层 executable plan →
   控制器物化 → 业务测试 → 发布”，不含客户名、题号、固定图、Seed 或字段答案。

本轮平台改动为 **无**；没有增加积木、Connector、API 或项目专用旁路。聚焦回归新增一次
分层计划验证和带 iteration 的端到端测试，结果 `33 passed`；后者证明 Provider 调用从 6 次
降为 5 次，第 6 次 child planning 不再发生，而子节点、边、父配置、Draft commit、测试和
发布仍完整执行。完整 sibling 回归为 `528 passed, 1 warning in 55.19s`，Ruff、Skill 校验和
`git diff --check` 全部通过。下一步从另一全新空应用做一次真实前向运行，验证该结构能否在
DeepSeek V4 Pro、十分钟和 1M token 条件下真正推进平台 Draft 并发布。

## 二十八、一次分层计划的真实前向结果：首次推进 Draft，局部提交续跑失败

第二十七节完整回归后，从另一全新空应用运行真实 DeepSeek V4 Pro。一次分层计划消除了
上一轮的第二张大计划超时，并首次把真实项目一从空 Draft 推进到 revision 4；随后暴露出
编译器把“完整期望图”和“尚未提交的操作”存于同一列表，局部成功后丢失续跑游标。本轮
主动取消，项目一仍未完成。

| 项目 | 结果 |
|---|---|
| 应用 | `8ba28160-e41d-4da2-adbf-e3c8e40b16b2` |
| Assignment | `6846d113-8665-58cf-980c-b2eb20982a67` |
| Build | `ea9837d2-ddb3-58f8-9976-94998a4c5a73` |
| Session | `0b5d07f0-84e5-5ce3-b0ff-f494089f5a76` |
| 平台提交 / daemon 接单 / 主动取消 | `20:41:37Z` / `20:42:02Z` / `20:49:09Z` |
| 时间 | 桥接约 25 秒；接单至取消约 7 分 07 秒；未越过十分钟 |
| Token | 405,998 input + 31,404 output = 437,402；13 次全部 recorded；低于 1M |
| 分层计划 | 顶层 10 节点/13 边/8 业务测试；1 个 child，18 节点/32 边；7 个 Connector 全覆盖 |
| 平台草稿 | revision 4，公开 snapshot 为 4 节点/0 边/0 测试；未运行测试或发布 |
| 终态 | 发现确定性无进展后通过公开 cancel API 主动取消 |

正向证据：Blueprint 一次语义修复后提交；分层 executable plan 第一次被严格校验拒绝后，
第二次在约接单后 5 分 56 秒完整提交。顶层和 child 在同一调用中覆盖全部 7 个 Connector；
之后 5 批 child nodes、4 批 child edges、1 次 parent config 和第一次 Draft commit 均由
`assignment.framework_action_selected` 标明 `model_call_avoided=true`。上一轮第 7 次独立
child-planning timeout 没有复发，说明第二十七节改动确实改善了 Lilies。

直接失败证据：第一次公开 Draft commit 共 31 个操作，前 4 个成功，revision 从 0 前进到
4，第 5 个返回公开错误后按规定停止。运行时把待处理列表切成失败操作开始的后缀，但下一轮
manifest 只看这个后缀，误判已成功的前 4 个顶层节点“缺失”；修复路径遂重新补回前缀并从
第 1 个操作重放，在 revision 4 上反复冲突。后续不同模型调用虽尝试 commit、inspect 或新增
操作，均无法恢复准确游标；Token 增至 437,402 而 revision 不再前进，因此提前取消。

根因归属为 `LiliesAgent draft compiler transaction state`：同一状态变量同时承担“全图覆盖
证明”和“未提交操作队列”，局部提交后这两个含义冲突；此外没有只允许替换失败身份的专用
修复工具。本轮平台改动为 **无**，平台正确保留了 revision 4 和已接受的 4 个操作；没有
读取隐藏 Seed、oracle、平台数据库或源码答案。下一步把已应用前缀与待提交后缀分开保存，
manifest 以二者并集判断全图覆盖，公开 commit 只消费后缀；失败时只开放一个按相同身份替换
失败/依赖操作的修复工具，随后从准确游标继续，禁止从操作 0 重放。

## 二十九、可恢复 Draft 编译事务的框架修复

根据第二十八节 revision 4 的直接证据，继续只修改 `../LiliesAgent/` controller：

1. Draft compiler 将 `applied_draft_operations` 与待提交的
   `staged_draft_operations` 分开保存。Manifest 用二者并集证明完整业务图覆盖，公开
   `platform_draft_apply` 只消费待提交后缀；局部成功的前缀不会被误报为缺失，也不会重放。
2. 部分提交失败后新增唯一的本地 `lilies_workflow_draft_repair` 阶段。它只接受 1–4 个替换
   操作，必须包含当前失败身份，所有身份必须已存在于 pending suffix；禁止新增无关节点、
   连线或测试，禁止改动已应用前缀。修复本身不修改平台。
3. 运行时公开状态携带失败操作、失败身份、公开错误、last accepted revision、已应用数量和
   待提交数量。修复成功后控制器从准确 suffix 游标再次确定性 commit；全部提交后清空事务
   状态并进入业务测试和发布。
4. 新增公开事件 `assignment.workflow_draft_repair_committed`，记录被替换的公开身份以及
   applied/pending 数量；Prompt/Skill 同步写明“全图并集验证、仅提交后缀、同身份修复”。

本轮平台改动仍为 **无**。端到端回归模拟 8 个操作的第 2 个被公开 API 拒绝：第一次 commit
只接受第 1 个并停下；模型仅用 repair tool 同身份修正第 2 个；第二次 commit 的
`operation_count=7`，只提交失败项及其后缀，最终 revision 恰为 8，随后业务测试和发布通过。
因此不是靠放宽校验或重置 Draft 获得绿色。聚焦回归 `33 passed`，完整 sibling 回归
`528 passed, 1 warning in 56.88s`；Ruff、Skill 校验和 `git diff --check` 均通过。下一步做
一次新的空应用前向运行，检验真实平台第一个配置错误能否被同身份修复并在十分钟内继续。

## 三十、可恢复编译器后的真实前向运行：分层计划纠正仍在流式思考

第二十九节回归后从全新空应用再次运行。该轮在分层 executable plan 完成前失败，未触发
部分 Draft 提交，因此不能用它否定可恢复事务修复。

| 项目 | 结果 |
|---|---|
| 应用 | `e1b8b295-5d28-4951-9e5d-417429a20e64` |
| Assignment | `9a1e6797-58f5-5c65-8f0e-4a65be2014e8` |
| Build | `f4669037-7214-59ca-b4c6-e09d54d375c5` |
| Session | `1e7849e9-75eb-5827-be4f-3dc3fa2ad23c` |
| 平台提交 / daemon 接单 / 失败 | `20:59:10Z` / `20:59:45Z` / `21:04:10Z` |
| 时间 | 桥接约 35 秒；接单后约 4 分 25 秒失败；未越过十分钟 |
| Token | 67,794 input + 7,999 output = 75,793 已记录；2 次 unknown；低于 1M |
| 平台草稿 | revision 0；无测试或发布 |
| 终态 | `failed / daemon_session_error` |

Blueprint 仍表现为第一次严格校验失败、第二次成功；这是连续三轮可复现的模型效率问题，
后续需从蓝图 expected/actual 模式压缩 Prompt，而不是放宽校验。随后第 6 次调用生成唯一的
分层 executable plan 时发生 `provider_output_serialization`，用量按未知记录；框架正确没有
执行残缺 JSON，并安排一次有界纠正。但第 7 次纠正仍是 `thinking_enabled=true`、
`streaming_enabled=true`，60 秒后 `provider_timeout`，会话安全失败。

根因归属为 `LiliesAgent provider recovery policy`：初次语义规划需要推理，但已知唯一工具的
序列化纠正目标是重新输出完整结构化对象；继续使用流式长思考放大了再次截断和超时概率。
本轮平台改动为 **无**，也未进入客户连接器运行。下一步保留首次计划的 reasoning，只把
单一结构化计划工具的唯一纠正切换成 forced tool、non-streaming 完整消息和 thinking off；
仍只允许一次，完整响应继续走相同 schema/语义校验，绝不本地修补截断 JSON。

## 三十一、单一结构化计划工具的可靠序列化纠正

根据第三十节第 6/7 次调用证据，修改 `../LiliesAgent/` Provider recovery policy：

1. 首次验收、蓝图和分层 executable plan 仍使用 thinking + streaming，保留真实语义设计。
2. 只有当唯一暴露的已知结构化工作流工具返回 `invalid_tool_arguments_json` 时，唯一一次
   corrective regeneration 强制同一工具，使用 non-streaming 完整消息并关闭 thinking。
   这不是把残缺 JSON 在本地拼好，而是让 Provider 重新产生一份完整对象。
3. 完整响应仍按原工具 schema、架构蓝图、Connector 全图覆盖、图可达性和业务测试合同重新
   校验；再次格式错误或语义错误继续失败，不放宽验收。
4. `provider.call_mode_selected` 继续公开记录 `forced_tool_name`、`thinking_enabled` 和
   `streaming_enabled`，因此可直接证明纠正是否走了新模式。

通用 Skill 同步区分“首次语义规划”和“已知唯一工具的可靠序列化纠正”。本轮平台改动为
**无**。聚焦回归 `34 passed`，完整 sibling 回归
`528 passed, 1 warning in 55.70s`；Ruff、Skill 和 diff 检查均通过。下一次真实前向运行重点
观察：若首次分层计划再次截断，第二次调用必须公开显示 forced tool、thinking off、
non-streaming，并在六十秒纠正窗口内返回完整对象。

## 三十二、可靠序列化纠正的真实模式证据与时间窗失败

第三十一节回归后从全新空应用运行。首次分层计划再次发生相同序列化故障，因而真实验证了
新恢复模式；但固定 60 秒纠正时间窗不足以生成完整的大型分层对象，本轮仍失败。

| 项目 | 结果 |
|---|---|
| 应用 | `a9b3e156-230c-4e5a-85d2-62d8aa400c0e` |
| Assignment | `29168686-2591-5ffd-b17d-c4de67bdbdc4` |
| Build | `93acf5d1-6aa9-5f7b-99d2-36365fbcc52c` |
| Session | `5662550d-b0fa-585c-867f-be2c69087ab1` |
| 平台提交 / daemon 接单 / 失败 | `21:08:32Z` / `21:09:21Z` / `21:13:31Z` |
| 时间 | 桥接约 49 秒；接单后约 4 分 10 秒失败 |
| Token | 66,982 input + 8,318 output = 75,300 recorded；2 次 unknown；低于 1M |
| 平台草稿 | revision 0；无测试或发布 |

关键正向证据：第 6 次首次计划调用公开为 thinking=true、streaming=true，随后以
`provider_output_serialization` 失败；第 7 次唯一纠正公开为
`forced_tool_name=lilies_workflow_executable_plan_commit`、thinking=false、
streaming=false。第三十一节的模式改造真实生效，没有再走流式长思考。

直接失败是第 7 次完整消息在固定 60 秒后 `provider_timeout`。它开始时任务总 deadline 仍有
约五分钟，并且同等规模的分层计划在第二十八节曾成功返回约 12k 输出；因此 60 秒是恢复
策略自身的过短上限，不是用户十分钟或 1M token 上限。本轮平台改动为 **无**。下一步只把
该唯一纠正上限调到 120 秒，同时继续取总 deadline 剩余值的较小者、保留取消预留、Token
保守预留和一次纠正上限；不增加第三次调用，不放宽结构或业务校验。

## 三十三、完整结构化纠正的时间预算修正

第三十二节证明模式正确但 60 秒不足后，只把
`_SHORT_BUILD_SERIALIZATION_RETRY_TIMEOUT_SECONDS` 从 60 调整为 120。实际调用仍取该值、
总任务剩余安全时间和 Provider 总 timeout 的最小值；仍预留取消/收证时间，仍做 1M token
保守预留，仍只有一次纠正。没有增加第三次模型调用，也没有降低 schema、蓝图、Connector、
图可达性或业务测试校验。

本轮智能体框架改动仅此一项；Prompt/Skill 无需加入具体秒数，继续表达“有界完整消息纠正”。
平台改动为 **无**。聚焦回归 `34 passed`，完整 sibling 回归
`528 passed, 1 warning in 50.88s`；Ruff、Skill 和 diff 检查通过。下一次真实运行验证 forced
non-streaming 纠正是否能在总十分钟窗口内返回完整分层计划。

## 三十四、120 秒纠正后的真实运行：计划成功，失败操作修复合同失效

第三十三节回归后，从新的空应用进行一次真实 DeepSeek V4 Pro 前向运行。120 秒完整消息
纠正确实让一次分层 executable plan 成功返回，并首次在同一轮同时完成顶层、child 和全部
Connector 的严格计划；但公开 Draft 的第一个配置错误出现后，现有 repair tool 要求模型
重新提交完整失败操作，工具身份、输入规模和上下文表达共同导致连续无效修复。本轮在会话
自身截止前失败，没有测试或发布。

| 项目 | 结果 |
|---|---|
| 应用 | `e81720f9-ccc7-49fd-9c00-1924912eb893` |
| Assignment | `ffe85b52-a61f-5db4-8949-9e34d5897f24` |
| Build | `26c0509e-3ff2-54f9-95c6-80991ae4c54f` |
| Session | `1d695850-6386-55fe-9c49-13aab2ae1722` |
| 平台提交 / daemon 接单 / 失败 | `21:16:55Z` / `21:18:01Z` / `21:26:31Z` |
| 时间 | 提交至失败 9 分 36 秒；daemon 接单后 8 分 30 秒；未越过十分钟 |
| Token | 243,178 input + 30,575 output = 273,753 recorded；10 次 recorded、3 次 unknown；低于 1M |
| 分层计划 | 顶层 9 节点/8 边/8 业务测试；1 个 child，21 节点/34 边；7 个 Connector 全覆盖 |
| 平台草稿 | revision 4，4 节点/0 边/0 测试；无 immutable version |
| 终态 | `failed`；最后一次 Provider 调用超过 assignment call deadline |

正向证据：蓝图在 `21:18:30Z` 提交；原来连续两轮超时的分层 executable plan 在
`21:20:35Z` 成功提交。控制器随后零模型调用完成 6 批 child nodes、5 批 child edges、父
配置和 Draft commit。25 个公开操作的前 4 个成功，平台 revision 从 0 精确前进到 4；已
应用前缀没有被重放，说明第二十九节的事务游标修复真实生效。

第一直接失败来自第 5 个操作 `add_node:process_each_doc`。child 中一个 `if_else` 条件使用了
公开手册不允许的 `not_empty`；平台公开校验明确给出允许值是 `equals`、`not_equals`、
`contains`、`not_contains`、`gt`、`gte`、`lt`、`lte`、`exists` 或 `empty`。框架已有公开
手册的压缩字段合同，却只在计划阶段校验节点类型、图、Connector 和覆盖关系，没有用这些
合同校验节点 config 的 enum，因此错误延迟到部分公共提交后才暴露。

第二直接失败来自 repair 设计。当前工具要求 1–4 个**完整 public operation**，且第一个
必须与失败身份完全相同。实际失败操作是带 21 节点、34 边 child workflow 的 iteration
`add_node`，重新输出整个对象既昂贵又容易改错。真实 transcript 显示：

1. 第一次 repair 响应发生 Provider JSON 序列化失败；forced retry 虽公开标记唯一工具为
   `lilies_workflow_draft_repair`，Provider 却仍返回旧的 `lilies_workflow_draft_commit` 名称，
   框架按 active-tool 边界正确拒绝。
2. 后续一次 repair 提交了 `update_node:process_each_doc` 加三个已应用前缀节点，违反 pending
   identity；另一次只重交已应用的四个顶层节点，仍没有失败 identity。
3. 再一次 forced repair 只生成空对象，得到 `repair_requires_one_to_four_operations`；最后
   一次调用在剩余 deadline 用尽时 `provider_timeout`。没有工作流测试或发布可被冒充为成功。

为核对上述参数，只读查询了 standalone `LiliesAgent` 自己的持久会话记录；平台公开会话页
只投影工具名和成功/失败，不投影工具参数。该取证没有读取平台数据库、隐藏 Seed、oracle、
客户宿主数据或源码答案。诊断后重新启动的只读 daemon 已立即关闭；Paperless、InvenTree、
数据库、缓存和 Docker 网络也全部停止并移除。

根因归属为 `LiliesAgent executable-plan config validation + failed-operation repair protocol`。
本轮平台改动为 **无**，平台公开 API 正确拒绝非法 enum、保留已成功 revision 4，并返回了
可理解的校验信息。下一步只改通用智能体框架和 Skill：在完整计划进入 Draft 前用已读取的
公开积木字段合同检查 config enum/const；把 repair 从“重发完整 operation”改为“对唯一失败
operation 做小型路径编辑，身份由控制器锁定”；repair 阶段只开放并强制该唯一工具，同时把
失败路径、允许值和上次 repair issue 保留在可追踪事件中。完成确定性回归后再做一次新空应用
运行，不增加平台积木或项目专用逻辑。

## 三十五、公开配置合同前置校验与失败操作路径修复

根据第三十四节真实证据，修改同级 `../LiliesAgent/` 原始智能体框架和通用
`ten-minute-workflow-builder` Skill，未修改平台：

1. 新增通用 public-manual config contract 校验。控制器对已经通过公开 API 读取并压缩的
   `config_fields` 解析嵌套对象/数组路径，在 executable hierarchical plan 进入 compiler
   state 前检查 enum、const、基础类型、数值上下界、字符串/数组长度。顶层和每个 child 都
   使用同一逻辑；非法项返回 node、block type、schema path、具体 config path、expected 和
   actual，并记录 `assignment.workflow_plan_config_rejected`。没有读取平台源码来猜规则；用
   平台公开 `/api/v1/blocks/if_else` 再确认真实手册确实公开了相同 operator enum。
2. `lilies_workflow_draft_repair` 从“重发 1–4 个完整 operation”改成“对当前失败 operation
   提交 1–8 个小型 JSON 路径编辑”。路径必须从 `data` 开始，可用字符串对象键和整数数组
   下标；控制器复制原失败操作、应用修改、再次校验 public operation envelope，并证明稳定
   identity 未改变。已成功前缀、pending suffix 和 commit cursor 全由控制器拥有，模型不能
   重放、改身份或新增无关 operation。
3. repair 是 partial commit 后唯一有效动作时，即使上一结果属于 semantic failure，也强制
   唯一 repair tool、non-thinking 完整消息；模型只决定直接证据支持的字段值，不再重新规划
   或重序列化整个 nested workflow。每次非法修复记录
   `assignment.workflow_draft_repair_rejected`，包含失败 identity、attempt、issue code 和路径；
   成功记录 `assignment.workflow_draft_repair_committed` 的 identity 和实际编辑路径。
4. Prompt/Skill 同步写明：公开手册合同在计划提交前是可执行约束；partial commit 后只改
   当前失败 operation 的小路径；controller 锁住 identity、前缀、后缀和游标。规则不含项目
   名、客户字段、固定图、fixture、Seed 或预期答案。

回归过程中的失败也完整保留：第一次聚焦回归为 `32 passed, 2 failed`，原因是旧工具 schema
装配器仍把 repair 当成 `operations` 数组并动态投影 public draft-operation schema，新工具已
改为 `edits`，因此触发 `KeyError('operations')`；修正为只给 stage/commit 投影。紧接着一次
collection failure 是该小改动的循环缩进错误，Ruff 和 Python parser 同时发现；修正缩进后
没有改变设计。最终聚焦回归 `34 passed`，其中新增证据覆盖：非法 `not_empty` 在层级计划
阶段被公开 enum 精确拒绝，改为合法值后通过；非法 repair 根路径生成 rejection event；合法
路径只补失败配置并从 suffix 继续，最终 revision、业务测试和发布全部成功。

最终完整 sibling 回归为 `529 passed, 1 warning in 50.03s`；Ruff 全通过，Skill validator
返回 `Skill is valid!`，`git diff --check` 通过。本轮平台改动为 **无**，没有新增积木、
Connector、API、宿主 adapter、数据库字段或项目专用旁路。下一步只从另一全新空应用运行
一次项目一，验证真实 DeepSeek V4 Pro 是否能在十分钟/1M token 内通过计划校验、必要路径
修复、业务测试和发布。

## 三十六、前置配置校验后的真实运行：计划 Provider 传输失败被原样重放

第三十五节全部回归通过后，从另一全新空应用进行真实 DeepSeek V4 Pro 运行。该轮在完整
hierarchical executable plan 返回前结束，因此没有执行到新增的 public-manual config 校验或
failed-operation path repair，不能用本轮否定这两项改动。

| 项目 | 结果 |
|---|---|
| 应用 | `82fea1e5-aea3-410f-94a5-6790eb9cb0f7` |
| Assignment | `c724f462-89bf-5feb-b468-80ef8b21c633` |
| Build | `f39735d2-72e5-5025-ae50-79c88fd1c1c9` |
| Session | `c7fe86dd-2499-5265-b622-43dcbc3e2d2c` |
| 平台提交 / daemon 接单 / daemon 失败 / 平台终态 | `21:43:58Z` / `21:44:34Z` / `21:50:40Z` / `21:50:43Z` |
| 时间 | 提交至平台终态约 6 分 45 秒；daemon 接单至失败约 6 分 06 秒；未越过十分钟 |
| Token | 64,764 input + 7,148 output = 71,912 recorded；5 次 recorded、2 次 unknown；低于 1M |
| 平台草稿 | revision 0；0 节点/0 边/0 测试；无版本 |
| 终态 | `failed / daemon_session_error`；两次完整计划 Provider 调用都没有返回可执行对象 |

正向证据：验收计划和公开能力映射完成；第一次蓝图因遗漏要求中的 `workflow.json` 被严格
拒绝，第二次根据 expected/actual 补齐后成功。控制器按蓝图通过公开 API 一次性读取并压缩
17 个选定积木手册，锚定全新空 Draft。该错误仍属于模型第一次蓝图完整性效率问题，但第二次
能够收敛，且没有引发平台写入。

直接失败：第 6 次调用是唯一的 `lilies_workflow_executable_plan_commit`，使用
thinking=true、streaming=true 生成完整顶层与 child 计划；约 1 分 48 秒后 Provider 返回
`provider_transport_or_timeout`，用量记为 unknown。错误可重试且预算充足，框架安排唯一一次
重试，但使用 `retry_strategy=exact_replay`，把同一个大型 thinking/streaming 请求原样重放；
第 7 次在 150 秒后 `provider_timeout`，会话安全失败。没有残缺计划进入 config 校验、compiler
state 或公共 Draft。

根因归属为 `LiliesAgent provider recovery policy`：已知只有一个结构化规划工具时，传输失败
后的原样重放重复了相同延迟和流式风险。第三十一节只对 `invalid_tool_arguments_json` 切换
forced/non-thinking/non-streaming，却没有覆盖“响应对象完全未形成”的可重试传输失败。本轮
平台改动为 **无**；平台未收到 Draft mutation，也没有客户连接器副作用。运行结束后 daemon
和全部客户 Docker 容器、数据库、缓存、网络已关闭。

下一步只扩展通用 Provider 恢复策略：当 active tools 恰好只有一个已知结构化工作流工具且
首次调用发生可重试传输/超时错误时，唯一重试改为 forced tool、thinking off、non-streaming
完整再生成，并使用 120 秒及剩余 assignment 时间的较小值；不是执行残缺响应，也不增加第
三次调用。完成确定性回归后再做一次新空应用运行。

## 三十七、单结构化阶段的传输恢复策略

根据第三十六节的真实失败，修改同级 `../LiliesAgent/` 的原始智能体框架和通用
`ten-minute-workflow-builder` Skill；本轮平台改动为 **无**。

智能体框架只增加一个有证据边界的通用恢复分支：当当前阶段恰好只授予一个已知的结构化
工作流工具，第一次 Provider 调用又在形成完整响应前发生可重试传输错误或超时时，框架不再
原样重放大型 thinking/streaming 请求，而是把唯一工具锁定为 forced tool，关闭 thinking 和
streaming，要求模型完整重生同一个结构化决策。该纠正最多一次；超时取 120 秒和 assignment
安全剩余时间的较小值；不执行残缺响应、不增加第三次调用，也不改变工作流语义。若当前同时
存在多个工具，仍保持原恢复策略，因为框架不能替模型猜下一动作。若 assignment 还设置了
美元硬预算，失败调用用量未知时仍禁止继续付费重试；1M Token 和十分钟门禁也继续计算重试
预留量。

Prompt/Skill 同步记录同一原则：传输/超时调用的 Token 用量必须记为 unknown，不估成零；只有
唯一结构化工具且 Token 预算同时容纳失败调用与一次完整重生时才允许恢复；恢复必须是 forced、
non-thinking、non-streaming 的完整重生，禁止 exact replay 和第三次调用。这些规则不含项目名、
积木组合、客户字段、固定工作流、Seed 或预期答案。

测试过程中的失败也保留：第一次聚焦回归为 `33 passed, 1 failed`。失败原因不是生产恢复逻辑，
而是新增用例沿用了 `max_budget_usd=1`，却又预期一次未知用量的传输失败后继续付费重试；原有
预算门禁正确终止了会话。用例随后改为与本次真实项目一致的“十分钟 + 1M Token、无美元硬
预算”，没有放松生产预算规则。单用例复跑 `1 passed`，完整聚焦回归 `34 passed`。

最终 sibling 全量回归为 `529 passed, 1 warning in 50.85s`；Ruff 全通过，Skill validator 返回
`Skill is valid!`，`git diff --check` 通过。下一步从全新空应用进行一次真实 DeepSeek V4 Pro
前向运行，验证该恢复分支是否只在真实 Provider 首次传输失败时降低重试成本，并继续观察前置
配置合同与失败操作路径修复是否真正被执行。

## 三十八、运行初始化失误与 Connector 身份合同死锁

第三十七节回归后先后发生两次运行。第一轮不是有效的 Lilies 能力实验：新建空应用时没有把
应用加入现有 Paperless/InvenTree 公开 Connector binding，任务权限又误把自然语言资源描述
写进可读/可写 lane，而不是公开 catalog 的精确 operation 名。Lilies 因而看见空 Connector
目录，并准备退回原始 HTTP；监控发现后立即取消，没有 Draft mutation 或客户写入。

| 项目 | 无效初始化轮 |
|---|---|
| 应用 | `6f447077-8282-40cf-a075-a9432cb5a7c2` |
| Assignment | `c42435ec-cd37-5843-861e-da5f17912088` |
| Build | `5b11113b-30b8-51c8-87ca-775914a1a62f` |
| Session | `9f5d8f89-f80c-5ea3-914a-d7b8677e0ea2` |
| 会话时间 | `22:00:38Z` 至 `22:03:31Z`，约 2 分 53 秒后主动取消 |
| Token | 61,738 input + 9,266 output = 71,004 recorded；5 次 recorded、2 次 unknown |
| 平台草稿/副作用 | revision 0；无 Draft mutation、测试、发布或客户写入 |
| 失败归属 | 实验任务作者初始化错误；不是 Lilies 模型、智能体框架或平台业务能力失败 |

随后只通过平台公开 API 把新应用加入现有两条 binding，并把权限 lane 改成 catalog 暴露的精确
operation。Binding revision 从 19 更新到 20；这是**公开运行配置改动**，不是平台代码改动，也
没有读取平台数据库。之后从另一全新空应用运行真实 DeepSeek V4 Pro：

| 项目 | 有效真实运行 |
|---|---|
| 应用 | `52e7854c-0d46-4626-a6ad-97066c653c63` |
| Assignment | `1dfc7a13-6f52-5908-87a6-bc372a40f609` |
| Build | `3609510e-b474-5f4e-9ddc-a869af77693b` |
| Session | `be00d560-fff4-5fa6-a52b-3c3b90cd71c0` |
| 提交 / daemon 终态 / 平台终态 | `22:06:11Z` / assignment deadline 前安全终止 / `22:17:02Z` |
| Token | 293,911 input + 46,553 output = 340,464 recorded；13 次调用中 11 次 recorded、2 次 unknown |
| 平台草稿/副作用 | revision 0；无 Draft mutation、测试、发布或客户写入 |

本轮验收计划、公开能力搜索、17 个积木手册预取和蓝图都完成；蓝图第一次遗漏
`workflow.json`，第二次按公开 expected/actual 补齐。首次完整 executable plan 遇到
`provider_output_serialization`，第三十七节的 forced/non-thinking/non-streaming 完整纠正确实
返回了对象。之后模型连续提交了六份大型分层计划，均在进入 Draft 前被同一个
`invalid_connector_nodes` 拒绝，直到 deadline。只读查询 standalone `LiliesAgent` 自己的会话
记录后定位到精确框架死锁（没有读取平台数据库、隐藏 Seed、oracle 或源码答案）：

1. 平台公开 catalog 的身份是 `connector:paperless:2:documents_retrieve`。
2. Lilies 节点解析器却把合法 `connector_action` 转成旧格式
   `connector:paperless@2.documents_retrieve`。
3. 校验器直接把旧格式节点身份和公开格式 catalog/蓝图身份比较，因此每个正确 Connector 节点
   永远被判非法。模型在两种格式、execution mode、actor role 和 auth 猜测间反复重写，但没有
   任何提示词能够解开框架内部自相矛盾的判定。

失败归属是 **LiliesAgent 内部 Connector identity contract bug**，不是平台缺积木、Connector、
API 或客户能力，也不是 DeepSeek V4 Pro 智力不足。平台代码改动为 **无**。运行结束后 standalone
daemon、Paperless、InvenTree、数据库、缓存、worker、broker、网络和长时环境控制进程均关闭；
平台主服务保留。

### 本轮智能体框架、Prompt/Skill 和门禁改动

1. 新增统一 Connector identity normalizer。公开冒号格式是唯一内部规范；旧 `@/.` 格式只作为
   历史输入兼容，进入蓝图、节点、nested plan、manifest、coverage 和 Draft 校验前一律规范为
   公开格式；畸形或重复表示继续拒绝，不猜测。
2. `connector_action` 节点解析器直接产生公开格式，消除 catalog、蓝图与节点三方身份分裂。
3. 完整结构化计划出现语义拒绝、且当前只暴露唯一计划工具时，纠正调用强制该工具并关闭
   thinking/streaming，避免再次返回旧工具名或无关自然语言。
4. 同一完整 executable plan 最多允许 3 次完整校验失败。第 3 次后记录
   `assignment.workflow_executable_plan_repair_exhausted` 并明确失败，不再让同类 30–45K-token
   全图重写耗满十分钟；一旦计划成功，计数清零。
5. 通用 `ten-minute-workflow-builder` Skill 同步规定只使用公开 catalog 冒号格式。规则没有项目
   名、固定 Connector、节点图、客户字段、Seed 或答案，适用于任何平台 Connector。

### 本轮测试与工具失败记录

- 第一次聚焦回归为 `30 passed, 4 failed`。四项都是旧测试仍期待 `@/.` 输出；实现实际统一输出
  公开冒号格式。保留若干旧格式输入作为兼容证据，只把规范输出断言改为公开格式，并新增公开
  输入保持不变、旧输入规范化、畸形输入拒绝的断言。
- 增加三次计划拒绝门禁测试后，第一次 Ruff 失败于新增 import 顺序；整理 import 后通过。这是
  测试文件格式问题，不是生产逻辑失败。
- Skill validator 第一次直接执行脚本得到 `permission denied`，原因是脚本没有 executable bit；
  改为用当前虚拟环境 Python 调用后返回 `Skill is valid!`，未修改校验器权限或内容。
- 最终聚焦回归 `35 passed`；完整 sibling 回归 `530 passed, 1 warning in 53.37s`；Ruff、
  Skill validator、`git diff --check` 均通过。

下一轮必须先复用同一公开 binding 初始化检查，再从空应用运行；重点验证首次完整计划能否一次
通过 Connector 身份校验、控制器能否无额外模型调用完成 compiler materialization，以及是否在
十分钟和 1M token 内进入真实业务测试与发布。

## 三十九、Connector 身份修复后的真实运行：单条输出哨兵边导致三次全图重写

第三十八节回归后启动新的空应用，并在提交前通过公开 HTTP API 完成环境预检：两条现有
Paperless/InvenTree binding 都包含精确 operation catalog；新应用通过公开 binding PUT 同时加入
两条 binding，revision 从 20 更新到 21；standalone LiliesAgent 连接公开 refresh 后为 connected。
这是运行配置变化，不是平台代码补能。

| 项目 | 结果 |
|---|---|
| 应用 | `b54b10d4-e23e-44fa-8f62-0d79ce68bca9` |
| Assignment | `2fe6e989-347f-5e3b-bb3d-d07e70e3dbec` |
| Build | `78982411-1dc2-5e36-9693-fdc4e6471b0c` |
| Session | `954d8573-26fa-5fb1-9f19-360bc459bf15` |
| 创建 / 终态 | `22:30:49Z` / `22:37:12Z` |
| 总时间 | 约 6 分 23 秒；第 3 次完整计划拒绝后主动失败，没有拖到十分钟 |
| Token | 167,282 input + 20,190 output = 187,472 recorded；8 次 recorded、1 次 unknown；低于 1M |
| 平台草稿/副作用 | revision 0、无 active version；无 Draft mutation、测试、发布或客户写入 |
| 终态 | `failed / daemon_session_error`，直接原因为完整计划三次严格校验失败 |

正向证据：约 77 秒内完成验收计划、能力目录、17 个公开积木手册、蓝图和空 Draft 锚定。第一次
大型 executable plan Provider 调用约 138 秒后发生 `provider_error`，用量正确记为 unknown；
第三十七节新增的恢复分支真实选择同一 plan tool、thinking=false、streaming=false，没有原样
重放。完整响应约 86 秒后返回。三次严格校验的 `connector_error_count` 全部为 0，证明第三十八
节的 Connector 身份规范化在真实 DeepSeek V4 Pro 上生效。

新的失败稳定且可复现：三个计划实际都只有 child edge index 22 非法。只读查询 standalone
`LiliesAgent` 自己的 transcript 后看到该边始终是
`assemble_record -> __output__`，而 child 已经用 `output_node_id=assemble_record` 正确声明终点；
17 个真实 child 节点全部有到该输出节点的路径。`__output__` 不是公开节点，因此这条边只是模型
额外生成的冗余哨兵表示。

框架反馈把该错误放大成无法有效纠正的两项：

1. `invalid_edges` 只返回 `[22]`，没有回显 source、target 或原因；模型连续三次保留原边。
2. 因 child 有任一图错误，校验器在汇总 Connector 前提前 `continue`，随后又派生
   `hierarchical_connector_coverage_gap`，错误声称全部 5 个 Connector 缺失；但同一 child 的
   `connector_plan_gap=[]` 且 connector node 均合法。这是派生诊断错误，不是实际能力缺口。

第二次计划仍被拒绝后，模型还调用了当前阶段未开放的 `lilies_workflow_draft_stage`；active-tool
门禁正确拒绝。第 3 次计划仍保留同一哨兵边，新加入的三次上限随即记录
`assignment.workflow_executable_plan_repair_exhausted` 并终止，证明防止疯狂迭代的门禁真实有效。

### 本轮智能体框架和 Skill 修复

1. 新增通用、无损的 nested-output normalization：child 已用真实节点声明 `output_node_id` 时，
   只删除从该同一节点到不存在的 `__output__` 的冗余边，并记录
   `assignment.workflow_plan_normalized`；任何其他未知端点、自环或非法 edge shape 仍拒绝。
2. child Connector 合同本身完整时，即使另有图错误，也把已配置 Connector 计入全局覆盖；不再
   派生“全部 Connector 缺失”的假错误。Connector 自身非法时仍不计入。
3. nested `invalid_edges` 改为返回 index、source、target 和明确 reason，便于一次局部纠正。
4. executable-plan 工具说明和通用 Skill 明确：child 输出只由 `output_node_id` 声明；每条 edge
   必须连接两个真实 child node，禁止额外合成 `__output__` 节点/边。没有项目名、固定节点 ID、
   Connector、客户字段、Seed 或答案。

### 实验和操作失败记录

- 环境预检中有一次只读 `rg` 范围误包含 state root 的 `platform-data`，输出带出旧 formal
  assignment 的公开 debug bundle 元数据。该内容没有用于本轮工作流设计；此后不再读取该目录，
  需求只读仓库公开需求包，平台状态只走 HTTP API。这是 Codex 操作边界失误。
- 第一次监控脚本误用 zsh 只读变量 `status`，在一次成功轮询后退出；构建会话未受影响。改用
  `build_state` 后继续监控。这是监控脚本错误，不是 Lilies 或平台失败。
- standalone daemon 用 Ctrl-C 正常完成 Uvicorn shutdown 后，Python 外层打印
  `CancelledError/KeyboardInterrupt`；客户容器、数据库、缓存、worker、broker、网络和环境服务
  均随后关闭。这是交互式停止的终端退出形态，不是运行失败原因。
- 新增/扩展定向测试覆盖冗余输出哨兵的无损删除、normalization 证据、未知端点仍拒绝、非法边
  详情以及图错误不再派生 Connector gap；定向回归 `35 passed`，Ruff 通过。
- 第一次完整回归为 `529 passed, 1 failed`，失败项是与本次 service/Skill 改动无关的 macOS
  process watchdog 50ms 边界测试：两次运行得到 `process_supervision_failed`，而等价取证脚本和
  紧接着的原单测得到预期 `process_timeout`。未修改该无关模块或放宽断言；完整套件重跑最终为
  `530 passed, 1 warning in 51.67s`。Skill validator 返回 `Skill is valid!`，
  `git diff --check` 通过。

本轮平台代码改动为 **无**。直接失败归属为 `LiliesAgent nested graph normalization + diagnostic
projection`，不是平台业务能力、DeepSeek V4 Pro 智力或 Connector 能力不足。下一次空应用运行
必须验证 normalization 事件出现后同一计划能直接进入 controller materialization，而不是再次
调用模型重写全图。

## 四十、图修复后的真实运行：完整计划与物化成功，嵌套公开 Schema 未前置执行

第三十九节回归后再次从全新空应用运行。新应用通过公开 binding PUT 加入 Paperless/InvenTree，
两条 binding revision 从 21 更新到 22，standalone 连接公开 refresh 成功。平台代码改动为
**无**。

| 项目 | 结果 |
|---|---|
| 应用 | `9e819c90-bace-468a-9b05-a6e5aec14ed0` |
| Assignment | `5c683d66-34ac-5e90-9f12-c45510a97d53` |
| Build | `1505a808-5ea5-51e3-9beb-4b10f42d99ea` |
| Session | `1c4a4e2a-8484-55b8-ac97-582c82d213cb` |
| 创建 / 主动取消 | `22:44:01Z` / `22:49:28Z` |
| 总时间 | 约 5 分 27 秒；重复 repair 协议错误后主动取消，没有等待十分钟 |
| Token | 319,159 input + 23,204 output = 342,363 recorded；14 次 recorded、2 次 unknown；低于 1M |
| 平台草稿 | revision 10、无 active version；前 10 个操作成功，第 11 个公开操作失败后停止 |
| 客户副作用 | 无运行、发布或 Connector 客户写入；仅应用 Draft 元数据前缀发生公共平台变更 |

首次大型 executable plan 调用再次在约 150 秒发生 Provider 错误，框架按合同切换 forced、
non-thinking、non-streaming。返回的第一份完整计划只有 1 个 nested issue；第二份计划成功提交：
12 个顶层节点、13 条顶层边、23 个 child 节点、29 条 child 边、33 个 Draft operation。控制器
随后无需模型逐节点决定，自动完成 child nodes、edges、parent config 和完整 Draft compile，证明
第三十九节的图诊断/规范化改造没有引入回归，复杂分层计划和物化能力已真实通过。

公共 Draft 在第 11 个操作 `add_node:write_xlsx` 停止；前 10 个 operation 已按 revision 链提交，
pending suffix 未继续，事务游标安全。平台公开错误是 `TypedWorkbookConfig` 的 27 个校验问题：
每个 column 要求 `key: string` 且禁止额外字段，模型却为列使用 `path: string[]`。公开
`typed_workbook` 手册的 `config_schema` 已通过 API 明确暴露 `$defs/WorkbookColumn`、required
`key/header` 和 `additionalProperties:false`；因此这不是平台缺能力或手册缺信息，而是
LiliesAgent 原先把手册压成扁平字段 ledger 时没有解析 `$ref/$defs`，导致计划前置校验看不到
嵌套 column 合同。

partial commit 后，框架唯一开放并 forced `lilies_workflow_draft_repair`，但 DeepSeek 连续返回
旧的 `lilies_workflow_draft_stage`、`lilies_workflow_draft_commit` 和
`platform_draft_inspect`。其中一份 stale stage 全图已经把 workbook column 改为正确 `key`，
说明模型理解了公开错误；只是没有遵守当前小型 `edits` repair 协议。active-tool 门禁始终拒绝
这些错误调用，没有重放前 10 个已提交操作；但 repair 阶段没有独立响应上限，短时间内产生了
多次约 24K-token 调用。监控看到同类错误持续后通过公开 cancel API 主动终止。

### 本轮智能体框架与 Skill 修复

1. 公开 block manual 增加有界的完整 schema contract：只保留可执行 JSON Schema 结构，解析
   local `$ref/$defs`、`anyOf/oneOf/allOf`、properties/items/required、
   `additionalProperties`、enum/const、类型、长度、数值和 pattern；不保留说明性大文本。
2. executable-plan config 前置校验改为递归验证该 schema；分支按真实对象 key 重合度选择最相关
   失败分支，返回 node、block、完整 config path、reason、expected、actual。旧扁平 ledger 仅作
   无完整 schema 时的兼容 fallback。
3. `$ref` 也在公开扁平阅读摘要中展开，使 Builder 可直接看到
   `spec.sheets[].columns[].key`，但确定性验收仍以完整嵌套 schema 为准。
4. Draft repair 增加独立 Provider-response 上限：进入一个 failed identity 的 repair 后，任何
   response 仍未成功应用 repair（包括 stale/future tool、空参数或非法 edits）都计数；第 3 次
   记录 `assignment.workflow_draft_repair_exhausted` 并终止，不再循环到 deadline。成功 repair
   清除状态。
5. 通用 Skill 同步完整嵌套 schema 前置执行与三响应 repair 上限。规则不含 typed_workbook 专用
   转换、项目节点、客户字段、Connector、Seed 或答案。

新增定向证据用一个最小公开 schema 复现 Workbook/Reference `anyOf` 和本地 `$ref`：错误
column 同时得到 missing `key` 与 forbidden `path`，且扁平摘要包含 resolved key 路径；修复后
定向回归 `36 passed`，Ruff 通过。第一次完整回归为 `530 passed, 1 failed`；失败是已有
compact-manual 精确字典断言尚未包含新加入的去说明文字 `config_schema_contract`，不是生产行为
错误。更新通用期望后，assignment + context-window 聚焦回归 `44 passed`；Ruff、Skill
validator、`git diff --check` 通过。完整套件随后重新运行并据实补记。
最终完整 sibling 回归为 `531 passed, 1 warning in 50.77s`。

运行停止后 standalone daemon、Paperless、InvenTree、数据库、缓存、worker、broker、网络和
环境服务均关闭。下一次空应用必须证明 workbook 错误在 Draft revision 0 的计划阶段被拦截并
纠正；若仍进入 partial commit，则 repair 最多三个响应后明确失败。

## 四十一、完整 Schema 修复后的真实运行：失败编译上下文被误写成进展

第四十节回归后从另一全新空应用运行 standalone `LiliesAgent`。新应用通过平台公开 binding
PUT 同时加入 Paperless/InvenTree，两条 binding revision 从 22 更新到 23；平台代码改动为
**无**，binding 是公开运行配置。模型仍为 DeepSeek V4 Pro，Builder 只读取公开需求、公开积木
手册、公开 schema 和当前授予的平台 API。

| 项目 | 结果 |
|---|---|
| 应用 | `14ea87de-7a0f-4adb-9910-90e7aafa3e9a` |
| Assignment | `a42e30bd-efeb-58b2-af90-3e4239fedcb3` |
| Build | `10a06d12-0fd6-5a34-9d91-c7ce602813aa` |
| Session | `88fd573e-41a4-5848-a1c8-a63a979b22f1` |
| 创建 / 主动取消 | `22:58:26Z` / `23:04:32Z` |
| 总时间 | 约 6 分 05 秒；确认同一协议错误已经重复后主动取消 |
| Token | 201,997 input + 17,731 output = 219,728 recorded；9 次 recorded、2 次 unknown；低于 1M |
| 平台草稿/副作用 | revision 0、无 active version；无 Draft mutation、测试、发布或客户写入 |
| 终态 | `cancelled`；任务未完成，不能把结构预检通过当业务通过 |

验收计划、公开目录、精确积木手册和架构蓝图均完成。第一份 executable plan 的 streaming
provider 调用发生一次可重试错误；框架按既有合同切换到唯一计划工具、thinking=false、
streaming=false，完整恢复调用成功，证明 provider 恢复路径仍有效。完整嵌套 schema 前置校验也
确实在 Draft revision 0 拦截了错误，没有重演第四十节的 partial commit。

被拦截的公开配置错误分三类：

1. 模型把运行时数据输入误放进静态 config：三个 normalize 的 `value`、artifact/validate 的
   `value`、两个 record_match 的 `source/candidates` 都被公开 schema 的
   `additionalProperties:false` 拒绝。这些应由上游边或引用传入，属于模型计划错误，校验器不应
   放行。
2. 三个 Connector 合同未完整覆盖；事件记录 `connector_error_count=3`。完整 plan 因而需要真实
   修正，而不是直接物化。
3. 顶层 iteration 壳被报告缺少 `workflow` 和 `output_node_id`。这一项是框架自己的矛盾：分层
   编译合同明确禁止壳提前携带这两个字段、要求 child plan 稍后物化，但新完整 schema 校验又把
   它们当作当前壳的必填项。

第一次 plan 拒绝后，当前唯一授予工具是
`lilies_workflow_executable_plan_commit`，Provider 层也确实强制选择它；但 DeepSeek 连续四次返回
旧 `lilies_workflow_draft_stage`。active-tool 门禁每次正确拒绝，接口没有缺失，也没有执行错误
Draft 操作。真正让循环持续的框架缺口有两层：

- 旧的“三次上限”只统计完整 plan 内容被校验拒绝；调用错工具不增加该计数，所以四个约
  26K-token 的无效响应没有熔断。
- 上下文压缩把被拒绝的 compiler 调用统一改写为“已提交本地编译动作”，随后丢掉完整失败
  exchange。这个摘要把失败伪装成进展，和 active-tool 的纠正提示相互矛盾，强化了 stale tool
  重复。失败原因属于 **LiliesAgent 状态机和上下文编译器**，不是平台能力、客户环境或项目业务
  逻辑。

### 本轮智能体框架改动

1. executable-plan 阶段改为 Provider-response 统一预算。只要计划工具是唯一当前动作，完整计划
   内容错误、stale/future tool、以及没有 tool call 的纯文本响应都计入同一个失败计数；第 3 次
   记录 `assignment.workflow_executable_plan_repair_exhausted` 后明确停止。成功提交计划立即清零。
2. compiler exchange 压缩保留真实 outcome。成功才写“已提交”；失败写明“平台拒绝、没有动作
   应用”，并把当前唯一授予工具保留进短摘要，禁止重复 stale tool。事件新增 `outcome` 字段，
   使后续会话和调优报告可区分 accepted/rejected。
3. 完整 config schema 校验增加严格限定的 nested-shell 延后规则：只有在同一 executable plan
   中明确拥有 child plan 的 iteration/loop 节点，才忽略根级 `workflow`、`output_node_id` 缺失；
   `items` 等其他必填项、额外字段、类型、枚举和所有普通节点继续严格校验。它没有按项目名、
   节点 ID 或业务字段硬编码。
4. 用户在本轮之后取消十分钟硬限制。Skill 从 `ten-minute-workflow-builder` 更名为通用
   `bounded-workflow-builder`：时间改为默认效率指标，显式 assignment deadline 才是硬停止；当前
   实验仍按用户此前要求保持每项目 1M token 硬上限。三响应熔断保留，因为它约束无界 token
   循环，而非追求十分钟数字。

### Prompt / Skill 经验

- 当前授予工具列表是状态机事实；Prompt 只负责导航，runtime 必须再次校验并限制失败响应。
- durable context compaction 必须保存“是否真的产生平台进展”，不得用中性/成功语句概括失败。
- Schema 校验必须理解编译阶段所有权：延后字段仅在已有可验证的后续 materialization 合同时
  延后，不能全局放宽必填项。
- 时间放宽不能变成无限推理；token、重复语义签名、阶段响应次数和 provider retry 仍需硬界限。

### 测试、Codex 操作错误与证据

- 新增 plan-response helper 和失败 compiler summary 定向测试，首轮 `2 passed`；随后完整回归
  `532 passed, 1 warning in 54.61s`。
- 新增 nested-shell 所有权测试。第一次补测试时 Codex 把新测试函数插在现有测试中间，导致
  `state/declared/top_nodes` 的 `NameError` 和 Ruff `F821/F841`；移动到原测试结束后，定向回归
  `4 passed`。这是测试编辑错误，不是产品逻辑失败。
- Codex 最初两次运行完整回归时没有保留执行工具返回的 session id，把仍在后台执行的 pytest
  误当成已结束并重复启动，造成一次不必要的并发 CPU 消耗。确认进程后只保留一个；后续统一用
  session id 轮询直到拿到 exit code，不再从截断进度推断成功。
- 最终完整 sibling 回归为 `533 passed, 1 warning in 54.39s`；Ruff、Skill validator 和
  `git diff --check` 均通过。

该真实运行仍是失败证据，不宣称项目一完成。下一轮从空应用验证三件事：错误计划能否在有限
响应内修正；失败摘要是否阻止 stale tool 重复；成功计划是否进入 controller materialization、
真实业务测试和发布。时间只记录，1M token 仍为当前单项目硬上限。

## 四十二、取消十分钟硬限后的框架解耦与启动失败记录（进行中）

用户明确把成功条件从“十分钟内”改为“在有限 token 内完成”；本实验继续使用此前确认的每项目
1M token 上限，elapsed time 只作为效率观测。第一次直接删除 build 的 `deadline_at` 后，平台
公开请求被 daemon 拒绝；后续证据证明最初的“daemon 必须 deadline”判断不是最终根因，真正
原因是 Codex 手工重启 standalone 时漏掉了 Workflow Studio/model 环境。错误推断和纠正过程均
保留，不能用后来的根因覆盖早先操作失败。

### 智能体框架与 Skill 改动

1. standalone 的可靠 staged controller 原来只在 `deadline-created_at <= 15m` 时启用。新增
   `finite_workflow_delivery` 判定，使无短 deadline、但有有限 session-token 合同、required
   workflow 和额外业务工件的复杂 Builder assignment 仍走 acceptance → blueprint → executable
   plan → controller materialization。deadline 只负责时间停止，不再是可靠框架的唯一开关。
2. 第一个实现把条件扩得过宽，完整回归出现 `12 failed, 522 passed`：只有 workflow 交付物的
   旧普通 assignment 也被切进 staged controller；两项纯 helper 测试还因内部 keyword 从
   `short_assignment` 改名而失败；另一个 development CLI 进程测试出现一次 409 状态时序冲突。
   这说明“有限 token”不能等价于“所有 assignment 强制分层编译”。
3. 收紧后的合同保持旧行为：原 ≤15 分钟 assignment 继续使用已有 bounded 行为；无短 deadline
   时，只有 required workflow + 至少一个额外业务工件 + finite token 的复杂工作流启用 staged
   controller；内部 helper keyword 保持兼容。assignment 回归 `39 passed`，完整回归最终
   `534 passed, 1 warning in 55.64s`，此前 development CLI 409 未复现。
4. `bounded-workflow-builder` Skill 同步规定：可靠状态机由有限 token 与 required workflow
   交付合同启用，而非任意短时间；失败 compiler 摘要必须保留 rejected outcome；计划阶段三次
   无效响应熔断。无项目名、固定节点、字段、Connector、Seed 或答案。

### 平台通用可观测性补能

平台原先把所有 daemon 4xx/5xx 统一投影成 `daemon_rejected`，任务作者只能看到“local Lilies
rejected the persisted operation”，无法区分 schema 422、路由 404 或权限失败。新增最小安全投影：

- 只在结构化 `LocalLiliesRemoteError` 上返回 `daemon_status_code`；
- FastAPI validation 只返回最多 20 个 `{location, type}`，每个 location 有长度、字符和深度
  上限；
- 不返回 `input`、`ctx`、URL、自由文本 message、异常字符串、凭据或 daemon payload；
- 持久化 last_error 仍保持通用文本，不扩大数据库敏感面。

定向测试用包含 `SENSITIVE` message/input 的 422 验证公开结果只有
`body.constraints.deadline_at / missing`，另用非结构化 409 验证只返回状态码；`2 passed`、Ruff
通过。该补能是平台通用诊断能力，不改变工作流积木、运行时、Connector 或项目一答案。

### 空应用与 0-token 初始化失败

新应用 `a5474923-92dd-47b6-98b2-2022437e91be` 创建成功，Paperless/InvenTree 公开 binding
revision 从 23 更新到 24，均包含新应用。第一次创建请求误用不存在的
`delivery_mode=autonomous`，公开 API 422 后改用 `quick`；无应用在失败请求中创建。

下列 build 全部在 model/session 开始前失败，token=0、Draft revision=0、无客户副作用：

| Build | Assignment | Session | 表面证据 |
|---|---|---|---|
| `9dd01a4f-97f4-5faf-ab11-fd8efe9a857f` | `b1876d60-0109-5b77-a8af-75ba6b5db97b` | `9111ef67-b7e8-5403-8bdd-90fbbb04e85e` | 无 deadline；旧平台只报 daemon_rejected |
| `f878bbd7-90d9-549c-9b4f-05dd27e6e8d6` | `f061e597-7ade-518c-8f5f-c598c3a65dc5` | `ce84cee3-9133-570d-8653-f17b601209f1` | 2 小时安全 deadline；仍为通用拒绝 |
| `b74b7507-d30d-5f08-93e7-4bbf9c8d144a` | `8c0156d2-01f0-527e-a05a-ed84598f8e8a` | `cf58241c-a7d3-53bf-9a66-edb8a5a2c2f3` | refresh 后仍拒绝，排除旧应用权限投影 |
| `f2f2aa25-f0ec-52cc-97c0-41fe4793cce2` | `c0e030a9-52c0-51b2-8f33-c154bdf3a0c5` | `370a3290-d9d7-54b0-9240-d2770e0b206c` | 新安全投影明确 `daemon_status_code=404` |

404 发生在 assignment phase `recorded` 的第一步 `POST /local/v1/sessions`。standalone 健康接口
随后显示 `model_egress_enabled=false`；Codex 重启只复用了 CLI 参数，没有复用原进程的
`LILIES_WORKFLOW_STUDIO_ENABLED=true`、DeepSeek profile、1M token 和 Skill 环境，因此 Studio
session 路由未注册。这四次是 **Codex 启动编排错误**，不是 Lilies 模型、平台业务能力、deadline
合同或项目一失败。

历史 standalone session 只读证据确认模型 profile 是 `deepseek-v4-pro`。API key 不在仓库、
state root、shell、launchctl 或 standalone DB；macOS Keychain 已有
`com.lilies.desktop.provider-api-key / deepseek`。新启动命令已设置 Studio、model egress、
`deepseek-v4-pro`、1M token、skills_dir 和 `bounded-workflow-builder`，但当前停在 macOS Keychain
一次性读取授权；Computer Use 按安全策略不能操作 `SecurityAgent`。用户批准一次“允许”后继续，
不需要重建应用或环境。

### 操作边界与工具失败

- 平台重启第一次误在 `platform/backend` 执行 `./.venv/bin/python`，该目录没有 venv，立即
  exit 127；随后在仓库根使用已验证命令启动。没有修改状态。
- 一次 `find` 输出格式命令的 BSD `sed` 表达式写错，产生 `bad flag`；只影响文件名显示。
- 为定位 `daemon_rejected` 的一次仓库级 `rg` 范围过大，输出命中了历史 evidence 文档中的隐藏
  包路径与 digest 元数据。没有打开隐藏文件、Seed 或 oracle 内容，没有用于 Builder 或代码决策；
  此后所有搜索限定到精确源码文件。该行为违反本实验最小读取纪律，必须保留为 Codex 边界错误。
- Keychain metadata 检查只读取 service/account 名称；密码读取通过命令替换直接注入 daemon
  环境，标准输出不打印密钥。当前命令仍等待用户授权，尚未取得或发送密钥。

## 四十三、最终取消十分钟硬限与产品内凭据启动路径（进行中）

用户再次明确：十分钟不再是单项目停止条件，只要求在有限 Token 内完成。当前实验因此使用以下
边界：每个 Lilies Builder 项目继续硬限 `1,000,000` Token；elapsed time、阶段耗时和重复响应
仍完整记录，用于效率调优，但不会因为超过十分钟主动取消一个仍在产生真实平台进展的任务。
无进展重复、阶段响应上限、语义重复和 Provider 重试仍须有界，不能把取消时间限制解释为允许
无限循环。

本轮恢复后确认先前命令行钥匙串读取持续等待约五分钟，daemon 尚未启动，模型调用与 Token
增量均为零。Codex 主动终止该等待，先用完整 Workflow Studio、DeepSeek V4 Pro、1M Token 和
`bounded-workflow-builder` 配置启动一个 `model_egress_enabled=false` 的 daemon；公开 health
返回 `status=ok`，证明 Studio 启动配置与 daemon 本体可独立于 Provider 凭据正常启动。

随后检查独立 macOS 客户端实现，确认它已经具备正确的产品路径：凭据由
`KeychainProviderCredentialStore` 保存，用户显式启用时由桌面端读取并通过认证 daemon API
注入内存；禁用时同时清除 daemon 内存和钥匙串副本。Codex 尝试通过允许的 Computer Use
provider 只操作 `LiliesDesktop.app`，但原生 UI provider 在读取任何界面前持续无响应，约一分钟
后主动终止；没有读取、输入、传输或修改凭据。该现象与既有 `V0413-ED-004` 环境债务一致，
不应反复重试或误判为 LiliesAgent 产品失败。

为继续真实模型实验，Codex 正常停止出口关闭的 daemon，再次发起一次 macOS Keychain 读取；
密钥只通过命令替换进入 daemon 环境，标准输出不打印。当前仍等待用户在系统弹窗中点击一次
“允许”，尚未产生模型调用。授权后将直接复用现有 fresh app、客户环境和公开 binding 启动
项目一，不重建已验证的环境。

### 有限 Token 实验 1：公开 schema 压缩自相矛盾

用户授权钥匙串读取后，daemon 以 `deepseek-v4-pro`、Workflow Studio、1M Token 和
`bounded-workflow-builder` 正常启动；平台 connection refresh 成功。复用仍为 revision 0 的
fresh app `a5474923-92dd-47b6-98b2-2022437e91be`，启动新 assignment
`d2b687fb-041c-516a-8bf7-f3a4111b9d22`、session
`eaff5b61-1dab-539a-b70e-4755c0af60e4`。请求未设置十分钟 deadline，平台当前通用默认投影为
一小时安全 deadline；实际失败发生在约五分半，和 deadline 无关。

该运行完成验收计划、读取 15 个真实积木手册、提交完整业务蓝图，并在三次可执行计划修复上限
后明确失败。权威终态为 7 model calls、23 tool calls、输入 158,081、输出 30,953、总计
189,034 tokens、unknown calls 0；Draft revision 0、无发布版本、无客户写入。蓝图已正确覆盖明确
匹配、低置信度、缺字段、数量冲突、未知物料、重复、403 和混合批次，并只选择 assignment
授权的 `attachment_create`，因此任务理解和公开能力发现不是失败点。

根因是 LiliesAgent 的通用公开 schema 压缩器：Pydantic 对任意 JSON 值字段公开
`{"title":"Value"}`。压缩器删除 prose-only `title` 后把整个 property 删除，但根 schema 仍保留
`required:["value"]` 和 `additionalProperties:false`。结果同一个校验器在字段存在时报告
`additional_property`，模型删除后又报告 `required`；第三次只能回到第一次，无法产生合法计划。
`regex_extract.text`、`record_match.source/candidates` 和 typed artifact `value` 同受影响。这不是项目
一字段或积木专用问题，任何带 unconstrained JSON input 的公开 schema 都会触发。

修复把已访问的空 schema `{}` 作为有效合同保留；只有达到深度/节点边界时仍返回 `None`，不会
用空 schema 掩盖截断。新增反例证明 property identity、required 和
`additionalProperties:false` 三者同时保持一致。定向测试 `2 passed`，assignment/context 完整
回归 `48 passed`，最终 standalone 全量 `535 passed, 1 warning in 57.26s`，Ruff 通过。该改动
没有项目名、业务字段、节点 ID、Connector、Seed、adapter、映射或最终图。

### 有限 Token 实验 2：复杂计划被旧短任务单调用时限截断

只带上述 schema 压缩修复启动 assignment `fb216c8a-0ee7-51bd-8c85-6ff240e0183b`、session
`adc679d1-90e7-55ff-a06f-a652b01e46ab`；其余需求、权限、fresh app、模型和 Skill 不变，构成
单变量对照。Lilies 再次完成验收计划、15 份手册和蓝图，并主动加入 LLM 结构化抽取与
`documents_metadata_retrieve`。第一次大体量 executable plan 调用从
`00:33:36.897Z` 运行到 `00:36:06.899Z`，精确 150 秒后由框架终止；没有产生可校验 plan，
因此本轮未再出现上一轮的 schema 互斥错误。

终态为 5 model calls，其中 4 次有权威 usage、1 次因 provider timeout 为 unknown；已记录调用
输入 69,817、输出 6,176、合计 75,993 tokens，20 tool calls，Draft revision 0、无客户写入。
失败文本为 `model provider request exceeded the assignment call deadline`。根因是旧的
`_SHORT_BUILD_PROVIDER_CALL_TIMEOUT_SECONDS=150` 被错误套用到所有 staged workflow：即使任务
不是十分钟短任务、只由有限 token 合同启用可靠 controller，仍被强制使用 150 秒；配置本身的
Provider timeout 是 600 秒。

修复把两种边界分开：真正的短 deadline assignment 继续使用 150 秒单调用上限；非短时、有限
Token 的复杂工作流使用配置的 Provider timeout（当前 600 秒），同时仍受总 assignment deadline、
1M session-token 预留和 Provider 自身 HTTP 总时限约束。retry 也用同一规则重新计算剩余时长，
不会绕过总体截止时间。新增测试覆盖一小时/无 deadline 的 600 秒和十分钟任务的 150 秒；
49 项 assignment/context 回归、Ruff 通过，standalone 全量最终
`536 passed, 1 warning in 54.83s`。改动只依据时间与 token 合同，不含任何项目业务信息。

### 有限 Token 实验 3：完整计划成功，但部分写入后陷入无效修复循环

只带前两项通用修复启动 assignment `3d389ca1-ac16-58c2-a830-5d2ba43d49d6`、session
`f2b5283b-f211-5dc3-a504-11fdd5b00aee`。Lilies 完成验收计划、公开能力映射和完整可执行计划；
计划包含 20 个顶层节点、26 条边、5 个业务测试，共编译为 51 个公开 Draft 操作。Controller
成功应用前 14 个操作，应用 `a5474923-92dd-47b6-98b2-2022437e91be` 从 revision 0 推进至
revision 14。这证明 schema 空对象保留与非短任务 600 秒 Provider 边界都已生效。

第 15 个操作 `add_node:create_attachment` 被公开 Draft API 拒绝，安全投影为
`invalid_request / body.node.error_strategy / enum`。此后模型多次修改 config 内的同名字段，或把
节点级值改为仍不合法的字符串；平台游标始终没有越过该节点。终态为 17 model calls、56 tool
calls、输入 401,369、输出 16,820、总计 418,189 tokens、unknown calls 0；Draft revision 14，
未运行客户 Connector、未发布版本。因此这是失败实验，不是项目一交付。

根因有两个，均为通用智能体框架问题：

1. Builder 只在完整计划阶段校验了各积木 `config` 手册和自定义的宽松 node envelope；
   `error_strategy` 是平台 Draft 的节点级字段，不属于 connector_action config。公开
   `platform_draft_apply` schema 已有真实枚举，但框架直到写到第 15 个操作才让平台暴露错误，
   造成不必要的部分 Draft。
2. repair tool 接受一个语法合法的字段补丁后，框架立即把 repair-required 清空。下一次同一
   mutation 再被平台拒绝时，又创建 response/repair count 为零的新状态。于是“补丁被接收”被
   错当成“平台修复成功”，三响应熔断被反复刷新；最终只是连续 stale commit 恰好耗尽预算。

对应的通用修复：

- 在完整 executable plan 被接受前，把生成的每个 `add_node/add_edge/add_test` 操作重新投影到
  当前公开 `platform_draft_apply` 输入 schema，逐条执行确定性合同校验。任意错误都会返回操作
  序号、稳定 identity、字段路径、公开 expected/actual；整批保持本地，平台 Draft 一行不写。
  这同时覆盖 node-level 字段、wrapper、edge 和 test 合同，不依赖项目或某个枚举特例。
- 将 repair 成功标准改为“下一次公开 commit 接受同一 mutation 并推进游标”。语法合法的补丁
  只进入 pending verification；若同一 identity 再失败，原 response/repair count 保留并增加；
  只有操作成功或真正进入另一个失败 identity 才清零。
- `bounded-workflow-builder` Skill 同步加入整批公开 mutation schema 预检与平台验证后才算 repair
  成功两条规则。确定性校验与预算仍由 runtime 强制，Skill 只帮助模型理解错误所属层。

新增回归分别证明非法节点级枚举在写入前返回精确允许值，以及同一 identity 的补丁验证失败后
计数从既有值递增、换成新 identity 才归零；现有分阶段构建、部分提交修复、测试和发布集成用例
一并通过。定向结果为 `3 passed`，Ruff check 通过。下一轮必须从新的空应用开始，避免把
revision 14 的失败前缀伪装成从零构建；仍使用相同公开需求、DeepSeek V4 Pro、1M Token 上限和
客户环境，不读取隐藏 Seed、oracle 或数据库。

修复完成后的 assignment/context 回归为 `51 passed, 1 warning in 8.10s`，standalone 全量回归
为 `538 passed, 1 warning in 59.14s`，`git diff --check` 通过。全量数量相较实验 2 增加两项，
分别对应公开 mutation schema 写前校验和 repair 验证状态延续；没有删除或放宽既有测试。

### 有限 Token 实验 4：写前校验生效，但框架错误关闭语义修复思考

使用上述两项实验 3 修复后，从新的空应用 `7a85f7da-818e-4912-ab9c-db9241c703b0` 启动 build
`14b13e2a-1e85-5ffc-a96c-63fa17a96e53`、assignment
`85a984af-a91c-5b56-8cea-2b48aee1b066`、session
`a0a45191-1490-5bfa-9e75-1720c8b32625`。Paperless/InvenTree binding revision 25 均通过公开 API
纳入新应用；需求、权限、DeepSeek V4 Pro、Skill、客户环境和 1M Token 上限与上一轮一致。

Lilies 在约 5 分 29 秒内完成验收清单、目录发现、10 节点分层业务蓝图、15 份手册预取、空草稿
确认和第一次完整可执行计划。第一次大计划调用约 3 分 37 秒，成功产生工具响应，没有触发 600 秒
上限。新的写前校验在平台 mutation 前拒绝该计划：10 项公开 Draft 合同错误、1 项 nested workflow
错误；草稿保持 revision 0，证明实验 3 的“整批写前拒绝”有效，没有再产生 revision 14 的部分图。

但第一次失败后的两次框架调用都被记录为
`forced_tool_name=lilies_workflow_executable_plan_commit / thinking_enabled=false / streaming=false`。
第 6 次调用提交了第二份计划，但错误数量原样保持 10+1；第 7 次返回历史
`lilies_workflow_draft_stage` 名称，被 active-stage 门禁拒绝。三响应预算随后正常停止。权威终态为
7 model calls、21 tool calls、输入 125,584、输出 23,860、总计 149,444 tokens、unknown calls 0；
应用无版本、无客户 Connector 写入。

这次失败的直接根因是 LiliesAgent 把“只有一个允许的语义修复工具”误等同于“机械动作”：
DeepSeek 的 forced-tool 协议要求关闭 thinking，因此模型面对 10 个 schema 路径和子流程图错误时
被强制跳过推理、立即序列化完整计划。该策略原本为了避免 stale tool，却剥夺了真正修复所需的
判断能力；实验 3 中 Draft repair 反复改错层级也受同一策略影响。接口没有缺失，平台没有拒绝
合法交付，基座模型第一次计划也正常完成；失败属于智能体框架的 reasoning/tool-choice 分层错误。

通用修复如下：

1. `executable plan` 与 `draft repair` 都从 mechanical forced-tool 集合移出。语义失败时仅保留
   当前工具列表和三响应预算，不再强制 DeepSeek non-thinking；模型必须先解释公开 expected/actual
   再提交修正。已经提交的节点、边、测试、commit、运行和发布仍由 controller 零模型复制，
   serialization transport retry 仍可单独使用受限 non-thinking。
2. 每次完整计划失败生成安全 repair capsule：公开 mutation 只保留操作序号、identity、节点、
   字段路径、reason 和小型公开 expected；nested 错误只保留父节点、结构缺口、图连通性和安全化
   config 路径。业务 actual 值、配置内容和秘密不进入公开事件。下一轮 Prompt 明确要求所有列出
   项消失或数量下降，禁止跳到后续 compiler 工具。
3. 平台 owner 当前 messages 视图只显示 `tool_result is_error=true`，无法看到安全原因；新增的
   `assignment.workflow_executable_plan_rejected` capsule 通过既有公开事件流提供可追踪摘要，满足
   后续会话、Prompt/Skill 和平台 UX 调优，不读取 daemon DB 或私有思维链。
4. `bounded-workflow-builder` Skill 修正此前自相矛盾的表述：单一工具只约束动作，不代表参数已
   确定；语义修复保持 reasoning，只有复制既有参数或纯 serialization retry 才关闭 thinking。

新增安全 capsule 测试证明路径/允许值被保留而 `customer-secret-value` 和嵌套 business actual
被删除；原 compiler/tool-stage 测试改为要求 semantic repair 不强制 non-thinking。定向 4 项与
assignment/context 52 项通过，standalone 全量 `539 passed, 1 warning in 55.40s`，Ruff 和
`git diff --check` 通过。该结论由同一真实 session 的七条 `provider.call_mode_selected` 事件直接
证明，不是事后猜测。

### 有限 Token 实验 5：增量编译器已开始保留节点，但按用户要求中止

在把顶层工作流从“一次提交完整 executable plan”改为 1–4 个同质增量 chunk 后，从空应用
`7a85f7da-818e-4912-ab9c-db9241c703b0` 启动 build
`6f798c6c-73a0-5988-bebf-a776d492f751`、assignment
`eaae2c0e-98a8-56cb-8301-543abde4ee60`、session
`fef39bfc-dc77-5581-954e-980fe4501d4e`。模型为 DeepSeek V4 Pro，单项目 Token 上限仍为
1,000,000；没有十分钟硬停止。

本轮先完成验收计划、公开目录/工具读取、10 个顶层节点与 11 条边的业务蓝图、公开手册和空草稿
确认，随后成功接受第一批 3 个顶层节点。记录到的逐调用 Token 分别为 967、7,323、17,202、
25,401、35,406，另有首次序列化失败调用用量未知；已知合计 86,299。这里的单条 `total_tokens`
是每次模型调用用量，不是会话累计值。应用的公开 Draft 仍为 revision 0，因为这些节点只进入本地
编译器，还没有到完整图的一次公开提交；客户 Connector 没有运行，也没有发布版本。

用户要求先依据仓库内 Claude Code 源码重构智能体框架，因此通过公开 assignment cancel 路径中止
本轮；终态 phase/status/daemon_status 均为 `cancelled`，不是模型自主失败。取消时已接受的三个
节点和完整公开会话事件保留为对照证据，不把它们冒充平台交付。

### Claude Code 源码对照后的通用智能体循环重构

授权参考实现为仓库中的 `references/claude-code`。本次只读取其通用会话、工具编排和上下文管理
源码，没有从参考实现或平台源码寻找项目一答案。直接核对的机制包括：

- `src/query.ts` 在一个持续循环里把 assistant tool use 与全部 tool results 追加到同一 transcript，
  再交给下一次模型调用；普通循环不按业务阶段强制一个 tool choice。
- `src/services/tools/StreamingToolExecutor.ts` 保证每个 tool use 都有配对 result，未知工具、异常和
  abort 产生可恢复的合成结果；部分流式调用失败时丢弃未完成调用，避免孤儿 result ID。
- `src/services/tools/toolOrchestration.ts` 只并发安全只读调用，写调用保持串行和原请求结果顺序。
- `src/QueryEngine.ts` 明确持有跨迭代 state、transition、turn count、usage 和 transcript，并把安全
  事件持续写入会话。
- `src/services/compact/microCompact.ts` 与 `src/utils/toolResultStorage.ts` 保留 tool use/result
  结构，只缩旧的大结果；
  最近错误与可恢复状态不被一段“成功摘要”替换。

据此完成的 LiliesAgent 通用改动：

1. 把“assignment 可用工具”与“当前推荐动作”拆开。平台授权和本地编译器能力在整个 assignment
   内稳定可见，`recommended_next_tools` 只说明最小下一步；每个工具在执行时验证自己的状态前置
   条件并返回成对错误，不再靠单工具菜单把模型锁死。
2. 不再向模型广告已被增量编译器取代的完整 executable-plan 大对象。顶层节点、边、测试和每个
   子图逐块保留，错误只影响当前 chunk；已接受内容不会要求重写。
3. assignment 主循环不再使用 forced non-thinking 恢复。畸形 JSON 会丢弃未执行的部分调用，在
   原 transcript 和稳定工具集上追加一次小型纠正并保持 reasoning；普通 transport retry 也不把
   工具缩成单例。
4. 实际存在 tool-use block 时以该事实为准；Provider 报错的 stop reason 只记录归一化事件，不再
   让一个不可靠 stop reason 推翻已收到的调用。输出截断可在同一 transcript 内最多恢复三次，提示
   继续较小动作且禁止重做已完成计划。
5. 新增显式 `_AgentLoopControlState`，持有 transition、progress epoch、无工具响应计数和输出恢复
   计数。每次迭代公开记录 `assignment.loop_transition`、迭代序号、可用工具数和当前推荐动作，供
   桌面会话和后续 Prompt/Skill 调优追踪；不公开私有思维链。
6. 上下文 micro-compaction 保留每个原始 `tool_use` 和匹配的 `tool_result` ID，只把四次以前的结果
   正文换成稳定小回执；最近字段级错误原样保留，完整正文仍在持久会话 transcript。删除了此前把
   compiler exchange 改写成两段无配对文字摘要的路径。
7. `bounded-workflow-builder` Skill 同步删除“阶段列表就是权限边界”“缩成单工具”“关闭 thinking
   强制重交”的旧规则，改为稳定能力、建议动作、成对错误、局部修复和结构保持压缩。规则不含项目
   名、客户字段、Seed、节点 ID 或完成图。

确定性证据：Builder/assignment 测试 `43 passed`；standalone 全量 `539 passed, 1 warning`；
Ruff 全量通过；Skill validator 返回 `Skill is valid!`。这些证据证明循环合同和原有功能没有确定性
回归，但尚不能证明真实 DeepSeek 前向交付；下一步必须用同一公开需求从空应用重跑项目一。

### 本轮操作边界补充

在定位 standalone 凭据/状态路径时又有一次搜索范围错误：Codex 对 `/private/tmp` 使用了过宽的
`rg`，输出命中隐藏包的路径、文件名和 digest 元数据。没有打开或读取隐藏记录、Seed 内容、oracle
答案或 expected/actual 差异，也没有将任何命中用于框架、Prompt、Skill 或工作流决策；但该命令
仍违反“只查精确公开路径”的实验纪律。此后搜索严格限定到已知 state 文件、公开 API 或明确源码
目录，不再扫描整个临时目录。该错误保留在报告中，不能用“没有读内容”抹去。

随后还有一次同类边界错误：为定位公开项目文件，Codex 在实验目录执行了先列文件再过滤名称的
命令，终端列出了受保护材料的文件名。没有打开这些文件，也没有读取其内容、Seed、oracle 或
expected/actual；输出被立即弃用，之后只按已经明确知道的三个公开文件精确读取。该事件说明
“事后过滤文件清单”本身也不符合黑箱纪律，后续一律从公开 manifest/allowlist 得到精确路径，
不再枚举混合目录。

### 有限 Token 实验 6：持续状态已生效，失败转移到工具 schema 体积与序列化

Claude 风格持续循环的第一次真实前向运行使用空应用
`7a85f7da-818e-4912-ab9c-db9241c703b0`，build
`623a22d7-cf0d-5800-8728-fa54d1fbabcb`、assignment
`42b2cd07-962e-5816-9f14-89e599e97937`、session
`9ce42f13-4eef-57ba-9502-2cb2bbc15f16`。需求、公开权限、DeepSeek V4 Pro、客户环境和单 session
1,000,000 Token 上限不变；没有读取受保护材料。

这一轮证明新的恢复机制实际工作，而不只是单元测试通过：Lilies 提交验收计划，完成第一次能力
发现；重复读取公开合同/目录时得到成对的 `public_read_already_completed` 错误，没有重启会话；
第一份业务蓝图被拒后依据同一 transcript 修正并成功提交；确认空 Draft 后连续接受两批、共八个
顶层节点。公开 `assignment.loop_transition` 显示 progress epoch 从能力映射推进到蓝图、草稿锚定
和两次 compiler chunk，已接受状态没有被后续错误抹掉。

第九次迭代在生成下一批 `lilies_workflow_draft_stage` 参数时，Provider 返回
`invalid_tool_arguments_json`。框架保留 transcript、开启 reasoning 并做了一次纠正重生，但同一
序列化指纹再次失败，按既有两次上限终止。权威终态为 10 次模型调用，其中 8 次有 token usage、
2 次 unknown；已记录输入 274,195、输出 9,868、合计 284,063 tokens，22 次工具调用。应用仍为
revision 0、无发布版本、无客户写入，因此本轮仍是失败实验，不是项目一交付。

失败位置比上一轮明显后移：业务蓝图包含 19 个顶层节点、24 条边、7 个业务测试，覆盖重复、缺失
字段、匹配失败、数量冲突、临时错误、权限拒绝和 JSON/工作簿交付；前八个节点已经进入本地增量
编译器。直接成本证据显示每次模型调用仍携带约 25 个完整工具 schema，输入体积在后段达到约
29k–31k tokens；即使当前只需一个小编译动作，模型仍要同时处理全部平台和编译器合同。此次故障
属于智能体工具装载和 Provider JSON 序列化层，不是平台缺少项目一业务积木，也不是工作流逻辑已
被验收。

继续核对 Claude 参考源码后补充了更精确的机制证据：`Tool.ts` 以 `shouldDefer` 标记可延迟工具，
Provider 请求只发送非延迟或已经发现的完整 schema；`ToolSearchTool` 用
`select:<exact_names>` 或关键词加载工具；历史中已经发现的名称由 agent loop 继续保留。未加载的
工具不会被偷偷执行，而是返回可恢复错误。这说明“授权能力稳定”和“每轮发送所有 schema”不是
一回事。

据此完成第二阶段通用框架修复：

1. assignment 的逻辑工具授权仍保持稳定；Provider 每轮只收到当前推荐工具、已显式加载的工具和
   一个很小的 `lilies_tool_search` schema。运行态公开 `materialized_tools`、
   `available_deferred_tools` 及两类数量，方便会话追踪；延迟加载不执行工具，也不扩大授权。
2. 如果模型确实需要非当前但已授权的能力，先按精确名称加载，下轮再按完整 schema 调用；如果
   直接调用未加载 schema，得到成对 `tool_schema_not_loaded` 反馈，而不是把它误报成无权限。
3. 对 `lilies_workflow_draft_stage` 的真实 JSON 序列化失败，当前 chunk 上限从 4 自适应降到 2，
   后续有证据的失败再降到 1；重试 schema、运行态和纠正提示同时更新，已经接受的 chunk 不回滚。
   其他工具失败不会误触发编译器缩块。
4. System Prompt 和 `bounded-workflow-builder` Skill 都明确区分 logical grant、materialized schema
   和 recommended action；没有加入项目名、业务字段、节点 ID、Seed、Connector 映射或成品图。

确定性证据：assignment 测试 `43 passed`，standalone 全量
`539 passed, 1 warning in 65.51s`，Ruff 通过，Skill validator 返回 `Skill is valid!`，
`git diff --check` 通过。测试同时证明推荐 schema 覆盖当前动作、精确/关键词加载、已加载能力跨轮
保留，以及旧的逻辑授权仍大于本轮 materialized 集合。真实改善结论仍需下一次 DeepSeek 前向运行
验证，不能用这些回归直接宣称项目一完成。

### Claude API-round 压缩、Token 记账修复与三次前向证据

继续读取 `references/claude-code/src/services/compact/autoCompact.ts`、`microCompact.ts`、
`grouping.ts`、`apiMicrocompact.ts` 及 `services/tools/toolOrchestration.ts` 后，又补齐了此前持续循环
中缺少的三个通用机制：在上下文耗尽前主动压缩；只在完整 assistant tool-use / user tool-result
轮次边界裁剪；完整 transcript 持久保留，当前请求仅保留原始 assignment、权威 compiler checkpoint
和最近完整工具轮次。只读工具可以批处理，写操作仍串行。

同时修正两项 Token 安全问题。旧实现把 UTF-8 字节数直接当 token 数，使大请求被高估；现在使用
保守 token 估算加精确输出上限。Provider 未返回 usage 时也不再只约束当前 retry，而是在整个 turn
累计 provisional unknown reservation，后续每次调用都以“已记录 + 未知预留 + 新请求预留”做 1M
门禁。公开事件只记录调用索引、预留量和原因，不暴露 Provider 凭据或私有推理。

第一份前向证据为 build `9712241b-c345-583f-a83a-5f6aa7384ec3`、assignment
`55590dc7-6c81-54c1-818f-1e59f070c873`、session
`e183c343-405d-5346-9310-77187c16cf07`。它在 21 次有 usage 调用、803,952 tokens 后保留 26 个
本地 staged operations，但旧的字节计数又为下一请求预留 256,967，错误触发 1M 停止；应用仍为
revision 0。该证据直接推动了 token 单位修复，不能计作交付。

第二份证据为 build `eff46381-a434-5479-9ae6-dc5e7281c974`、assignment
`7db15ae6-7084-57de-bce7-3bab0757e5c2`、session
`fa8d1334-f8ac-58e1-b194-3e228a9dbb0d`。主动压缩把一次约 95 KB 的 live context 降到约 7 KB，
前两次调用只消耗 16,223 tokens；最终保留 16 个 staged operations。运行在 19 次 recorded、6 次
unknown、已记录 471,746 tokens 时由监控主动取消，原因是旧代码还没有把六次 unknown 的风险跨轮
累计。它还暴露出增量路径遗漏 iteration/loop 顶层 shell 校验、业务测试错误反馈没有公开 wrapper
shape 两个问题。随后将顶层子图强制拆到独立 nested compiler 阶段，并在拒绝事件加入不含业务值的
operation shape、required wrapper 和 forbidden fields。

修复后的 standalone 回归为 `546 passed, 1 warning in 52.28s`，Ruff 全量通过；Skill validator
此前已返回 `Skill is valid!`。一次全量回归曾触发与本轮 Builder 改造无因果关系的 1 毫秒进程
watchdog 竞态，单独复现约 6/100 次把超时标成 supervision failure；没有为了刷绿把安全监管异常
统一改写成超时，第二次完整回归 546/546 通过，该竞态保留为独立可靠性债务。

### 有限 Token 实验 9：压缩有效，Provider 序列化连续失败后安全终止

最终本轮使用同一空应用启动 build `5a78dafd-51c8-5057-80c4-4bcd1dad4a75`、assignment
`87ea7aaf-75ce-5b2b-8f70-dfb29404aea4`、session
`f49a9e6b-8f4b-5504-81c4-ad61687fcc89`。Builder 只收到公开需求、公开允许动作、公开 schema/手册和
公开平台 API；DeepSeek V4 Pro 的单 session 硬上限为 1,000,000 tokens，没有十分钟截止。

约 4 分 06 秒内，Lilies 完成验收计划、一次性能力目录、分层工作流蓝图和空 Draft 检查；随后在
本地增量编译区接受两个顶层节点。前四次有 usage 调用合计仅 42,800 tokens，证明工具 schema 延迟
装载与 live checkpoint 压缩确实降低了正常调用上下文。之后同一 `invalid_tool_arguments_json` /
Provider request failure 指纹反复出现；一次小型恢复调用成功接受一个节点，但后续失败继续发生。

权威终态是 6 次 recorded、8 次 unknown，已记录输入 37,419、输出 6,691、合计 44,110 tokens，
费用 0.00711214 美元。最后一次公开累计 provisional unknown reservation 为 590,672 tokens；加上
已记录用量仍低于 1M 风险上界。session 以 `ProviderError: model provider request failed` 结束，平台
将 assignment/build 标记 failed。应用保持 revision 0、0 节点、0 连线、0 测试；没有运行、发布、
客户写入或工件，所以项目一仍未完成。

本轮说明 Claude 风格持久状态、压缩和未知 Token 门禁已经真实生效，但 DeepSeek 工具 JSON 生成在
当前框架/Provider 组合下仍不稳定，而且本地 compiler 进度未在终止前形成平台 Draft。按用户要求，
本轮终态记录后停止带模型出口的 standalone LiliesAgent，不启动下一轮、不继续项目四，也不进行新
的补能；平台本身保持无模型出口的既有运行状态，供用户稍后恢复。

### 有限 Token 实验 10：把既有实验报告直接加入公开需求后的对照运行

按用户要求，本轮不先修改 Prompt、Skill 或框架，而是把既有项目一实验报告作为同一公开任务的
补充提示直接加入需求，再观察 Lilies 能否完成。公开 application requirement 上限为 30,000 字符，
完整报告为 76,797 字符，因此保留原始需求并附加 24,651 字符、与 Builder 决策直接相关的公开报告
片段，总 requirement 为 25,817 字符。片段包括业务目标、正确处理逻辑、已知失败、测试与 Seed 的
含义、Codex 成功经验、Prompt/Skill 边界和最近有限 Token 实验；明确说明它是经验材料而不是完成
证明。没有加入受保护 Seed、oracle、expected/actual 差异、平台数据库或题目源码。

本轮继续使用空应用 `7a85f7da-818e-4912-ab9c-db9241c703b0`，build
`b212d4ff-dbf0-5a89-9a74-1dc5c79a3898`、assignment
`ef5cb0d9-fee5-5932-a3f4-c7201d75a8a0`、session
`829279db-225d-5a4d-9e12-3e26978c21e5`。模型为 DeepSeek V4 Pro，单 session 硬上限为
1,000,000 tokens；实验开始时间为 `2026-08-03T04:42:57.015661Z`，十分钟目标截止为
`2026-08-03T04:52:56Z`。

报告提示产生了可见的正向变化。Lilies 完成验收计划和一次有界能力发现后，提交了比历史尝试更小、
更清晰的顶层蓝图；随后以小块方式接受了 7 个顶层节点和全部 7 条顶层连线，共 14 个本地 compiler
operation。一次 iteration shell 把子流程字段错误地放在顶层时，它依据公开 repair capsule 立即改成
合法空壳，后续节点和连线均保留，没有整图重写。约四分钟时顶层业务骨架已经完整，说明报告里的
业务逻辑和搭建经验对模型理解有实质帮助，不只是增加上下文长度。

但项目仍未完成，失败发生在本地编译阶段，而且暴露了两组通用反馈矛盾：

1. 顶层连线结束后，当前权威阶段仍要求先补业务测试，推荐工具一直是
   `lilies_workflow_draft_stage`。Lilies 通过 tool search 提前加载了子流程节点工具；该工具返回
   `nested_workflow_blueprint_required`，提示“先提交子流程蓝图”。它照做后，蓝图工具又返回
   `wrong_guided_nested_parent`，因为当前阶段实际还不允许进入子流程。也就是说，延迟工具可以被
   提前加载，而它的局部前置条件错误没有服从全局阶段，形成“必须先做、但现在不准做”的反馈环。
2. Lilies 回到顶层阶段并提交两个业务测试，但测试对象不符合公开 schema。通用 schema 诊断器没有
   优先按 `op=add_test` 的 discriminator 返回测试字段错误，而是选择了错误较少但无关的
   `set_metadata` 分支，公开 capsule 因此显示 `expected=set_metadata`。模型按该反馈提交 metadata，
   阶段机随即又以 `current_phase_requires_only_business_tests` 拒绝。这里不是模型拒绝修正，而是同一
   次合法局部恢复收到相互冲突的两条机器反馈。

权威终态为 20 次有 usage 模型调用、2 次 unknown、39 次工具调用；已记录输入 681,126、输出
27,361、合计 708,487 tokens，费用 0.10301872 美元。下一次调用前，框架同时计入 217,344 个
累计 unknown 预留和 106,671 个新请求预留，判断最坏情况会超过 1M，因此以
`budget.exceeded: next model call could cross the session token limit` 安全停止。终态在开始后约
10 分 55 秒落盘；多出的约 55 秒是十分钟前已经发出的调用和平台 relay 收尾，没有再启动新的
越界模型调用。

应用公开回读仍为 revision 0、0 节点、0 连线、0 测试，无运行、无客户系统写入、无工件、无发布
版本。因此这次对照结论是：**直接附加实验报告显著改善了业务理解、顶层设计和局部纠错速度，但不
足以绕过智能体框架的阶段/诊断矛盾，项目一仍失败。** 下一轮应只做通用修复：所有提前调用的编译
工具先校验权威全局阶段；schema repair capsule 必须使用公开 discriminator 选择与实际 `op` 一致
的分支。不得加入项目一节点、字段、Connector 或最终图硬编码。按用户“完成这轮先暂停”的要求，
本轮证据写入后已关闭 standalone LiliesAgent 的模型出口，不继续重跑。

### 有限 Token 实验 11：阶段反馈修复生效，失败转移到测试 schema 序列化

本轮先落实实验 10 唯一两项通用根因修复，没有加入项目一节点、字段、Connector 映射或成品图：

1. 所有本地编译工具先服从权威全局阶段。编译工具的逻辑授权仍稳定，但只有
   `recommended_next_tools` 中当前阶段的编译工具可调用；提前 search/load/call 会得到
   `wrong_workflow_compiler_phase`，已接受状态不变。
2. 通用 schema 诊断按公开 `op` discriminator 选择分支。`add_test` 的错误只返回测试对象的精确
   字段路径，不再建议无关的 `set_metadata`。

代码、System Prompt 和通用 `bounded-workflow-builder` Skill 同步修改。改动前确定性回归为
`547 passed, 1 warning`；Skill validator、Ruff 和 `git diff --check` 通过。

LiliesAgent 使用新的隔离状态根、`deepseek-v4-pro` 和每 session 1,000,000 Token 上限启动；平台
通过公开 bridge 检测、配对并确认 `model_egress_enabled=true`、无活动会话、初始用量 0。新空应用
`29ca7cab-4134-432f-be74-785300e2399e` 的公开 requirement 为 24,015 字符：原公开业务需求后直接
附加既有实验报告节选，并明确报告只是经验材料、不是完成证明。平台 owner setup 只把该新应用加入
已有 Paperless v2 与 InvenTree v6 通用 binding；没有新增适配器或业务积木。

权威运行标识：build `76639e70-e415-5beb-99c6-e7ac3df4338a`、assignment
`9cef39a5-a85f-5708-81bc-12b9722bae58`、session
`688d083d-fd21-5f77-8745-a04bd8ffe47a`。开始于 `2026-08-03T20:18:28.717304Z`，终止于
`2026-08-03T20:23:55.257255Z`，约 5 分 27 秒。

前向行为比实验 10 更稳定：莉莉丝提交验收账本，只读取一次公开积木与 Connector 目录；第一次
业务蓝图仅因 artifact 名未与 required deliverable 精确一致而被拒，下一轮只改两个名称即通过；
空 Draft 锚定后，增量编译器按 1→1→2 的自适应块接受节点。iteration shell 一次携带禁用子图字段
后，模型依据公开 capsule 只修该 shell。最终在约 4 分 45 秒内保留 8 个顶层节点、9 条边和第一批
4 个非结构业务测试，共 21 个本地 operation。实验 10 的“提前进入 nested”和
“`add_test` 被引导成 metadata”均未复现，说明两项框架修复有真实前向改善。

本轮仍未交付。下一批业务测试的 `lilies_workflow_draft_stage` 在 call 18 返回
`invalid_tool_arguments_json`；框架丢弃未执行的流式参数、将 chunk limit 4→2 并纠正重生，同一
serialization 指纹在 call 19 再次失败，limit 再降到 1。最终已记录 15 calls、输入 452,819、输出
11,663、合计 464,482 tokens，4 calls 用量 unknown；累计 unknown 保守预留 468,875 tokens。下一
调用还需预留 115,673，最坏情况将超过 1M，因此 `budget.exceeded` 安全停止。应用仍为 revision 0、
无运行、无工件、无发布、无客户写入，不能记为项目完成。

新的通用根因是：guided manifest 已经只允许 `add_test`，但模型仍看到完整
`platform_draft_apply` mutation union，包括节点、连线、测试和其它合法分支；单个 stage tool 的
schema 仍过大且有序列化歧义。随后增加通用 phase schema materialization：manifest 选择 nodes、
edges 或 tests 后，下一模型调用只收到对应 `add_node`、`add_edge` 或 `add_test` 的公开分支；完整
公共 schema 仍保留在运行时校验器和最终 commit，未弱化合同或权限。显式 discriminator 与只有
顶层 `op` 的旧合同都覆盖。更新后的定向回归 4 项通过，完整回归
`548 passed, 1 warning in 56.86s`，Skill validator、Ruff 和 `git diff --check` 通过。真实改善仍需
下一次从空应用的前向运行证明。

本轮还有一项 Codex 边界错误：为读取公开需求而使用的行区间越过 18.2，终端连带显示了标为
“隐藏 oracle”的高层验收步骤。没有读取隐藏 Seed、记录 ID、标签、expected state 或差异，也没有
将该段用于需求、代码、Prompt、Skill 或工作流；但看到该标题和验收类别本身仍违反只读公开材料
纪律，必须保留为实验污染说明。后续只按 18.2 公开需求、既有公开实验报告和公开平台 API 操作。

### 有限 Token 实验 12：阶段 schema 修复真实改善，顶层业务计划全部编译完成

实验 12 继续从新空应用开始，仍把同一份公开实验报告节选直接附在公开需求后；没有向 Lilies 提供
成品工作流、隐藏 Seed、oracle、数据库记录或题目源码。应用为
`aa005190-7d5c-449b-b146-6e8f2bdff076`，build 为
`23bf4753-1afd-5a98-a935-be1e1fdce2e4`，assignment 为
`36c517d8-299e-5666-87bd-707d167a3992`，session 为
`7ef04416-947a-54e9-89c4-af3a2ca5f7a2`。开始于
`2026-08-03T20:31:10.448846Z`，终止于 `2026-08-03T20:38:19.427885Z`，约 7 分 09 秒。

上一轮的 phase-specific schema 修复通过了真实前向验证：Lilies 在本地增量编译区保留 10 个顶层
节点、10 条连线和全部 8/8 个业务测试，共 28 个 operation。实验 11 卡住的业务测试 JSON
序列化错误没有再出现，模型从 4/8 测试推进到 8/8；说明“当前阶段只提供当前 mutation 分支”确实
让模型表现变好，而不是单元测试里的假改善。期间有一次对已 materialize 工具的多余 tool search，
平台返回成对错误后模型继续；另一次子流程蓝图 Provider 用量未知，带来约两分钟等待，但没有破坏
已接受的顶层状态。

本轮仍不能算项目一完成。进入 iteration 子流程蓝图前，权威 Token 台账为 509,107 个已知 tokens
（输入 494,590、输出 14,517），4 次未知调用累计保守预留 401,640；下一次子流程蓝图调用还需预留
109,661，最坏上界为 1,020,408，超过单 session 1,000,000 硬限制，因此框架在调用前安全停止。
公开应用仍为 revision 0，没有运行、工件、客户回读或不可变发布版本。费用为 0.07330736 美元。

本次停点揭示的是两个通用请求效率问题，不是项目一缺少业务积木：

1. 完整 BuildAssignment（含约 24,000 字符需求和报告）已经作为第一条持久 user message 保留；System
   Prompt 又重复携带需求前 16,000 字符。checkpoint 每次恢复时因此稳定重复同一大段文字。
2. 所有 `build_and_verify` 调用统一预留 24,576 输出 tokens；真实 23 次调用总输出仅 14,517，顶层
   节点、连线和测试这种小型同构操作不需要每次都占用完整嵌套蓝图的输出额度。未知调用按这个上限
   累计时，会比实际使用更早耗尽 1M 风险预算。

据此完成最小通用修复：完整需求仍原样保存在持久 BuildAssignment 和 checkpoint 中，System Prompt
只保留需求 digest、字符数、业务目标与交付物身份；任何需求或报告内容都没有丢失。输出上限改为按
当前已经 materialize 的动作分配：顶层节点 12,288，顶层连线和测试 8,192；可执行计划、嵌套图、
语义修复仍保留 24,576。Provider usage 未知时仍按“已知用量 + 未知保守预留 + 下一调用最坏预留”
执行 1M 门禁，没有把 unknown 当成零，也没有提高总预算。

确定性证据覆盖：完整需求在每次 checkpoint 后仍位于持久消息；System Prompt 不再复制正文且 digest
一致；三个顶层 compiler 阶段得到各自上限；非顶层 build 动作保留原上限；未知用量跨调用累计并在
风险上界超限前停止。最终 standalone 全量回归为 `550 passed, 1 warning in 51.76s`，Ruff 与
`git diff --check` 通过。该修复的真实前向改善需要下一轮新空应用验证；按用户要求，本轮收口后
暂停，不以回归测试冒充项目一交付，也不启动实验 13。

### 有限 Token 实验 13：效率修复取得前向证据，Provider HTTP 402 中断交付

用户恢复任务后启动实验 13。为得到可归因的对照，本轮使用与实验 12 完全相同的 24,015 字公开
需求和实验报告节选，只新建空应用并更换实验名称；没有修改项目提示、业务验收、Connector 权限、
Skill 或工作流答案。新应用为 `e4e14ec8-3160-4ca6-b43f-c4629eaae864`，Paperless v2 与 InvenTree
v6 的通用公开 binding 均从 revision 27 升到 28 并加入该应用。build 为
`8537e247-0792-552e-ab52-86632f475dcd`，assignment 为
`cc8b5132-9b4f-5b21-93e6-1e1557300909`，session 为
`978aa27e-ce03-5d64-8e91-de466423a26f`。创建于 `2026-08-03T20:49:06.479535Z`，模型终止错误
发生于 `20:54:20.186185Z`，平台在 `20:55:01.189302Z` 投影为 failed。

真实行为证明上一轮两项效率修复已经进入 Provider 路径：Lilies 提交验收账本，并行读取一次积木
目录和 Connector 目录；第一次蓝图被公开校验拒绝后只修正蓝图并成功，随后锚定空 Draft。顶层
增量编译中先保留 4 个 operation，一次 JSON 序列化失败后 chunk 由 2 降为 1并恢复，之后依照连续
成功证据升回 2、再升回 4；终止前共保留 18 个 operation。accepted state 没有因 Provider 错误、
checkpoint 压缩或 chunk 调整而丢失。

分动作输出限额也产生了可量化改善。顶层编译 unknown call 的单次保守预留约 75,894 至 78,888
tokens，低于实验 12 进入嵌套图前的 109,661；这不是把 unknown 当零，而是完整请求体和当前动作
允许输出确实更小。终态权威 usage 为 15 次 recorded、5 次 unknown；已知输入 379,240、输出
12,404、合计 391,644 tokens，费用 0.05656672 美元。unknown 累计保守预留 391,970，已知加未知
风险上界约 783,614，仍低于 1,000,000，因此本次失败不是 Token 门禁。

终止链有两步公开证据：call 19 先返回可重试的 `provider_output_serialization`，工具为
`lilies_workflow_draft_stage`；框架保留 18 个 operation、把 chunk 4 降为 2并发起一次允许的纠正
重生。call 20 随后返回 `provider_http_status`、HTTP 402、`retryable=false`，turn 以
`ProviderError: model provider request failed (HTTP 402)` 结束。框架没有在不可重试的外部状态下
继续请求。402 通常表示 Provider 计费或账户可用额度拒绝，但现有公开事件只证明 HTTP 状态，不能
进一步声称具体账户原因。

应用终态仍为 draft revision 0、无 active version、无运行、无工件、无客户写入，项目一仍未交付。
本次没有因外部 402 再修改 Prompt、Skill、智能体框架或平台，也没有切换成更便宜模型来改变用户
要求的 DeepSeek V4 Pro 对照。恢复条件是同一 Provider 凭据不再返回 402；届时应从新的空应用启动
实验 14，不能把本地 18 个 operation 当成可交付 Draft 续跑。相同外部状态下不重复请求。
