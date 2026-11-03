# 微信本地知识库采集（WeChat KB Collector）

一个**纯本地、只读、不发送消息**的微信 4.x 知识归档工具。

它把「锦」会话里的合并转发记录、普通消息和图片，整理成 Markdown 存到
`D:\KnowledgeBase`，再由本地 FastAPI 提供给前端知识库页面。

> 不 Hook 微信进程，不解密 SQLite 数据库，不调用任何发送接口。

---

## 功能

- **托盘常驻**：`Ctrl+Alt+K` 剪贴板采集、`Ctrl+Alt+S` 强制扫描、暂停/恢复、状态面板、退出。
- **合并转发记录**：双击卡片打开记录窗口，`Ctrl+A/C` 获取全文，逐屏保存图片原图，关闭窗口。
- **普通单条消息**：每 15 秒轻量 OCR，只抓左侧气泡（对方消息），15 秒聚合后提交。
- **图片归档**：
  - 合并记录里的图片：跟随记录一个 MD，原图作为附件；
  - 普通聊天的图片：只取当前可视区域最后一张，裁剪保存；
  - 不会把图片内容 OCR 进正文。
- **统一写入**：所有通道都走 `ingest.py`，做内容 hash 去重、10 秒防抖、意图冲突切批、AI 分类、原子写 Markdown、manifest 记录。
- **状态面板**：Tkinter 原生小窗，显示运行状态、当前聊天、扫描时间、队列深度、下次任务倒计时、微信 UI 自检、今日归档、最近文件、最近错误；支持置顶。
- **夜间/电池策略**：23:00–07:00 或电池供电时自动暂停轮询；手动 `Ctrl+Alt+S` 仍可用。
- **全屏/游戏免打扰**：前台是全屏应用或已知游戏进程时，自动扫描和 15 秒消息轮询全部挂起；`Ctrl+Alt+S` 仍可手动覆盖。
- **隐私过滤**：正文命中 `身份证`、`密码`、`银行卡`、`家庭住址` 时，整条丢弃，不调 AI、不落盘。
- **剪贴板保护**：抓取结束后把用户原来的文字/图片原样写回剪贴板。
- **焦点归还**：操作完成后把前台窗口还给用户原来的窗口。
- **多屏/DPI**：全流程 DPI 感知，坐标按窗口相对位置和截图比例换算，不写死绝对坐标。
- **防内存/日志膨胀**：每处理 10 条任务 `gc.collect()`；日志 10MB 轮转，保留 3 份归档。

---

## 架构

```
通道 1  chat_watcher      空闲扫描「锦」会话里的 [聊天记录] 卡片
通道 2  message_watcher   15 秒轻量 OCR，抓普通单条消息/最后一张图
通道 3  hotkey_capture    Ctrl+Alt+K 剪贴板文字/图片
通道 4  record_capture    记录窗口全文、图片原图、发送者裁剪
                ↓
        ingest.Pipeline
  归一化 → 隐私过滤 → 当日去重 → 防抖/意图冲突
  → DeepSeek 分类 → Markdown + 附件 + manifest
                ↓
        D:\KnowledgeBase
```

托盘层：

```
tray_app.py        pystray 托盘、热键、扫描调度、全屏守卫、命令总线
status_panel.py    Tkinter 状态面板
status_bus.py      状态/命令 JSON 总线
game_guard.py      全屏/游戏免打扰检测
```

---

## 快速开始

```powershell
cd D:\CodeBuddyWorkSpace\wechat

# 安装依赖（建议国内镜像）
.\venv\Scripts\pip.exe install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

# 推荐：静默启动 API + 托盘（无黑框）
start_tray.vbs

# 或双击桌面「微信知识库采集」快捷方式
# 或 bat（会闪一下后自动退出，实际仍由 VBS 隐藏启动）
start_tray.bat

# 打开桌面管家（含一键启动全部）
start_control_center.bat
```

`start_tray.vbs` 会调用 `start_services.py`，同时拉起：

- FastAPI（127.0.0.1:8000）
- 托盘程序

两个进程都不显示控制台窗口；API 已运行时会跳过，托盘有单实例互斥，重复双击安全。

`start_api.bat` 也已整合为同一条静默启动链，不再直接运行 `python.exe`，不会弹 FastAPI 黑框。

