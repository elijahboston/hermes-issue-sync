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

const ScrollArea = sdk.ScrollArea ?? (({ children }) => jsx('div', { className: 'h-full overflow-auto', children }))
const EmptyState =
  sdk.EmptyState ??
  (({ title, description }) =>
    jsxs('div', { className: 'p-3 text-xs', children: [jsx('div', { className: 'font-medium', children: title }), description] }))

const PLUGIN_ID = 'hermes-issue-sync'
const PANE_ID = `${PLUGIN_ID}:pane`
const POLL_MS = 10_000
const TRACKER_LABEL = { linear: 'Linear', jira: 'Jira', fake: 'Fake' }
// Default credential names. The snapshot never carries the configured *_env names.
const TRACKER_KEY_ENV = { linear: 'LINEAR_API_KEY', jira: 'JIRA_API_TOKEN' }
export const SETUP_COMMAND = 'hermes issues setup'

const trackerLabel = t => TRACKER_LABEL[t] ?? t ?? '?'

// ── vocabulary ──────────────────────────────────────────────────────────────────────────────
// One word per idea, shared by the pane, the status-bar chip and the card badges.
export const WORDS = {
  issues: 'issues',
  needs: 'needs you',
  landing: 'landing',
  error: 'error',
  synced: 'synced',
  queued: 'queued',
  running: 'running',
  done: 'done'
}

const plural = (n, word) => `${n} ${n === 1 ? word.replace(/s$/, '') : word}`

// card id → the item plus its kind; refreshed by every poll, read by the card badges.
let cardIndex = new Map()

function indexCards(snapshot) {
  const next = new Map()
  for (const conn of snapshot?.connections ?? []) {
    for (const item of conn.items ?? []) {
      if (item.card_id) next.set(item.card_id, { ...item, kind: cardKind(item), tracker: conn.tracker, conn: conn.name })
    }
  }
  cardIndex = next
}

// ── pure helpers (tested in tests/test_pane_js.py) ──────────────────────────────────────────

// Pure: short relative time: "just now", "23s ago", "4m ago", "2h ago", "3d ago".
export function shortAgo(seconds, now = Date.now() / 1000) {
  if (!seconds) return 'never'
  const d = Math.max(0, Math.round(now - seconds))
  if (d < 5) return 'just now'
  if (d < 60) return `${d}s ago`
  if (d < 3600) return `${Math.floor(d / 60)}m ago`
  if (d < 86400) return `${Math.floor(d / 3600)}h ago`
  return `${Math.floor(d / 86400)}d ago`
}

// Pure: the landing step for a done card, or null. A done card waiting on its land script is
// not a sync mismatch; the tracker only moves to Done once it lands.
export function landingInfo(item) {
  if (item?.card_status !== 'done') return null
  const s = item.landing
  if (s === 'running') return { marker: 'landing', tone: 'warn', line: item.landing_message || 'land script running' }
  if (s === 'waiting') return { marker: 'landing', tone: 'warn', line: item.landing_message || 'waiting to retry the land script' }
  if (s === 'failed') return { marker: 'landing failed', tone: 'bad', line: item.landing_error || 'land script failed' }
  return null
}

export const ISSUE_STATE_TOOLTIP =
  'Last state Issue Sync pushed to the tracker. The tracker may have changed since (e.g. closed by a PR merge).'

// Pure: what the kanban card is doing, as one word of the vocabulary.
// error > needs you > landing > running > done > queued. A failed landing is an error (the row
// says how to fix it), even though the backend also marks it as waiting.
export function cardKind(item) {
  if (item?.error || (item?.landing === 'failed' && item?.card_status === 'done')) return 'error'
  if (item?.waiting || item?.card_status === 'blocked') return 'needs'
  if (landingInfo(item)) return 'landing'
  if (item?.card_status === 'running' || item?.card_status === 'review') return 'running'
  if (item?.card_status === 'done') return 'done'
  return 'queued'
}

// Pure: which of Linear's shapes to draw for the issue's last-pushed state. The backend sends
// tracker_state_kind (from Linear state.type / Jira status category); older snapshots only have
// the name, so fall back to reading it.
export function issueStateKind(item) {
  const k = item?.tracker_state_kind
  if (['todo', 'progress', 'review', 'done', 'canceled'].includes(k)) return k
  const name = String(item?.tracker_state ?? '').trim().toLowerCase()
  if (!name) return null
  if (name.includes('review')) return 'review'
  if (['done', 'closed', 'complete', 'completed', 'resolved', 'merged'].includes(name)) return 'done'
  if (['canceled', 'cancelled', 'duplicate'].includes(name)) return 'canceled'
  if (name.includes('progress') || name === 'doing' || name === 'started') return 'progress'
  return 'todo'
}

// Pure: split a board's items into the rows shown and the done ones folded into "N done".
// Order is kept (the backend already sorts needs you → error → running → queued → done).
// A board with nothing but a few done items shows them directly: a lone "1 done" row hides
// everything there is to see.
export const DONE_SHOWN_ALONE = 3
export function foldDone(items) {
  const active = []
  const done = []
  for (const item of items ?? []) (cardKind(item) === 'done' ? done : active).push(item)
  if (!active.length && done.length <= DONE_SHOWN_ALONE) return { active: done, done: [] }
  return { active, done }
}

// Pure: counts for a collapsed header.
export function countItems(items, extraErrors = 0) {
  let needs = 0
  let errors = extraErrors
  let issues = 0
  for (const item of items ?? []) {
    if (item.card_id) issues += 1
    const k = cardKind(item)
    if (k === 'needs') needs += 1
    if (k === 'error') errors += 1
  }
  return { issues, needs, errors }
}

