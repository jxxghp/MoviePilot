/** 只复用相同工作流对相同合并代码的完整成功检查；证据不足时执行全量门禁。 */
export async function reuse({ github, context, core }) {
  core.setOutput('reuse', 'false')
  if (
    context.eventName !== 'push' ||
    context.payload.forced ||
    !context.payload.before?.match(/^[a-f0-9]{40}$/) ||
    /^0+$/.test(context.payload.before)
  )
    return

  try {
    const repo = context.repo
    const { data: current } = await github.rest.actions.getWorkflowRun({ ...repo, run_id: context.runId })
    const { data: pushed } = await github.rest.git.getCommit({ ...repo, commit_sha: context.sha })
    const pulls = await github.paginate(github.rest.repos.listPullRequestsAssociatedWithCommit, {
      ...repo,
      commit_sha: context.sha,
      per_page: 100,
    })
    for (const pull of pulls) {
      if (
        !pull.merged_at ||
        pull.merge_commit_sha !== context.sha ||
        pull.base?.ref !== context.ref.replace('refs/heads/', '') ||
        pull.base?.repo?.full_name !== `${repo.owner}/${repo.repo}`
      )
        continue

      // 不过滤 success：同一 PR 最新执行失败或仍在运行时，不能退回旧的绿灯。
      const { data } = await github.rest.actions.listWorkflowRuns({
        ...repo,
        workflow_id: current.workflow_id,
        event: 'pull_request',
        head_sha: pull.head.sha,
        per_page: 100,
      })
      const run = data.workflow_runs
        .filter(
          candidate => candidate.head_sha === pull.head.sha && candidate.head_repository?.id === pull.head.repo?.id,
        )
        .sort((left, right) => right.id - left.id)[0]
      if (!run || run.status !== 'completed' || run.conclusion !== 'success') continue

      const jobs = await github.paginate(github.rest.actions.listJobsForWorkflowRun, {
        ...repo,
        run_id: run.id,
        filter: 'latest',
        per_page: 100,
      })
      const proofs = jobs.filter(
        job =>
          job.status === 'completed' && job.conclusion === 'success' && /^CI proof \([a-f0-9]{40}\)$/.test(job.name),
      )
      if (proofs.length !== 1) continue
      const testedSha = proofs[0].name.slice(10, -1)
      const { data: tested } = await github.rest.git.getCommit({ ...repo, commit_sha: testedSha })
      // run.head_sha 是 PR 分支头；只有 proof 记录的 github.sha 才是实际测试的模拟合并提交。
      if (
        tested.parents.length !== 2 ||
        tested.parents[0].sha !== context.payload.before ||
        tested.parents[1].sha !== pull.head.sha ||
        tested.tree.sha !== pushed.tree.sha
      )
        continue

      // 查询证明期间可能有人重新运行 CI，不能复用已经失效的运行快照。
      const { data: latest } = await github.rest.actions.getWorkflowRun({ ...repo, run_id: run.id })
      if (latest.status !== 'completed' || latest.conclusion !== 'success' || latest.run_attempt !== run.run_attempt)
        continue

      core.setOutput('reuse', 'true')
      core.notice(`复用 PR #${pull.number} 的完整检查：${run.html_url}；代码树 ${tested.tree.sha}`)
      return
    }
    core.info('没有匹配的完整 PR 检查，执行全量门禁。')
  } catch (error) {
    core.warning(`无法确认 PR 检查，执行全量门禁：${error.message}`)
  }
}
