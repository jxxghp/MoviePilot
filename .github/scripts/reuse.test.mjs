import assert from 'node:assert/strict'
import test from 'node:test'
import { reuse } from './reuse.mjs'

/** 构造带真实 GitHub 字段形状的离线 API 替身。 */
function fixture() {
  const head = 'a'.repeat(40),
    base = 'b'.repeat(40),
    sha = 'c'.repeat(40),
    merge = 'd'.repeat(40)
  const state = {
    context: {
      eventName: 'push',
      repo: { owner: 'owner', repo: 'repo' },
      ref: 'refs/heads/v3',
      sha,
      runId: 20,
      payload: { before: base },
    },
    pull: {
      number: 7,
      merged_at: '2026-09-09T00:00:00Z',
      merge_commit_sha: sha,
      head: { sha: head, repo: { id: 2 } },
      base: { ref: 'v3', repo: { full_name: 'owner/repo' } },
    },
    runs: [
      {
        id: 10,
        run_attempt: 1,
        status: 'completed',
        conclusion: 'success',
        head_sha: head,
        head_repository: { id: 2 },
        html_url: 'https://github.com/owner/repo/actions/runs/10',
      },
    ],
    jobs: [{ name: `CI proof (${merge})`, status: 'completed', conclusion: 'success' }],
    tested: { parents: [{ sha: base }, { sha: head }], tree: { sha: 'tree' } },
    outputs: {},
    calls: [],
    warnings: [],
  }
  /** 保留 API 参数供测试验证，避免错误地过滤历史失败记录。 */
  const api = name => async args => {
    state.calls.push({ name, args })
    if (state.error) throw new Error('API unavailable')
    if (name === 'getWorkflowRun')
      return { data: args.run_id === 20 ? { workflow_id: 100 } : { ...state.runs[0], ...state.refreshed } }
    if (name === 'getCommit') return { data: args.commit_sha === sha ? { tree: { sha: 'tree' } } : state.tested }
    if (name === 'listPullRequestsAssociatedWithCommit') return [state.pull]
    if (name === 'listWorkflowRuns') return { data: { workflow_runs: state.runs } }
    if (name === 'listJobsForWorkflowRun') return state.jobs
    throw new Error(name)
  }
  state.github = {
    rest: {
      actions: {
        getWorkflowRun: api('getWorkflowRun'),
        listWorkflowRuns: api('listWorkflowRuns'),
        listJobsForWorkflowRun: api('listJobsForWorkflowRun'),
      },
      git: { getCommit: api('getCommit') },
      repos: { listPullRequestsAssociatedWithCommit: api('listPullRequestsAssociatedWithCommit') },
    },
    paginate: (method, args) => method(args),
  }
  state.core = {
    setOutput: (key, value) => {
      state.outputs[key] = value
    },
    notice: () => {},
    info: () => {},
    warning: message => state.warnings.push(message),
  }
  return state
}

test('相同合并代码且完整成功时复用，即使 fork run 没有 pull_requests 字段', async () => {
  const state = fixture()
  await reuse(state)
  assert.equal(state.outputs.reuse, 'true')
  const query = state.calls.find(call => call.name === 'listWorkflowRuns').args
  assert.equal(query.workflow_id, 100)
  assert.equal(query.event, 'pull_request')
  assert.equal(query.status, undefined)
  assert.equal(state.calls.find(call => call.name === 'listJobsForWorkflowRun').args.filter, 'latest')
})

const cases = {
  'PR 必须完整执行': state => {
    state.context.eventName = 'pull_request'
  },
  '手工触发必须完整执行': state => {
    state.context.eventName = 'workflow_dispatch'
  },
  'force push 必须完整执行': state => {
    state.context.payload.forced = true
  },
  '新建分支必须完整执行': state => {
    state.context.payload.before = '0'.repeat(40)
  },
  '直接 push 没有对应合并': state => {
    state.pull.merge_commit_sha = 'other'
  },
  '未合并 PR 不可复用': state => {
    state.pull.merged_at = null
  },
  '目标分支不同': state => {
    state.pull.base.ref = 'other'
  },
  '目标仓库不同': state => {
    state.pull.base.repo.full_name = 'other/repo'
  },
  'PR head 已改变': state => {
    state.pull.head.sha = 'e'.repeat(40)
  },
  'PR 来源仓库不同': state => {
    state.pull.head.repo.id = 9
  },
  '旧工作流没有 proof': state => {
    state.jobs = []
  },
  'proof 被跳过': state => {
    state.jobs[0].conclusion = 'skipped'
  },
  'proof 尚未完成': state => {
    state.jobs[0].status = 'in_progress'
  },
  'proof 重复': state => {
    state.jobs.push({ ...state.jobs[0] })
  },
  '工作流有失败': state => {
    state.runs[0].conclusion = 'failure'
  },
  '工作流尚未完成': state => {
    state.runs[0].status = 'in_progress'
  },
  '最新执行失败不可退回旧成功': state => {
    state.runs.push({ ...state.runs[0], id: 11, conclusion: 'failure' })
  },
  '查询期间开始重跑': state => {
    state.refreshed = { status: 'in_progress', conclusion: null }
  },
  '查询期间运行轮次变化': state => {
    state.refreshed = { run_attempt: 2 }
  },
  '目标分支已经前进': state => {
    state.tested.parents[0].sha = 'e'.repeat(40)
  },
  '证明不是 PR 模拟合并': state => {
    state.tested.parents.pop()
  },
  '模拟合并来自其他 PR head': state => {
    state.tested.parents[1].sha = 'e'.repeat(40)
  },
  '合入代码树有变化': state => {
    state.tested.tree.sha = 'different'
  },
  'API 异常回退全量': state => {
    state.error = true
  },
}
for (const [name, mutate] of Object.entries(cases)) {
  test(name, async () => {
    const state = fixture()
    mutate(state)
    await reuse(state)
    assert.equal(state.outputs.reuse, 'false')
  })
}
