import { Box, Text, useInput } from '@hermes/ink'
import { useCallback, useEffect, useMemo, useState } from 'react'

import type { GatewayClient } from '../gatewayClient.js'
import { rpcErrorMessage } from '../lib/rpc.js'
import type { Theme } from '../theme.js'

import { OverlayHint, windowItems } from './overlayControls.js'
import { clampOverlayWidth, listRowStyle } from './overlayPrimitives.js'
import { TextInput } from './textInput.js'

const VISIBLE = 10
const MIN_WIDTH = 72
const MAX_WIDTH = 132

export interface MissionContractView {
  allow_local_commit?: boolean
  boundaries?: Record<string, unknown>
  constraints?: string[]
  outcome?: string
  stop_when?: string[]
  verification?: string[]
}

export interface MissionView {
  autonomy_level: number
  blocked_reason?: null | string
  branch_name?: null | string
  contract?: MissionContractView
  final_disposition?: null | string
  id: string
  objective: string
  phase: string
  project_id?: null | string
  repo_path?: string
  risk_level: string
  rollback_ref?: null | string
  status: string
  verified_commit?: null | string
  worktree_path?: null | string
}

export interface MissionTaskView {
  current_run_id?: null | number
  id: string
  mission_role?: null | string
  status: string
  title: string
}

export interface MissionEvidenceView {
  command?: null | string
  exit_code?: null | number
  id: number
  kind: string
  status: string
}

export interface MissionLinkView {
  child_id: string
  parent_id: string
}

export interface MissionReportView {
  evidence: MissionEvidenceView[]
  evidence_chain_valid: boolean
  links?: MissionLinkView[]
  mission: MissionView
  open_intents: unknown[]
  tasks: MissionTaskView[]
}

interface MissionListResponse {
  board?: string
  missions?: MissionView[]
}

interface MissionActionResponse {
  mission?: MissionView
}

interface MissionCreateResponse {
  mission_id?: string
}

export interface MissionCreateForm {
  allowLocalCommit: string
  allowedPaths: string
  autonomy: string
  objective: string
  outcome: string
  project: string
  repo: string
  verification: string
}

export interface MissionActionSpec {
  action: string
  hotkey: string
  label: string
  method?: 'missions.action' | 'missions.plan_auto'
}

const EMPTY_FORM: MissionCreateForm = {
  allowLocalCommit: 'no',
  allowedPaths: '',
  autonomy: '3',
  objective: '',
  outcome: '',
  project: '',
  repo: '',
  verification: ''
}

const FIELDS: Array<{ key: keyof MissionCreateForm; label: string; placeholder: string }> = [
  { key: 'objective', label: 'Objective', placeholder: 'What must be completed?' },
  { key: 'outcome', label: 'Outcome', placeholder: 'What observable result proves success?' },
  { key: 'verification', label: 'Verify', placeholder: 'Commands separated by semicolons' },
  { key: 'project', label: 'Project', placeholder: 'Registered project id or slug' },
  { key: 'repo', label: 'Git root', placeholder: '/absolute/path/to/repository' },
  { key: 'allowedPaths', label: 'Scope', placeholder: 'Allowed relative paths, comma separated' },
  { key: 'autonomy', label: 'Autonomy', placeholder: '0, 1, 2, 3, or 4' },
  { key: 'allowLocalCommit', label: 'L4 commit', placeholder: 'yes or no' }
]

const yes = (value: string) => /^(1|true|y|yes|on)$/i.test(value.trim())
const splitValues = (value: string, pattern: RegExp) => value.split(pattern).map(item => item.trim()).filter(Boolean)
const shortId = (value: string) => value.length > 14 ? `${value.slice(0, 12)}…` : value

