// hermes-issue-sync — Desktop half: the Issue Sync pane, a ⌘K command, a status-bar chip,
// and a badge on synced kanban cards. Data comes from the plugin's own backend
// (dashboard/plugin_api.py → GET /api/plugins/hermes-issue-sync/status), which only reads the
// snapshot the gateway writes after each sync pass. No secrets reach this file.
// Namespace import on purpose: a named import of something an older Desktop lacks fails the whole
// file at load ("does not provide an export named …"). Optional surfaces are looked up here and
// skipped (or replaced by a plain fallback) when absent, so the pane works on older builds too.
import * as sdk from '@hermes/plugin-sdk'
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

function connectionTone(conn) {
  if (conn.error || conn.last_error) return 'bad'
  if (!conn.running) return 'muted'
  if (conn.counts?.waiting) return 'warn'
  return 'good'
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

function ConnectionBlock({ ctx, conn }) {
  const tracker = TRACKER_LABEL[conn.tracker] ?? conn.tracker
  const projects = conn.projects?.length ? conn.projects.join(', ') : 'all projects'
  return jsxs('section', {
    className: 'flex flex-col gap-1',
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-2 px-2 text-xs',
        children: [
          jsx(StatusDot, { tone: connectionTone(conn) }),
          jsx('span', { className: 'font-medium', children: tracker }),
          jsx('span', { children: projects }),
          jsx(Muted, { children: `label ${conn.label}` }),
          jsx(Muted, { children: `→ board ${conn.board}` }),
          conn.counts?.waiting ? jsx(Badge, { variant: 'outline', children: `${conn.counts.waiting} waiting` }) : null,
          jsx(Muted, { className: 'ml-auto', children: `${conn.counts?.linked ?? 0} linked · checked ${ago(conn.last_poll_at)}` })
        ]
      }),
      conn.error && jsx('div', { className: 'px-2 text-xs text-destructive', children: `Not running: ${conn.error}` }),
      conn.last_error && jsx('div', { className: 'px-2 text-xs text-destructive', children: `Last sync failed: ${conn.last_error}` }),
      conn.backoff_until && jsx(Muted, { className: 'px-2 text-xs', children: `Rate limited; retrying ${ago(conn.backoff_until)}` }),
      ...(conn.items ?? []).map(item => jsx(ItemRow, { ctx, item }, `${conn.name}:${item.key}`))
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
        jsx(Muted, { className: 'px-2 text-[0.6875rem] uppercase tracking-wide', children: 'Synced with' }),
        ...connections.map(conn => jsx(ConnectionBlock, { ctx, conn }, conn.name))
      ]
    })
  })
}

function StatusChip({ ctx }) {
  const { data } = useSnapshot(ctx)
  const connections = data?.connections ?? []
  if (!connections.length) return null
  const waiting = connections.reduce((n, c) => n + (c.counts?.waiting ?? 0), 0)
  const errors = connections.reduce((n, c) => n + (c.counts?.errors ?? 0), 0)
  const tone = errors ? 'bad' : waiting ? 'warn' : 'good'
  const label = waiting ? `${waiting} waiting` : errors ? `${errors} sync error${errors > 1 ? 's' : ''}` : 'issues synced'
  return jsxs('button', {
    type: 'button',
    className: 'flex items-center gap-1 px-1.5 text-[0.6875rem] text-(--ui-text-tertiary)',
    title: 'Open the Issue Sync pane',
    onClick: () => host.revealPane?.(PANE_ID),
    children: [jsx(StatusDot, { tone }), label]
  })
}

export default {
  id: PLUGIN_ID,
  name: 'Issue Sync',
  register(ctx) {
    cardIndex = new Map()
    ctx.onDispose(() => {
      cardIndex = new Map()
    })
    const contributions = [
      {
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
          label: 'Issue Sync: show pane',
          keywords: ['linear', 'jira', 'issues', 'sync', 'tracker'],
          run: () => host.revealPane?.(PANE_ID)
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
    ctx.registerMany(contributions.filter(c => c.area))
  }
}