启动结果写入：

```
D:\KnowledgeBase\launcher.log
```

开机自启：

```powershell
install_autostart.bat
```

自启同样走 `start_tray.vbs`，会同时启动 API 和托盘，不会弹黑框。

卸载：

```powershell
uninstall_autostart.bat
```

---

## 桌面管家与 Vue 控制台

### 桌面管家

双击桌面上的 **微信知识库管家**，或运行：

```powershell
.\venv\Scripts\pythonw.exe control_center.py
```

窗口里可以直接：

- 一键启动本地 API、托盘和前端；
- 打开 Vue 控制台 / 知识库；
- 立即扫描、暂停/恢复、打开状态面板；
- 打开日志、知识库目录；
- 查看托盘/API/前端、当前聊天、队列、倒计时、自检。

重新生成桌面快捷方式：

```powershell
.\venv\Scripts\python.exe control_center.py --install-shortcut
```

### Vue 控制台

Vue 源码在 `C:\dev\notice-to-calendar`，新增路由：

```
http://127.0.0.1:8000/console     # 控制台
http://127.0.0.1:8000/knowledge   # 知识库
```

`api_server.py` 会直接托管 `dist/` 构建产物，所以日常不需要启动 Vite：

```powershell
cd C:\dev\notice-to-calendar
npm run build
```

修改前端后重新 build 即可。开发模式仍可：

```powershell
npm run dev -- --host 127.0.0.1 --port 5173
```

前端路径可用环境变量覆盖：

```powershell
set KB_FRONTEND_DIR=C:\dev\notice-to-calendar
set KB_FRONTEND_DIST=C:\dev\notice-to-calendar\dist
```

### 本地 API Token 与 CORS

写操作（POST）必须带本地 Token：

- 后端 `.env`：`LOCAL_API_TOKEN=...`
- 前端 `.env.local`：`VITE_LOCAL_API_TOKEN=...`
- 请求头：`Authorization: Bearer <token>`
- CORS 只允许 `http://127.0.0.1:8000`、`http://localhost:8000`、`http://127.0.0.1:5173`、`http://localhost:5173`
- 没有 Token 时，写接口返回 401；读取接口仍可本地访问，但不再允许任意网页跨域读取。

改 Token 后必须重新构建前端（`npm run build`），否则浏览器里还是旧 Token。

API 新增：

- `GET /api/health`
- `GET /api/status`
- `POST /api/control`（`scan` / `toggle_pause` / `open_panel`）
- `POST /api/open/log`
- `POST /api/open/folder`
- `POST /api/knowledge/merge`（合并多张卡片，原卡片写入 `is_merged: true` 和 `merged_into: 新卡名.md`）
- `POST /api/knowledge/category`（分类写回 YAML frontmatter）
- `POST /api/knowledge/link`（把链接存成卡片，同 URL 自动去重）
- `GET /images/*`（知识库图片）

### 知识卡片交互

- 点击卡片打开详情弹窗，不再在卡片内展开；
- 正文用 Markdown 渲染，`![图片](./images/...)` 直接显示为图片，点击可放大；
- 支持搜索、类型筛选、星标；
- 多选后可合并成一张新卡片；原卡片保留，frontmatter 标记 `is_merged: true` 和 `merged_into`，前端“已合并”徽章可点击跳到合并卡；已合并的卡片再次合并会被后端 409 拦截；
- 分类写入文件为权威；写回失败时前端会回滚并显示红色“未同步”，不会假成功；
- “一键迁移旧分类到文件”会把浏览器里残留的本地分类批量写回 Markdown；
- 详情弹窗支持“添加到日程”，使用 `YYYY-MM-DD` 标准日期，只把前 200 字摘要写入 localStorage，不塞全文；
- 右上角“粘贴链接”可直接把公众号文章链接存成知识卡片；同 URL 不重复创建；
- 公众号/小程序卡片：复制链接后用 `Ctrl+Alt+K` 或“粘贴链接”归档，链接在卡片里可点击；小程序卡片可保留截图/图片，但不能在没有数据库/Hook 的前提下结构化提取内容；
- 微信没有公开的原文深链，“复制定位信息”会复制标题和日期，需要到微信里手动搜索。