// Pure: "8 issues · 1 needs you · 1 error". Problems that are zero are left out.
export function countsLabel({ issues, needs, errors }) {
  const parts = [plural(issues, WORDS.issues)]
  if (needs) parts.push(`${needs} ${WORDS.needs}`)
  if (errors) parts.push(plural(errors, 'errors'))
  return parts.join(' · ')
}

// Pure: a raw error → a sentence that says what to do; the raw text goes in the tooltip.
export function friendlyError(raw, { tracker, conn } = {}) {
  const text = String(raw ?? '').trim()
  if (!text) return null
  const name = trackerLabel(tracker)
  const where = conn ? ` for ${conn}` : ''
  const env = TRACKER_KEY_ENV[tracker] ?? 'the API key'
  const rules = [
    [
      /\b(401|403)\b|not authori[sz]ed|unauthori[sz]ed|authentication (required|failed)|invalid api key|could not identify the api token/i,
      { title: `${name} rejected the API key${where}.`, fix: `Update ${env}, then retry.` }
    ],
    [/\b429\b|rate.?limit|too many requests/i, { tone: 'warn', title: `${name} is rate limiting${where}.`, fix: 'Sync backs off and retries on its own.' }],
    [
      /timed? ?out|connection (refused|reset)|unreachable|name or service not known|nodename nor servname|getaddrinfo|urlerror|network is down|could not connect|temporary failure in name resolution/i,
      { title: `Can't reach ${name}${where}.`, fix: 'Check the network or the base URL; sync retries every pass.' }
    ],
    [/not mergeable|merge conflict|cannot be cleanly created/i, { title: 'Landing failed: the PR has merge conflicts.', fix: 'Rebase it, then reply on the issue to retry.' }],
    [/\bland(\.sh| script| command)\b|land_command/i, { title: 'Landing failed: the land script exited with an error.', fix: 'Fix it, then reply on the issue to retry.' }],
    [/no state .* for|cannot move to|reachable statuses|valid states/i, { title: `${name} has no matching state${where}.`, fix: 'Check state_map for this connection.' }],
    [/unknown tracker|no tracker set/i, { title: `Connection${where} is not set up right.`, fix: `Run ${SETUP_COMMAND} to fix it.` }]
  ]
  for (const [re, out] of rules) if (re.test(text)) return { tone: 'bad', ...out, raw: text }
  const first = text.split('\n')[0]
  return { tone: 'bad', title: first.length > 160 ? `${first.slice(0, 157)}…` : first, fix: '', raw: text }
}

// Pure: a failed land script's output → a friendly line. Never read as a tracker auth error.
export function landingError(raw) {
  const text = String(raw ?? '').trim()
  if (/not mergeable|merge conflict|cannot be cleanly created/i.test(text)) {
    return { tone: 'bad', title: 'Landing failed: the PR has merge conflicts.', fix: 'Rebase it, then reply on the issue to retry.', raw: text }
  }
  return { tone: 'bad', title: 'Landing failed.', fix: 'Fix what the land script reports, then reply on the issue to retry.', raw: text || 'land script failed' }
}

// Pure: is sync stale? The newest last_poll_at among running connections is older than
// STALE_FACTOR × the longest poll interval. Sync that is turned off is never stale.
export const STALE_FACTOR = 3
export function staleness(data, now = Date.now() / 1000) {
  const conns = (data?.connections ?? []).filter(c => c.running !== false && !c.error)
  let last = null
  let interval = 0
  for (const c of conns) {
    if (c.last_poll_at && (!last || c.last_poll_at > last)) last = c.last_poll_at
    interval = Math.max(interval, Number(c.poll_interval_seconds) || 60)
  }
  if (data?.enabled === false || !conns.length) return { last, stale: false }
  // Never polled: stale only once the snapshot itself is that old.
  const ref = last ?? data?.generated_at ?? null
  return { last, stale: ref != null && now - ref > STALE_FACTOR * (interval || 60) }
}

// Pure: one group per kanban board. Several connections can feed the same board; their issues
// and errors are merged under one header. Order follows the first connection that names a board.
export function groupByBoard(connections) {
  const boards = new Map()
  for (const conn of connections ?? []) {
    const name = conn.board || 'default'
    if (!boards.has(name)) boards.set(name, { name, sources: [], items: [], problems: [], lastPoll: null })
    const b = boards.get(name)
    b.sources.push({
      tracker: trackerLabel(conn.tracker),
      projects: conn.projects?.length ? conn.projects.join(', ') : 'all projects',
      conn: conn.name
    })
    const where = { tracker: conn.tracker, conn: conn.name }
    if (conn.error) b.problems.push(friendlyError(conn.error, where))
    if (conn.last_error) b.problems.push(friendlyError(conn.last_error, where))
    else if (conn.backoff_until) b.problems.push(friendlyError('rate limited', where))
    for (const item of conn.items ?? []) b.items.push({ ...item, conn: conn.name, tracker: conn.tracker })
    if (conn.last_poll_at && (!b.lastPoll || conn.last_poll_at > b.lastPoll)) b.lastPoll = conn.last_poll_at
  }
  for (const b of boards.values()) {
    b.counts = countItems(b.items, b.problems.filter(p => p.tone === 'bad').length)
    b.tone = b.counts.errors ? 'bad' : b.counts.needs ? 'warn' : 'muted'
    // Where the board syncs from: a tooltip, shown inline only when it has several sources.
    b.sourceTip = b.sources.map(s => `${s.tracker} · ${s.projects} · connection: ${s.conn}`).join('\n')
    b.inlineSources = b.sources.length > 1
  }
  return [...boards.values()]
}

