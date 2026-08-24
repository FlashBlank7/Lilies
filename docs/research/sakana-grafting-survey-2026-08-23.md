# Sakana 及可嫁接机制增量调研（调研代理原文，2026-08-23）

## 1. Sakana 动态与 TreeQuest 集成
8/18 后无新研究发布（8/21 仅 Translate 产品更新）。TreeQuest 最新 v0.3.2
（2026-02-05，562★）：`pip install "treequest[all]"`，Python≥3.11。核心接口：
```python
algo = tq.ABMCTSA()
tree = algo.init_tree()
tree, trials = algo.ask_batch(tree, batch_size, actions)
for t in trials:
    state, score = generate_fns[t.action](t.parent_state)  # None=根
    tree = algo.tell(tree, t.trial_id, (state, score))
best = tq.top_k(tree, algo, k=1)
```
score 必须归一 [0,1]；**0/1 校验信号与 ABMCTSA 的 Beta-Bernoulli 共轭天然适配**。
多模型=每模型一个 action。坑：batch_size>5 扭曲树形；tell 幂等可乱序；无内置
checkpoint（tree 纯值可 pickle）；ABMCTSM 依赖 PyMC 且对稀疏 0/1 慢，先用 A。

## 2. 验证器裁决 best-of-N 实证
- Large Language Monkeys（abs/2407.21787）：coverage 对数线性；SWE-bench Lite
  1 样本 15.9%→250 样本 56%；**有自动验证器的域收益全额兑现**；最陡段 N=4→16。
- CodeMonkeys（abs/2501.14723）：并行+串行组合，ensemble 裁决 66.2% 超最好单成员。
- 开源：huggingface/search-and-learn（vLLM 基座，BoN/beam/DVTS）；1B+PRM 可超
  405B（abs/2502.06703）。打分器接口=每步 float，可原样换 draft_validate。

## 3. bandit/路由在线学习
- OrcaRouter（abs/2605.30736，2026-05）：离线 reward 矩阵暖启动 + LinUCB 在线
  只更新被选臂；RouterArena 第 2。
- 0/1 信号用 **Beta-Bernoulli Thompson 采样**：每（步骤类型,模型）一对 α/β；
  冷启动=历史验收日志回放；非平稳=折扣 γ≈0.99。库：MABWiser 2.7.4（稳定）。
- 同构先例：ShinkaEvolve 内置 bandit LLM-ensemble 选择器（ICLR 2026）。

## 4. 多小模型并行提案工程
- 单端点最廉价：vLLM `SamplingParams(n=N)` + prefix caching（prefill 近免费）。
- **多温度实证**：单温度 TTS 平台化；Qwen3 0.6-8B 多温度较单温度再 +7.3 分；
  N 越大最优温度越高（abs/2510.02611，2025-10）。模型多样性：Multi-LLM
  AB-MCTS 超任何单模型重复采样。先吃同模型温度阶梯，不够再加异构端点。
- 大 N 用 DVTS 拆独立子树防同质化。

## 5. 其它同构项目
- **MetaFlow**（abs/2606.30704，2026-06）：工作流生成当元学习，SFT+RLVR 以
  执行反馈为奖励，单次推理逼近搜索式 SOTA——"搜索日志蒸馏成生成器"路线。
- A2Flow（AAAI 2026）、FlowSteer（abs/2602.01664）、RobustFlow（abs/2509.21834）。
  趋势：任务级搜索 → 实例级生成。

## 嫁接行动清单（原文）
1. C 档骨架用 treequest ABMCTSA：多模型 partial dict + draft_validate 归一分数，ask_batch≤5。
2. B 档选型步：升级 32B 前先插 N=4-8 同模型多温度 best-of-N + 验证器裁决。
3. 路由器（B/C 共用）：每（步骤类型,模型）Beta-TS，折扣 γ≈0.99，MABWiser 起原型。
4. 冷启动仿 OrcaRouter：历史验收日志离线回放建先验。
5. vLLM 开 prefix caching，n=N + 温度阶梯 0.3/0.7/1.0。
6. 大 N 用 DVTS 拆子树。
7. A 档硬门 0/1 判决并入同一 reward 流水线回灌路由器，统一三档信号。
8. 长线：MetaFlow/ScoreFlow 路线，把搜索蒸馏进 4B。
