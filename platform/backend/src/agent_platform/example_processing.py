"""Portable code-node implementation. This source is copied into editable examples.

Uses standard-library CSV/text processing; XLSX and document readers are optional
dependencies of the selected execution environment, never silently substituted.
"""
import csv
import difflib
import json
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4


def source(value):
    path = Path(str(value))
    if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] not in ('requirement-package', 'results'):
        raise ValueError('请选择当前项目资料或结果文件')
    if not path.resolve().is_relative_to(Path.cwd().resolve()) or any(p.is_symlink() for p in [path, *path.parents]):
        raise ValueError('不能读取项目外文件或符号链接')
    if not path.is_file():
        raise ValueError('文件不存在：' + str(path))
    if path.stat().st_size > 10_000_000:
        raise ValueError('示例支持 10 MB 以内资料，请拆分后处理')
    return path


def table(value):
    path = source(value)
    if path.suffix.lower() == '.xlsx':
        try:
            from openpyxl import load_workbook
        except ImportError as error:
            raise ValueError('当前执行环境缺少 openpyxl，请配置该依赖或将资料导出为 CSV') from error
        book = load_workbook(path, read_only=True, data_only=True)
        try:
            data = list(book.active.values)
        finally:
            book.close()
        fields = [str(v or '').strip() for v in data[0]] if data else []
        rows = [dict(zip(fields, [str(v) if v is not None else '' for v in row])) for row in data[1:]]
    elif path.suffix.lower() in ('.csv', '.tsv'):
        with path.open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream, delimiter='\t' if path.suffix.lower() == '.tsv' else ',')
            fields = reader.fieldnames or []
            rows = list(reader)
    else:
        raise ValueError('请选择 CSV、TSV 或 XLSX 表格')
    if not fields or len(fields) != len(set(fields)) or any(not f for f in fields):
        raise ValueError('表头为空或含重名字段，请修正后重试')
    if any(None in row for row in rows):
        raise ValueError('存在列数超过表头的记录，请检查分隔符')
    if not rows:
        raise ValueError('表格没有数据行')
    if len(rows) > 100000:
        raise ValueError('示例最多处理 10 万行，请拆分资料')
    return fields, rows


def document(value):
    path = source(value)
    suffix = path.suffix.lower()
    if suffix == '.pdf':
        try:
            from pypdf import PdfReader
        except ImportError as error:
            raise ValueError('当前执行环境缺少 pypdf，请配置依赖或提供文字版') from error
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ValueError('请提供未加密 PDF')
        parts = [(f'第 {i+1} 页', p.extract_text() or '') for i, p in enumerate(reader.pages)]
    elif suffix == '.docx':
        from zipfile import ZipFile
        import xml.etree.ElementTree as ET
        with ZipFile(path) as archive:
            if archive.getinfo('word/document.xml').file_size > 10_000_000:
                raise ValueError('文档正文过大，请拆分')
            root = ET.fromstring(archive.read('word/document.xml'))
        ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        parts = [(f'第 {i+1} 段', ''.join(p.itertext())) for i, p in enumerate(root.findall('.//w:p', ns))]
    elif suffix in ('.txt', '.md', '.csv', '.tsv'):
        parts = [(f'第 {i+1} 行', text) for i, text in enumerate(path.read_text(encoding='utf-8-sig').splitlines())]
    else:
        raise ValueError('请选择 TXT、Markdown、文字 PDF 或 DOCX')
    if not any(text.strip() for _, text in parts):
        raise ValueError('没有可读取的正文；扫描件需要先转为文字')
    text = '\n'.join(f'[{path.name} · {location}] {text}' for location, text in parts)
    if len(text) > 45000:
        raise ValueError('正文超过示例单次读取范围，请拆分文件；没有截断后假装已读完整')
    return text


def decimal(value, row, field):
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            raise InvalidOperation()
        return number
    except (InvalidOperation, ValueError):
        raise ValueError(f'第 {row} 行的 {field} 不是有效数字：{value}') from None