// Pure: tracker → boards. Each tracker (Linear, Jira) is a top-level group; its boards sit under
// it, built only from that tracker's connections, so the same board name can appear under both.
export function groupByTracker(connections) {
  const trackers = new Map()
  for (const conn of connections ?? []) {
    const key = conn.tracker || '?'
    if (!trackers.has(key)) trackers.set(key, [])
    trackers.get(key).push(conn)
  }
  return [...trackers.entries()].map(([key, conns]) => {
    const boards = groupByBoard(conns)
    const counts = boards.reduce(
      (n, b) => ({ issues: n.issues + b.counts.issues, needs: n.needs + b.counts.needs, errors: n.errors + b.counts.errors }),
      { issues: 0, needs: 0, errors: 0 }
    )
    return { key, label: trackerLabel(key), boards, counts, tone: counts.errors ? 'bad' : counts.needs ? 'warn' : 'muted' }
  })
}

// Pure: drop single-child levels. One tracker → its boards are the top level (no tracker
// header). A tracker with one board → that board is folded into the tracker row.
// Nodes: { type: 'board', tracker, board } | { type: 'tracker', tracker, board? }.
export function paneTree(connections) {
  const trackers = groupByTracker(connections)
  if (trackers.length === 1) return trackers[0].boards.map(board => ({ type: 'board', tracker: trackers[0], board }))
  return trackers.map(tracker =>
    tracker.boards.length === 1 ? { type: 'tracker', tracker, board: tracker.boards[0] } : { type: 'tracker', tracker }
  )
}

// Collapse keys: "t:<tracker>" for a tracker group, "b:<tracker>:<board>" for a board, and
// "d:<tracker>:<board>" for its done fold (that one is in the set when OPEN: closed by default).
// Tree connectors, drawn with inline styles so they don't depend on which CSS classes the host
// app ships. "rounded" (default): last branch curves into its row, lines tinted per tracker.
// "lines": square elbows in the theme's stroke colour.
export const TREE_STYLES = ['rounded', 'lines']
const TREE_TINT = { linear: '#5e6ad2', jira: '#2684ff' }
const STROKE = 'var(--ui-stroke-secondary, rgba(128, 128, 128, 0.45))'
const ELBOW_Y = 12 // px from the top of a row to its centre line
const INDENT = 16

// The style comes only from the plugin setting (tree_style), carried in the snapshot.
export function treeStyleOf(data) {
  const fromConfig = data?.display?.tree_style
  return TREE_STYLES.includes(fromConfig) ? fromConfig : 'rounded'
}

export function connectorStyles(style, tracker, last) {
  const color = style === 'rounded' && TREE_TINT[tracker] ? `color-mix(in srgb, ${TREE_TINT[tracker]} 55%, ${STROKE})` : STROKE
  const line = `1.5px solid ${color}`
  const rail = { position: 'absolute', left: 0, top: 0, borderLeft: line, pointerEvents: 'none' }
  const elbow = { position: 'absolute', left: 0, top: ELBOW_Y, width: INDENT - 3, borderTop: line, pointerEvents: 'none' }
  if (!last) return { rail: { ...rail, bottom: 0 }, elbow }
  if (style === 'rounded') {
    return { rail: { ...rail, height: ELBOW_Y + 1, width: INDENT - 3, borderBottom: line, borderBottomLeftRadius: 7 }, elbow: null }
  }
  return { rail: { ...rail, height: ELBOW_Y }, elbow }
}

// Pure: what the status-bar chip shows. Healthy = icon only; words only for problems.
// Always returns something, so a broken backend still shows up as a red chip.
export function chipState({ data, error, isLoading }, now = Date.now() / 1000) {
  if (error) {
    const why = error?.message || String(error)
    return { tone: 'bad', icon: 'alert', label: WORDS.error, title: `Issue Sync backend not reachable: ${why}. Click to open the pane.` }
  }
  if (isLoading && !data) return { tone: 'muted', icon: 'sync', label: '', title: 'Issue Sync: loading' }
  const connections = data?.connections ?? []
  if (!connections.length) return { tone: 'muted', icon: 'sync', label: 'set up', title: "Issue Sync isn't connected yet — click to set up" }
  if (data.enabled === false) return { tone: 'muted', icon: 'sync', label: 'off', title: 'Issue Sync is turned off in settings' }
  const totals = groupByTracker(connections).reduce((n, t) => ({ needs: n.needs + t.counts.needs, errors: n.errors + t.counts.errors }), {
    needs: 0,
    errors: 0
  })
  const parts = []
  if (totals.needs) parts.push(`${totals.needs} ${WORDS.needs}`)
  if (totals.errors) parts.push(plural(totals.errors, 'errors'))
  if (parts.length) {
    return { tone: totals.errors ? 'bad' : 'warn', icon: 'alert', label: parts.join(' · '), title: `Issue Sync: ${parts.join(', ')} — click to open` }
  }
  const { last, stale } = staleness(data, now)
  if (stale) return { tone: 'warn', icon: 'alert', label: 'stale', title: `Issue Sync: last ${WORDS.synced} ${shortAgo(last, now)} — sync may have stopped` }
  return { tone: 'muted', icon: 'sync', label: '', title: `Issue Sync: all ${WORDS.synced} — click to open` }
}