export const missionActionsFor = (mission: MissionView): MissionActionSpec[] => {
  const rows: MissionActionSpec[] = []

  const add = (hotkey: string, action: string, label: string, method: MissionActionSpec['method'] = 'missions.action') =>
    rows.push({ action, hotkey, label, method })

  switch (mission.status) {
    case 'draft':
      if (mission.autonomy_level >= 1) {add('i', 'inspect', 'inspect repository')}

      if (mission.autonomy_level >= 2) {add('e', 'prepare', 'prepare isolated worktree')}

      break

    case 'planning':
      add('p', 'plan_auto', 'generate bounded plan', 'missions.plan_auto')

      break

    case 'ready':
      add('s', 'start', 'start execution')

      break

    case 'running':
      add('u', 'pause', 'pause execution')
      add('v', 'verify', 'run independent verification')

      break

    case 'waiting_for_user':
      add('s', 'resume', 'resume execution')

      break

    case 'blocked':
      add('t', 'retry', 'retry safely')
      add('y', 'reconcile', 'reconcile recovery state')

      break

    case 'verifying':
      add('y', 'reconcile', 'reconcile verification state')

      break

    case 'awaiting_approval':
      add('a', 'approve', 'approve local commit')
      add('d', 'deny', 'deny local commit')

      break

    case 'committing':
      add('k', 'commit', 'create verified local commit')

      break
  }

  if (!['succeeded', 'failed', 'cancelled', 'rolled_back'].includes(mission.status)) {
    add('x', 'cancel', 'cancel mission')
  }

  if (mission.worktree_path && ['blocked', 'succeeded', 'failed', 'cancelled'].includes(mission.status)) {
    add('b', 'rollback', 'restore rollback reference')
  }

  return rows
}

export const parseMissionCreateForm = (
  form: MissionCreateForm
): { error?: string; params?: Record<string, unknown> } => {
  const objective = form.objective.trim()
  const outcome = form.outcome.trim()
  const project = form.project.trim()
  const repo = form.repo.trim()
  const verification = splitValues(form.verification, /[;\n]+/)
  const allowedPaths = splitValues(form.allowedPaths, /,/)
  const autonomy = Number.parseInt(form.autonomy.trim(), 10)
  const allowLocalCommit = yes(form.allowLocalCommit)

  if (!objective || !outcome || !project || !repo) {
    return { error: 'objective, outcome, project, and Git root are required' }
  }

  if (!verification.length) {
    return { error: 'at least one verification command is required' }
  }

  if (!allowedPaths.length) {
    return { error: 'at least one allowed relative path is required' }
  }

  if (!Number.isInteger(autonomy) || autonomy < 0 || autonomy > 4) {
    return { error: 'autonomy must be an integer from 0 through 4' }
  }

  if (allowLocalCommit && autonomy !== 4) {
    return { error: 'local-commit authority is valid only for autonomy level 4' }
  }

  return {
    params: {
      allow_local_commit: allowLocalCommit,
      autonomy_level: autonomy,
      boundaries: {
        allowed_paths: allowedPaths,
        allowed_roots: [repo],
        allowed_terminal_backends: ['docker'],
        network_destinations: []
      },
      constraints: ['Do not modify files outside the declared mission scope'],
      objective,
      outcome,
      project_id: project,
      repo_path: repo,
      risk_level: 'medium',
      stop_when: ['The declared scope cannot be enforced'],
      verification
    }
  }
}

export const missionGraphLines = (report: MissionReportView): string[] => {
  const labels = new Map(report.tasks.map(task => [task.id, `${shortId(task.id)} ${task.mission_role ?? 'task'}:${task.status}`]))
  const links = report.links ?? []

  if (!links.length) {
    return report.tasks.map(task => labels.get(task.id) ?? shortId(task.id))
  }

  return links.map(link => `${labels.get(link.parent_id) ?? shortId(link.parent_id)} → ${labels.get(link.child_id) ?? shortId(link.child_id)}`)
}

const statusColor = (mission: MissionView, t: Theme) => {
  if (mission.status === 'succeeded') {return t.color.ok}

  if (['failed', 'blocked', 'cancelled'].includes(mission.status)) {return t.color.error}

  if (['awaiting_approval', 'waiting_for_user'].includes(mission.status)) {return t.color.warn}

  return t.color.accent
}

