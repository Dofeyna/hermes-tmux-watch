/**
 * Tmux Watch - Hermes Desktop pane + status-bar chip.
 *
 * Polls the tmux-watch plugin backend (/api/plugins/tmux-watch/status) every
 * 5s and shows live tmux session state across every configured SSH host:
 * which sessions are running, their last output line (with real ANSI
 * color), and an activity tier - active / idle / possibly stuck - based on
 * how long the WHOLE captured screen has gone unchanged. This is
 * deliberately app-agnostic: it doesn't try to recognize any particular
 * program's prompt character (that broke for Claude Code's TUI, which
 * never ends its idle screen in a plain $/>/❯).
 *
 * Read-only by default. A host can opt in to a confirm-gated "kill session"
 * button by setting allow_control: true in its config.json entry - off by
 * default, and the backend re-checks that flag itself before acting (the
 * UI hiding the button is a convenience, not the security boundary).
 *
 * Hosts are configured on the GATEWAY side (wherever the backend runs), in
 * ~/.config/hermes-tmux-watch/config.json - see the project README.
 */

import { usePluginI18n, useQuery, useMutation, useQueryClient, Tip, host } from '@hermes/plugin-sdk'
import { jsx, jsxs, Fragment } from 'react/jsx-runtime'

const ID = 'tmux-watch'
const QUERY_KEY = ['tmux-watch']
let pluginCtx = null

// ANSI SGR -> color. This is NOT UI chrome - it's rendering the remote
// program's own intended colors (a red FAIL, a green PASS), the same way a
// syntax highlighter picks fixed colors for keywords. The skill rule against
// hardcoded colors is about chrome/backgrounds; program output content is
// the deliberate exception, same as any terminal emulator.
const ANSI_FG = {
  30: '#5c6370', 31: '#e06c75', 32: '#98c379', 33: '#e5c07b',
  34: '#61afef', 35: '#c678dd', 36: '#56b6c2', 37: '#dcdfe4',
  90: '#7f848e', 91: '#e06c75', 92: '#98c379', 93: '#e5c07b',
  94: '#61afef', 95: '#c678dd', 96: '#56b6c2', 97: '#ffffff'
}

