// hermes-issue-sync — Desktop half: the Issue Sync pane, a ⌘K command, a status-bar chip,
// and a badge on synced kanban cards. Data comes from the plugin's own backend
// (dashboard/plugin_api.py → GET /api/plugins/hermes-issue-sync/status), which only reads the
// snapshot the gateway writes after each sync pass. No secrets reach this file.
// Namespace import on purpose: a named import of something an older Desktop lacks fails the whole
// file at load ("does not provide an export named …"). Optional surfaces are looked up here and
// skipped (or replaced by a plain fallback) when absent, so the pane works on older builds too.
import * as sdk from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const { host, useQuery } = sdk
const PANES_AREA = sdk.PANES_AREA ?? 'panes'
const PALETTE_AREA = sdk.PALETTE_AREA // absent → no ⌘K row
const STATUSBAR_RIGHT = sdk.STATUSBAR_AREAS?.right ?? 'statusBar.right'
const KANBAN_BADGES = sdk.KANBAN_CARD_AREAS?.badges // absent on Desktop builds before 2026-10-23

const StatusDot = sdk.StatusDot ?? (() => jsx('span', { children: '•' }))
const Badge = sdk.Badge ?? (({ children }) => jsx('span', { className: 'rounded border px-1 text-[0.6875rem]', children }))
const ScrollArea = sdk.ScrollArea ?? (({ children }) => jsx('div', { className: 'h-full overflow-auto', children }))
const EmptyState =
  sdk.EmptyState ??
  (({ title, description }) =>
    jsxs('div', { className: 'p-3 text-xs', children: [jsx('div', { className: 'font-medium', children: title }), description] }))
const relativeTime = sdk.relativeTime ?? (ms => new Date(ms).toLocaleString())

const PLUGIN_ID = 'hermes-issue-sync'
const PANE_ID = `${PLUGIN_ID}:pane`
const POLL_MS = 10_000
const TRACKER_LABEL = { linear: 'Linear', jira: 'Jira', fake: 'Fake' }

// card id → { key, waiting, error }; refreshed by every poll, read by the card badges.
let cardIndex = new Map()

function indexCards(snapshot) {
  const next = new Map()
  for (const conn of snapshot?.connections ?? []) {
    for (const item of conn.items ?? []) {
      if (item.card_id) {
        next.set(item.card_id, { key: item.key, waiting: item.waiting, error: item.error, tracker: conn.tracker })
      }
    }
  }
  cardIndex = next
}

function ago(seconds) {
  return seconds ? relativeTime(seconds * 1000) : 'never'
}

function itemTone(item) {
  if (item.error) return 'bad'
  if (item.waiting) return 'warn'
  if (item.card_status === 'done') return 'muted'
  return 'good'
}

function Muted({ children, className = '' }) {
  return jsx('span', { className: `text-(--ui-text-tertiary) ${className}`, children })
}

function IssueLink({ ctx, item }) {
  if (!item.url) return jsx('span', { className: 'font-mono', children: item.key })
  return jsx('button', {
    type: 'button',
    className: 'font-mono underline-offset-2 hover:underline',
    title: `Open ${item.key}`,
    onClick: () => ctx.os.openExternal(item.url),
    children: item.key
  })
}

function ItemRow({ ctx, item }) {
  return jsxs('div', {
    className: 'flex flex-col gap-0.5 rounded px-2 py-1 hover:bg-(--ui-bg-tertiary)',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2 text-xs',
        children: [
          jsx(StatusDot, { tone: itemTone(item) }),
          jsx(IssueLink, { ctx, item }),
          jsx('span', { className: 'min-w-0 flex-1 truncate', title: item.title, children: item.title }),
          jsx(Muted, { children: `card: ${item.card_status ?? '—'}` }),
          jsx(Muted, { children: `issue: ${item.tracker_state ?? '—'}` })
        ]
      }),
      item.waiting &&
        jsxs('div', {
          className: 'pl-4 text-xs text-amber-600',
          children: ['Waiting on you: ', jsx('span', { className: 'italic', children: item.question || 'see the issue' })]
        }),
      item.error && jsx('div', { className: 'pl-4 text-xs text-destructive', children: item.error })
    ]
  })
}

