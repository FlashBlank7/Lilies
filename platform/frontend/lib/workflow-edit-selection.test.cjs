const test = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { Module } = require('node:module')
const { join } = require('node:path')
const ts = require('typescript')

const selectionModulePath = join(__dirname, 'workflow-edit-selection.ts')
const selectionModule = new Module(selectionModulePath, module)
selectionModule.filename = selectionModulePath
selectionModule.paths = module.paths
selectionModule._compile(
  ts.transpileModule(
    readFileSync(selectionModulePath, 'utf8'),
    {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2020,
      },
      fileName: selectionModulePath,
    },
  ).outputText,
  selectionModulePath,
)
const {
  boundedCanvasMenuPosition,
  buildNaturalLanguageEditRequest,
  draftIdentityChanged,
  naturalLanguageEditContextMatches,
  normalizeWorkflowEditSelection,
  selectionForEdgeContextMenu,
  selectionForNodeContextMenu,
  selectionForRightDrag,
} = selectionModule.exports

const edges = [
  { id: 'edge-a-b', source: 'a', target: 'b' },
  { id: 'edge-b-c', source: 'b', target: 'c' },
]

test('right-drag selection includes only edges contained by selected nodes', () => {
  assert.deepEqual(selectionForRightDrag(['a', 'b'], edges), {
    nodeIds: ['a', 'b'],
    edgeIds: ['edge-a-b'],
  })
})

test('right-clicking an already selected node keeps the group context', () => {
  assert.deepEqual(
    selectionForNodeContextMenu('b', { nodeIds: ['a', 'b'], edgeIds: [] }, edges),
    { nodeIds: ['a', 'b'], edgeIds: ['edge-a-b'] },
  )
})

test('right-clicking outside the current node selection selects only that node', () => {
  assert.deepEqual(
    selectionForNodeContextMenu('c', { nodeIds: ['a', 'b'], edgeIds: ['edge-a-b'] }, edges),
    { nodeIds: ['c'], edgeIds: [] },
  )
})

test('right-clicking an edge carries its endpoints into edit context', () => {
  assert.deepEqual(
    selectionForEdgeContextMenu(edges[1], { nodeIds: [], edgeIds: [] }),
    { nodeIds: ['b', 'c'], edgeIds: ['edge-b-c'] },
  )
})

test('draft refresh prunes deleted nodes and edges while preserving valid context', () => {
  assert.deepEqual(
    normalizeWorkflowEditSelection(
      { nodeIds: ['a', 'missing', 'a'], edgeIds: ['edge-a-b', 'missing'] },
      ['a', 'b'],
      [edges[0]],
    ),
    { nodeIds: ['a'], edgeIds: ['edge-a-b'] },
  )
})

test('a changed draft identity invalidates an already reviewed preview', () => {
  assert.equal(
    draftIdentityChanged(
      { revision: 7, content_hash: 'hash-a' },
      { revision: 8, content_hash: 'hash-b' },
    ),
    true,
  )
  assert.equal(
    draftIdentityChanged(
      { revision: 7, content_hash: 'hash-a' },
      { revision: 7, content_hash: 'hash-a' },
    ),
    false,
  )
  assert.equal(
    draftIdentityChanged(null, { revision: 0, content_hash: 'hash-a' }),
    false,
  )
})

test('a delayed preview is accepted only for the exact instruction, scope, and draft', () => {
  const expected = {
    instruction: '修改所选步骤',
    selection: { nodeIds: ['a', 'b'], edgeIds: ['edge-a-b'] },
    revision: 7,
    contentHash: 'hash-a',
  }
  assert.equal(naturalLanguageEditContextMatches(expected, {
    ...expected,
    selection: { nodeIds: ['a', 'b'], edgeIds: ['edge-a-b'] },
  }), true)
  assert.equal(naturalLanguageEditContextMatches(expected, {
    ...expected,
    instruction: '另一条指令',
  }), false)
  assert.equal(naturalLanguageEditContextMatches(expected, {
    ...expected,
    selection: { nodeIds: ['b'], edgeIds: [] },
  }), false)
  assert.equal(naturalLanguageEditContextMatches(expected, {
    ...expected,
    revision: 8,
  }), false)
})

test('natural-language request is bound to selection and optimistic draft identity', () => {
  assert.deepEqual(
    buildNaturalLanguageEditRequest(
      '  调整所选步骤  ',
      { nodeIds: ['a', 'a'], edgeIds: ['edge-a-b'] },
      { revision: 7, content_hash: 'sha256:draft' },
      'idem-1',
      true,
    ),
    {
      instruction: '调整所选步骤',
      node_ids: ['a'],
      edge_ids: ['edge-a-b'],
      expected_revision: 7,
      expected_content_hash: 'sha256:draft',
      idempotency_key: 'idem-1',
      preview_only: true,
    },
  )
})

test('apply request binds the exact reviewed task and preview digest', () => {
  assert.deepEqual(
    buildNaturalLanguageEditRequest(
      '调整所选步骤',
      { nodeIds: ['a'], edgeIds: [] },
      { revision: 7, content_hash: 'sha256:draft' },
      'idem-apply',
      false,
      { taskId: 'preview-task', digest: 'sha256:preview' },
    ),
    {
      instruction: '调整所选步骤',
      node_ids: ['a'],
      edge_ids: [],
      expected_revision: 7,
      expected_content_hash: 'sha256:draft',
      idempotency_key: 'idem-apply',
      preview_only: false,
      preview_task_id: 'preview-task',
      expected_preview_digest: 'sha256:preview',
    },
  )
})

test('context menu remains inside a small canvas', () => {
  assert.deepEqual(
    boundedCanvasMenuPosition({ x: 790, y: 590 }, { width: 800, height: 600 }, { width: 260, height: 180 }),
    { x: 528, y: 408 },
  )
})

test('studio wires right-drag and current selection to the atomic natural-language edit flow', () => {
  const studio = readFileSync(join(__dirname, '..', 'app', 'applications', '[id]', 'page.tsx'), 'utf8')
  assert.match(studio, /onMouseDownCapture=\{handleCanvasMouseDownCapture\}/)
  assert.match(studio, /onNodeContextMenu=\{handleNodeContextMenu\}/)
  assert.match(studio, /onSelectionContextMenu=\{handleSelectionContextMenu\}/)
  assert.match(studio, /data-workflow-edit-context-menu="open"/)
  assert.match(studio, /draft\/natural-language-edit/)
  assert.match(studio, /digest: patchPreview\.preview_digest/)
  assert.match(studio, /if \(changed\) setWorkflowEditContextMenu\(null\)/)
  assert.match(studio, /workflowEditPreviewGenerationRef/)
  assert.match(studio, /naturalLanguageEditContextMatches\(previewContext, latestContext\)/)
  assert.doesNotMatch(studio, /draft\/preview-patch/)
})
