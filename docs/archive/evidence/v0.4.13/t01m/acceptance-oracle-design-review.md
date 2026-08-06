# T01M 验收规则独立设计复审

- 任务：`V04-13-T01M`
- 题面：`T01M-CIVILIZATION-SEED-ANDROID-v1`
- 复审身份：`/root/t01m_freeze_review4`
- 复审方式：只读；未参与应用实现或外部验收驱动实现
- 最终验收规则 raw SHA-256：
  `52c7998a6b9f3d710f043e53f49d2176ed50bb8bf4bfc782d7ec1b5321a8bbe4`
- A01–A10 case ID 集 SHA-256：
  `0a4177e533d9ee6eab47f70a7fce638c41becdeea73be18175f8b10fede4ce90`
- 最终结论：`PASS`

## 已重建的关键边界

- A08 的 sRGB、OKLab、距离、坐标映射、binary64 舍入、StrictMath、Gaussian
  elimination、回代和 IRLS 运算顺序均已唯一冻结。
- `numeric_reference` 只覆盖完整 A08 对比度路径与 A09 的 sRGB 预处理，不宣称
  覆盖 SVD、去趋势、相关、自相关、轮廓拟合或运动统计。
- A09 的页面事件、目标 hierarchy、frame capture-start/capture-complete、
  request sequence、像素摘要与 `stable_start` 绑定已闭合。
- reduced-motion 的 R08 使用“生存基础”类别筛选；状态从“沉睡”变为“重建中”
  后仍满足同一筛选，因此 R10 可达。
- Unicode 15 `White_Space`、NFC、非法 UTF-16 surrogate、补充平面字符、
  长度边界和排序合同均具备确定性验证路径。

本复审只确认题面与验收规则可实现、可复现且无内部矛盾。它不确认外部驱动实现、
Android 应用、APK、模拟器结果或 T01M 项目闭环；这些仍须分别独立复审。