// Pure: one group per kanban board. Several connections can feed the same board; their issues
// and errors are merged under one header. Order follows the first connection that names a board.
export function groupByBoard(connections) {
  const boards = new Map()
  for (const conn of connections ?? []) {
    const name = conn.board || 'default'
    if (!boards.has(name)) boards.set(name, { name, sources: [], items: [], problems: [], linked: 0, waiting: 0, errors: 0, lastPoll: null })
    const b = boards.get(name)
    const tracker = TRACKER_LABEL[conn.tracker] ?? conn.tracker ?? '?'
    b.sources.push(`${tracker} · ${conn.projects?.length ? conn.projects.join(', ') : 'all projects'}`)
    if (conn.error) b.problems.push(`${conn.name}: not running: ${conn.error}`)
    if (conn.last_error) b.problems.push(`${conn.name}: last sync failed: ${conn.last_error}`)
    if (conn.backoff_until) b.problems.push(`${conn.name}: rate limited; retrying ${ago(conn.backoff_until)}`)
    for (const item of conn.items ?? []) b.items.push({ ...item, conn: conn.name })
    b.linked += conn.counts?.linked ?? 0
    b.waiting += conn.counts?.waiting ?? 0
    b.errors += conn.counts?.errors ?? 0
    if (conn.last_poll_at && (!b.lastPoll || conn.last_poll_at > b.lastPoll)) b.lastPoll = conn.last_poll_at
  }
  for (const b of boards.values()) {
    b.tone = b.errors || b.problems.length ? 'bad' : b.waiting ? 'warn' : 'good'
  }
  return [...boards.values()]
}

const COLLAPSED_KEY = 'collapsedBoards'

function useCollapsed(ctx) {
  // Saved per plugin on this Desktop (ctx.storage); boards start expanded.
  const [collapsed, setCollapsed] = useState(() => {
    const v = ctx.storage?.get?.(COLLAPSED_KEY, [])
    return new Set(Array.isArray(v) ? v : [])
  })
  const toggle = name => {
    const next = new Set(collapsed)
    next.has(name) ? next.delete(name) : next.add(name)
    ctx.storage?.set?.(COLLAPSED_KEY, [...next])
    setCollapsed(next)
  }
  return [collapsed, toggle]
}

function BoardGroup({ ctx, board, open, onToggle }) {
  return jsxs('section', {
    className: 'flex flex-col gap-0.5',
    children: [
      jsxs('div', {
        role: 'button',
        tabIndex: 0,
        'aria-expanded': open,
        title: open ? 'Collapse' : 'Expand',
        className: 'flex cursor-pointer select-none flex-wrap items-center gap-2 rounded border border-(--ui-bg-tertiary) px-2 py-1 text-xs hover:bg-(--ui-bg-tertiary)',
        onClick: onToggle,
        onKeyDown: e => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            onToggle()
          }
        },
        children: [
          jsx(StatusDot, { tone: board.tone }),
          jsx('span', { className: 'font-medium', children: board.name }),
          ...board.sources.map((src, i) => jsx(Muted, { children: src }, `src${i}`)),
          board.waiting ? jsx(Badge, { variant: 'outline', children: `${board.waiting} waiting` }) : null,
          jsx(Muted, { className: 'ml-auto', children: `${board.linked} linked · checked ${ago(board.lastPoll)}` }),
          jsx('span', { 'aria-hidden': true, className: 'w-3 text-center', children: open ? '▲' : '▼' })
        ]
      }),
      // Problems stay visible even when the board is collapsed, so collapsing never hides a failure.
      ...board.problems.map((p, i) => jsx('div', { className: 'pl-6 text-xs text-destructive', children: p }, `p${i}`)),
      open &&
        jsx('div', {
          className: 'flex flex-col pl-5',
          children: board.items.length
            ? board.items.map(item => jsx(ItemRow, { ctx, item }, `${item.conn}:${item.key}`))
            : jsx(Muted, { className: 'px-2 py-1 text-xs', children: 'No synced issues yet.' })
        })
    ]
  })
}

function useSnapshot(ctx) {
  return useQuery({
    queryKey: [ctx.source, 'status'],
    queryFn: async () => {
      const data = await ctx.rest('/status')
      indexCards(data)
      return data
    },
    refetchInterval: POLL_MS
  })
}

function SyncPane({ ctx }) {
  const { data, error, isLoading } = useSnapshot(ctx)
  const [collapsed, toggle] = useCollapsed(ctx)
  if (isLoading) return jsx(Muted, { className: 'p-3 text-xs', children: 'Loading sync status…' })
  if (error) {
    return jsx(EmptyState, {
      title: 'Issue Sync backend not reachable',
      description: 'Enable hermes-issue-sync in Capabilities → Plugins (both halves), then reload.'
    })
  }
  const connections = data?.connections ?? []
  if (!connections.length) {
    return jsx(EmptyState, {
      title: 'Nothing synced yet',
      description: 'Add a connection under plugins.entries.hermes-issue-sync.settings.connections, or wait for the first sync.'
    })
  }
  return jsx(ScrollArea, {
    className: 'h-full',
    children: jsxs('div', {
      className: 'flex flex-col gap-3 p-2',
      children: [
        data.enabled === false && jsx(Muted, { className: 'px-2 text-xs', children: 'Sync is turned off in settings.' }),
        ...groupByBoard(connections).map(board =>
          jsx(BoardGroup, { ctx, board, open: !collapsed.has(board.name), onToggle: () => toggle(board.name) }, board.name)
        )
      ]
    })
  })
}

