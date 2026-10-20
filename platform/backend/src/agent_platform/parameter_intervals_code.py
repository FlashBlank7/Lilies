"""Portable interval analysis: known failures and unsampled gaps always split ranges."""
import csv
import hashlib
import io
import json
from collections import defaultdict
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction
from pathlib import Path
from uuid import uuid4


def numeric(value, label):
    try:
        n=Decimal(str(value).strip())
        if n.is_finite() and len(n.as_tuple().digits)<=40 and (n==0 or -40<=n.adjusted()<=40):return Fraction(n)
    except (InvalidOperation, ValueError):pass
    raise ValueError(label+'需要有限数值（最多40位有效数字，非零数量级1e-40至1e40）')


def display(n):
    if n is None:return ''
    with localcontext() as ctx:
        ctx.prec=90
        return format(Decimal(n.numerator)/Decimal(n.denominator),'f').rstrip('0').rstrip('.') if n.denominator!=1 else str(n.numerator)


def main(inputs):
    checked=inputs['checked'];path=Path(checked['source_path'])
    if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0]!='results' or not path.is_file():
        raise ValueError('请选择本项目已检查的扫描结果')
    if any(p.is_symlink() for p in [path,*path.parents]) or not path.resolve().is_relative_to(Path.cwd().resolve()):
        raise ValueError('不支持项目外文件或符号链接')
    if path.stat().st_size>50_000_000:raise ValueError('检查结果超过50 MB，请拆分')
    raw=path.read_bytes();reader=csv.DictReader(io.StringIO(raw.decode('utf-8-sig')))
    fields=reader.fieldnames or [];rows=list(reader)
    if not rows or len(rows)>20000:raise ValueError('需要1至20000条扫描记录')
    parameter=str(inputs.get('parameter_column') or '').strip();score=str(inputs.get('score_column') or '').strip()
    group=str(inputs.get('group_column') or '').strip()
    if any(k not in fields for k in [parameter,score,'comparison_status','comparison_source_record']) or (group and group not in fields):
        raise ValueError('请检查数值参数、分数和分组列是否存在')
    if any(f.startswith('interval_') for f in fields):raise ValueError('已有interval_字段，请选择原扫描表')
    direction=inputs.get('direction','越大越好');smoothing=inputs.get('smoothing','不平滑')
    if direction not in ['越大越好','越小越好']:raise ValueError('请选择有效的分数比较方向')
    if smoothing not in ['不平滑','相邻3点均值','相邻5点均值']:raise ValueError('请选择有效的分数计算方式')
    gap=numeric(inputs.get('max_gap'),'最大相邻间隔');tol=numeric(inputs.get('tolerance',0),'容差')
    minimum=numeric(inputs.get('min_width',0),'最小宽度');min_points=numeric(inputs.get('min_points',2),'最少点数')
    maximum=None if inputs.get('max_width') in (None,'') else numeric(inputs['max_width'],'最大宽度')
    if gap<=0 or tol<0 or minimum<0:raise ValueError('最大相邻间隔必须大于0，容差和最小宽度不能为负')
    if min_points.denominator!=1 or not 2<=min_points<=20000:raise ValueError('最少点数必须是2至20000之间的整数')
    if maximum is not None and (maximum<=0 or maximum<minimum):raise ValueError('最大宽度必须大于0且不小于最小宽度')
    origin=inputs.get('origin','其他或不清楚')
    if origin not in ['模型计算结果','实际测量','自编示例','其他或不清楚']:raise ValueError('请选择扫描数值来源')
    config=dict(parameter_column=parameter,score_column=score,group_column=group,direction=direction,max_gap=display(gap),
                tolerance=display(tol),min_width=display(minimum),min_points=int(min_points),max_width=display(maximum),smoothing=smoothing,origin=origin)
    groups=defaultdict(list);seen={};sign=1 if direction=='越大越好' else -1
    for row in rows:
        name=row[group].strip() if group else '全部扫描';record=row['comparison_source_record']
        x=numeric(row[parameter],f'第{record}条记录的参数{parameter}：');key=(name,x)
        if key in seen:raise ValueError(f'第{record}与第{seen[key]}条记录在同组参数值重复，请先明确汇总方法')
        seen[key]=record
        try:value=numeric(row[score],'分数：')
        except ValueError:value=None
        status=row['comparison_status']
        if status not in ['pass','fail','unknown','unchecked']:raise ValueError('条件检查状态无效，请从原扫描表重新运行')
        reason=row.get('comparison_reason','')
        if value is None:reason+='；分数缺失或不是有效数值'
        # Comparison ranks are raw-score ranks; omit them from the interval result to avoid confusing smoothed ranks.
        clean={k:v for k,v in row.items() if not k.startswith('comparison_')}
        clean.update(interval_source_record=record,interval_condition_status=status,interval_reason=reason.strip('；'),
                     interval_comparison_score='',interval_near_best=False,interval_is_best=False,interval_break_before='')
        groups[name].append(dict(x=x,score=value,eligible=status in ['pass','unchecked'] and value is not None,row=clean,near=False,best=False))
    summaries=[];points=[];intervals=[];half={'不平滑':0,'相邻3点均值':1,'相邻5点均值':2}[smoothing]
    for name,entries in groups.items():
        entries.sort(key=lambda e:e['x']);runs=[];current=[]
        for i,e in enumerate(entries):
            breaks=[]
            if i and e['x']-entries[i-1]['x']>gap:breaks.append('超过最大相邻间隔')
            if i and not entries[i-1]['eligible']:breaks.append('前一点条件或分数不可用')
            if not e['eligible']:breaks.append('本点条件或分数不可用')
            e['row']['interval_break_before']='；'.join(breaks)
            if breaks and current:runs.append(current);current=[]
            if e['eligible']:current.append(e)
        if current:runs.append(current)
        for run in runs:
            for i,e in enumerate(run):
                neighbors=run[max(0,i-half):i+half+1]
                e['smooth']=sum((v['score'] for v in neighbors),Fraction(0))/len(neighbors)
                e['row']['interval_comparison_score']=display(e['smooth'])
        eligible=[e for e in entries if e['eligible']]
        best=max((e['smooth']*sign for e in eligible),default=None)
        for e in eligible:
            e['near']=e['smooth']*sign>=best-tol;e['best']=e['smooth']*sign==best
            e['row']['interval_near_best']=e['near'];e['row']['interval_is_best']=e['best']
            if not e['near']:e['row']['interval_reason']='不在近最佳容差内'
        segments=[]
        for run in runs:
            current=[]
            for e in run:
                if e['near']:current.append(e)
                elif current:segments.append(current);current=[]
            if current:segments.append(current)
        group_intervals=[]
        for segment in segments:
            prefix=[0]
            for e in segment:prefix.append(prefix[-1]+int(e['best']))
            windows=[]
            if maximum is None:windows=[(0,len(segment)-1)]
            else:
                j=0;previous=-1
                for i in range(len(segment)):
                    j=max(i,j)
                    while j+1<len(segment) and segment[j+1]['x']-segment[i]['x']<=maximum:j+=1
                    if j>previous:windows.append((i,j));previous=j
            for i,j in windows:
                a,b=segment[i],segment[j];width=b['x']-a['x'];count=j-i+1;reasons=[]
                if count<min_points:reasons.append('采样点数不足')
                if width<minimum:reasons.append('宽度不足')
                group_intervals.append(dict(group=name,start=display(a['x']),end=display(b['x']),width=display(width),points=count,
                    contains_best=prefix[j+1]>prefix[i],qualified=not reasons,reason='；'.join(reasons),
                    start_source_record=a['row']['interval_source_record'],end_source_record=b['row']['interval_source_record']))
        qualified=[r for r in group_intervals if r['qualified']];containing=sum(r['contains_best'] for r in qualified)
        state='no_eligible_points' if not eligible else 'intervals_found' if containing else 'best_point_has_no_interval' if qualified else 'no_interval_meets_requirements'
        summaries.append(dict(group=name,rows=len(entries),eligible=len(eligible),best_score=display(best*sign if best is not None else None),
            best_parameters=[display(e['x']) for e in eligible if e['best']],qualified_intervals=len(qualified),best_intervals=containing,
            alternatives=len(qualified)-containing,status=state))
        intervals.extend(group_intervals);points.extend(e['row'] for e in entries)
    result=dict(rows=len(rows),groups=summaries,config=config,constraints=checked['constraints'],source=checked['source'],
                checked_source={'path':str(path),'sha256':hashlib.sha256(raw).hexdigest()},
                scope='区间只连接满足本次规则的已采样点；未测位置不保证满足条件，不证明生产安全、全局最优或多参数组合可行。')
    folder=Path('results')/('parameter-intervals-'+uuid4().hex);folder.mkdir(parents=True)
    artifacts=[f for f in checked.get('artifacts',[]) if f['file_path'].endswith('/conditions.csv')]
    def save_csv(name,fields,data,label):
        with (folder/name).open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(data)
        artifacts.append({'file_path':str(folder/name),'label':label})
    save_csv('points.csv',list(points[0]),points,'逐点分数、条件与中断原因 CSV')
    save_csv('intervals.csv',['group','start','end','width','points','contains_best','qualified','reason','start_source_record','end_source_record'],intervals,'全部区间与不足原因 CSV')
    markdown=report(result,intervals,points,parameter,score,group)
    for name,content,label in [('summary.json',json.dumps(result,ensure_ascii=False,indent=2),'配置与分组结果 JSON'),('report.md',markdown,'连续区间分析说明 Markdown')]:
        (folder/name).write_text(content,encoding='utf-8');artifacts.append({'file_path':str(folder/name),'label':label})
    return {**result,'source_path':str(folder/'points.csv'),'intervals_path':str(folder/'intervals.csv'),'preview':points[:12],
            'interval_preview':intervals[:20],'markdown':markdown,'artifacts':artifacts}


