"""Human image review and transferable notes, using the existing persisted loop."""
from pathlib import Path

NAME='看图复核与经验记录'
DESCRIPTION='查看项目图片和已有机器判断，逐条填写复判、理由及可复用经验；保留原结果并整理待确认记录和参考样本候选，不自动训练。'
GUIDE='''当员工已有检查图片，希望人工复判、记录判断原因并整理后续样本时使用。没有视觉模型也能运行，不调用LLM，不训练或写回现场。
输入为项目CSV/TSV/XLSX清单，默认列sample_id、image_path、product_type、machine_prediction；表单可改列名。机器判断可缺失，其他类别原样保存，不猜0/1含义。本流程的人工结论为合格、不合格、无法判断；其他任务可修改人工输入及整理代码。
图片单元格填写项目完整路径或唯一文件名；同名文件不猜测位置。支持PNG/JPEG/WebP。可选reference_path、reference_product、reference_version、reference_available_at、captured_at、batch列保留参考图片、产品、版本、时间和批次。参考对象不明、产品不同或参考形成时间晚于检测时点会明确提示；不据此自动否定或放行产品。
每次从指定记录开始最多20条，默认10条。不是整批自动检测；剩余记录数量明确列出。原图片、清单及元数据先固定为本次副本，暂停后继续不重新读取更换过的原文件。
页面每张图可选合格、不合格或无法判断，并填写观察、缺项和可复用方法；不是教员工凭图猜工艺规则。判定依据未提供时说明缺项，允许无法判断。只有员工明确表示建议作为参考、结论明确、产品类型和理由完整时才进入候选清单，仍不自动更新参考库或训练数据。
workflow_run开始后可读取runs.waiting_input；respond只提交员工明确给出的答案，不能代填“已人工复核”。刷新或重新登录继续原任务，停止后不自行恢复。已提交答案和每张复核产物保存在原运行。
结果包含机器原判断、人工标签、两者是否一致、理由、方法说明、图片哈希和来源、参考元数据，以及候选清单和未知项。比较仅描述这次人工复判，不是生产准确率、独立测试或模型在线学习。
旧review.json可通过source_result_path重新导出，不重新逐张提问；新资料或重新复核创建新运行。可将有用的经验经员工整理后保存为项目Skill，不自动把个人判断推广到其他产品。
后续类别对照复用“预测与实测反馈对照”：result.feedback_inputs给出可直接传入的字段配置，result.labeled_path仅包含明确人工标签；不要用完整reviews.csv把无法判断误算成质量类别。没有明确标签时不要启动评价，先保留未知项。视觉训练不在本流程内。'''