// Pure: what the chip says for a query state. Always returns something, so a broken backend shows
// up as a red "Issue Sync: error" chip instead of nothing.
export function chipState({ data, error, isLoading }) {
  if (error) {
    const why = error?.message || String(error)
    return { tone: 'bad', label: 'Issue Sync: error', title: `Issue Sync backend not reachable: ${why}. Click to open the pane.` }
  }
  if (isLoading && !data) return { tone: 'muted', label: 'Issue Sync…', title: 'Loading sync status' }
  const connections = data?.connections ?? []
  if (!connections.length) {
    return { tone: 'muted', label: 'Issue Sync: not set up', title: 'No connections configured on this Hermes backend' }
  }
  if (data.enabled === false) return { tone: 'muted', label: 'Issue Sync: off', title: 'Sync is turned off in settings' }
  const waiting = connections.reduce((n, c) => n + (c.counts?.waiting ?? 0), 0)
  const errors = connections.reduce((n, c) => n + (c.counts?.errors ?? 0), 0)
  if (errors) return { tone: 'bad', label: `${errors} sync error${errors > 1 ? 's' : ''}`, title: 'Click to see the errors' }
  if (waiting) return { tone: 'warn', label: `${waiting} waiting`, title: 'Questions waiting on an answer in the tracker' }
  return { tone: 'good', label: 'issues synced', title: 'Open the Issue Sync pane' }
}

function StatusChip({ ctx }) {
  const { tone, label, title } = chipState(useSnapshot(ctx))
  return jsxs('button', {
    type: 'button',
    className: 'flex items-center gap-1 px-1.5 text-[0.6875rem] text-(--ui-text-tertiary)',
    title,
    onClick: () => togglePanel(ctx),
    children: [jsx(StatusDot, { tone }), label]
  })
}

// The panel is opened on demand with host.openWorkspace, so closing it only closes the tab:
// sync, the chip and the card badges stay active. (A contributed pane can't do that: closing a
// plugin's only pane turns the whole plugin off.) Older Desktops without openWorkspace fall back
// to a regular docked pane.
let closePanel = null

export function togglePanel(ctx, h = host) {
  if (typeof h.openWorkspace !== 'function') {
    h.undismissPane?.(PANE_ID)
    h.revealPane?.(PANE_ID)
    return 'revealed'
  }
  if (closePanel) {
    const close = closePanel
    closePanel = null
    close()
    return 'closed'
  }
  closePanel = h.openWorkspace(PLUGIN_ID, {
    title: 'Issue Sync',
    dock: { pane: 'workspace', pos: 'bottom' },
    render: () => jsx(SyncPane, { ctx }),
    onClose: () => {
      closePanel = null
    }
  })
  return 'opened'
}

export default {
  id: PLUGIN_ID,
  name: 'Issue Sync',
  register(ctx) {
    cardIndex = new Map()
    ctx.onDispose(() => {
      cardIndex = new Map()
      const close = closePanel
      closePanel = null
      close?.()
    })
    // `/issues show` on the backend broadcasts plugin.hermes-issue-sync.pane.show.
    const onEvent = ctx.onEvent ?? host.onEvent
    const stop = onEvent?.(`plugin.${PLUGIN_ID}.pane.show`, () => togglePanel(ctx))
    if (typeof stop === 'function' && !ctx.onEvent) ctx.onDispose(stop)
    const useWorkspace = typeof host.openWorkspace === 'function'
    const contributions = [
      !useWorkspace && {
        id: 'pane',
        area: PANES_AREA,
        title: 'Issue Sync',
        data: { placement: 'bottom', dock: { pane: 'workspace', pos: 'bottom' }, height: '220px' },
        render: () => jsx(SyncPane, { ctx })
      },
      {
        id: 'open-pane',
        area: PALETTE_AREA,
        data: {
          id: 'open-pane',
          label: 'Issue Sync: toggle panel',
          keywords: ['linear', 'jira', 'issues', 'sync', 'tracker'],
          run: () => togglePanel(ctx)
        }
      },
      {
        id: 'chip',
        area: STATUSBAR_RIGHT,
        order: 140,
        render: () => jsx(StatusChip, { ctx })
      },
      {
        id: 'card-badge',
        area: KANBAN_BADGES,
        data: {
          decorate: card => {
            const hit = cardIndex.get(card.id)
            if (!hit) return null
            const tone = hit.error ? 'red' : hit.waiting ? 'orange' : 'neutral'
            const tooltip = hit.error
              ? `${hit.key}: ${hit.error}`
              : hit.waiting
                ? `${hit.key} is waiting on an answer in ${TRACKER_LABEL[hit.tracker] ?? hit.tracker}`
                : `Synced with ${TRACKER_LABEL[hit.tracker] ?? hit.tracker} issue ${hit.key}`
            return { label: hit.key, tone, tooltip }
          }
        }
      }
    ]
    // Skip any surface this Desktop build does not have (area undefined).
    ctx.registerMany(contributions.filter(c => c && c.area))
  }
}