def export(result, rows=None):
    folder = Path('results/examples') / str(uuid4())
    folder.mkdir(parents=True)
    files = []
    for name, content in [('report.md', result['markdown']), ('result.json', json.dumps(result, ensure_ascii=False, indent=2))]:
        path = folder / name
        path.write_text(content, encoding='utf-8')
        files.append({'file_path': str(path), 'label': '报告 Markdown' if name.endswith('.md') else '结构化结果 JSON'})
    if rows:
        path = folder / 'details.csv'
        with path.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            # Protect spreadsheet readers while retaining raw values in result.json.
            writer.writerows({k: "'"+v if isinstance(v, str) and v.startswith(('=', '+', '-', '@')) else v for k,v in row.items()} for row in rows)
        files.append({'file_path': str(path), 'label': '明细 CSV'})
    return {**result, 'artifacts': files}


def main(inputs):
    mode = inputs['operation']
    if mode == 'export':
        return export({'markdown': str(inputs['text']), 'sources': inputs.get('sources', [])})
    if mode == 'read':
        paths = [inputs['source_path']]
        if inputs.get('second_path'):
            paths.append(inputs['second_path'])
        texts = [document(p) for p in paths]
        if sum(map(len, texts)) > 45000:
            raise ValueError('合计正文过长，请拆分资料')
        return {'text': '\n\n'.join(texts), 'sources': paths}
    if mode == 'diff':
        before, after = document(inputs['source_path']), document(inputs['second_path'])
        # Compare content; provenance labels should not make every line differ.
        a = [line.split('] ',1)[-1] for line in before.splitlines()]
        b = [line.split('] ',1)[-1] for line in after.splitlines()]
        changes = [{'old_start': i+1, 'new_start': j+1, 'type': tag, 'before': '\n'.join(a[i:ii]), 'after': '\n'.join(b[j:jj])}
                   for tag,i,ii,j,jj in difflib.SequenceMatcher(None,a,b,autojunk=False).get_opcodes() if tag!='equal']
        return export({'markdown': '# 文档差异\n\n共 '+str(len(changes))+' 处变化。以下是原文差异，未推断业务影响。\n\n'+ '\n\n'.join(
            f"原文第 {c['old_start']} 处 → 新文第 {c['new_start']} 处\n\n删除／原文：{c['before']}\n\n新增／新文：{c['after']}" for c in changes),
            'changes': changes, 'sources': [inputs['source_path'],inputs['second_path']]}, changes)
    fields, rows = table(inputs['source_path'])
    if mode == 'profile':
        counts = Counter(tuple(row.get(f,'') for f in fields) for row in rows)
        missing = {f:sum(not str(r.get(f) or '').strip() for r in rows) for f in fields}
        duplicates = sum(n-1 for n in counts.values())
        distributions = {f:dict(Counter(str(r.get(f) or '') for r in rows).most_common(10)) for f in fields}
        issues = [{'row':i+2, 'reason':'缺失字段', 'fields':','.join(f for f in fields if not str(r.get(f) or '').strip())}
                  for i,r in enumerate(rows) if any(not str(r.get(f) or '').strip() for f in fields)]
        from statistics import quantiles
        for f in fields:
            values=[]
            for i,r in enumerate(rows):
                if not str(r.get(f) or '').strip():continue
                try:values.append((i+2,decimal(r[f],i+2,f)))
                except ValueError:values=[];break
            if len(values)<4:continue
            q1,_,q3=quantiles([float(v) for _,v in values],n=4,method='inclusive')
            spread=q3-q1
            if spread<=0:continue
            for i,v in values:
                if float(v)<q1-1.5*spread or float(v)>q3+1.5*spread:
                    issues.append({'row':i,'reason':'数值超出四分位距范围（需人工判断）','fields':f})
        return export({'markdown':f'# 数据体检\n\n{len(rows)} 行，{len(fields)} 列，完全重复 {duplicates} 行。缺失统计：{missing}。\n\n频数展示每列前 10 项；数值异常使用1.5倍四分位距提示，不代表数据错误，未自动删除。',
                       'rows':len(rows),'missing':missing,'duplicates':duplicates,'distributions':distributions,'issues':issues},issues)
    if mode == 'join':
        right_fields, right = table(inputs['second_path'])
        key = inputs.get('key','sample_id')
        if key not in fields or key not in right_fields:
            raise ValueError('两份资料都必须包含关联字段：'+key)
        keys = [r.get(key,'') for r in right]
        if any(not k for k in keys) or len(keys)!=len(set(keys)):
            raise ValueError('右表关联键为空或重复，请先去重或明确一对多规则')
        index = {r[key]:r for r in right}
        result = [{**r, **{'right_'+f:index.get(r.get(key),{}).get(f,'') for f in right_fields if f!=key},
                   'match_status':'matched' if r.get(key) in index else 'unmatched'} for r in rows]
        missing = sum(r['match_status']=='unmatched' for r in result)
        return export({'markdown':f'# 关联结果\n\n保留左表 {len(rows)} 行，其中 {missing} 行未匹配；未匹配记录没有被删除。', 'rows':len(result),'unmatched':missing,'preview':result[:20]},result)
    if mode == 'expenses':
        if inputs.get('second_path'):
            more_fields, more = table(inputs['second_path'])
            if set(fields)!=set(more_fields):
                raise ValueError('两份费用表的字段必须一致')
            rows += more
        required = ['date','category','amount','currency','merchant']
        if not set(required)<=set(fields):
            raise ValueError('费用表需要字段：'+', '.join(required))
        totals, seen, details = defaultdict(Decimal),set(),[]
        for i,row in enumerate(rows):
            try:
                month = date.fromisoformat(row['date']).strftime('%Y-%m')
            except ValueError:
                raise ValueError(f'第 {i+2} 行日期须为 YYYY-MM-DD') from None
            amount=decimal(row['amount'],i+2,'amount')
            if any(not str(row[f] or '').strip() for f in ['category','currency','merchant']):
                raise ValueError(f'第 {i+2} 行缺少类别、币种或商户')
            fingerprint=(row['date'],row['merchant'],amount,row['currency'])
            duplicate=fingerprint in seen; seen.add(fingerprint)
            totals[(month,row['category'],row['currency'])]+=amount
            details.append({**row,'suspected_duplicate':'yes' if duplicate else 'no'})
        summary=[{'month':m,'category':c,'currency':u,'amount':str(n)} for (m,c,u),n in sorted(totals.items())]
        return export({'markdown':'# 费用汇总\n\n按币种分别计算；疑似重复只标记、不自动扣除。\n\n'+'\n'.join(f"- {r['month']} / {r['category']}：{r['amount']} {r['currency']}" for r in summary),
                       'summary':summary,'suspected_duplicates':sum(r['suspected_duplicate']=='yes' for r in details),'rows':len(rows),'preview':details[:20]},details)
    if mode == 'summary':
        group,value = inputs.get('group','device'),inputs.get('value','value')
        if group not in fields or value not in fields:
            raise ValueError('缺少分组或数值字段：'+group+' / '+value)
        groups=defaultdict(list)
        for i,r in enumerate(rows):
            groups[r[group] or '未填写'].append(decimal(r[value],i+2,value))
        summary=[{'group':k,'count':len(v),'sum':str(sum(v)),'mean':str(sum(v)/len(v))} for k,v in sorted(groups.items())]
        result=export({'markdown':'# 数据汇总\n\n'+'\n'.join(f"- {r['group']}：{r['count']} 条，合计 {r['sum']}，均值 {r['mean']}" for r in summary), 'summary':summary},summary)
        from html import escape
        maximum=max([abs(float(r['sum'])) for r in summary]+[1])
        svg='<svg xmlns="http://www.w3.org/2000/svg" width="640" height="'+str(45*len(summary)+40)+'">'
        for i,r in enumerate(summary):
            svg+=f'<text x="8" y="{i*45+26}">{escape(r["group"])}</text><rect x="160" y="{i*45+8}" width="{abs(float(r["sum"]))/maximum*330}" height="24" fill="#567bd8"/><text x="510" y="{i*45+26}">{escape(r["sum"])}</text>'
        chart=Path(result['artifacts'][0]['file_path']).parent/'chart.svg';chart.write_text(svg+'</svg>')
        result['artifacts'].append({'file_path':str(chart),'label':'分组汇总图 SVG（条长表示绝对值）'})
        return result
    raise ValueError('未知处理方法：'+mode)