const SGR_RE = /\u001b\[([0-9;]*)m/g

/** Parse a line that may still carry `ESC[...m` SGR codes (the backend only
 * strips non-color escape sequences, color is left intact) into plain text
 * runs with an associated inline style. Unknown/reset codes just clear state. */
function ansiToRuns(line) {
  const runs = []
  let fg = null
  let bold = false
  let lastIndex = 0
  let match
  SGR_RE.lastIndex = 0
  while ((match = SGR_RE.exec(line)) !== null) {
    if (match.index > lastIndex) {
      runs.push({ text: line.slice(lastIndex, match.index), fg, bold })
    }
    const codes = match[1] === '' ? [0] : match[1].split(';').map(Number)
    for (const code of codes) {
      if (code === 0) { fg = null; bold = false }
      else if (code === 1) bold = true
      else if (code === 22) bold = false
      else if (code === 39) fg = null
      else if (ANSI_FG[code] !== undefined) fg = ANSI_FG[code]
    }
    lastIndex = SGR_RE.lastIndex
  }
  if (lastIndex < line.length) runs.push({ text: line.slice(lastIndex), fg, bold })
  return runs
}

function AnsiLine({ line }) {
  const runs = ansiToRuns(line)
  return jsx(Fragment, {
    children: runs.map((r, i) =>
      jsx('span', {
        key: i,
        style: { color: r.fg || undefined, fontWeight: r.bold ? 600 : undefined },
        children: r.text
      })
    )
  })
}

/** Only two theme-safe tones are used for the dot itself (bright = accent,
 * dim = quaternary text) - a third "stalled" visual distinction comes from
 * the text label next to it, not a third hardcoded dot color. */
function Dot({ bright }) {
  return jsx('span', {
    className: 'inline-block h-2 w-2 shrink-0 rounded-full',
    style: { backgroundColor: bright ? 'var(--ui-accent)' : 'var(--ui-text-quaternary)' }
  })
}

function formatDuration(totalSeconds) {
  const s = Math.max(0, Math.round(totalSeconds || 0))
  if (s < 60) return `${s}s`
  const m = Math.round(s / 60)
  if (m < 60) return `${m}m`
  const h = Math.round(m / 60)
  return `${h}h`
}

function KillButton({ hostEntry, session, t }) {
  const qc = useQueryClient()
  const killMutation = useMutation({
    mutationFn: () =>
      pluginCtx.rest(
        `/kill?ssh_host=${encodeURIComponent(hostEntry.ssh_host)}&session_name=${encodeURIComponent(session.name)}`,
        { method: 'POST', timeoutMs: 10000 }
      ),
    onSuccess: (res) => {
      if (!res?.ok) host.notify({ kind: 'error', message: res?.error || res?.message || t('killFailed') })
      qc.invalidateQueries({ queryKey: QUERY_KEY })
    },
    onError: () => host.notify({ kind: 'error', message: t('killFailed') })
  })

  return jsx('button', {
    type: 'button',
    className: 'text-xs text-(--ui-text-quaternary) hover:text-(--ui-accent) underline-offset-2 hover:underline',
    disabled: killMutation.isPending,
    onClick: () => {
      if (window.confirm(t('confirmKill', session.name))) killMutation.mutate()
    },
    children: killMutation.isPending ? t('killing') : t('kill')
  })
}

function ActivityLabel({ s, t }) {
  if (s.activity === 'stalled') {
    return jsx('span', { className: 'text-xs text-(--ui-accent)', children: t('stalled', formatDuration(s.idle_seconds)) })
  }
  if (s.activity === 'idle') {
    return jsx('span', { className: 'text-xs text-(--ui-text-quaternary)', children: t('idle', formatDuration(s.idle_seconds)) })
  }
  return null
}

function SessionCard({ s, t, hostEntry }) {
  const tail = (s.tail || []).filter(l => l.replace(/\u001b\[[0-9;]*m/g, '').trim().length > 0).slice(-6)
  return jsxs('div', {
    className: 'flex flex-col gap-1 pl-3',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2 text-sm font-medium',
        children: [
          jsx(Dot, { bright: s.activity === 'active' }),
          s.name,
          s.attached
            ? jsx('span', { className: 'text-xs text-(--ui-text-quaternary)', children: t('attached') })
            : null,
          jsx(ActivityLabel, { s, t }),
          hostEntry.allow_control
            ? jsx('span', { className: 'ml-auto', children: jsx(KillButton, { hostEntry, session: s, t }) })
            : null
        ]
      }),
      jsx('pre', {
        className: 'max-h-28 overflow-auto whitespace-pre-wrap break-all text-[0.6875rem] text-(--ui-text-tertiary)',
        children: tail.length
          ? tail.map((l, i) => jsxs(Fragment, { key: i, children: [jsx(AnsiLine, { line: l }), i < tail.length - 1 ? '\n' : null] }))
          : t('noOutput')
      })
    ]
  })
}

function HostCard({ h, t }) {
  return jsxs('div', {
    className: 'flex flex-col gap-2 border-b border-(--ui-stroke-secondary) pb-2 last:border-none last:pb-0',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2 text-sm font-semibold',
        children: [
          jsx(Dot, { bright: h.reachable }),
          h.label
        ]
      }),
      !h.reachable
        ? jsx('div', { className: 'pl-3 text-xs text-(--ui-text-tertiary)', children: h.note || t('unreachable') })
        : (h.sessions || []).length === 0
          ? jsx('div', { className: 'pl-3 text-xs text-(--ui-text-tertiary)', children: t('noSessions') })
          : jsx('div', {
              className: 'flex flex-col gap-2',
              children: h.sessions.map(s => jsx(SessionCard, { key: s.name, s, t, hostEntry: h }))
            })
    ]
  })
}

