'use client'

import { useMemo, useState, type DragEvent } from 'react'

import type { Block, WorkflowNode } from '@/lib/platform'
import type { Locale } from '@/lib/i18n'

import styles from './block-catalog-panel.module.css'

const BLOCK_DRAG_MIME = 'application/x-lilies-block-type'

type BlockCatalogPanelProps = {
  blocks: Block[]
  expanded: boolean
  locale: Locale
  onAdd: (block: Block) => Promise<void>
  onToggle: () => void
}

type BlockPurposeProps = {
  block: Block
  locale: Locale
  compact?: boolean
}

type UndefinedBusinessWorkflowNoticeProps = {
  expanded: boolean
  locale: Locale
  nodeCount: number
  onToggle: () => void
}

type BlockInstanceDetailsProps = {
  locale: Locale
  node: WorkflowNode
}

const ZH_BLOCK_FALLBACKS: Record<string, { title: string; description: string; category: string }> = {
  connector_action: {
    title: '连接器操作',
    description: '通过租户、权限和请求策略执行一个已登记的外部系统接口操作。',
    category: '集成',
  },
}

function localizedTitle(block: Block, locale: Locale) {
  if (locale === 'zh' && ZH_BLOCK_FALLBACKS[block.type]) {
    return ZH_BLOCK_FALLBACKS[block.type].title
  }
  return block.editor?.i18n?.[locale]?.title || block.title
}

function localizedDescription(block: Block, locale: Locale) {
  if (locale === 'zh' && ZH_BLOCK_FALLBACKS[block.type]) {
    return ZH_BLOCK_FALLBACKS[block.type].description
  }
  return block.editor?.i18n?.[locale]?.description || block.description
}

function localizedCategory(block: Block, locale: Locale) {
  if (block.block_kind === 'agent_architecture') {
    return locale === 'zh' ? 'Agent 架构积木' : 'Agent Architecture'
  }
  if (block.block_kind === 'legacy_compatibility') {
    return locale === 'zh' ? '旧版兼容积木' : 'Legacy Compatibility'
  }
  if (locale === 'zh' && ZH_BLOCK_FALLBACKS[block.type]) {
    return ZH_BLOCK_FALLBACKS[block.type].category
  }
  return block.editor?.i18n?.[locale]?.category || block.category
}

function groupedBlocks(blocks: Block[], locale: Locale) {
  return blocks.reduce<Record<string, Block[]>>((groups, block) => {
    const category = localizedCategory(block, locale)
    groups[category] ||= []
    groups[category].push(block)
    return groups
  }, {})
}

function portSummary(ports: Block['input_ports'] | Block['output_ports']) {
  if (!ports.length) return '—'
  return ports.map(port => `${port.name}: ${port.value_type}`).join(' · ')
}

function manualItems(items: string[] | undefined, limit: number) {
  return (items || []).filter(Boolean).slice(0, limit)
}

