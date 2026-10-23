"""Shared authoring method for both project assistant providers and manuals."""
HTTP_WORKFLOW_GUIDE = '''
外部服务流程：先读 project_connections(action="list") 的项目服务说明，不请求或读取密钥。
HTTP 积木使用 connection_id 与相对 path；禁止把网络调用放进 Python 代码节点。
全局积木只用于搭建；通过 project_calls 调用人已加入的项目能力。草稿随时可保存。
外部写入的默认步骤是：读取完整数据 → 匹配稳定标识 → 固定原处理对象 → 分步操作 → 回读结果 → 核对目标。
分页必须逐页获取并核对总量/游标与重复页，不能只取首页。核对不同服务的 ID、路径和版本；标题只用于显示，匹配缺失或不唯一时保留原因，不猜对象。
对缺失字段、类型变化、重复 ID、时区、完成时间与最后播放时间的区别、时间阈值等分别处理；业务来源和边界不清楚时明确说明。
读取请求最多尝试三次；写入只有连接声明支持幂等，或 outcome_query 确认未执行时才能自动重发。不能伪造幂等支持、换编号或无限续跑。
outcome_query 是同一连接的 GET：path、query、result_path（字段路径数组）、completed_value、not_applied_value。query 可引用 $operation.id 和 $operation.request。缺字段、查询失败或既不等于已完成也不等于未执行，均为未知。不要把查询失败/404当作写入未发生。
失败时先读本次任务和已完成步骤。project_calls(resume, task_id) 继续原快照和输入；修改后 start 带 recovery_task_id 创建修复运行。
修复代码从 $recovery.inputs、$recovery.runs[].steps / nested_progress / http_operations 读取原目标与进度；连续修复的更早来源在 $recovery.original。只读上下文不能当本次成功结果。
“文件不存在”只能说明文件状态，不能说明取消监控、移除下载任务、记录日志已完成。必须围绕原目标补齐剩余动作，不能重新筛掉已部分处理的对象。
end.checks 配置检查名称 name、实际值 value、比较 operator、预期 expected、失败说明 failure_message；实际值应引用本次回读/计算结果，全部必要动作分别检查。
影音清理分别检查原目标的文件、监控、下载任务、日志。先核对目标数与原计划一致，再核对各动作；目标列表为空不能把遗留事项抹掉。报告从本次查询生成。
未配置结果检查仅表示执行成功；检查通过只证明被检查的内容，不推测业务含义，也不保证未覆盖的场景。出现新错误按实际现象修正并补回归。
'''