---

## 使用

| 操作 | 说明 |
|---|---|
| `Ctrl+Alt+K` | 采集剪贴板文字/图片；结束后恢复原剪贴板 |
| `Ctrl+Alt+S` | 忽略空闲/游戏模式，强制扫描当前可见记录卡片 |
| 托盘右键 → 状态面板 | 查看运行状态、扫描时间、最近归档、错误 |
| 托盘右键 → 暂停监听 | 停止自动扫描与 15 秒消息轮询（手动热键仍可用） |
| 托盘右键 → 立即扫描 | 等同 `Ctrl+Alt+S` |

运行前提：

1. 微信已登录；
2. 当前聊天切到「锦」（否则聊天校验会跳过，防止抓错会话）；
3. 合并记录需要双击打开 `[聊天记录]` 卡片，由 watcher 自动处理。

---

## 配置

### AI 分类

在 `.env` 中配置（不要提交）：

```ini
DEEPSEEK_API_KEY=你的 key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
```

没有 key 时自动降级为 `待分类-HHMMSS.md`，不影响归档。

### 隐私黑名单

`privacy.py`：

```python
BLACKLIST_KEYWORDS = ("身份证", "密码", "银行卡", "家庭住址")
```

命中任意关键词，整条消息丢弃，不调用 AI、不写文件。

### 全屏/游戏免打扰

`game_guard.py`：

- `GAME_PROCESSES`：常见游戏进程名；
- `SHELL_PROCESSES`：即使全屏也不挂起的程序（微信、IDE、终端等）。

检测周期 10 秒；全屏/游戏时自动任务挂起，状态面板显示橙色提示。

---

## 目录

```
D:\KnowledgeBase\
  YYYY-MM-DD-标题.md       归档 Markdown
  images\                  截图、原图、发送者裁剪、剪贴板图片
  .processed.jsonl         内容指纹，按天去重
  .manifest.jsonl          每次归档的元数据
  .chat_seen.json          普通消息 OCR 行去重
  .tray_status.json        状态面板数据
  .tray_command.json       面板控制命令
  images\chat-image-*.png  普通聊天最后一张图
  .staging\                图片临时目录（Markdown 成功后清空）
```

---

## 只读与安全边界

- 不 Hook、不注入、不改微信内存；
- 不解密、不写回微信 SQLite 数据库；
- 不发送、不转发、不加好友、不遍历群聊、不抓朋友圈；
- 所有写操作只发生在 `D:\KnowledgeBase`；
- 聊天内容、`.env`、日志、图片不应提交到 Git。

> 微信用户协议通常禁止自动化工具，本项目仅用于个人本地归档，存在账号风险；
> 聊天记录包含他人个人信息，请勿公开分享或用于商业用途。

**建议定期手动备份 `D:\KnowledgeBase` 目录**（复制到移动硬盘或加密盘），
工具不会自动清理历史归档，误删/磁盘故障只能靠备份恢复。

---

## 已知限制

- 微信 4.x 是 Qt 自绘，只能靠剪贴板/OCR/截图，无法保证 100% 稳定；
- 普通消息只抓左侧气泡，无法识别被折叠/引用/撤回的上下文；
- 图片只保存渲染图或最后一张裁剪图，不保证原图；
- 记录窗口图片原图保存依赖「图片和视频」预览窗口和保存对话框，微信改版可能失效；
- 全屏检测按前台窗口所在显示器判断，副屏/特殊游戏可能需要补充进程名；
- 一条合并记录一个 MD，内部按文本分段，发送者信息是文末裁剪图，不做逐条映射。

---

## 说明书

- v1.1 Word：`D:\KnowledgeBase\微信知识库采集-使用说明-v1.1.docx`
- v1.1 PDF：`D:\KnowledgeBase\微信知识库采集-使用说明-v1.1.pdf`
- v1.0 原版 Word/PDF 保留，不覆盖。

## 仓库

<https://github.com/jinhh123/wechat-kb-backend>

如果仓库 About 简介需要中文，可使用：

> 微信 4.x 本地知识库采集：托盘常驻、合并记录全文、普通消息聚合、图片归档、全屏免打扰；只读、不发送、不解密数据库。
