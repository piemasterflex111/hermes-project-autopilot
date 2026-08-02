import { describe, expect, it } from 'vitest'

import {
  missionActionsFor,
  type MissionCreateForm,
  missionGraphLines,
  type MissionReportView,
  type MissionView,
  parseMissionCreateForm
} from '../components/missionCenter.js'

const mission = (status: string, overrides: Partial<MissionView> = {}): MissionView => ({
  autonomy_level: 3,
  id: 'm_1234567890',
  objective: 'Ship bounded change',
  phase: 'execution',
  risk_level: 'medium',
  status,
  ...overrides
})

const form = (overrides: Partial<MissionCreateForm> = {}): MissionCreateForm => ({
  allowLocalCommit: 'no',
  allowedPaths: 'src, tests',
  autonomy: '3',
  objective: 'Ship bounded change',
  outcome: 'Tests pass',
  project: 'demo-project',
  repo: '/work/demo',
  verification: 'pytest -q; npm test',
  ...overrides
})

describe('Mission Center contracts', () => {
  it('shows approval and denial as separate supervised actions', () => {
    expect(missionActionsFor(mission('awaiting_approval')).map(item => item.action)).toEqual([
      'approve', 'deny', 'cancel'
    ])
  })

  it('shows recovery actions for a blocked prepared mission', () => {
    expect(missionActionsFor(mission('blocked', { worktree_path: '/tmp/wt' })).map(item => item.action)).toEqual([
      'retry', 'reconcile', 'cancel', 'rollback'
    ])
  })

  it('builds the same fail-closed creation contract as the backend surfaces', () => {
    const parsed = parseMissionCreateForm(form({ allowLocalCommit: 'yes', autonomy: '4' }))
    expect(parsed.error).toBeUndefined()
    expect(parsed.params).toMatchObject({
      allow_local_commit: true,
      autonomy_level: 4,
      project_id: 'demo-project',
      repo_path: '/work/demo',
      verification: ['pytest -q', 'npm test'],
      boundaries: {
        allowed_paths: ['src', 'tests'],
        allowed_roots: ['/work/demo'],
        allowed_terminal_backends: ['docker'],
        network_destinations: []
      }
    })
  })

  it('rejects implicit local-commit authority below autonomy level four', () => {
    expect(parseMissionCreateForm(form({ allowLocalCommit: 'yes', autonomy: '3' })).error).toContain('level 4')
  })

  it('renders durable graph edges instead of guessing task order', () => {
    const report: MissionReportView = {
      evidence: [],
      evidence_chain_valid: true,
      links: [{ parent_id: 'controller-long-id', child_id: 'executor-long-id' }],
      mission: mission('planning'),
      open_intents: [],
      tasks: [
        { id: 'controller-long-id', mission_role: 'controller', status: 'done', title: 'Controller' },
        { id: 'executor-long-id', mission_role: 'executor', status: 'ready', title: 'Executor' }
      ]
    }

    expect(missionGraphLines(report)).toEqual([
      'controller-l… controller:done → executor-lon… executor:ready'
    ])
  })
})
