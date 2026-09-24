"""Optional report production workflow and on-demand project instructions."""
from pathlib import Path

NAME='已有结果整理为汇报幻灯片'
DESCRIPTION='把已整理的汇报稿生成可编辑PPTX、PDF及逐页预览，保留正文、表格、图片与出处；改汇报不重新训练。'
GUIDE='''用户要汇报已有研究、数据分析或模型结果时使用。先按用途发现本项目的结果与已有流程，读取相关原文。可以直接帮助员工整理和解释，也可以生成可管理流程，不要求先经过训练或统一审批。
汇报内容围绕受众的问题组织：背景和范围、方法、实际结果、局限与下一步。保留数字、单位、样本量、评价划分与来源，区分历史结果、构造示例和本轮实际计算。结果不足就明确缺项，不编造更好的指标。没有必要的受众信息时可以先按内部讨论稿处理并说明假设。
已有汇报稿可直接交给本项目“已有结果整理为汇报幻灯片”。project_workflows list发现，inspect查看当前输入，workflow_run运行。输入source_path（Markdown或TXT）与outline（直接填写稿件）二选一，title和subtitle可选。普通表单无需填写JSON。
员工只有零散原始资料时，先用对话整理Markdown汇报稿。标题使用#或##，正文短段落或列表，比较数据使用Markdown表格，图片单独一行![说明](当前项目PNG/JPEG路径)。保留原文出处与待确认事项。长内容会分页，宽表按列拆页并保留第一列对应关系，不能用页数目标为理由悄悄删除信息。
一次性需求不必先创建工作流。读取本Skill的render.py引用资料，使用project_file保存到solution下，通过project_code运行同一代码的prepare和render：prepared=main({'operation':'prepare','source_path':'results/汇报稿.md'}); result=main({'operation':'render','prepared':prepared})，打印result的JSON即可获得可下载路径。代码在现有禁网Docker环境执行，需要负责人提供带PptxGenJS、LibreOffice Impress、Poppler和中文字体的文档镜像。缺组件时报告具体原因，不下载第三方程序或伪造PPT成功。
生成器只排版所给稿件，不调用模型改写。各页原文位置保存在演讲者备注，保存输入快照及内容哈希，图片冻结为独立副本。检查生成的每页PNG、PDF和PPTX中的文字与表格，发现拥挤时修改稿件分段，不靠极小字号塞入。PPTX文字和表格可编辑，图片保持原比例；不承诺复刻任意客户模板、复杂公式或动画。
修改汇报稿、标题或资料后创建新运行，旧文件保持不变。不因导出PPT重新训练或调用预测。需要更自由的布局，可编辑代码或直接使用已有文档环境的PptxGenJS。分享方法只分享代码与说明，资料、图片和模型由接收项目自行配置。'''


def source_code():
    return Path(__file__).with_name('presentation_code.py').read_text()


def workflow():
    from .official_workflows import graph,node,ref
    fields=[{'name':n,'label':label,'type':kind,'default':'','required':False} for n,label,kind in [
        ('source_path','已整理的汇报稿文件（Markdown或TXT）','file'),
        ('outline','直接填写汇报稿（与文件二选一）','string'),
        ('title','汇报标题（留空沿用稿件首个标题）','string'),
        ('subtitle','副标题或汇报对象（可选）','string'),
    ]]
    return graph([
        node('start','start','选择已有汇报稿',inputs=fields),
        node('prepare','code','读取内容并保存汇报快照',code=source_code(),timeout=120,
             inputs={'operation':'prepare',**{f['name']:ref('start',f['name']) for f in fields}}),
        node('render','code','生成可编辑幻灯片与预览',code=source_code(),timeout=300,
             inputs={'operation':'render','prepared':ref('prepare','output')}),
        node('end','end','汇报文件与逐页预览',outputs={'result':ref('render','output'),'markdown':ref('render','output','markdown')})])


def example_files():
    return {'内部试用汇报.md':'''# 质量分类流程试用

## 试用范围
这是一份自编汇报稿。以下数字用于展示排版，并非本次真实训练结果。
目标是在不熟悉算法时，完成资料检查、训练评价和结果复核。
输入按批次划分，预处理只在训练数据上拟合。指标代表离线测试，不代表现场放行效果。

## 方法比较
| 方法 | 测试宏平均F1 | 计算秒数 | 说明 |
| --- | --- | --- | --- |
| 简单基线 | 0.61 | 1.2 | 便于理解的比较起点 |
| 线性分类 | 0.74 | 4.8 | 保留各类指标和错误样本 |
| 树模型 | 0.77 | 12.6 | 改善幅度需要更多批次复验 |

## 结果解释
- 少数类别只有8条样本，不能据此保证可靠性。
- 分数较高的模型仍会出错，需要保留人工复核入口。
- 具体字段含义和预测时点由业务说明确定。

## 后续安排
补充独立批次资料，保持测试集不参与阈值选择。
员工可以修改汇报稿，重新生成幻灯片，已有模型和预测保持原状。

来源：本项目自编内部试用汇报，用于功能练习。
''','修改后的汇报.md':'''# 本周数据准备进展

## 当前进展
这份自编稿件用于更换输入练习。
已完成字段说明与批次标识整理，尚未开始训练。

## 尚缺资料
| 资料 | 用途 |
| --- | --- |
| 标签定义 | 区分质量类别的业务含义 |
| 预测时点 | 确定哪些字段当时可用 |

## 下一步
先补充现有资料的含义，再根据实际目标选择已有工作流。
来源：本项目自编修改练习。
'''}