// Pure: the badge on a synced kanban card, in the shared vocabulary.
export function cardBadge(hit) {
  if (!hit) return null
  const name = trackerLabel(hit.tracker)
  const kind = hit.kind ?? cardKind(hit)
  if (kind === 'error') {
    const f = hit.error ? friendlyError(hit.error, { tracker: hit.tracker, conn: hit.conn }) : landingError(hit.landing_error)
    return { label: hit.key, tone: 'red', tooltip: `${hit.key}: ${WORDS.error}. ${f.title}` }
  }
  if (kind === 'needs') return { label: hit.key, tone: 'orange', tooltip: `${hit.key}: ${WORDS.needs}. Answer on the issue in ${name}.` }
  if (kind === 'landing') return { label: hit.key, tone: 'neutral', tooltip: `${hit.key}: ${WORDS.landing}` }
  return { label: hit.key, tone: 'neutral', tooltip: `${name} issue ${hit.key}` }
}

// ── icons ───────────────────────────────────────────────────────────────────────────────────
// Inline SVG on a 14px grid. Colours come from the --isx-* variables below, which follow the
// host's --ui-* tokens and switch between light and dark.

const svg = (children, size = 14) =>
  jsx('svg', { width: size, height: size, viewBox: '0 0 14 14', 'aria-hidden': true, focusable: 'false', children })

const STATE_ICONS = {
  todo: () => svg(jsx('circle', { cx: 7, cy: 7, r: 5.5, fill: 'none', stroke: 'var(--isx-todo)', strokeWidth: 1.5 })),
  progress: () =>
    svg([
      jsx('circle', { cx: 7, cy: 7, r: 5.5, fill: 'none', stroke: 'var(--isx-prog)', strokeWidth: 1.5 }, 'c'),
      jsx('path', { d: 'M7 3.5a3.5 3.5 0 0 1 0 7z', fill: 'var(--isx-prog)' }, 'p')
    ]),
  review: () =>
    svg([
      jsx('circle', { cx: 7, cy: 7, r: 5.5, fill: 'none', stroke: 'var(--isx-ok)', strokeWidth: 1.5 }, 'c'),
      jsx('path', { d: 'M7 3.5a3.5 3.5 0 1 1-3.03 1.75L7 7z', fill: 'var(--isx-ok)' }, 'p')
    ]),
  done: () =>
    svg([
      jsx('circle', { cx: 7, cy: 7, r: 6.25, fill: 'var(--isx-done)' }, 'c'),
      jsx('path', { d: 'M4.3 7.2l1.9 1.9 3.6-3.8', fill: 'none', stroke: '#fff', strokeWidth: 1.5, strokeLinecap: 'round', strokeLinejoin: 'round' }, 'p')
    ]),
  canceled: () =>
    svg([
      jsx('circle', { cx: 7, cy: 7, r: 6.25, fill: 'var(--isx-todo)' }, 'c'),
      jsx('path', { d: 'M5 5l4 4M9 5l-4 4', stroke: '#fff', strokeWidth: 1.4, strokeLinecap: 'round' }, 'p')
    ]),
  unknown: () => svg(jsx('circle', { cx: 7, cy: 7, r: 5.5, fill: 'none', stroke: 'var(--isx-todo)', strokeWidth: 1.5, strokeDasharray: '2 2' }))
}
const STATE_WORDS = { todo: 'Todo', progress: 'In Progress', review: 'In Review', done: 'Done', canceled: 'Canceled' }

const cardFrame = (color, extra = {}) =>
  jsx('rect', { x: 1.5, y: 2.5, width: 11, height: 9, rx: 2, fill: 'none', stroke: color, strokeWidth: 1.3, ...extra }, 'f')

const CARD_ICONS = {
  queued: () => svg(cardFrame('var(--isx-muted)', { strokeDasharray: '2 1.6' })),
  running: () => svg([cardFrame('var(--isx-muted)'), jsx('path', { d: 'M5.5 5.2v3.6L8.6 7z', fill: 'var(--isx-muted)' }, 'p')]),
  needs: () => svg([cardFrame('var(--isx-warn)'), jsx('circle', { cx: 7, cy: 7, r: 1.3, fill: 'var(--isx-warn)' }, 'p')]),
  landing: () =>
    svg([
      cardFrame('var(--isx-land)'),
      jsx('path', { d: 'M7 4.6v4.6M5 7.4l2 2 2-2', fill: 'none', stroke: 'var(--isx-land)', strokeWidth: 1.4, strokeLinecap: 'round', strokeLinejoin: 'round' }, 'p')
    ]),
  error: () =>
    svg([
      cardFrame('var(--isx-bad)'),
      jsx('path', { d: 'M7 4.6v2.8', stroke: 'var(--isx-bad)', strokeWidth: 1.5, strokeLinecap: 'round' }, 'p'),
      jsx('circle', { cx: 7, cy: 9.3, r: 0.85, fill: 'var(--isx-bad)' }, 'd')
    ]),
  done: () =>
    svg([
      cardFrame('var(--isx-ok)'),
      jsx('path', { d: 'M4.6 7.1l1.7 1.6 3.2-3.3', fill: 'none', stroke: 'var(--isx-ok)', strokeWidth: 1.4, strokeLinecap: 'round', strokeLinejoin: 'round' }, 'p')
    ])
}
const CARD_WORDS = {
  queued: `Card ${WORDS.queued}`,
  running: `Card ${WORDS.running}`,
  needs: `Card blocked — ${WORDS.needs}`,
  landing: `Card done — ${WORDS.landing}`,
  error: `Card ${WORDS.error}`,
  done: `Card ${WORDS.done}`
}

