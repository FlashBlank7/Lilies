export type WorkflowEditSelection = {
  nodeIds: string[]
  edgeIds: string[]
}

export type WorkflowEditEdge = {
  id: string
  source: string
  target: string
}

export type NaturalLanguageEditRequest = {
  instruction: string
  node_ids: string[]
  edge_ids: string[]
  expected_revision: number
  expected_content_hash: string
  idempotency_key: string
  preview_only: boolean
  preview_task_id?: string
  expected_preview_digest?: string
}

export type NaturalLanguageEditContext = {
  instruction: string
  selection: WorkflowEditSelection
  revision: number
  contentHash: string
}

export function naturalLanguageEditContextMatches(
  expected: NaturalLanguageEditContext,
  current: NaturalLanguageEditContext,
) {
  return expected.instruction === current.instruction
    && expected.revision === current.revision
    && expected.contentHash === current.contentHash
    && expected.selection.nodeIds.join('\u0000') === current.selection.nodeIds.join('\u0000')
    && expected.selection.edgeIds.join('\u0000') === current.selection.edgeIds.join('\u0000')
}

export function draftIdentityChanged(
  previous: { revision: number; content_hash: string } | null,
  next: { revision: number; content_hash: string },
) {
  return previous !== null
    && (
      previous.revision !== next.revision
      || previous.content_hash !== next.content_hash
    )
}

function uniqueIds(ids: string[]) {
  return [...new Set(ids.filter(Boolean))]
}

export function edgesInsideNodeSelection(nodeIds: string[], edges: WorkflowEditEdge[]) {
  const selected = new Set(nodeIds)
  return edges
    .filter(edge => selected.has(edge.source) && selected.has(edge.target))
    .map(edge => edge.id)
}

export function normalizeWorkflowEditSelection(
  selection: WorkflowEditSelection,
  availableNodeIds: string[],
  availableEdges: WorkflowEditEdge[],
): WorkflowEditSelection {
  const availableNodes = new Set(availableNodeIds)
  const availableEdgeIds = new Set(availableEdges.map(edge => edge.id))
  return {
    nodeIds: uniqueIds(selection.nodeIds).filter(id => availableNodes.has(id)),
    edgeIds: uniqueIds(selection.edgeIds).filter(id => availableEdgeIds.has(id)),
  }
}

export function selectionForNodeContextMenu(
  clickedNodeId: string,
  current: WorkflowEditSelection,
  availableEdges: WorkflowEditEdge[],
): WorkflowEditSelection {
  if (current.nodeIds.includes(clickedNodeId)) {
    return {
      nodeIds: uniqueIds(current.nodeIds),
      edgeIds: uniqueIds([
        ...current.edgeIds,
        ...edgesInsideNodeSelection(current.nodeIds, availableEdges),
      ]),
    }
  }
  return { nodeIds: [clickedNodeId], edgeIds: [] }
}

export function selectionForEdgeContextMenu(
  clickedEdge: WorkflowEditEdge,
  current: WorkflowEditSelection,
): WorkflowEditSelection {
  if (current.edgeIds.includes(clickedEdge.id)) {
    return {
      nodeIds: uniqueIds(current.nodeIds),
      edgeIds: uniqueIds(current.edgeIds),
    }
  }
  return {
    nodeIds: uniqueIds([clickedEdge.source, clickedEdge.target]),
    edgeIds: [clickedEdge.id],
  }
}

export function selectionForRightDrag(nodeIds: string[], availableEdges: WorkflowEditEdge[]) {
  const normalizedNodeIds = uniqueIds(nodeIds)
  return {
    nodeIds: normalizedNodeIds,
    edgeIds: edgesInsideNodeSelection(normalizedNodeIds, availableEdges),
  }
}

export function buildNaturalLanguageEditRequest(
  instruction: string,
  selection: WorkflowEditSelection,
  draft: { revision: number; content_hash: string },
  idempotencyKey: string,
  previewOnly: boolean,
  preview?: { taskId: string; digest: string },
): NaturalLanguageEditRequest {
  const request: NaturalLanguageEditRequest = {
    instruction: instruction.trim(),
    node_ids: uniqueIds(selection.nodeIds),
    edge_ids: uniqueIds(selection.edgeIds),
    expected_revision: draft.revision,
    expected_content_hash: draft.content_hash,
    idempotency_key: idempotencyKey,
    preview_only: previewOnly,
  }
  if (preview) {
    request.preview_task_id = preview.taskId
    request.expected_preview_digest = preview.digest
  }
  return request
}

export function boundedCanvasMenuPosition(
  point: { x: number; y: number },
  canvas: { width: number; height: number },
  menu: { width: number; height: number },
  margin = 12,
) {
  return {
    x: Math.max(margin, Math.min(point.x, Math.max(margin, canvas.width - menu.width - margin))),
    y: Math.max(margin, Math.min(point.y, Math.max(margin, canvas.height - menu.height - margin))),
  }
}
