# CloakBrowser 集成方案分析

## 问题背景

Flow2API 使用 Playwright 的 stock Chromium 处理 reCAPTCHA Enterprise。
当前状态：
- **图片生成** (`batchGenerateImages`) → reCAPTCHA 通过 ✅
- **图片放大** (`upsampleImage`) → reCAPTCHA 持续被拒 ❌ (`PUBLIC_ERROR_UNUSUAL_ACTIVITY`)

根因：stock Playwright Chromium 的 reCAPTCHA v3 评分仅 **0.1**（被判定为 bot），
Google 对 upsample 端点的评分阈值高于 image generation。

## CloakBrowser 是什么

[CloakBrowser](https://github.com/CloakHQ/CloakBrowser) — 开源 Chromium fork，
在 **C++ 源码层面** 修改了 58 处指纹，包括 canvas、WebGL、audio、fonts、GPU、
WebRTC、network timing、automation signals、CDP input behavior。

关键能力：
| 检测项 | Stock Playwright | CloakBrowser |
|--------|:---:|:---:|
| reCAPTCHA v3 score | 0.1 (bot) | **0.9** (human) |
| navigator.webdriver | `true` | **`false`** |
| window.chrome | `undefined` | **`object`** |
| CDP detection | Detected | **Not detected** |
| TLS fingerprint | Mismatch | **Identical to Chrome** |
| UA string | HeadlessChrome | **Chrome/146.0.0.0** |

## 能否解决当前问题？

**高概率能解决。** 理由：

1. 我们的问题本质是 reCAPTCHA Enterprise 给自动化浏览器低分 → CloakBrowser 直接提分到 0.9
2. CloakBrowser 明确支持 **extension loading**（`extension_paths` 参数）
3. 支持 **persistent profiles**（保留 cookies/localStorage）
4. Docker 内和 VPS 上表现一致
5. Drop-in 替换 — API 与 Playwright 完全相同
6. 官方 README 特别提到 "Use Playwright for sites with **reCAPTCHA Enterprise**"

**风险点：**
- 需要验证 CloakBrowser 的 Chromium 版本是否兼容现有 extension_v2 的 MV3 API
- 二进制大小约 200MB，需确认服务器磁盘空间
- 新 Chromium 版本可能有兼容性细节（但 CloakBrowser 基于 Chromium 146/148）

## 当前架构分析

```
ChromeManager (chrome_manager.py)
├── _resolve_chrome_binary()  → 查找 Playwright 安装的 Chromium 二进制
├── _build_chrome_args()      → 构建启动参数（extension、user-data-dir、anti-detect flags）
└── _ChromeInstance.start()   → subprocess.Popen(chrome_binary, args...) 直接启动

关键：不使用 Playwright API 启动，而是直接调用 Chromium 可执行文件！
```

这意味着替换方案非常简单：**只需换一个二进制路径**。

## 集成方案

### 方案 A：二进制替换（推荐，最小改动）

**原理**：用 CloakBrowser 的 stealth Chromium 替换 Playwright 的 stock Chromium

**步骤**：

1. **Dockerfile 修改**：
```dockerfile
# 在 requirements.txt 添加 cloakbrowser
RUN pip install --no-cache-dir cloakbrowser
# CloakBrowser 会自动下载 stealth Chromium binary

# 找到 binary 路径（通常在 ~/.cloakbrowser/bin/chromium 或类似位置）
RUN python3 -c "from cloakbrowser._binary import get_binary_path; print(get_binary_path())" > /tmp/cloak_path.txt
```

2. **ChromeManager 配置**：
```python
# chrome_manager.py 修改 _resolve_chrome_binary()
@staticmethod
def _resolve_chrome_binary() -> str:
    configured = (ChromeManager.CHROME_BINARY or "").strip()
    if configured:
        return configured

    # Priority 1: CloakBrowser stealth binary
    try:
        from cloakbrowser._binary import get_binary_path
        cloak_path = get_binary_path()
        if cloak_path and os.path.exists(cloak_path):
            return cloak_path
    except ImportError:
        pass

    # Priority 2: Playwright bundled Chromium (fallback)
    # ... existing logic ...
```

3. **移除冗余 anti-detect flags**：
```python
# _build_chrome_args() 中以下参数可以移除（CloakBrowser 已在源码层处理）
# "--disable-blink-features=AutomationControlled"  ← 不再需要
# CloakBrowser 自带这些修补，重复设置可能冲突
```

4. **验证**：
```bash
# 进入容器测试
docker exec -it flow2api-headed python3 -c "
from cloakbrowser._binary import get_binary_path
print(f'Binary: {get_binary_path()}')
"
```

**改动文件**：
- `Dockerfile.headed` — 加 `cloakbrowser` 依赖
- `requirements.txt` — 加 `cloakbrowser`
- `src/services/chrome_manager.py` — 修改 `_resolve_chrome_binary()` 优先使用 CloakBrowser

### 方案 B：使用 CloakBrowser Playwright API（更彻底但改动大）

**原理**：用 `cloakbrowser.launch_persistent_context_async()` 替换 `subprocess.Popen`

**优势**：
- 获得 `humanize=True`（人类化鼠标/键盘行为）提高 reCAPTCHA 分数
- 更好的生命周期管理

**劣势**：
- 需要大量重构 `ChromeManager` 和 `_ChromeInstance`
- 从 subprocess 管理改为 Playwright BrowserContext 管理
- extension 加载方式需要适配（从 --load-extension 改为 extension_paths 参数）

**暂不推荐**，因为方案 A 足够解决问题且风险更低。

## 实施计划

### 已完成的代码改动

**1. `requirements.txt`** — 添加依赖：
```
cloakbrowser>=0.3.30
```

**2. `Dockerfile.headed`** — 构建时下载 binary + 禁用自动更新：
```dockerfile
ENV ... \
    CLOAKBROWSER_AUTO_UPDATE=false \
    CLOAKBROWSER_CACHE_DIR=/opt/cloakbrowser

RUN pip install ... -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && python -m cloakbrowser install
```

**3. `src/services/chrome_manager.py`** — binary 查找优先级：
```
CloakBrowser (/opt/cloakbrowser/**/chrome) > 系统 Chrome > Playwright Chromium
```
当检测到 CloakBrowser 时，跳过 `--disable-blink-features=AutomationControlled` 等冗余 flags。

### 部署步骤

```bash
# 1. 推送代码
cd /Users/lsq/AIProjects/flow2api && git add -A && git push

# 2. 远程构建（首次约多 2-3 分钟下载 CloakBrowser ~200MB）
ssh ubuntu@10.66.0.1 "cd /path/to/flow2api && git pull && \
  sudo docker compose -f docker-compose.headed.yml up -d --build"

# 3. 验证 CloakBrowser 加载
ssh ubuntu@10.66.0.1 "sudo docker logs flow2api-headed 2>&1 | grep CloakBrowser"
# 期望输出: [ChromeManager] Using CloakBrowser: /opt/cloakbrowser/.../chrome

# 4. 测试 imagen-4.0-2k upsample
```

### 回滚方法

```bash
# 方法 1: 环境变量覆盖（不需要重新构建）
# 设置 CHROME_BINARY 为 Playwright 路径即可跳过 CloakBrowser
docker exec flow2api-headed env | grep PLAYWRIGHT

# 方法 2: 代码回滚
git revert HEAD  # 回滚 CloakBrowser 集成 commit
# 重新构建部署
```

## 注意事项

1. **extension_v2 兼容性**：CloakBrowser 基于 Chromium 146+，支持 MV3，应该兼容
2. **Docker 镜像大小**：增加约 200MB（CloakBrowser binary）
3. **许可证**：MIT License，商用无限制
4. **不需要代理**：CloakBrowser 不内置代理轮换，使用现有网络配置即可
5. **auto-update**：生产环境建议固定版本，避免自动更新导致不稳定
