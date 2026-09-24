"""Compare saved answers and reported usage without an extra model judge."""
import csv
import hashlib
import json
import re
from pathlib import Path
from uuid import uuid4


def folder():
    root=Path('results')/('answer-comparison-'+uuid4().hex);root.mkdir(parents=True);return root


def digest(raw):return hashlib.sha256(raw).hexdigest()


def project_file(value):
    p=Path(str(value))
    if p.is_absolute() or '..' in p.parts or not p.parts or p.parts[0] not in ['requirement-package','results']:
        raise ValueError('请选择当前项目的文字材料或已有回答记录')
    if any(x.is_symlink() for x in [p,*p.parents]) or not p.resolve().is_relative_to(Path.cwd().resolve()):raise ValueError('不能读取项目外文件')
    if not p.is_file():raise ValueError('文件不存在：'+str(p))
    if p.stat().st_size>2_000_000:raise ValueError('文件超过2 MB，请选择相关文字部分')
    return p


def prepare(inputs):
    question=str(inputs.get('question') or '').strip()
    if not question or len(question)>4000:raise ValueError('请填写1至4000字的问题')
    context='';source={}
    if inputs.get('source_path'):
        p=project_file(inputs['source_path'])
        if p.suffix.lower() not in ['.txt','.md','.csv','.tsv']:raise ValueError('本流程读取TXT、Markdown、CSV或TSV文字，请先把其他格式整理成文字材料')
        raw=p.read_bytes();context=raw.decode('utf-8-sig')
        if len(context)>16000:raise ValueError('材料超过16000字符，请选择相关部分；尚未调用模型')
        source={'path':str(p),'sha256':digest(raw)}
    prompt=json.dumps({'question':question,'material':context},ensure_ascii=False)
    data={'format':'answer-comparison-input-v1','question':question,'source':source,'prompt':prompt,'prompt_sha256':digest(prompt.encode())}
    path=folder()/'input.json';raw=json.dumps(data,ensure_ascii=False,indent=2).encode();path.write_bytes(raw)
    return {'input_path':str(path),'sha256':digest(raw),'prompt':prompt}


def frozen(prepared):
    raw=project_file(prepared['input_path']).read_bytes()
    if digest(raw)!=prepared['sha256']:raise ValueError('本次输入记录已改变，请重新比较')
    value=json.loads(raw)
    if value.get('format')!='answer-comparison-input-v1':raise ValueError('不是本流程的输入记录')
    return value


def metric(usage,key):
    support=usage.get('field_support',{}).get(key)
    if key=='cost_usd':support={'provider_reported':'reported','estimated_configured_price':'estimated'}.get(usage.get('cost_source'))
    value=usage.get(key)
    return value if support in ['reported','estimated'] and isinstance(value,(int,float)) and not isinstance(value,bool) and value>=0 else None


def finish(inputs):
    data=frozen(inputs['prepared']);answers=[]
    for label in ['A','B']:
        value=inputs.get(label)
        if not isinstance(value,dict):raise ValueError('回答'+label+'缺少步骤输出')
        text=value.get('text');error=value.get('error') or ('' if isinstance(text,str) and text.strip() else '模型没有返回非空回答')
        usage=value.get('usage') if isinstance(value.get('usage'),dict) else {}
        answers.append({'label':label,'status':'failed' if error else 'answered','model':value.get('model'),
            'input_sha256':value.get('input_sha256'),
            'text':text if isinstance(text,str) else '', 'error':str(error),'seconds':value.get('seconds'),
            'usage':usage,'metrics':{k:metric(usage,k) for k in ['input_tokens','output_tokens','cache_read_input_tokens','reasoning_tokens','cost_usd']}})
    record={'format':'answer-comparison-v1','prepared':inputs['prepared'],'answers':answers}
    path=folder()/'answers.json';path.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    return report({'source_result_path':str(path)})


def escape(value):
    return re.sub(r'([\\`*_{}\[\]<>()!|#])',r'\\\1',str(value)).replace('\r',' ').replace('\n',' / ')


def show(value):return '未报告' if value is None else str(round(value,6) if isinstance(value,float) else value)


