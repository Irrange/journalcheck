# journalcheck

[English](README.md) | [中文](README.zh-CN.md)

跟踪 3 类投稿系统中的稿件审稿状态：

- `aha`：AHA / eJournalPress
- `em`：Editorial Manager
- `bmc`：Springer Nature / BMC 投稿详情页

## 快速开始

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
python main_gui.py
```

然后在 GUI 中填写投稿账号和通知设置，也可以在启动程序前直接编辑
`.env`。

## 图形界面

用下面任意一种方式启动桌面窗口：

```powershell
python main_gui.py
```

或者双击：

- `dist/JournalCheck.exe`

主要功能：

- 立即刷新
- 按时间间隔自动刷新
- 顶部显示状态变化摘要
- 活跃投稿列表，包含完整稿件摘要
- 点击单个投稿后在下方显示详情
- 管理 AHA / EM / BMC 账号
- 通知设置和测试邮件
- 为刷新请求设置 HTTP / HTTPS 代理

## 命令行

仍然可以使用命令行：

```powershell
python main.py
python main.py --once
python main.py --interval-minutes 30
python main.py --site aha --once
python main.py --test-email
```

## 多配置格式

复制 `.env.example` 为 `.env`，并且只在 `.env` 中填写真实投稿账号、
邮件设置、webhook URL 和代理值。`.env` 只用于本地，不能提交到仓库。

3 类投稿系统都支持编号配置：

- `AHA_1_*`、`AHA_2_*`、`AHA_3_*`
- `EM_1_*`、`EM_2_*`、`EM_3_*`
- `BMC_1_*`、`BMC_2_*`、`BMC_3_*`

示例：

```env
AHA_1_BASE_URL=https://example-1
AHA_1_USERNAME=...
AHA_1_PASSWORD=...

EM_1_BASE_URL=https://example-2
EM_1_JOURNAL_CODE=jaaa
EM_1_USERNAME=...
EM_1_PASSWORD=...
```

旧版单配置名称仍然可用：

- `AHA_BASE_URL`、`AHA_USERNAME`、`AHA_PASSWORD`
- `EM_BASE_URL`、`EM_JOURNAL_CODE`、`EM_USERNAME`、`EM_PASSWORD`
- `BMC_SUBMISSION_URL`、`BMC_USERNAME`、`BMC_PASSWORD`

如果某个系统存在任意编号配置，程序会优先使用该系统的编号配置。

## 输出

程序会持续更新同一组文件，而不是每次刷新都创建新文件：

- `outputs/statuses.json`：当前活跃投稿和增量历史
- `outputs/statuses.csv`：当前活跃状态快照
- `outputs/refresh_log.csv`：追加式刷新历史；每次刷新都会写入汇总行和当前快照行

## 代理

如果投稿网站或 Gmail SMTP 只有通过代理才稳定，可以在 `.env` 或 GUI 中设置：

```env
HTTP_PROXY=http://proxy-host:proxy-port
HTTPS_PROXY=http://proxy-host:proxy-port
NO_PROXY=example.com,example.org
```

说明：

- `HTTP_PROXY` / `HTTPS_PROXY` 应该填写完整代理 URL
- `NO_PROXY` 可选，用于指定不走代理的域名
- SMTP 邮件会通过 HTTP CONNECT 隧道使用同一组代理值

## 构建 EXE

构建 Windows 启动器：

```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

输出：

- `dist/JournalCheck.exe`

说明：

- 这个启动器会使用你的本地 Python 或 `.venv`
- 它是原生 Windows 启动器，不是完整自包含的冻结包

## 状态变化通知

如果刷新后检测到状态变化，程序可以发送通知。

支持的通知渠道：

- SMTP 邮件
- 企业微信群机器人 webhook（`WECOM_WEBHOOK_URL`）

## 备注

- AHA 只检查 `Live Manuscripts`
- EM 只保留活跃投稿
- BMC 还会记录邀请、接受和返回审稿人数
- AHA 历史审稿意见 URL 会被记录，程序会尝试提取可读的审稿意见块

## 支持更多投稿平台

如果你需要支持其他投稿平台，请提交 issue，并说明平台名称以及你希望跟踪的状态页或工作流。