function localizedManualItems(items: string[] | undefined, locale: Locale, limit: number) {
  const available = manualItems(items, limit)
  if (locale !== 'zh') return available
  return available.filter(item => /[\u3400-\u9fff]/u.test(item))
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function asNodeList(value: unknown) {
  return Array.isArray(value)
    ? value.filter(item => item && typeof item === 'object' && !Array.isArray(item)).map(item => item as Record<string, unknown>)
    : []
}

export function UndefinedBusinessWorkflowNotice({
  expanded,
  locale,
  nodeCount,
  onToggle,
}: UndefinedBusinessWorkflowNoticeProps) {
  const zh = locale === 'zh'
  return <section
    className={styles.undefinedBusiness}
    data-business-workflow-definition="missing"
    data-collapsed={expanded ? 'false' : 'true'}
  >
    <div><strong>{zh ? '这还不是已定义的业务工作流' : 'This is not a defined business workflow yet'}</strong><span>{zh ? `${nodeCount} 个当前节点` : `${nodeCount} current nodes`}</span></div>
    <button
      aria-expanded={expanded}
      className={styles.noticeToggle}
      data-studio-chrome-toggle="undefined-business"
      onClick={onToggle}
      type="button"
    >{expanded ? (zh ? '收起' : 'Collapse') : (zh ? '展开' : 'Expand')}</button>
    <p aria-hidden={!expanded}>{zh
      ? '该应用没有业务需求或业务目标。画布中的节点可能只是初始化骨架或技术探针，不能据此判断项目已经完成。'
      : 'This application has no business requirement or business goal. Its nodes may be initialization scaffolding or technical probes and must not be treated as a completed project.'}</p>
  </section>
}

export function BlockInstanceDetails({ locale, node }: BlockInstanceDetailsProps) {
  const zh = locale === 'zh'
  const config = asRecord(node.config)
  if (node.type === 'connector_action') {
    return <section className={styles.instance} data-block-instance-details="connector_action">
      <strong>{zh ? '这个节点实际调用什么' : 'What this node actually calls'}</strong>
      <dl>
        <div><dt>Connector</dt><dd>{String(config.connector_id || '—')}@{String(config.connector_version || 1)}</dd></div>
        <div><dt>Operation</dt><dd>{String(config.operation_id || '—')}</dd></div>
        <div><dt>{zh ? '执行方式' : 'Mode'}</dt><dd>{String(config.execution_mode || '—')}</dd></div>
        <div><dt>{zh ? '租户' : 'Tenant'}</dt><dd>{String(config.tenant_id || '—')}</dd></div>
      </dl>
      <p>{zh
        ? 'Connector Action 一次只执行一个已登记接口操作；它不是把整条业务流程藏进一个超级积木。'
        : 'A Connector Action executes one registered operation. It does not hide an entire business workflow inside one super-brick.'}</p>
    </section>
  }
  if (node.type === 'iteration' || node.type === 'loop') {
    const workflow = asRecord(config.workflow)
    const nestedNodes = asNodeList(workflow.nodes)
    const nestedEdges = Array.isArray(workflow.edges) ? workflow.edges.length : 0
    return <section className={styles.instance} data-block-instance-details={node.type}>
      <strong>{zh ? '内层工作流不是黑盒' : 'Nested workflow contents'}</strong>
      <p>{zh
        ? `这个容器内部有 ${nestedNodes.length} 个节点、${nestedEdges} 条连线。`
        : `This container has ${nestedNodes.length} nested nodes and ${nestedEdges} edges.`}</p>
      {nestedNodes.length > 0 && <ol>{nestedNodes.map((item, index) => <li key={String(item.id || index)}>
        <b>{String(item.title || item.id || `${zh ? '节点' : 'Node'} ${index + 1}`)}</b>
        <code>{String(item.type || 'unknown')}</code>
      </li>)}</ol>}
    </section>
  }
  return null
}

export function BlockPurpose({ block, locale, compact = false }: BlockPurposeProps) {
  const zh = locale === 'zh'
  const whenToUse = localizedManualItems(block.when_to_use, locale, compact ? 2 : 4)
  const boundaries = localizedManualItems(block.composability_constraints, locale, compact ? 2 : 4)
  const antiPatterns = localizedManualItems(block.anti_patterns, locale, compact ? 1 : 3)
  const example = block.examples?.[0]
  const rawExampleDescription = typeof example?.description === 'string' ? example.description : ''
  const exampleDescription = !zh || /[\u3400-\u9fff]/u.test(rawExampleDescription)
    ? rawExampleDescription
    : ''
  const exampleConnection = typeof example?.connection === 'string' ? example.connection : ''

  return <section className={styles.purpose} data-block-purpose={block.type}>
    <div className={styles.purposeHeading}>
      <span>{localizedCategory(block, locale)}</span>
      <code>{block.type}</code>
    </div>
    <h3>{localizedTitle(block, locale)}</h3>
    <p className={styles.description}>{localizedDescription(block, locale)}</p>
    <dl className={styles.ports}>
      <div><dt>{zh ? '输入' : 'Inputs'}</dt><dd>{portSummary(block.input_ports)}</dd></div>
      <div><dt>{zh ? '输出' : 'Outputs'}</dt><dd>{portSummary(block.output_ports)}</dd></div>
    </dl>
    {whenToUse.length > 0 && <div className={styles.manualSection}>
      <strong>{zh ? '什么时候使用' : 'When to use it'}</strong>
      <ul>{whenToUse.map((item, index) => <li key={`${block.type}-when-${index}`}>{item}</li>)}</ul>
    </div>}
    {(exampleDescription || exampleConnection) && <div className={styles.example}>
      <strong>{zh ? '示例' : 'Example'}</strong>
      {exampleDescription && <span>{exampleDescription}</span>}
      {exampleConnection && <code>{exampleConnection}</code>}
    </div>}
    {(boundaries.length > 0 || antiPatterns.length > 0) && <details className={styles.boundaries}>
      <summary>{zh ? '边界与不适用情况' : 'Boundaries and anti-patterns'}</summary>
      {boundaries.length > 0 && <ul>{boundaries.map((item, index) => <li key={`${block.type}-boundary-${index}`}>{item}</li>)}</ul>}
      {antiPatterns.length > 0 && <ul>{antiPatterns.map((item, index) => <li key={`${block.type}-anti-${index}`}>{item}</li>)}</ul>}
    </details>}
  </section>
}

export function BlockCatalogPanel({ blocks, expanded, locale, onAdd, onToggle }: BlockCatalogPanelProps) {
  const zh = locale === 'zh'
  const [query, setQuery] = useState('')
  const [selectedType, setSelectedType] = useState<string | null>(null)
  const [addingType, setAddingType] = useState<string | null>(null)
  const filtered = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase()
    if (!needle) return blocks
    return blocks.filter(block => [
      block.type,
      localizedTitle(block, locale),
      localizedDescription(block, locale),
      localizedCategory(block, locale),
      block.manual_summary || '',
      ...(block.when_to_use || []),
    ].join(' ').toLocaleLowerCase().includes(needle))
  }, [blocks, locale, query])
  const grouped = useMemo(() => groupedBlocks(filtered, locale), [filtered, locale])

  async function addSelected(block: Block) {
    if (addingType) return
    setAddingType(block.type)
    try {
      await onAdd(block)
    } finally {
      setAddingType(null)
    }
  }

  function startBlockDrag(event: DragEvent<HTMLButtonElement>, block: Block) {
    event.dataTransfer.effectAllowed = 'copy'
    event.dataTransfer.setData(BLOCK_DRAG_MIME, block.type)
    event.dataTransfer.setData('text/plain', block.type)
  }

  return <aside
    aria-label={zh ? '积木库' : 'Brick library'}
    className={`block-panel ${styles.panel}`}
    data-block-catalog="inspect-before-add"
    data-collapsed={expanded ? 'false' : 'true'}
    id="studio-block-catalog"
  >
    <button
      aria-controls="studio-block-catalog-content"
      aria-expanded={expanded}
      className={styles.panelToggle}
      data-studio-chrome-toggle="catalog"
      onClick={onToggle}
      title={expanded ? (zh ? '收起积木库' : 'Collapse brick library') : (zh ? '展开积木库' : 'Expand brick library')}
      type="button"
    >
      <span aria-hidden="true">{expanded ? '›' : '‹'}</span>
      <b>{zh ? '积木' : 'Bricks'}</b>
    </button>
    <div className={styles.content} id="studio-block-catalog-content">
      <header className={styles.heading}>
        <div><strong>{zh ? '积木库' : 'Brick library'}</strong><small>{zh ? `${blocks.length} 个可用积木` : `${blocks.length} available`}</small></div>
        <span>{zh ? '可拖到画布；点击先看说明' : 'Drag to canvas, or click to inspect'}</span>
        <input
          aria-label={zh ? '搜索积木' : 'Search bricks'}
          onChange={event => setQuery(event.target.value)}
          placeholder={zh ? '搜索用途、输入或名称' : 'Search purpose or name'}
          type="search"
          value={query}
        />
      </header>
      <div className={styles.groups}>
      {Object.entries(grouped).map(([category, items]) => <section className={styles.group} key={category}>
        <h2>{category}</h2>
        {items.map(block => {
          const selected = selectedType === block.type
          return <article className={`${styles.item} ${selected ? styles.selected : ''}`} data-block-catalog-item={block.type} key={block.type}>
            <button
              aria-expanded={selected}
              className={styles.inspect}
              draggable
              onClick={() => setSelectedType(current => current === block.type ? null : block.type)}
              onDragStart={event => startBlockDrag(event, block)}
              type="button"
            >
              <i aria-hidden="true" />
              <span><b>{localizedTitle(block, locale)}</b><small>{localizedDescription(block, locale)}</small></span>
              <em>{selected ? (zh ? '收起' : 'Close') : (zh ? '查看' : 'Inspect')}</em>
            </button>
            {selected && <div className={styles.expanded} data-block-catalog-details={block.type}>
              <BlockPurpose block={block} compact locale={locale} />
              <button
                className={styles.add}
                disabled={Boolean(addingType)}
                onClick={() => void addSelected(block)}
                type="button"
              >
                {addingType === block.type
                  ? (zh ? '正在添加…' : 'Adding…')
                  : (zh ? '添加到工作流并打开配置' : 'Add and open configuration')}
              </button>
            </div>}
          </article>
        })}
      </section>)}
      {filtered.length === 0 && <div className={styles.empty}>{zh ? '没有匹配的积木。' : 'No matching bricks.'}</div>}
      </div>
    </div>
  </aside>
}