const LOGOS = {
  linear: () =>
    svg([
      jsx('circle', { cx: 7, cy: 7, r: 6, fill: '#5e6ad2' }, 'c'),
      jsx('path', { d: 'M3.2 8.2l2.6 2.6M3.4 6l4.6 4.6M4.6 4.2l5.2 5.2M6.6 3.2l4.2 4.2', stroke: '#fff', strokeWidth: 1, strokeLinecap: 'round' }, 'p')
    ]),
  jira: () =>
    svg([
      jsx('rect', { x: 1, y: 1, width: 12, height: 12, rx: 3, fill: '#2684ff' }, 'r'),
      jsx('path', { d: 'M7 3.6L10.4 7 7 10.4 3.6 7z', fill: 'none', stroke: '#fff', strokeWidth: 1.3, strokeLinejoin: 'round' }, 'p')
    ]),
  other: () => svg(jsx('rect', { x: 1.5, y: 1.5, width: 11, height: 11, rx: 3, fill: 'none', stroke: 'var(--isx-muted)', strokeWidth: 1.3 }))
}
const logo = t => (LOGOS[t] ?? LOGOS.other)()

const strokeGroup = (paths, size = 13) =>
  svg(
    jsx('g', {
      fill: 'none',
      stroke: 'currentColor',
      strokeWidth: 1.3,
      strokeLinecap: 'round',
      strokeLinejoin: 'round',
      children: paths.map((d, i) => jsx('path', { d }, i))
    }),
    size
  )

const ICONS = {
  board: () =>
    svg(
      jsx('g', {
        fill: 'none',
        stroke: 'var(--isx-muted)',
        strokeWidth: 1.2,
        children: [
          jsx('rect', { x: 1.5, y: 2, width: 3, height: 10, rx: 1 }, 'a'),
          jsx('rect', { x: 5.5, y: 2, width: 3, height: 7, rx: 1 }, 'b'),
          jsx('rect', { x: 9.5, y: 2, width: 3, height: 8.5, rx: 1 }, 'c')
        ]
      }),
      13
    ),
  ext: () => strokeGroup(['M8 2.5h3.5V6M11.5 2.5L6.5 7.5', 'M10 8.5v2.5a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h2.5']),
  card: () => strokeGroup(['M3.5 2.5h7a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-7a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2z', 'M4 5.5h6M4 8h3.5']),
  alert: () =>
    svg([
      jsx('circle', { cx: 7, cy: 7, r: 5.6, fill: 'none', stroke: 'currentColor', strokeWidth: 1.3 }, 'c'),
      jsx('path', { d: 'M7 4v3.6', stroke: 'currentColor', strokeWidth: 1.5, strokeLinecap: 'round' }, 'l'),
      jsx('circle', { cx: 7, cy: 9.8, r: 0.85, fill: 'currentColor' }, 'd')
    ]),
  sync: () => strokeGroup(['M2 7a5 5 0 0 1 8.6-3.5M12 7a5 5 0 0 1-8.6 3.5', 'M10.8 1.6v2.2H8.6M3.2 12.4v-2.2h2.2'], 14)
}

// ── styles ──────────────────────────────────────────────────────────────────────────────────
// Scoped under .isx. Colour only for problems (amber = needs you, red = error). Light mode uses
// darker amber/yellow so they hold up on white; the host marks dark mode with :root.dark.
const STYLE_ID = 'hermes-issue-sync-styles'
export const CSS = `
.isx{--isx-muted:var(--ui-text-tertiary,#6b7079);--isx-todo:var(--ui-text-tertiary,#9aa0a8);--isx-prog:#c99a06;--isx-ok:var(--ui-green,#2f8f5b);
 --isx-done:#5e6ad2;--isx-warn:#b8700b;--isx-bad:var(--ui-red,#cf3a3a);--isx-land:var(--ui-accent,#2f6fe0);
 --isx-hover:var(--ui-bg-tertiary,rgba(128,128,128,.12));--isx-line:var(--ui-stroke-secondary,rgba(128,128,128,.35));--isx-tip-bg:#1d2026;
 font-size:12px;line-height:1.35}
:root.dark .isx{--isx-prog:#f2c94c;--isx-warn:#f2a33a;--isx-tip-bg:#0e0f12}
.isx svg{flex:none;display:block}
.isx-row{display:flex;align-items:center;gap:7px;min-height:24px;padding:2px 8px;border-radius:5px;white-space:nowrap;min-width:0}
.isx-row:hover{background:var(--isx-hover)}
.isx-hdr{cursor:pointer;user-select:none}
.isx :focus-visible{outline:2px solid var(--ui-accent,#4c8dff);outline-offset:1px}
.isx-grow{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis}
.isx-m{color:var(--isx-muted)}
.isx-key,.isx-mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px}
.isx-key{color:var(--isx-muted)}
.isx-warn{color:var(--isx-warn)} .isx-bad{color:var(--isx-bad)}
.isx-pill{display:inline-flex;align-items:center;padding:1px 6px;border-radius:999px;font-size:10.5px;border:1px solid var(--isx-line);color:var(--isx-muted)}
.isx-pill.isx-warn{border-color:color-mix(in srgb,var(--isx-warn) 50%,transparent);color:var(--isx-warn)}
.isx-sub{display:block;padding:0 8px 3px 29px;font-size:11.5px;white-space:normal}
.isx-problem{display:flex;gap:7px;align-items:flex-start;margin:2px 8px 4px 26px;padding:5px 8px;border-radius:5px;font-size:11.5px;
 background:color-mix(in srgb,var(--isx-bad) 11%,transparent);color:var(--isx-bad);white-space:normal}
.isx-problem.isx-warn{background:color-mix(in srgb,var(--isx-warn) 11%,transparent);color:var(--isx-warn)}
.isx-problem svg{margin-top:1px}
.isx-acts{display:none;gap:2px}
.isx-item:hover .isx-acts,.isx-item:focus-within .isx-acts{display:inline-flex}
.isx-ibtn{all:unset;display:inline-flex;width:18px;height:18px;align-items:center;justify-content:center;border-radius:4px;color:var(--isx-muted);cursor:pointer}
.isx-ibtn:hover,.isx-ibtn:focus-visible{background:color-mix(in srgb,currentColor 14%,transparent)}
.isx-tip{position:relative}
.isx-icon{display:inline-flex;align-items:center;border-radius:3px}
.isx-tip[data-tip]:hover::after,.isx-tip[data-tip]:focus-visible::after{content:attr(data-tip);position:absolute;right:0;top:calc(100% + 3px);background:var(--isx-tip-bg);color:#eee;
 padding:3px 6px;border-radius:4px;font-size:10.5px;z-index:20;white-space:pre-wrap;width:max-content;max-width:420px;font-weight:400;pointer-events:none}
.isx-chev{flex:none;width:16px;height:16px;margin-left:-4px;margin-right:-2px;display:inline-flex;align-items:center;justify-content:center;color:var(--isx-muted)}
.isx-hdr:hover .isx-chev{color:inherit}
.isx-chev::before{content:"";width:5.5px;height:5.5px;border-right:1.6px solid currentColor;border-bottom:1.6px solid currentColor;transform:translateY(-1.5px) rotate(45deg);transition:transform .15s ease}
.isx-chev.isx-closed::before{transform:translateX(-1.5px) rotate(-45deg)}
.isx-foot{display:flex;justify-content:space-between;align-items:center;gap:6px;padding:4px 4px 0 8px;margin-top:4px;border-top:1px solid var(--isx-line);font-size:10.5px;color:var(--isx-muted)}
.isx-foot.isx-stale{color:var(--isx-warn)}
.isx-fl{display:inline-flex;align-items:center;gap:5px}
.isx-empty{padding:18px 14px;display:flex;flex-direction:column;gap:8px;align-items:flex-start}
.isx-cmd{display:flex;gap:6px;align-items:center;padding:4px 4px 4px 8px;border:1px solid var(--isx-line);border-radius:5px}
.isx-btn{all:unset;font-size:11px;padding:2px 8px;border-radius:4px;border:1px solid var(--isx-line);cursor:pointer}
.isx-btn:hover,.isx-btn:focus-visible{background:var(--isx-hover)}
.isx-link{all:unset;cursor:pointer;text-decoration:underline;text-underline-offset:2px}
.isx-chip{all:unset;display:inline-flex;align-items:center;gap:4px;padding:0 6px;font-size:.6875rem;color:var(--isx-muted);cursor:pointer}
.isx-chip.isx-warn{color:var(--isx-warn)} .isx-chip.isx-bad{color:var(--isx-bad)}
`

