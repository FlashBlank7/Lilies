# 供应商文档入库、采购匹配与库存系统交接

当 Paperless-ngx 出现新的供应商发票或质量证明时，读取文档和 OCR 文本，提取供应商、采购单号、物料号、批次、数量、日期和证书类型；与 InvenTree 中的供应商、物料和采购记录匹配。

明确匹配且规则通过的记录写入受控元数据并建立可追溯关联；缺字段、低置信度、数量冲突或未知物料必须暂停给采购或质量人员选择，不得猜测或写入。重复业务文档不得产生重复副作用。临时错误只能有界重试，权限错误必须明确归类，不能误报为平台能力缺口。

最终生成 `reconciliation.xlsx` 和机器可读 `enterprise-result.json`，保存每条记录的来源、判断、人工决定、写回收据和失败原因。工作流必须仅使用平台公开合同自动发现并对齐接口，不得依赖预写宿主适配器、字段映射或人工修改最终图。

InvenTree 1.4.2 的正式关联声明上限是 link-only 外部关联，不是二进制文件复制。`attachment_create` 只使用 JSON `{model_type: "purchaseorder", model_id: integer >= 0, link: URI (max 2000), comment?: string (max 250), tags?: string[]}`，成功返回 201 Attachment；`attachment_list` 只按 `model_type`、`model_id`、`is_link`、`limit` 过滤，`attachment_destroy` 仅作为按 attachment id 删除并返回 204 的补偿。metadata 操作固定使用 `/api/metadata/purchaseorder/{id}/`：GET、PUT、PATCH 的请求或响应合同均为 `{metadata: object}`；PATCH 是顶层浅合并，PUT 是全量覆盖。冻结宿主的 live OpenAPI 未给出这些 metadata content schema，因此只能依据该官方合同通过平台通用 operation-contract overlay 补齐，不能引入 InvenTree 专用映射、adapter 或 wrapper。
