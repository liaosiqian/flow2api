# Flow2API Agent 指引

本仓库用于维护 Flow2API 服务源码、浏览器插件、headed Docker 部署和 Token 自动刷新逻辑。

## 仓库与远程

- 主维护仓库：`https://github.com/liaosiqian/flow2api.git`
- 上游参考仓库：`https://github.com/aicrossai/flow2api.git`
- 旧/来源仓库如 `TheSmallHanCat/flow2api` 只能作为参考，不应作为默认 push 目标。
- 本地开发目录：`/Users/lsq/AIProjects/flow2api`
- 生产服务器：`ubuntu@43.135.154.121`
- 服务器仓库路径：`/home/ubuntu/flow2api`
- 生产容器名：`flow2api-headed`

## 修改与发布原则

- 所有正式代码改动必须先在本地仓库完成、检查、提交，并 push 到 `liaosiqian/flow2api`。
- 生产服务器只应从 `liaosiqian/flow2api` 拉取已提交代码后构建部署，避免本地、GitHub、服务器三边持续分叉。
- 不要长期只在运行中容器或服务器工作区热修；紧急热修后必须回流到本地源码并提交。
- 部署前先检查服务器工作区状态，确认是否存在有价值的未提交远程特化改动；有价值内容应回收进本地提交。
- 不要把运行态数据提交或同步进仓库，例如 `data/`、`tmp/`、`browser_profiles/`、`warp-data/`、`.tmp-*`、`.pytest_cache/`。

## 标准发布流程

```bash
# 1. 本地检查与提交
cd /Users/lsq/AIProjects/flow2api
git status --short
git diff --check
git add <changed-files>
git commit -m "..."
git push liaosiqian main

# 2. 服务器拉取并部署
ssh -o ProxyCommand=none ubuntu@43.135.154.121 \
  'cd /home/ubuntu/flow2api && git status --short && git pull liaosiqian main'

ssh -o ProxyCommand=none ubuntu@43.135.154.121 \
  'sudo docker compose -f /home/ubuntu/flow2api/docker-compose.headed.yml --project-directory /home/ubuntu/flow2api up -d --build flow2api-headed'
```

如果当前服务器工作区因为历史热修无法直接 `git pull`，应先把服务器差异与本地提交比对清楚，再通过安全同步方式更新源码；不要直接覆盖 `data/`、`tmp/`、`browser_profiles/`、`warp-data/` 或 `config/setting.toml`。

## Token 自动刷新关键约束

- 正确 Labs 入口是 `https://labs.google/fx/tools/flow`。
- `extension_v2/` 是当前 headed/extension 模式使用的正式插件目录。
- Chrome 路径必须动态解析，不要硬编码 Playwright Chromium 版本目录。
- `cryptography` 是 Chrome Cookie DB 解密所需依赖，不能从 `requirements.txt` 移除。
- `extension_generation_enabled` 默认应保持 `False`，避免未完整验证的浏览器代理生成路径默认开启。

## 部署后验证

```bash
ssh -o ProxyCommand=none ubuntu@43.135.154.121 \
  'sudo docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" && sudo docker logs flow2api-headed --tail=80 2>&1'

ssh -o ProxyCommand=none ubuntu@43.135.154.121 \
  'curl -s http://127.0.0.1:8000/health'
```

成功标准：服务健康，Chrome extension 已连接，`/health` 中 `tokens_expired=0`，且目标 Token 的 `at_expires` 被刷新到未来。
