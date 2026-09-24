"""Portable report-to-slides code, copied into editable workflow nodes."""
import hashlib
import json
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path
from uuid import uuid4


def source(value, limit=2_000_000):
    p = Path(str(value or ''))
    if p.is_absolute() or '..' in p.parts or not p.parts or p.parts[0] not in ('requirement-package', 'results', 'solution'):
        raise ValueError('请选择当前项目的资料、结果或汇报稿文件')
    if not p.resolve().is_relative_to(Path.cwd().resolve()) or any(x.is_symlink() for x in [p, *p.parents]):
        raise ValueError('不支持项目外文件或符号链接')
    if not p.is_file() or p.stat().st_size > limit:
        raise ValueError('文件不存在或超出本流程的大小限制：' + p.name)
    return p


def units(text):
    return sum(1 if unicodedata.east_asian_width(c) in ('W', 'F') else .58 for c in text)


def wrap(text, width):
    lines, current, size = [], '', 0
    for c in text:
        weight = units(c)
        if c == '\n' or (current and size + weight > width):
            lines.append(current); current, size = '', 0
        if c != '\n': current += c; size += weight
    if current or not lines: lines.append(current)
    return lines


def cells(line):
    text = line.strip()
    if text.startswith('|'): text = text[1:]
    if text.endswith('|') and not text.endswith('\\|'): text = text[:-1]
    return [v.strip().replace('\\|', '|') for v in re.split(r'(?<!\\)\|', text)]


def plain(text):
    # Preserve URLs and source markers as visible text, never fetch them.
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1 (\2)', text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    return re.sub(r'`([^`]+)`', r'\1', text)


def parse(text):
    lines = text.splitlines(); blocks = []; heading = '报告内容'; i = 0
    while i < len(lines):
        line = lines[i].strip(); start = i + 1
        if not line or re.fullmatch(r'(?:---+|___+|\*\*\*+)', line): i += 1; continue
        h = re.match(r'^#{1,6}\s+(.+)$', line)
        if h:
            heading = plain(h[1]).strip()
            if units(heading) > 52: raise ValueError(f'第{start}行标题过长，请拆为简短标题和正文')
            blocks.append({'kind':'heading','title':heading,'start':start,'end':start}); i += 1; continue
        if re.match(r'^(`{3,}|~{3,})', line):
            fence = re.match(r'^(`{3,}|~{3,})', line)[1]; content = []; i += 1
            while i < len(lines) and not re.fullmatch(re.escape(fence[0])+r'{'+str(len(fence))+r',}\s*', lines[i].strip()):
                content.append(lines[i]); i += 1
            if i == len(lines): raise ValueError(f'第{start}行代码块没有结束标记，请修正汇报稿')
            i += 1
            blocks.append({'kind':'text','title':heading,'text':'\n'.join(content),'start':start,'end':i}); continue
        image = re.fullmatch(r'!\[([^\]]*)\]\(([^)]+)\)', line)
        if image:
            if units(image[1]) > 45: raise ValueError(f'第{start}行图片说明过长，请把详细解释移到正文')
            blocks.append({'kind':'image','title':heading,'label':image[1],'path':image[2],'start':start,'end':start}); i += 1; continue
        if i+1 < len(lines) and '|' in line:
            sep = cells(lines[i+1])
            if sep and all(re.fullmatch(r':?-{3,}:?', x) for x in sep):
                headers = [plain(x) for x in cells(line)]; rows=[]; i += 2
                if len(headers) != len(sep): raise ValueError(f'第{start}行表头与分隔线列数不同')
                while i < len(lines) and '|' in lines[i] and lines[i].strip():
                    row = [plain(x) for x in cells(lines[i])]
                    if len(row) != len(headers): raise ValueError(f'第{i+1}行表格列数不一致')
                    rows.append({'values':row,'line':i+1}); i += 1
                blocks.append({'kind':'table','title':heading,'headers':headers,'rows':rows,'start':start,'end':i}); continue
        bullet = re.match(r'^(?:[-*+]\s+|\d+[.)]\s+)(.*)$', line)
        blocks.append({'kind':'text','title':heading,'text':plain(bullet[1] if bullet else line),
                       'bullet':bool(bullet),'start':start,'end':start}); i += 1
    return blocks