function ensureStyles() {
  if (typeof document === 'undefined' || document.getElementById(STYLE_ID)) return
  const el = document.createElement('style')
  el.id = STYLE_ID
  el.textContent = CSS
  document.head.appendChild(el)
}

// ── components ──────────────────────────────────────────────────────────────────────────────

// An icon with its words: aria-label for screen readers, a tooltip on hover AND keyboard focus.
function Labeled({ label, children }) {
  return jsx('span', { className: 'isx-tip isx-icon', role: 'img', tabIndex: 0, 'aria-label': label, 'data-tip': label, children })
}

function IconButton({ label, onClick, children }) {
  return jsx('button', {
    type: 'button',
    className: 'isx-ibtn isx-tip',
    'aria-label': label,
    'data-tip': label,
    onClick: e => {
      e.stopPropagation()
      onClick()
    },
    children
  })
}

function onActivate(fn) {
  return e => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      fn()
    }
  }
}

// A header row: drawn chevron on the left; the whole row toggles.
function HeaderRow({ open, onToggle, label, title, children }) {
  return jsxs('div', {
    role: 'button',
    tabIndex: 0,
    'aria-expanded': open,
    'aria-label': label,
    title,
    className: 'isx-row isx-hdr',
    onClick: onToggle,
    onKeyDown: onActivate(onToggle),
    children: [jsx('span', { 'aria-hidden': true, className: `isx-chev${open ? '' : ' isx-closed'}` }, 'chev'), ...children]
  })
}

function openCard(item) {
  // The host has no "open this card" API yet; go to the kanban page (the task id rides along
  // as a query so a later host can pick it up).
  host.navigate?.(`/kanban?task=${encodeURIComponent(item.card_id)}`)
}

function ItemRow({ ctx, item }) {
  const kind = cardKind(item)
  const stateKind = issueStateKind(item)
  const land = landingInfo(item)
  const name = trackerLabel(item.tracker)
  const issueWord = item.tracker_state || STATE_WORDS[stateKind] || 'not pushed yet'
  const cardWord = kind === 'landing' ? `${CARD_WORDS.landing}: ${land.line}` : CARD_WORDS[kind]
  const problem = item.error
    ? friendlyError(item.error, { tracker: item.tracker, conn: item.conn })
    : kind === 'error'
      ? landingError(item.landing_error)
      : null
  return jsxs('div', {
    className: 'isx-item',
    children: [
      jsxs('div', {
        className: 'isx-row',
        children: [
          jsx(Labeled, { label: `Issue: ${issueWord} (last pushed)`, children: (STATE_ICONS[stateKind] ?? STATE_ICONS.unknown)() }, 's'),
          jsx('span', { className: 'isx-key', children: item.key }, 'k'),
          jsx('span', { className: 'isx-grow', title: item.title, children: item.title }, 't'),
          kind === 'needs' ? jsx('span', { className: 'isx-pill isx-warn', children: WORDS.needs }, 'n') : null,
          jsxs(
            'span',
            {
              className: 'isx-acts',
              children: [
                item.url ? jsx(IconButton, { label: `Open in ${name}`, onClick: () => ctx.os?.openExternal?.(item.url), children: ICONS.ext() }, 'o') : null,
                item.card_id ? jsx(IconButton, { label: 'Open kanban card', onClick: () => openCard(item), children: ICONS.card() }, 'c') : null
              ]
            },
            'a'
          ),
          item.card_id ? jsx(Labeled, { label: cardWord, children: CARD_ICONS[kind]() }, 'cs') : null
        ]
      }),
      kind === 'needs' && !problem
        ? jsx('div', { className: 'isx-sub isx-warn', children: item.question ? `“${item.question}”` : `Answer on the issue in ${name}.` })
        : null,
      kind === 'landing' ? jsx('div', { className: 'isx-sub isx-m', children: land.line }) : null,
      problem
        ? jsx('div', {
            className: `isx-sub isx-tip ${problem.tone === 'warn' ? 'isx-warn' : 'isx-bad'}`,
            tabIndex: 0,
            'data-tip': problem.raw,
            children: [problem.title, problem.fix].filter(Boolean).join(' ')
          })
        : null
    ]
  })
}