def report(result,intervals,points,parameter,score,group):
    def cell(v):return str(v).replace('\\','\\\\').replace('|','\\|').replace('\r',' ').replace('\n',' ')[:180]
    cfg=result['config'];labels={'no_eligible_points':'没有条件与分数均可用的点，不能给出区间。',
        'intervals_found':'存在包含最佳点且满足宽度及点数要求的区间。',
        'best_point_has_no_interval':'最佳点未形成合格宽度的区间；存在近最佳备选段，需自行比较取舍。',
        'no_interval_meets_requirements':'可用点存在，但近最佳片段未达到宽度或点数要求；不把孤立高分点当作可用区间。'}
    text=f"# 参数扫描结果与连续区间\n\n共 {result['rows']} 个采样点，{len(result['groups'])} 组；数值来源：{cfg['origin']}。\n\n"
    text+='## 各组结论\n\n'
    for g in result['groups'][:20]:
        text+=f"- **{cell(g['group'])}**：{g['eligible']}/{g['rows']} 个点可参与分析；{labels[g['status']]}"
        if g['best_parameters']:text+=' 最佳比较分 '+cell(g['best_score'])+'，对应参数 '+ '、'.join(map(cell,g['best_parameters'][:10]))+('等（完整列表见JSON）' if len(g['best_parameters'])>10 else '')+'。'
        text+=f" 包含最佳点的区间 {g['best_intervals']} 个，其他备选 {g['alternatives']} 个。\n"
    text+='\n## 区间预览\n\n| 分组 | 起点 | 终点 | 宽度 | 点数 | 包含最佳点 | 判断 |\n| --- | --- | --- | --- | --- | --- | --- |\n'
    for r in intervals[:20]:text+='| '+' | '.join(map(cell,[r['group'],r['start'],r['end'],r['width'],r['points'],'是' if r['contains_best'] else '否','符合本次要求' if r['qualified'] else r['reason']]))+' |\n'
    if not intervals:text+='\n没有可展示的近最佳片段；请查看逐点条件和分数缺项。\n'
    text+='\n分组与区间预览最多20项；完整表保留不足宽度的片段及并列、重叠窗口，不擅自选出唯一范围。\n\n## 为什么这样划分\n\n'
    text+=f"参数列 {cell(parameter)}，分数列 {cell(score)}，{cfg['direction']}；比较方式：{cfg['smoothing']}，近最佳容差 {cfg['tolerance']}（分数单位）。最大相邻间隔 {cfg['max_gap']}，最小宽度 {cfg['min_width']}，最大宽度 {cfg['max_width'] or '未限制'}（均为参数单位），至少 {cfg['min_points']} 个采样点。\n\n"
    text+=('没有声明条件，本次仅分析分数范围，未检查限制。\n\n' if not result['constraints'] else '全部已声明条件满足才参与，未知或缺失不会放行。逐条件实际值和比较原因可下载。\n\n')
    text+='不满足、未知、分数无效、分数不在容差内的中间点均断开，不能跨过它们合并区间；相邻间隔过大也断开。平滑只在连续可用点内按点数平均，边缘用已有点，不跨条件失败和扫描缺口。最大宽度启用时保留最大连续窗口，端点只取输入中已有的参数值。\n\n'
    text+='## 逐点预览\n\n| 分组 | 参数 | 原分数 | 比较分 | 条件 | 近最佳 | 原因/中断 |\n| --- | --- | --- | --- | --- | --- | --- |\n'
    status={'pass':'满足已声明条件','fail':'不满足','unknown':'待补充','unchecked':'未检查限制'}
    for p in points[:12]:text+='| '+' | '.join(map(cell,[p[group] if group else '全部扫描',p[parameter],p[score],p['interval_comparison_score'],status[p['interval_condition_status']],
        '是' if p['interval_near_best'] else '否','；'.join(v for v in [p['interval_reason'],p['interval_break_before']] if v)]))+' |\n'
    text+='\n逐点预览最多12行，完整结果按组和数值参数排序，原记录号保留。\n\n## 下一步\n\n核对分数含义、单位、扫描覆盖及已声明条件。缺口可通过补充实际计算或测量再分析；信息不明确时保留缺项。修改条件、容差或宽度后重跑只重新分析这张扫描表，不训练或预测，旧结果保留。\n\n'+result['scope']
    if cfg['origin']=='模型计算结果':text+=' 当前数值来自模型，符合分数规则并不等于实际测量已通过。'
    return text