def workflow():
    from .official_workflows import node,ref,graph
    code=Path(__file__).with_name('visual_review_code.py').read_text()
    fields=[dict(name='source_path',label='图片清单（CSV／Excel）',type='file',default='',required=False),
            dict(name='sheet',label='Excel工作表（单表可留空）',type='string',default=''),
            dict(name='id_column',label='样本标识列',type='string',default='sample_id'),
            dict(name='image_column',label='图片路径或文件名列',type='string',default='image_path'),
            dict(name='product_column',label='产品类型列（缺失会提示）',type='string',default='product_type'),
            dict(name='prediction_column',label='已有机器判断列（缺失也可复核）',type='string',default='machine_prediction'),
            dict(name='criteria',label='已有判定依据（不知道可留空）',type='string',default=''),
            dict(name='start_row',label='从第几条数据开始（不含表头）',type='number',default=1),
            dict(name='sample_limit',label='本次最多复核几条（1至20）',type='number',default=10),
            dict(name='source_result_path',label='只导出已有复核（首次留空）',type='file',default='')]
    for f in fields:f.setdefault('required',False)
    inner=graph([
        node('begin','start','本条待复核样本',inputs=[dict(name='sample',type='object',required=True),dict(name='prepared',type='object',required=True)]),
        node('view','code','读取本次图片与判定说明',code=code,reuse_completed=True,inputs={'operation':'context','sample':ref('begin','sample'),'prepared':ref('begin','prepared')}),
        node('ask','human_input','填写你的复判与理由',description='可选择无法判断。这里保存你的复判，不改变原机器判断，不自动更新模型。',context=ref('view','output'),fields=[
            dict(name='label',label='复判结论',type='string',required=True,options=['合格','不合格','无法判断']),
            dict(name='reason',label='判断理由或还缺什么（可留空）',type='string',required=False),
            dict(name='method',label='下次可参考的判断方法和适用条件（可留空）',type='string',required=False),
            dict(name='reference_candidate',label='建议将此图列为后续参考样本候选',type='boolean',required=False)]),
        node('save','code','保存本条复核并保留原判断',code=code,reuse_completed=True,inputs={'operation':'collect','sample':ref('begin','sample'),'prepared':ref('begin','prepared'),'answer':ref('ask','output')}),
        node('done','end','本条复核记录',outputs={'result':ref('save','output')})])
    next(n for n in inner['nodes'] if n['id']=='ask')['config']['title']='查看图片，补充你的判断'
    nodes=[node('start','start','选择图片清单与判定说明',inputs=fields),
           node('route','if_else','新复核或导出已有结果',cases=[{'id':'reuse','conditions':[{'value':ref('start','source_result_path'),'operator':'not_equals','expected':''}]}]),
           node('prepare','code','核对清单并固定本次图片',code=code,reuse_completed=True,timeout=120,inputs={'operation':'prepare',**{f['name']:ref('start',f['name']) for f in fields}}),
           node('review','iteration','逐张看图与记录经验',items=ref('prepare','output','samples'),variables={'prepared':ref('prepare','output')},item_name='sample',workflow=inner,output_node_id='done',output_path=['result'],parallelism=1),
           node('report','code','整理复判、缺项与参考候选',code=code,inputs={'operation':'finish','prepared':ref('prepare','output'),'reviews':ref('review','items')}),
           node('end','end','复核记录与后续样本',outputs={'result':ref('report','output'),'markdown':ref('report','output','markdown')}),
           node('reuse','code','只导出已有复核',code=code,inputs={'operation':'report','source_result_path':ref('start','source_result_path')}),
           node('reused','end','已有复核报告',outputs={'result':ref('reuse','output'),'markdown':ref('reuse','output','markdown')})]
    pairs=[('start','route'),('route','prepare'),('prepare','review'),('review','report'),('report','end'),('route','reuse'),('reuse','reused')]
    return dict(nodes=nodes,edges=[dict(id=a+'-'+b,source=a,target=b,**({'branch':'reuse' if b=='reuse' else 'else'} if a=='route' else {})) for a,b in pairs])


def diagram(gap=False):
    # Procedural teaching diagram, not a photograph or synthetic production claim.
    import struct,zlib
    width,height=320,120;rows=[]
    for y in range(height):
        row=bytearray()
        for x in range(width):
            color=(247,249,252)
            if x in (19,20,299,300) or y in (19,20,99,100):color=(135,148,169)
            if 35<=x<=284 and 54<=y<=66 and not (gap and 150<=x<=175):color=(43,84,133)
            row.extend(color)
        rows.append(b'\0'+row)
    def chunk(kind,data):return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',width,height,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(b''.join(rows)))+chunk(b'IEND',b'')


def example_files():return {
    '连续线条.png':diagram(), '中间断开.png':diagram(True),
    '待复核.csv':'sample_id,image_path,product_type,machine_prediction,reference_path,reference_product,reference_version,batch\ns1,连续线条.png,演示线条,不合格,连续线条.png,演示线条,v1,A\ns2,中间断开.png,演示线条,合格,连续线条.png,演示线条,v1,A\n',
    '待复核-其他对象.csv':'sample_id,image_path,product_type,machine_prediction,reference_path,reference_product,reference_version,batch\nnew1,中间断开.png,未知元件,不合格,连续线条.png,演示线条,v1,B\n',
    '练习说明.txt':'两张图片是代码生成的教学图，不是生产照片。仅对“演示线条”使用这条自编规则：框内蓝线连续且不越过灰色边界为合格。第1条机器误报，第2条机器漏报；实际判断由员工填写，不预填答案。换其他对象清单时，这条规则没有适用依据，应说明缺项或无法判断。可填写观察、方法和适用条件，再导出旧review.json；不会训练或更新参考库。'}