function Problem({ p }) {
  return jsxs('div', {
    className: `isx-problem isx-tip${p.tone === 'warn' ? ' isx-warn' : ''}`,
    role: 'alert',
    tabIndex: 0,
    'data-tip': p.raw,
    children: [ICONS.alert(), jsxs('span', { children: [jsx('b', { children: p.title }, 't'), p.fix ? ` ${p.fix}` : ''] })]
  })
}

function Branch({ tree, last, children }) {
  const { rail, elbow } = connectorStyles(tree.style, tree.tracker, last)
  return jsxs('div', {
    style: { position: 'relative', paddingLeft: INDENT },
    children: [jsx('span', { 'aria-hidden': true, style: rail }), elbow && jsx('span', { 'aria-hidden': true, style: elbow }), children]
  })
}

// Children hang under the parent's chevron.
function Branches({ tree, rows, render }) {
  return jsx('div', {
    style: { display: 'flex', flexDirection: 'column', marginLeft: 12 },
    children: rows.map((row, i) => jsx(Branch, { tree, last: i === rows.length - 1, children: render(row) }, row.id ?? i))
  })
}

const COLLAPSED_KEY = 'collapsedGroups'

function useCollapsed(ctx) {
  // Saved per plugin on this Desktop (ctx.storage). Groups start open; done folds start closed.
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

const itemRows = items => items.map(item => ({ id: `i:${item.conn}:${item.key}`, item }))

// The rows under a board: the active items, then one "N done" fold.
function BoardBody({ ctx, board, tree, collapsed, toggle, dKey }) {
  const { active, done } = foldDone(board.items)
  const doneOpen = collapsed.has(dKey)
  const rows = itemRows(active)
  if (done.length) rows.push({ id: dKey, done })
  if (!rows.length) return null
  return jsx(Branches, {
    tree,
    rows,
    render: row =>
      row.item
        ? jsx(ItemRow, { ctx, item: row.item })
        : jsxs('div', {
            children: [
              jsx(HeaderRow, {
                open: doneOpen,
                onToggle: () => toggle(dKey),
                label: `${row.done.length} ${WORDS.done}`,
                children: [jsx('span', { className: 'isx-m', children: `${row.done.length} ${WORDS.done}` }, 'l')]
              }),
              doneOpen ? jsx(Branches, { tree, rows: itemRows(row.done), render: r => jsx(ItemRow, { ctx, item: r.item }) }) : null
            ]
          })
  })
}

function Counts({ counts }) {
  return jsxs('span', {
    className: 'isx-m',
    children: [
      plural(counts.issues, WORDS.issues),
      counts.needs ? jsx('span', { className: 'isx-warn', children: ` · ${counts.needs} ${WORDS.needs}` }, 'w') : null,
      counts.errors ? jsx('span', { className: 'isx-bad', children: ` · ${plural(counts.errors, 'errors')}` }, 'x') : null
    ]
  })
}

// One group: header (chevron, icon, name, counts when collapsed), its problems, then its body.
function Group({ gKey, lead, name, bold, title, counts, problems, extra, empty, collapsed, toggle, children }) {
  const open = !collapsed.has(gKey)
  return jsxs('section', {
    children: [
      jsx(HeaderRow, {
        open,
        onToggle: () => toggle(gKey),
        label: name,
        title,
        children: [
          jsx('span', { style: { display: 'inline-flex' }, children: lead }, 'lead'),
          jsx(bold ? 'b' : 'span', { children: name }, 'n'),
          extra ?? null,
          jsx('span', { className: 'isx-grow' }, 'g'),
          open ? (empty ? jsx('span', { className: 'isx-m', children: `no ${WORDS.issues}` }, 'e') : null) : jsx(Counts, { counts }, 'c')
        ]
      }),
      // Problems stay visible even when collapsed, so collapsing never hides a failure.
      ...(problems ?? []).map((p, i) => jsx(Problem, { p }, `p${i}`)),
      open ? children : null
    ]
  })
}

function boardGroup({ ctx, board, tracker, collapsed, toggle, style }) {
  const tree = { style, tracker: tracker.key }
  const id = `${tracker.key}:${board.name}`
  return jsx(
    Group,
    {
      gKey: `b:${id}`,
      lead: ICONS.board(),
      name: board.name,
      title: board.sourceTip,
      counts: board.counts,
      problems: board.problems,
      extra: board.inlineSources ? jsx('span', { className: 'isx-m', children: board.sources.map(s => `${s.projects} · ${s.conn}`).join(', ') }, 'src') : null,
      empty: !board.items.length,
      collapsed,
      toggle,
      children: jsx(BoardBody, { ctx, board, tree, collapsed, toggle, dKey: `d:${id}` })
    },
    `b:${id}`
  )
}

function trackerGroup({ ctx, tracker, board, collapsed, toggle, style }) {
  const tree = { style, tracker: tracker.key }
  const common = { gKey: `t:${tracker.key}`, lead: logo(tracker.key), name: tracker.label, bold: true, collapsed, toggle }
  if (board) {
    // A tracker with one board: the board is folded into the tracker row.
    return jsx(
      Group,
      {
        ...common,
        title: `${board.sourceTip}\nboard: ${board.name}`,
        counts: board.counts,
        problems: board.problems,
        empty: !board.items.length,
        children: jsx(BoardBody, { ctx, board, tree, collapsed, toggle, dKey: `d:${tracker.key}:${board.name}` })
      },
      common.gKey
    )
  }
  return jsx(
    Group,
    {
      ...common,
      title: tracker.label,
      counts: tracker.counts,
      children: jsx(Branches, {
        tree,
        rows: tracker.boards.map(b => ({ id: b.name, board: b })),
        render: row => boardGroup({ ctx, board: row.board, tracker, collapsed, toggle, style })
      })
    },
    common.gKey
  )
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

function Footer({ data, single }) {
  const { last, stale } = staleness(data)
  const when = shortAgo(last)
  if (data?.enabled === false) return jsx('div', { className: 'isx-foot', children: 'Sync is turned off in settings.' })
  if (stale) {
    return jsx('div', {
      className: 'isx-foot isx-stale',
      role: 'status',
      children: jsxs('span', { className: 'isx-fl', children: [ICONS.alert(), `Last ${WORDS.synced} ${when} — sync may have stopped. Is the gateway running?`] })
    })
  }
  return jsx('div', {
    className: 'isx-foot',
    children: jsxs('span', {
      className: 'isx-fl',
      children: single ? [logo(single.key), `${single.label} · ${WORDS.synced} ${when}`] : [`${WORDS.synced} ${when}`]
    })
  })
}

function copyText(text) {
  const write = sdk.writeClipboardText ?? (t => navigator.clipboard?.writeText(t))
  return Promise.resolve()
    .then(() => write(text))
    .then(() => true)
    .catch(() => false)
}

function SetupEmpty() {
  const [copied, setCopied] = useState(false)
  const settingsHref = sdk.pluginSettingsHref?.(PLUGIN_ID)
  return jsxs('div', {
    className: 'isx isx-empty',
    children: [
      jsx('b', { children: 'Connect an issue tracker' }),
      jsx('span', { className: 'isx-m', children: 'Issue Sync turns tracker issues into kanban cards and keeps both in step. Run this in a terminal:' }),
      jsxs('div', {
        className: 'isx-cmd',
        children: [
          jsx('code', { className: 'isx-mono', children: SETUP_COMMAND }),
          jsx('button', { type: 'button', className: 'isx-btn', onClick: () => copyText(SETUP_COMMAND).then(setCopied), children: copied ? 'Copied' : 'Copy' })
        ]
      }),
      settingsHref
        ? jsxs('span', {
            className: 'isx-m',
            children: ['Or ', jsx('button', { type: 'button', className: 'isx-link', onClick: () => host.navigate?.(settingsHref), children: 'open the settings' }), '.']
          })
        : null
    ]
  })
}

export function SyncView({ ctx, data, error, isLoading, collapsed, toggle }) {
  if (isLoading) return jsx('div', { className: 'isx isx-m', style: { padding: 12 }, children: 'Loading…' })
  if (error) {
    return jsx(EmptyState, {
      title: 'Issue Sync backend not reachable',
      description: 'Enable hermes-issue-sync in Capabilities → Plugins (both halves), then reload.'
    })
  }
  const connections = data?.connections ?? []
  if (!connections.length) return jsx(SetupEmpty, {})
  const trackers = groupByTracker(connections)
  const style = treeStyleOf(data)
  const args = { ctx, collapsed, toggle, style }
  return jsxs('div', {
    className: 'isx',
    style: { display: 'flex', flexDirection: 'column', gap: 2, padding: '6px 4px' },
    children: [
      ...paneTree(connections).map(node => (node.type === 'board' ? boardGroup({ ...args, ...node }) : trackerGroup({ ...args, ...node }))),
      jsx(Footer, { data, single: trackers.length === 1 ? trackers[0] : null }, 'foot')
    ]
  })
}

function SyncPane({ ctx }) {
  ensureStyles()
  const query = useSnapshot(ctx)
  const [collapsed, toggle] = useCollapsed(ctx)
  return jsx(ScrollArea, { className: 'h-full', children: jsx(SyncView, { ctx, ...query, collapsed, toggle }) })
}

export function Chip({ state, onClick }) {
  const { tone, icon, label, title } = state
  return jsxs('button', {
    type: 'button',
    className: `isx isx-chip${tone === 'warn' ? ' isx-warn' : tone === 'bad' ? ' isx-bad' : ''}`,
    title,
    'aria-label': title,
    onClick,
    children: [ICONS[icon](), label || null]
  })
}

function StatusChip({ ctx }) {
  ensureStyles()
  return jsx(Chip, { state: chipState(useSnapshot(ctx)), onClick: () => togglePanel(ctx) })
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
        data: { decorate: card => cardBadge(cardIndex.get(card.id)) }
      }
    ]
    // Skip any surface this Desktop build does not have (area undefined).
    ctx.registerMany(contributions.filter(c => c && c.area))
  }
}