def paginate(blocks):
    pages=[]; pending=None
    def flush():
        nonlocal pending
        if pending: pages.append(pending); pending=None
    for block in blocks:
        kind=block['kind']; title=block['title']
        if kind=='heading': flush(); continue
        if kind=='text':
            for text in ['\n'.join(part) for part in _chunks(wrap(block['text'], 36), 10)]:
                height=max(.44, len(text.splitlines())*.37+.13)
                if pending and (pending['title']!=title or pending['height']+height>4.75): flush()
                if not pending: pending={'kind':'text','title':title,'items':[],'height':0,'start':block['start'],'end':block['end']}
                pending['items'].append({'text':text,'bullet':block.get('bullet',False),'height':height})
                pending['height']+=height; pending['end']=block['end']
        elif kind=='table':
            flush(); width=len(block['headers'])
            if not width or width>30: raise ValueError('表格需有1至30列，请拆分过宽表格')
            groups=[list(range(width))] if width<=4 else [[0,*range(i,min(i+3,width))] for i in range(1,width,3)]
            for indices in groups:
                column_width=11.9/len(indices); chars=(column_width*72-20)/18
                header=[ '\n'.join(wrap(block['headers'][i],chars)) for i in indices]
                header_h=max(.48,max(len(x.splitlines()) for x in header)*.29+.18)
                if header_h>1.4: raise ValueError(f'第{block["start"]}行表头过长，请缩短名称并把说明放正文')
                rows=[]; heights=[header_h]; first_line=block['start']
                def emit():
                    if rows or not block['rows']:
                        pages.append({'kind':'table','title':title,'headers':header,'rows':list(rows),'row_heights':list(heights),
                                      'columns':[i+1 for i in indices],'start':first_line,'end':last_line if rows else block['end']})
                last_line=block['end']
                for row in block['rows']:
                    values=['\n'.join(wrap(row['values'][i],chars)) for i in indices]
                    height=max(.46,max(len(x.splitlines()) for x in values)*.29+.18)
                    if height+header_h>4.8: raise ValueError(f'第{row["line"]}行单元格内容过多，请拆分该行或移为正文')
                    if sum(heights)+height>4.8:
                        emit(); rows=[]; heights=[header_h]; first_line=row['line']
                    rows.append(values); heights.append(height); last_line=row['line']
                emit()
        else:
            flush(); pages.append(dict(block))
    flush()
    return pages


def _chunks(values, size):
    return [values[i:i+size] for i in range(0,len(values),size)]