export function MissionCenter({ gw, maxWidth, onClose, t }: MissionCenterProps) {
  const [screen, setScreen] = useState<'create' | 'detail' | 'list'>('list')
  const [missions, setMissions] = useState<MissionView[]>([])
  const [selected, setSelected] = useState(0)
  const [report, setReport] = useState<MissionReportView | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [form, setForm] = useState<MissionCreateForm>(EMPTY_FORM)
  const [field, setField] = useState(0)
  const width = clampOverlayWidth(MAX_WIDTH, maxWidth, MIN_WIDTH)

  const refreshList = useCallback(async () => {
    setBusy(true)
    setError(null)

    try {
      const response = await gw.request<MissionListResponse>('missions.list', { limit: 100 })
      const next = response.missions ?? []
      setMissions(next)
      setSelected(current => Math.max(0, Math.min(current, Math.max(0, next.length - 1))))
    } catch (reason) {
      setError(rpcErrorMessage(reason))
    } finally {
      setBusy(false)
    }
  }, [gw])

  const loadDetail = useCallback(async (id: string) => {
    setBusy(true)
    setError(null)

    try {
      const next = await gw.request<MissionReportView>('missions.get', { id })
      setReport(next)
      setScreen('detail')
    } catch (reason) {
      setError(rpcErrorMessage(reason))
    } finally {
      setBusy(false)
    }
  }, [gw])

  const runAction = useCallback(async (spec: MissionActionSpec) => {
    if (!report || busy) {return}
    setBusy(true)
    setError(null)

    try {
      if (spec.method === 'missions.plan_auto') {
        await gw.request<MissionActionResponse>('missions.plan_auto', {
          executor: 'default', id: report.mission.id, verifier: 'default'
        })
      } else {
        await gw.request<MissionActionResponse>('missions.action', { action: spec.action, id: report.mission.id })
      }

      const next = await gw.request<MissionReportView>('missions.get', { id: report.mission.id })
      setReport(next)
      await refreshList()
    } catch (reason) {
      setError(rpcErrorMessage(reason))
    } finally {
      setBusy(false)
    }
  }, [busy, gw, refreshList, report])

  const createMission = useCallback(async () => {
    const parsed = parseMissionCreateForm(form)

    if (!parsed.params) {
      setError(parsed.error ?? 'invalid mission contract')

      return
    }

    setBusy(true)
    setError(null)

    try {
      const response = await gw.request<MissionCreateResponse>('missions.create', parsed.params)
      setForm(EMPTY_FORM)
      setField(0)
      await refreshList()
      setScreen('list')

      if (response.mission_id) {
        await loadDetail(response.mission_id)
      }
    } catch (reason) {
      setError(rpcErrorMessage(reason))
    } finally {
      setBusy(false)
    }
  }, [form, gw, loadDetail, refreshList])

  useEffect(() => {
    void refreshList()
  }, [refreshList])

  const actions = useMemo(() => report ? missionActionsFor(report.mission) : [], [report])

  useInput((ch, key) => {
    if (screen === 'create') {
      if (key.escape) {
        setError(null)
        setScreen('list')

        return
      }

      if (key.tab) {
        setField(current => (current + (key.shift ? FIELDS.length - 1 : 1)) % FIELDS.length)
      }

      return
    }

    if (key.escape) {
      if (screen === 'detail') {
        setError(null)
        setScreen('list')
      } else {
        onClose()
      }

      return
    }

    if (ch === 'q') {
      onClose()

      return
    }

    if (busy) {return}

    if (screen === 'list') {
      if (key.upArrow) {setSelected(current => Math.max(0, current - 1))}

      if (key.downArrow) {setSelected(current => Math.min(Math.max(0, missions.length - 1), current + 1))}

      if (key.return && missions[selected]) {void loadDetail(missions[selected]!.id)}

      if (ch === 'r') {void refreshList()}

      if (ch === 'c') {
        setError(null)
        setScreen('create')
      }

      return
    }

    if (screen === 'detail') {
      if (ch === 'r' && report) {void loadDetail(report.mission.id)}
      const action = actions.find(item => item.hotkey === ch)

      if (action) {void runAction(action)}
    }
  })

  const { items: visible, offset } = windowItems(missions, selected, VISIBLE)

  return (
    <Box flexDirection="column" paddingX={1} paddingY={1} width={width}>
      <Box justifyContent="space-between">
        <Text bold color={t.color.primary}>Project Autopilot missions</Text>
        <Text color={busy ? t.color.warn : t.color.muted}>{busy ? 'working…' : screen}</Text>
      </Box>

      {error ? <Text color={t.color.error}>error: {error}</Text> : null}

      {screen === 'list' ? (
        <Box flexDirection="column" marginTop={1}>
          {!missions.length && !busy ? <Text color={t.color.muted}>No missions on this board.</Text> : null}
          {visible.map((mission, index) => {
            const absolute = offset + index
            const active = absolute === selected
            const row = listRowStyle(t, active)

            return (
              <Box backgroundColor={row.backgroundColor} key={mission.id} width="100%">
                <Text color={active ? row.color : statusColor(mission, t)}>{active ? '▸ ' : '  '}</Text>
                <Box flexShrink={0} width={18}><Text bold={active} color={active ? row.color : t.color.label}>{shortId(mission.id)}</Text></Box>
                <Box flexShrink={0} width={24}><Text color={active ? row.color : statusColor(mission, t)}>{mission.status} · L{mission.autonomy_level}</Text></Box>
                <Text color={active ? row.color : t.color.text} wrap="truncate-end">{mission.objective}</Text>
              </Box>
            )
          })}
          <Box marginTop={1}><OverlayHint t={t}>↑↓ move · Enter inspect · c create · r refresh · q/Esc close</OverlayHint></Box>
        </Box>
      ) : null}

      {screen === 'detail' && report ? (
        <Box flexDirection="column" marginTop={1}>
          <Text bold color={t.color.label}>{report.mission.objective}</Text>
          <Text color={statusColor(report.mission, t)}>{report.mission.status} · {report.mission.phase} · autonomy L{report.mission.autonomy_level} · risk {report.mission.risk_level}</Text>
          <Text color={t.color.muted}>id {report.mission.id} · project {report.mission.project_id ?? 'none'}</Text>
          {report.mission.blocked_reason ? <Text color={t.color.error}>blocker: {report.mission.blocked_reason}</Text> : null}
          {report.mission.verified_commit ? <Text color={t.color.ok}>verified commit: {report.mission.verified_commit}</Text> : null}

          <Box flexDirection="column" marginTop={1}>
            <Text bold color={t.color.label}>Task graph</Text>
            {missionGraphLines(report).slice(0, 10).map((line, index) => <Text color={t.color.text} key={`g-${index}`}>{line}</Text>)}
          </Box>

          <Box flexDirection="column" marginTop={1}>
            <Text bold color={t.color.label}>Evidence</Text>
            <Text color={report.evidence_chain_valid ? t.color.ok : t.color.error}>
              chain {report.evidence_chain_valid ? 'valid' : 'INVALID'} · {report.evidence.length} records · {report.open_intents.length} open intents
            </Text>
            {report.evidence.slice(-8).map(item => (
              <Text color={item.status === 'passed' ? t.color.ok : item.status === 'failed' ? t.color.error : t.color.muted} key={item.id} wrap="truncate-end">
                {item.id}. {item.kind} · {item.status}{item.exit_code == null ? '' : ` · exit ${item.exit_code}`}{item.command ? ` · ${item.command}` : ''}
              </Text>
            ))}
          </Box>

          <Box marginTop={1}>
            <OverlayHint t={t}>{`${actions.map(item => `${item.hotkey} ${item.label}`).join(' · ') || 'no lifecycle action available'} · r refresh · Esc list · q close`}</OverlayHint>
          </Box>
        </Box>
      ) : null}

      {screen === 'create' ? (
        <Box flexDirection="column" marginTop={1}>
          <Text color={t.color.muted}>The shared mission service will reject unregistered projects, dirty Git roots, and unenforceable scope.</Text>
          {FIELDS.map((spec, index) => {
            const active = index === field
            const value = form[spec.key]

            return (
              <Box key={spec.key} marginTop={index === 0 ? 1 : 0}>
                <Box flexShrink={0} width={14}><Text bold={active} color={active ? t.color.accent : t.color.label}>{active ? '▸ ' : '  '}{spec.label}</Text></Box>
                {active ? (
                  <TextInput
                    color={t.color.text}
                    columns={Math.max(20, width - 18)}
                    focus
                    onChange={next => setForm(current => ({ ...current, [spec.key]: next }))}
                    onSubmit={() => {
                      if (field === FIELDS.length - 1) {void createMission()}
                      else {setField(current => current + 1)}
                    }}
                    placeholder={spec.placeholder}
                    placeholderColor={t.color.muted}
                    value={value}
                  />
                ) : <Text color={value ? t.color.text : t.color.muted}>{value || spec.placeholder}</Text>}
              </Box>
            )
          })}
          <Box marginTop={1}><OverlayHint t={t}>Enter next/submit · Tab/Shift+Tab move · Esc cancel</OverlayHint></Box>
        </Box>
      ) : null}
    </Box>
  )
}

interface MissionCenterProps {
  gw: GatewayClient
  maxWidth?: number
  onClose: () => void
  t: Theme
}
