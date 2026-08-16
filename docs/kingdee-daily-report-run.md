# 销售日报工作流 × 真实金蝶星空 · 运行报告

**日期**：2026-08-16
**环境**：金蝶官方星空体验环境 `costexp1.open.kingdee.com`（"标准成本管理系统_体验环境"，K3Cloud 标准 demo 账套，含真实业务数据）
**性质**：只读。工作流只拉数（ExecuteBillQuery），不写回任何单据。

## 一句话结论

**销售日报工作流在莉莉丝平台上跑起来了，数据来自真实的金蝶 K3Cloud，
输出与 ERP 侧独立核算两头对账全绿。** 不是假 ERP、不是造的数据——是金蝶自己
在跑的星空实例里的 708 笔真实销售订单。

## 数据源（真实）

标准成本体验环境里有真业务数据：销售订单 708 笔、日期跨 2010–2026、
金额 19 万+的单比比皆是，39 个不同客户。挑数据最密的一天
**2018-12-24**（8 个客户、38 笔单）跑当天销售日报。

## 工作流（6 节点，平台运行时执行，零模型调用）

```
start(date/filter/target/stores)
  → http_request  拉当天销售订单（金蝶 ExecuteBillQuery，Cookie 走密钥库 $secret）
  → iteration     对每个客户：$sum 当天金额 + $count 单数 + $formula 达标判定
  → variable_assigner  全天合计 + 达标家数（$sum/$count/$length）
  → template_transform 运营抬头文字
  → end           date / summary / grand_total / stores[]
```

金蝶会话 cookie 存进平台密钥库（`KINGDEE_COST_COOKIE`），工作流里只写
`{"Cookie": {"$secret": "KINGDEE_COST_COOKIE"}}`——原文不进定义、不进日志。

## 运行输出（真实）

> 2018-12-24 销售日报：全天 38 单，销售额合计 621565.64 元；8 家门店中
> 4 家达标（目标 5万/店），逐店明细见下表。

## 两头对账（工作流输出 vs ERP 侧独立核算）

真值由独立脚本直接从金蝶按日期过滤重算，与工作流各走各的路：

| 客户 | 工作流 | ERP 真值 | |
|---|---|---|---|
| FYLGSP（ZG）YXGS | 393163.26 / 达标 | 393163.26 / 达标 | ✓ |
| FPYSGYLGLYXGS | 69529.32 / 达标 | 69529.32 / 达标 | ✓ |
| BJJDSJXXJSYXGS | 56070.00 / 达标 | 56070.00 / 达标 | ✓ |
| HZWYYSMYYXGS | 53680.00 / 达标 | 53680.00 / 达标 | ✓ |
| AZXBDSMYXGS | 28585.36 / 未达 | 28585.36 / 未达 | ✓ |
| FQ（SH）GJMYYXGS | 20537.70 / 未达 | 20537.70 / 未达 | ✓ |
| YCXXJKH | 0.00 / 未达 | 0.00 / 未达 | ✓ |
| SHKLMYYXGS | 0.00 / 未达 | 0.00 / 未达 | ✓ |
| **全天合计** | **621565.64** | **621565.64** | ✓ |

八店金额分毫不差、达标判定全对、全天合计精确匹配。**YCXXJKH 14 单却 0 元**
是真实的零金额边界，工作流也正确判为未达标——没有被单数迷惑。

## 沿途挖到并修复的真实平台缺陷

**http_request 只按 content-type 解析 JSON**。金蝶（一家主流 ERP）的 WebAPI
用 `text/plain` 回 JSON——运行时只认 `application/json`，于是把 JSON 当字符串
交给下游，`$sum`/数组索引全炸。修复：content-type 说 json 就解析；否则看正文
首字符像不像 JSON（`[`/`{`），像就试解析、失败退回文本——纯文本接口不受影响。
（`workflow_runtime._coerce_http_body`，7 条回归测试。这与 ERP 盲测那批坑同一
性质：真实集成才逼得出的缺陷。）

## 诚实标注的边界

- **会话 cookie 是临时的**：体验环境走令牌 SSO，cookie 会过期；这次跑通证明
  管道成立，长期运行需真实客户账套的稳定 API 凭证。
- **写回 + 幂等没验**：共享体验环境不做写操作；这半条等真实客户账套
  （走 Save/Submit + Idempotency-Key，假 ERP 已练过幂等课目）。
- **门店名册由调用方传入**（本次取当天有单的 8 家）：真实部署从金蝶组织/
  客户主数据取；工作流本身是通用的（date/filter/target/stores 都是入参）。

## 复现

```bash
# 前置：拿到体验环境落地后的会话 cookie（见 docs/kingdee-connector-report.md）
export KINGDEE_COST_COOKIE="kdservice-sessionid=…; ASP.NET_SessionId=…"
# 建应用→存密钥→搭图→试跑→对账，见 scripts/ 下编排脚本思路
# 平台运行时执行，全程零模型调用（造工作流才需要莉莉丝）
```
