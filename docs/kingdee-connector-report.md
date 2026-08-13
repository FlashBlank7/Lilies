# 金蝶云星空连接器 · 真实 WebAPI 验证报告

**日期**：2026-08-13
**环境**：金蝶官方星空体验环境（`tglexp3.open.kingdee.com`，"金蝶云星空企业版泰语体验环境"，开放平台体验中心公开提供）
**性质**：只读验证。全程未调用任何写接口（Save/Submit/Audit/Delete），未改动环境任何数据。

## 一句话结论

**莉莉丝平台的金蝶连接器协议契约，已对真实的金蝶 K3Cloud WebAPI 跑通**——
不是对我们自造的假 ERP，是金蝶官方在跑的星空实例。登录会话、单据查询、
字段选择（含外键点导航）、分页翻页到尽头，四项全部验证通过。

## 怎么连上的（体验环境的真实姿势）

体验环境不发账号密码，走的是**令牌 SSO**：

1. 开放平台体验中心 → "金蝶云星空企业版…体验环境" → "全功能体验"，
   后端 `GetUserExpressUrl` 服务下发一个带 `ud` 令牌的落地地址
   （`http://tglexp3.open.kingdee.com/K3Cloud/HTML5/Index.aspx?ud=…`）；
2. 落地后星空为当前会话种下 `kdservice-sessionid` 等 cookie；
3. **WebAPI（ExecuteBillQuery）认这个 session cookie**——透传 cookie 即可查询，
   无需再走 ValidateUser。

这条路已固化进 `scripts/kingdee_probe.py` 的**会话直连模式**
（`KINGDEE_BASE_URL` + `KINGDEE_SESSION_COOKIE`）。正式账套则走另一模式
（账密 ValidateUser 或应用授权 LoginByAppSecret），同一脚本两种鉴权自动择路。

## 验证结果（独立脚本，脱离浏览器）

用会话 cookie，纯标准库 Python 直接打 WebAPI：

| 课目 | 请求 | 结果 |
|---|---|---|
| 会话鉴权 | 透传 session cookie | ✓ 通过，返回真实数据 |
| 单据查询 | `ExecuteBillQuery` BD_MATERIAL | ✓ 2 行：`CH4441/sawadika`、`123/123` |
| 字段选择 | FNumber,FName + 外键 `FCreateOrgId.FName` | ✓ 点导航解析成功（值随环境为 null） |
| 分页完整性 | BD_Supplier，page_size=2，StartRow 翻页 | ✓ 第1页2行 + 第2页1行，末页 <page_size 即停，无重复无遗漏 |

分页这门课，正是此前用自造假 ERP 反复对练的同款（那时的坑：漏翻末页 →
西南一店误判）。这次在**真协议**上验证，行为与假 ERP 训练时一致。

## 诚实标注的边界（重要）

- **这是数据近乎空的语言演示壳**：物料 2、客户 2、供应商 3，
  销售订单/即时库存/销售出库等业务单据**全部为 0 行**。
  协议、查询、字段映射、分页机制都验证到了，但**没有业务数据可跑完整日报工作流**。
- **数据是金蝶的演示样例**，不是任何真实公司的经营数据。
- **写回 + 幂等这半条没验**：体验环境共享，写数据会污染所有人，
  这一半必须等真实客户账套（客户总有自己的账套）才能验，届时走
  `Save`/`Submit` + `Idempotency-Key`（假 ERP 已练过的幂等课目）。
- **落地日报工作流**同样要等有业务数据的账套——空壳里跑不出门店销售 KPI。

## 对商业化的意义

真实客户接入金蝶时，我们要的东西已经摸清且最小化：
**环境 URL + 账套 ID + 一个只读 API 用户**（或落地会话 cookie），
不需要"绑定公网数据库"。连接器协议在真实金蝶上已验证可用——
这一步把"能不能接金蝶"从假设变成了既成事实。

## 复现

```bash
# 会话直连模式（体验环境/SSO）——cookie 从环境变量传入，不落文件
export KINGDEE_BASE_URL="https://<实例>/k3cloud"
export KINGDEE_SESSION_COOKIE="kdservice-sessionid=…; ASP.NET_SessionId=…"
python scripts/kingdee_probe.py --step query --form BD_MATERIAL --fields FNumber,FName
python scripts/kingdee_probe.py --step paginate --form BD_Supplier --fields FNumber,FName --page-size 2

# 正式账套模式（账密）
export KINGDEE_BASE_URL="https://<公司>.ik3cloud.com/k3cloud"
export KINGDEE_ACCT_ID="<账套GUID>"; export KINGDEE_USERNAME="<API用户>"; export KINGDEE_PASSWORD="<密码>"
python scripts/kingdee_probe.py --step login
```