function useTmuxWatchStatus() {
  return useQuery({
    queryKey: QUERY_KEY,
    queryFn: () => pluginCtx.rest('/status', { method: 'GET', timeoutMs: 10000 }),
    refetchInterval: 5000
  })
}

function StatusPane() {
  const t = usePluginI18n(ID)
  const { data, isLoading, isError, refetch } = useTmuxWatchStatus()

  return jsxs('div', {
    className: 'flex h-full flex-col gap-2 overflow-auto p-3 text-sm',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between',
        children: [
          jsx('div', { className: 'font-medium', children: t('title') }),
          jsx('button', {
            type: 'button',
            className: 'text-xs text-(--ui-text-tertiary) hover:text-foreground',
            onClick: () => refetch(),
            children: t('refresh')
          })
        ]
      }),
      isLoading
        ? jsx('div', { className: 'text-(--ui-text-tertiary)', children: t('loading') })
        : isError || !data?.ok
          ? jsx('div', { className: 'text-(--ui-text-tertiary)', children: data?.note || t('offline') })
          : (data.hosts || []).length === 0
            ? jsx('div', { className: 'text-(--ui-text-tertiary)', children: data?.note || t('noHosts') })
            : jsx('div', {
                className: 'flex flex-col gap-3',
                children: data.hosts.map(h => jsx(HostCard, { key: h.ssh_host, h, t }))
              })
    ]
  })
}

function StatusChip() {
  const t = usePluginI18n(ID)
  const { data } = useTmuxWatchStatus()
  if (!data?.ok) return null
  const sessions = (data.hosts || []).flatMap(h => h.sessions || [])
  if (sessions.length === 0) return null
  const stalledCount = sessions.filter(s => s.activity === 'stalled').length
  const activeCount = sessions.filter(s => s.activity === 'active').length
  const label = stalledCount > 0
    ? t('chipStalled', stalledCount)
    : activeCount > 0
      ? t('chipActive', activeCount, sessions.length)
      : t('chipAllIdle', sessions.length)

  return jsx(Tip, {
    label: t('chipTip', sessions.length),
    children: jsx('span', {
      className: 'inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem] text-(--ui-text-tertiary)',
      children: [
        jsx(Dot, { key: 'dot', bright: stalledCount === 0 && activeCount > 0 }),
        ' ',
        label
      ]
    })
  })
}

export default {
  id: ID,
  name: 'Tmux Watch',
  register(ctx) {
    pluginCtx = ctx
    ctx.i18n.register({
      en: {
        title: 'Tmux Watch',
        refresh: 'refresh',
        loading: 'Checking hosts…',
        offline: 'Could not reach the backend.',
        unreachable: 'Host unreachable over SSH.',
        noHosts: 'No hosts configured yet - see the plugin README.',
        noSessions: 'No active tmux sessions.',
        noOutput: '(no output yet)',
        attached: '(attached)',
        idle: secs => `(idle ${secs})`,
        stalled: secs => `(possibly stuck, ${secs})`,
        kill: 'kill',
        killing: 'killing…',
        killFailed: 'Could not kill the session - see the Hermes log.',
        confirmKill: name => `Kill tmux session "${name}"? Anything running in it (unsaved work included) will be terminated.`,
        chipTip: count => `${count} tmux session(s) watched`,
        chipActive: (active, total) => `${active}/${total} active`,
        chipAllIdle: count => `${count} idle`,
        chipStalled: count => `⚠ ${count} possibly stuck`
      }
    })
    ctx.register({
      id: 'pane',
      area: 'panes',
      title: 'Tmux Watch',
      data: { placement: 'right', width: '300px' },
      render: () => jsx(StatusPane, {})
    })
    ctx.register({
      id: 'chip',
      area: 'statusBar.right',
      order: 130,
      render: () => jsx(StatusChip, {})
    })
  }
}
