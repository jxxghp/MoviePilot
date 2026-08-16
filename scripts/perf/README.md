# MoviePilot Docker A/B Harness

该工具用同一个冻结 Docker substrate 对两个 Git commit 做源码级 A/B。派生镜像会先清空
`/app`，复制目标 commit 的完整 `git archive`，再从 substrate 注入镜像构建阶段生成的插件目录、
`sites.*.so` 和 `user.sites.v3.bin`。如果依赖或 Docker substrate 输入发生变化，工具会拒绝继续，
避免把外部构建输入漂移误算成性能收益。

工具不会读取工作区或容器内的 `app.env`，不挂载真实配置、媒体目录或 Docker socket。固定配置只使用
内置实验室占位凭据，样本不发布主机端口，并运行在无外网的 internal network。

## 一次性预热浏览器

默认从固定命名 volume `mp-perf-v3-browser-seed` 复制 CloakBrowser 缓存。该 volume 只需联网预热
一次：

```bash
docker volume create mp-perf-v3-browser-seed
docker run --rm \
  --mount source=mp-perf-v3-browser-seed,target=/moviepilot/.cloakbrowser \
  --entrypoint python3 \
  jxxghp/moviepilot-v3@sha256:925de1fdf1bb0312144bc818bc8ebaa999a9a159c6d14f1b48b0ff05edb7f720 \
  -m cloakbrowser install
```

也可以在 `seed` 或 `run` 时显式传入 `--allow-browser-download`，但该选项会让 seed 阶段联网；
正式 Before/After 样本仍然只克隆预热结果并使用 internal network。

## 分阶段执行

所有全局参数必须放在子命令前。结果默认写到系统临时目录下的
`moviepilot-perf-results/<campaign>/`。

```bash
PYTHON=../.venv/bin/python
CAMPAIGN=v3-perf-001

${PYTHON} scripts/perf/moviepilot_docker_ab.py \
  --campaign "${CAMPAIGN}" \
  build --before-ref upstream/v3 --after-ref HEAD

${PYTHON} scripts/perf/moviepilot_docker_ab.py \
  --campaign "${CAMPAIGN}" \
  seed

${PYTHON} scripts/perf/moviepilot_docker_ab.py \
  --campaign "${CAMPAIGN}" \
  sample --variant before --index 1 --points 1,5,10,30
```

开发 harness 时可以用小数分钟做短冒烟，例如 `--points 0,0.02`。正式数据必须保持
`1,5,10,30`。

## 完整三组 A/B

```bash
../.venv/bin/python scripts/perf/moviepilot_docker_ab.py \
  --campaign v3-perf-001 \
  run \
  --before-ref upstream/v3 \
  --after-ref HEAD \
  --points 1,5,10,30
```

执行顺序固定为：

```text
Before-1 → After-1 → After-2 → Before-2 → Before-3 → After-3
```

每个样本都从 SQLite 和浏览器 seed 克隆新的命名 volume。样本结束后立即移除容器和样本卷；
完整 `run` 结束后还会移除 campaign seed 与 internal network。派生镜像和本地结果保留，便于复核。

## 输出

```text
<output>/<campaign>/
├── build.json
├── seed.json
├── results.json
├── report.md
└── samples/
    ├── before-1/
    │   ├── result.json
    │   ├── container.log
    │   └── modules/
    └── ...
```

- `results.json`：Engine stats、进程 PSS/USS/RSS/线程、网络累计值和模块前缀计数；
- `report.md`：三次原值与中位数 Before/After 汇总；
- `modules/`：由目标 MoviePilot Python 进程自身写出的完整 `sys.modules` 名称清单；
- `container.log`：已移除实验室凭据值和本地实例 UUID。

容器 working set 统一按 Docker Engine API 的
`memory_stats.usage - memory_stats.stats.inactive_file` 计算。进程 PSS 仅用于归因，不能替代容器指标。

## 清理

清理严格限定到 campaign 标签；不会执行 Docker prune：

```bash
../.venv/bin/python scripts/perf/moviepilot_docker_ab.py \
  --campaign v3-perf-001 cleanup --images
```

本地 JSON、Markdown 和日志不会被 `cleanup` 删除。