def prepare(inputs):
    path=str(inputs.get('source_path') or '').strip(); outline=str(inputs.get('outline') or '').strip()
    if bool(path)==bool(outline): raise ValueError('请选择一份汇报稿文件，或直接填写汇报稿，二者只填一项')
    original=source(path) if path else None
    if original and original.suffix.lower() not in ('.md','.txt'): raise ValueError('请选择Markdown或纯文本汇报稿；其他资料请先通过对话整理')
    text=original.read_text(encoding='utf-8-sig') if original else outline
    if not text.strip() or len(text)>60000: raise ValueError('汇报稿不能为空，且最多60000字；请先整理需要展示的内容')
    blocks=parse(text)
    title=str(inputs.get('title') or '').strip() or next((b['title'] for b in blocks if b['kind']=='heading'),'项目汇报')
    if units(title)>48: raise ValueError('汇报标题过长，请缩短至两行以内')
    subtitle=str(inputs.get('subtitle') or '').strip()
    if units(subtitle)>80: raise ValueError('副标题过长，请把详细说明放在正文')
    pages=paginate(blocks)
    if not pages: raise ValueError('汇报稿只有标题或空白，请加入需要展示的正文')
    if len(pages)>59: raise ValueError('汇报稿超过60页，请拆为多份汇报后再生成')
    folder=Path('results')/('presentation-'+uuid4().hex);folder.mkdir(parents=True)
    (folder/'source.md').write_text(text,encoding='utf-8')
    images=[]
    for page in pages:
        if page['kind']!='image': continue
        candidate=page['path']
        if original and not candidate.startswith(('requirement-package/','results/','solution/')):
            candidate=str(original.parent/candidate)
        image=source(candidate,10_000_000)
        if image.suffix.lower() not in ('.png','.jpg','.jpeg'): raise ValueError('图片请使用当前项目的PNG或JPEG，不能引用外网地址')
        from PIL import Image
        with Image.open(image) as im:
            if not im.width or not im.height or im.width*im.height>30_000_000: raise ValueError('图片尺寸过大或无效')
            im.verify()
        digest=hashlib.sha256(image.read_bytes()).hexdigest();dest=folder/('image-'+str(len(images))+image.suffix.lower())
        shutil.copyfile(image,dest);page['path']=str(dest);images.append({'source':str(image),'snapshot':str(dest),'sha256':digest})
    manifest={'schema':'lilies_presentation_v1','title':title,'subtitle':subtitle,'source':str(original) if original else '直接填写的汇报稿',
              'source_sha256':hashlib.sha256(text.encode()).hexdigest(),'pages':pages,'images':images,'folder':str(folder),
              'source_snapshot':str(folder/'source.md'),'font':'Noto Sans CJK SC','renderer':'pptxgenjs 4.0.1'}
    (folder/'input.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    return {'manifest_path':str(folder/'input.json'),'pages':len(pages)+1}


RENDER_JS = r'''
const fs=require('node:fs');
const PptxGenJS=require('pptxgenjs');
const data=JSON.parse(fs.readFileSync(0,'utf8'));
const pptx=new PptxGenJS();pptx.layout='LAYOUT_WIDE';
pptx.author='Lilies';pptx.subject='基于项目汇报稿生成';pptx.title=data.title;
pptx.lang='zh-CN';pptx.theme={headFontFace:data.font,bodyFontFace:data.font,lang:'zh-CN'};
function text(slide,value,x,y,w,h,size,extra={}){
  slide.addText(value,{x,y,w,h,fontFace:data.font,fontSize:size,color:'233344',margin:0,breakLine:false,
    valign:'top',paraSpaceAfterPt:0,wrap:true,...extra});
}
function base(title,index,source){
  const s=pptx.addSlide();s.background={color:'FFFFFF'};
  text(s,title,.7,.45,11.9,1.05,32,{bold:true,color:'244F68'});
  text(s,String(index)+' / '+String(data.pages.length+1),11.55,7.04,1.05,.22,11,{align:'right',color:'687785'});
  s.addNotes('资料：'+data.source+'\n内容版本：'+data.source_sha256+'\n'+source);
  return s;
}
const cover=pptx.addSlide();cover.background={color:'F5F8FA'};
text(cover,data.title,.9,2.25,11.5,1.65,42,{bold:true,color:'244F68'});
if(data.subtitle)text(cover,data.subtitle,.94,4.28,11.3,1.1,21,{color:'526673'});
cover.addNotes('资料：'+data.source+'\n内容版本：'+data.source_sha256);
data.pages.forEach((p,i)=>{
  const s=base(p.title,i+2,'原文第'+p.start+'至'+p.end+'行'+(p.columns?'，表格第'+p.columns.join('、')+'列':''));
  if(p.kind==='text'){
    let y=1.7;
    p.items.forEach(item=>{text(s,item.text,.8,y,11.7,item.height,22,item.bullet?{bullet:{indent:14},hanging:3}:{});y+=item.height;});
  }else if(p.kind==='table'){
    const rows=[p.headers.map(v=>({text:v,options:{bold:true,fill:'E9F0F4',color:'244F68'}})),...p.rows];
    s.addTable(rows,{x:.7,y:1.68,w:11.9,colW:Array(p.headers.length).fill(11.9/p.headers.length),rowH:p.row_heights,
      autoPage:false,fontFace:data.font,fontSize:18,color:'233344',margin:[6,10,6,10],valign:'top',
      border:{type:'solid',color:'D5DFE5',pt:.6}});
  }else if(p.kind==='image'){
    s.addImage({path:p.path,...pptx.imageSizingContain(p.path,.75,1.6,11.8,4.8),altText:p.label||p.title});
    if(p.label)text(s,p.label,.8,6.45,11.6,.42,17,{color:'526673'});
  }
});
pptx.writeFile({fileName:data.folder+'/presentation.pptx'}).then(()=>console.log(JSON.stringify({slides:data.pages.length+1})));
'''


def render(inputs):
    manifest_path=source(inputs['prepared']['manifest_path']);data=json.loads(manifest_path.read_text())
    if data.get('schema')!='lilies_presentation_v1': raise ValueError('请使用本流程准备的汇报输入')
    folder=source(data['source_snapshot']).parent
    if folder!=manifest_path.parent or str(folder)!=data['folder']: raise ValueError('汇报输入与保存目录不一致')
    if hashlib.sha256((folder/'source.md').read_bytes()).hexdigest()!=data['source_sha256']: raise ValueError('汇报快照已改变，请新建运行')
    for image in data['images']:
        snapshot=source(image['snapshot'],10_000_000)
        if snapshot.parent!=folder: raise ValueError('图片快照不属于本次汇报，请新建运行')
        if hashlib.sha256(snapshot.read_bytes()).hexdigest()!=image['sha256']:raise ValueError('图片快照已改变，请新建运行')
    snapshots={image['snapshot'] for image in data['images']}
    if any(page['path'] not in snapshots for page in data['pages'] if page['kind']=='image'):
        raise ValueError('图片与本次汇报快照不一致，请新建运行')
    if not shutil.which('node') or not shutil.which('soffice') or not shutil.which('pdftoppm'):
        raise ValueError('当前文档环境缺少Node、LibreOffice Impress或PDF预览组件，请负责人使用带演示文稿能力的文档镜像')
    result=subprocess.run(['node','-e',RENDER_JS],input=json.dumps(data,ensure_ascii=False),capture_output=True,text=True,timeout=60)
    if result.returncode:
        if "Cannot find module 'pptxgenjs'" in result.stderr:raise ValueError('当前文档环境没有PptxGenJS，请负责人更新文档镜像后继续原运行')
        raise ValueError('幻灯片生成失败：'+result.stderr[-1500:])
    profile='/tmp/lilies-presentation-'+uuid4().hex
    converted=subprocess.run(['soffice','-env:UserInstallation=file://'+profile,'--headless','--convert-to','pdf','--outdir',str(folder),str(folder/'presentation.pptx')],capture_output=True,text=True,timeout=75)
    if converted.returncode or not (folder/'presentation.pdf').is_file():raise ValueError('PPTX已保存，但PDF预览生成失败，请检查文档环境的Impress组件')
    from pypdf import PdfReader
    pdf=PdfReader(folder/'presentation.pdf')
    if len(pdf.pages)!=len(data['pages'])+1:raise ValueError('PDF页数与汇报内容不一致，PPTX和输入已保存以便修复')
    previews=subprocess.run(['pdftoppm','-scale-to','1400','-png',str(folder/'presentation.pdf'),str(folder/'slide')],capture_output=True,text=True,timeout=75)
    if previews.returncode:raise ValueError('PPTX和PDF已保存，逐页预览失败，请检查PDF组件')
    paths=[(folder/'presentation.pptx','可编辑汇报 PPTX'),(folder/'presentation.pdf','汇报 PDF'),(folder/'source.md','原汇报稿 Markdown'),(manifest_path,'本次内容与出处 JSON')]
    paths.extend((f,f'第{i+1}页预览 PNG') for i,f in enumerate(sorted(folder.glob('slide-*.png'),key=lambda f:int(f.stem.split('-')[-1]))))
    md='# '+data['title']+'\n\n共'+str(len(pdf.pages))+'页。正文和表格可在PPTX中编辑，图片保持原比例。\n\n'
    md+='原汇报稿内容未调用模型改写，页内内容对应原文行号保存在演讲者备注。汇报结论沿用输入，准确性和适用范围以原分析为准。\n\n'
    md+='\n'.join('- ['+label+']('+str(path)+')' for path,label in paths)
    (folder/'report.md').write_text(md,encoding='utf-8');paths.append((folder/'report.md','汇报文件说明 Markdown'))
    return {'slides':len(pdf.pages),'source_sha256':data['source_sha256'],'pptx_path':str(folder/'presentation.pptx'),
            'pdf_path':str(folder/'presentation.pdf'),'markdown':md,'artifacts':[{'file_path':str(path),'label':label} for path,label in paths]}


def main(inputs):
    if inputs.get('operation')=='prepare':return prepare(inputs)
    if inputs.get('operation')=='render':return render(inputs)
    raise ValueError('请选择准备汇报稿或生成汇报文件')