def report(inputs):
    path=project_file(inputs['source_result_path']);saved=json.loads(path.read_text())
    if saved.get('format')!='answer-comparison-v1':raise ValueError('请选择本流程的answers.json')
    data=frozen(saved['prepared']);answers=saved.get('answers')
    if not isinstance(answers,list) or len(answers)!=2 or any(not isinstance(a,dict) for a in answers):raise ValueError('回答记录格式不正确')
    successful=[a for a in answers if a['status']=='answered'];same_model=len(successful)==2 and successful[0]['model']==successful[1]['model']
    same_input=(successful[0]['input_sha256']==successful[1]['input_sha256']) if len(successful)==2 and all(a.get('input_sha256') for a in successful) else None
    root=folder();text=f'# 同题回答与用量对照\n\n获得 {len(successful)}/2 份回答。'+('两条调用均未产生可用回答，请按错误修复模型配置后重新运行。' if not successful else '')+'\n\n'
    text+='问题：'+escape(data['question'])+'\n\n回答是模型生成的说法；本流程没有另请模型评分或选择赢家。\n\n'
    text+=('两次实际提示词和图片输入相同。' if same_input else '两次实际提示词或图片输入不同，不能把差异只归因于模型。' if same_input is False else '不足两份带输入指纹的成功回答，不能确认实际输入相同。')+'\n\n'
    if same_model:text+='本次请求的是同一个模型名称，属于两次回答对照，不能据此得出不同模型孰优。相同名称也不证明服务端权重或版本不变。\n\n'
    text+='| 回答 | 请求模型 | 状态 | 调用秒数 | 输入token | 输出token | 已报告或配置估算USD |\n| --- | --- | --- | --- | --- | --- | --- |\n'
    rows=[]
    for a in answers:
        m=a['metrics'];row={'回答':a['label'],'请求模型':a['model'] or '未取得成功响应，见运行步骤','状态':a['status'],
            '调用秒数':a['seconds'],'输入token':m['input_tokens'],'输出token':m['output_tokens'],'USD':m['cost_usd'],
            '费用来源':a['usage'].get('cost_source','unsupported'),'错误':a['error']};rows.append(row)
        text+='| '+' | '.join(escape(show(row[k])) for k in ['回答','请求模型','状态','调用秒数','输入token','输出token','USD'])+' |\n'
    text+='\n缺少用量保持“未报告”；零费用不代表免费。缓存/推理token通常是用量中的组成部分，不额外相加。耗时包含当前调用的等待和传输，不代表稳定性能；本图顺序调用，不能用一次测量评判模型速度。\n\n'
    for a in answers:
        text+='## 回答'+a['label']+'\n\n'
        if a['error']:text+='调用失败：'+escape(a['error'])+'\n\n'
        # Fence raw replies so generated links/instructions remain quoted text.
        fence='`'*(max([len(part) for part in re.findall(r'`+',a['text'])] or [2])+1)
        fence='`'*max(3,len(fence));text+=fence+'text\n'+a['text']+'\n'+fence+'\n\n'
        (root/(a['label']+'.txt')).write_text(a['text'] or a['error'],encoding='utf-8')
    text+='## 如何继续\n\n对照是否回答了同一个问题、数字与条件是否保留、依据是否来自资料，以及哪里需要核实。可以由员工纠正、保存有用的方法或修改节点再运行；不自动开始后续任务。只导出已有回答时选择answers.json，不再次调用模型。\n'
    with (root/'usage.csv').open('w',encoding='utf-8-sig',newline='') as out:
        writer=csv.DictWriter(out,fieldnames=list(rows[0]));writer.writeheader()
        writer.writerows({k:None if v is None else "'"+v if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) else v for k,v in r.items()} for r in rows)
    (root/'report.md').write_text(text,encoding='utf-8')
    artifacts=[{'file_path':str(root/name),'label':label} for name,label in [('report.md','回答对照 Markdown'),('usage.csv','用量与耗时 CSV'),('A.txt','回答A 原文'),('B.txt','回答B 原文')]]
    artifacts += [{'file_path':str(path),'label':'可再次导出的回答 JSON'},{'file_path':saved['prepared']['input_path'],'label':'本次问题与材料 JSON'}]
    result={'answered':len(successful),'failed':2-len(successful),'same_model':same_model,'same_input':same_input,'answers':answers,'source':data['source'],
        'prompt_sha256':data['prompt_sha256'],'source_result_path':str(path),'artifacts':artifacts,'markdown':text}
    return result


def main(inputs):return {'prepare':prepare,'finish':finish,'report':report}[inputs['operation']](inputs)
