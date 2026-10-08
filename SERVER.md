# journalcheck 私有网页服务

支持个人多账号 AHA、Editorial Manager、Springer Nature/BMC 和 ScholarOne。后台与网页独立运行；没有账号时后台不会连接期刊或发送通知。

## DS 安装

项目位于 `/home/qidahe/Vibecode/journalcheck`。代码保留原 Windows GUI 与 CLI；新增 `python -m journalcheck.server` 入口。

```bash
cd /home/qidahe/Vibecode/journalcheck
/home/qidahe/.conda/envs/ukb-rpy/bin/python -m venv .venv
.venv/bin/python -m pip install -r requirements-server.lock
.venv/bin/python -m playwright install chromium
.venv/bin/python deploy/install.py --dry-run
.venv/bin/python deploy/install.py
```

安装脚本从当前 Tailscale 节点获取所有者身份，初始化私有数据，保存部署前 Serve 配置，并安装 `journalcheck-web.service` / `journalcheck-worker.service` 两个用户服务。需要当前用户已有 Tailscale 和 user systemd 权限，不使用 sudo。已有服务升级请使用下方专用升级脚本；安装脚本会拒绝覆盖已有门户认证配置。

入口：https://ds039180-qidahe.tail20892e.ts.net/journal/

网页需要你的 Tailscale 身份和管理密码。首次密码通过服务器 SSH 读取：

```bash
cat /home/qidahe/Vibecode/journalcheck/.runtime/initial-password.txt
```

首次修改后文件自动删除，所有原会话失效。请使用密码管理器保存新密码。

## 使用

1. 登录并修改初始密码。首页按来源展示稿件：BMC 按账号分组，EM/AHA 按期刊分组，可搜索标题、编号、期刊或账号并筛选平台与状态。
2. 在“期刊与账号”选择“添加期刊／账号”。BMC 先保存邮箱密码或 ORCID 登录资料，可不填文章；EM 只需期刊链接、用户名和密码，期刊代码自动识别，期刊显示名称可选；AHA 填官方投稿网址及登录资料。
3. BMC 进入“管理账号”，点击“添加文章”，填写一条或多行官方 submission-details 链接。无需重复输入密码，每个账号最多 30 篇。空账号不可检查；重复链接整批拒绝。
4. 点击“验证并启用”，任务后台运行，页面可继续使用。首次成功建立安静基线；已启用账号添加新文章后继续检查，新文章首次成功同样不发变化通知。
5. “文章”与“登录设置”分开管理。每篇文章可查看历史、打开官方页面、修改链接或停止追踪；更换/停止链接会保留并归档原稿件历史。移除最后一条链接后暂停账号。
6. 修改登录资料时密码留空保留原值，文章列表不受影响。用户名、密码、登录方式或期刊入口变化后暂停并需重新验证；账号名称/期刊显示名称变化不暂停检查。
7. EM/AHA/ScholarOne 从投稿系统自动获取文章。同一期刊的多个登录账号在期刊内独立管理，不共享密码；未配置期刊显示名称时采用抓取的期刊名称，EM/ScholarOne 尚无结果时采用代码（例如 GHRPJ、AGEING）。
8. 在“通知”填写 PushPlus Token，保存后点击“PushPlus 邮件”测试；请先在 PushPlus 个人资料绑定并验证收件邮箱。邮件编码留空使用官方渠道，自定义渠道才填写编码。
9. “设置”可选择 30/60/120/360 分钟及暂停自动检查，配置 HTTP/HTTPS 代理和 NO_PROXY。

归档账号仍可编辑资料，需恢复后添加文章、验证和监测。“更多操作”中可归档或删除账号；删除从管理列表移除、清除当前登录凭据并停止监测，稿件与历史保留。归档/删除均不会物理删除稿件历史。平台没有提供的审稿人数显示“未提供”，旧人数明确标注“上次记录”。

明确拒稿、录用、撤回和发表自动归档，保留原始状态及所有历史。按个人监测规则，BMC “You need to choose another journal”及明确建议转投也归类为“拒稿／建议转投”。Decision、With a Decision、Completed、审稿人接受邀请和转投处理中不能单独证明终态，继续显示。升级会一次性补归档旧的明确终态记录，保留 ID、历史和时间字段，不补发过往变化通知；后续真实状态变化仍正常通知。

BMC 后台复用一次登录并依次检查，文章有独立的尝试/成功时间及错误记录。某文章失败不覆盖其旧状态，也不误标为稿件消失；其他有效结果继续保存。检查期间账号资料或文章链接被修改，旧版本结果不写回。只有该账号有权限查看的文章链接才能追踪。

资料接口使用 `PATCH /journal/api/accounts/{id}`，不接受文章链接字段；文章接口使用 `POST /accounts/{id}/targets`（`urls` 数组）、`PATCH /accounts/{id}/targets/{target_id}`（单个 `url`）以及 `DELETE` 同路径。所有业务路径均位于 `/journal/api/`；详情读取返回脱敏账号和包括归档稿件的列表，写操作验证登录、Origin 和 CSRF。

ORCID 通过 Springer Nature 官方“Continue with ORCID”进入浏览器 OAuth 登录。用户名填完整 ORCID iD，密码填 ORCID 密码，加密保存；后台使用隔离浏览器上下文，检查结束释放，不导出登录 Cookie。仅支持官方登录流程及其只读授权；验证码、二次验证、额外权限或账号关联页面需在官方网页处理。进入投稿系统后，如果稿件显示 Not Found/403/404，提示为稿件访问问题，不把它当作密码错误。

验证码、MFA、SSO 或页面变化会显示需要处理或解析失败，不自动绕过。错误提示只显示分类，不保存响应全文或凭据。网站支持以实际账号验证为准。

ScholarOne 只需期刊链接、用户名和密码，例如 `https://mc.manuscriptcentral.com/ageing`；目录自动识别为期刊代码，该入口的期刊显示为 **Age and Ageing**。保存时移除登录会话查询参数，按期刊分组并自动读取作者中心的已提交稿件、可见修订队列、已决定稿件及合著稿件；稿件操作按钮不会被点击。期刊名优先取明确期刊标题或标识，已知期刊代码提供经过核对的名称回退，不把 Author Center 等页面名称当作期刊。只有识别出作者表格及明确空列表标记才接受空结果；分页、字段或任一队列无法确认时保留旧记录。

ScholarOne 默认全自动登录：点击“验证并启用”后，worker 在私有显示上的 Chromium 中识别官方表单、提交已保存凭据并读取作者队列。已有会话优先复用；失效后自动重新登录，成功后刷新加密会话副本。DS 的无 Cookie 实测验证了该路径，但不保证所有期刊和安全验证均可自动完成。`challenge` 表示官方要求额外验证，不代表密码错误；此时使用“手动登录／更新会话”兜底，完成官方验证、登录并保存后重新检查。手动窗口最长 15 分钟；查询页面不会触发登录，保存会话也不会自行启用账号或建立稿件基线。

自动登录、手动登录和 worker 共用每个账号独立的私有持久化浏览器资料，并用原 Fernet 密钥加密保存会话 Cookie／localStorage 的恢复副本，避免 Chromium 关闭后丢弃临时登录 Cookie。账号登录资料变化后选用新资料，名称修改不失效。手动登录期间该账号的检查暂缓，不增加失败次数或发失败邮件；其他账号仍可检查。浏览器／worker 进程重启会释放系统文件锁；过期和中断窗口可重新打开。删除账号移除当前凭据、关闭手动窗口，清理浏览器资料；若 worker 正在使用资料，等待本轮释放锁后清理。稿件历史仍保留。所有 Cookie／浏览器资料只存于私有运行目录，不通过 API 导出。

Ubuntu 22.04 amd64 的浏览器组件按锁定版本安装到项目目录，无需 root：

```bash
.venv/bin/python deploy/install_browser.py
```

脚本下载并校验 Ubuntu 的 TigerVNC、键盘与字体库以及 noVNC 1.6.0；保留各组件许可证与校验清单，不写系统目录。为支持私有键盘工具，将锁定 TigerVNC 二进制内唯一的 `/usr/bin` 键盘工具目录常量清空，改由进程私有 PATH 寻找 xkbcomp；不修改指令。RFB 仅监听权限 600 的私有 Unix 套接字，X11 使用私有 Xauthority、不监听 TCP。noVNC 的 HTTP 资源和 WebSocket 都受现有 Tailscale／签名门户认证保护，WebSocket 另校验同源 Origin，写操作仍校验 CSRF。

重建服务器时重新安装组件并更新网页服务。数据库和 Fernet 密钥恢复后，浏览器会话可重新登录（worker 自动登录或手动入口均可）；不需要把浏览器资料当作数据库恢复的必要条件。若备份浏览器资料，应先关闭登录窗口、等待该账号后台任务结束，并按秘密文件权限保管。会话仍可能被官方要求重新验证；届时自动路径会先用已保存账号重登，仍不能通过时才需要手动入口。

会话设计参考 [IEEE-ScholarOne-Status-Monitor](https://github.com/fdgdws/IEEE-ScholarOne-Status-Monitor)；远程浏览器采用 [noVNC](https://github.com/novnc/noVNC)（MPL-2.0）与 [TigerVNC](https://github.com/TigerVNC/tigervnc)（GPL-2.0）。journalcheck 的账号、队列、错误保护和 PushPlus 邮件继续由本服务管理。

2026-10-03 实际 AGEING 账号登录与后台复用已通过，正式 worker 建立 4 篇合著稿件的首次基线并启用监测：1 篇处理中，3 篇明确拒稿保留在归档。仅有合著稿件的账号不需要存在 Submitted Manuscripts 队列。决定日期按官方状态中的日期读取，首次基线不发送稿件变化通知。长期稳定性继续观察；详情见 `VALIDATION.md`。

ScholarOne 作者中心流程参考[官方作者支持](https://clarivate.com/academia-government/training-support/scholarone-manuscripts/authors/)；表单字段及队列定位参考公开作者项目的页面结构（[MIT 项目](https://github.com/AvivYaish/ScholarOneManuscriptcentralAutomaticStatusCheck)、[队列观察](https://github.com/emanueledelsozzo/manuscript-central-checker)）。解析器和保护逻辑独立实现，未复制其程序。

管理会话有效期 24 小时；修改密码会注销全部会话。期刊密码、PushPlus Token 和带认证信息的代理配置使用 Fernet 加密保存，密钥单独位于 `.runtime/keys/fernet.key`。目录 700，数据库及密钥 600。拥有同一服务器用户权限的进程仍属于信任边界；Tailscale 身份与管理密码均不替代服务器账户隔离。

## 运行和诊断

```bash
systemctl --user status journalcheck-web journalcheck-worker
journalctl --user -u journalcheck-web -u journalcheck-worker --since '1 hour ago' --no-pager
.venv/bin/python -m journalcheck.server backup
```

网页只监听 `127.0.0.1:18181`。业务接口对外位于 `/journal/api/`，Tailscale 去掉代理前缀，由 FastAPI root_path 生成链接。独立模式验证 Tailscale 身份及管理密码；现有 DS 门户模式验证门户签名的身份和会话上下文，拒绝缺失、伪造或过期签名。两种模式的写接口均验证同源 Origin 和 CSRF。未开放公开文档或公网 Funnel。

单 worker 由操作系统文件锁限制，检查依次执行；每个账号最多 10 分钟。临时网络错误有限重试，失败保留上次有效数据。超时会结束该次抓取及其浏览器子进程，不影响网页或 worker。进程重启将运行任务标为中断，仍在队列的任务继续处理；定时检查不补跑错过的所有周期。

通知和状态事件原子保存，仅通过 PushPlus 邮件渠道（`channel=mail`）发送纯文本。Token 加密保存，接口仅返回“已配置”，留空保留原值。无须在 journalcheck 填收件地址或 SMTP 账号；邮件发送到该 Token 所属 PushPlus 账号的已验证邮箱。

稿件变化邮件的主题包含稿件编号和标题，正文逐项列出完整文章标题、期刊、账号、编号、当前状态和本次变化。按 [PushPlus 标题长度限制](https://www.pushplus.plus/doc/help/limit.html)，主题超过 100 字时缩短并加省略号，正文保留完整标题；本轮未提供标题时采用上次有效标题。只显示实际变化的字段，审稿人数更新不会被写成状态变更。新文章首次成功建立基线时不通知；账号登录失败和恢复提醒继续按账号发送。

临时网络/HTTP 服务失败后按 1、5、15、60 分钟重试，随后保留失败记录供手动补发。Token、邮箱绑定、额度等被明确拒绝时停止自动重试，修正后再补发。暂停通知阻止发送，重新启用后可对失败记录手动补发。

PushPlus `/send` 为异步接口：返回 `code=200` 和有效流水号时记录为“PushPlus 已受理”，不标为邮件已送达。通知页显示流水号，请在 PushPlus 消息列表和收件箱确认最终结果。已受理记录不会自动或手动重发；发送超时、发送后进程中断可能造成少量重复。暂未接入需要额外 AccessKey 配置的最终结果查询接口。

升级会删除当前设置中的 SMTP/企业微信配置，取消旧渠道未完成通知，保留历史记录与稿件数据。旧备份仍可能含加密的旧配置；恢复旧数据库后启动新版会再次迁移。原 Windows GUI/CLI 保持兼容，DS 网页及 worker 完全使用 PushPlus。

日志不记录原始登录错误、Cookie、密码、Token 或 PushPlus 原始响应。需要检查服务状态时请避免输出或分享整个私有运行目录。

## 备份、恢复和升级

数据库每日在线备份到 `.runtime/backups/`，保留最近七份。安装/升级前另外触发备份。数据库备份不替代 Fernet 密钥的备份；请将 `.runtime/keys/fernet.key` 另存到可信的私有备份位置，保留原权限。新生成的密钥无法解密旧数据库。

恢复步骤：

1. 停止两个 journalcheck 用户服务。
2. 先保存当前运行目录；选择已确认的数据库备份及其配套原密钥。
3. 恢复数据库到 `.runtime/journalcheck.sqlite`，清除关闭服务后遗留的旧 `journalcheck.sqlite-wal` / `journalcheck.sqlite-shm`，恢复配套密钥到 `.runtime/keys/fernet.key`。
4. 设置目录 700、私有文件 600，确认用户为当前服务用户。
5. 启动两个服务，查看健康状态、稿件数量和加密配置是否可读取；确认后再启用检查。

管理密码遗失时在 SSH 内运行：

```bash
.venv/bin/python -m journalcheck.server reset-password
```

它生成新初始密码文件并注销会话，保留期刊账号和状态。升级前保存当前代码版本/工作副本、数据库、密钥和服务配置，安装锁定依赖后运行测试，再运行 `.venv/bin/python deploy/upgrade.py`。升级脚本等待运行中的任务结束，先在线备份数据库、原 Fernet 密钥和当前服务配置，再重启两个服务；不改写服务配置及 Tailscale 路由。备份使用私有权限保存到 `.runtime/upgrades/`，校验 SQLite 完整性。

回滚服务接入：

```bash
.venv/bin/python deploy/install.py --rollback
```

上述回滚仅适用于独立 Serve 部署。门户认证部署请停止 journalcheck 两个服务，并在原门户配置中移除 journal 路由；不要重新安装独立 Serve 配置。保留数据库及其它 Serve 配置。部署前配置快照在 `.runtime/serve-before.json`。

## 验证

```bash
.venv/bin/python -m pytest tests -q
node --check journalcheck/server/static/app.js
.venv/bin/python tests/browser_layout.py
.venv/bin/python main.py --help
```

自动测试使用脱敏 HTML 和模拟网络，不登录真实期刊、不发送真实通知。上线后由管理者填写账号，逐个平台与官方页面核对，再进行真实通知测试及 48 小时运行观察。缺少真实账号的平台不得标为真实登录验收通过。Windows GUI 在服务器上只能做导入/调用兼容检查，实际窗口运行需要 Windows 设备。

## PushPlus 官方配置参考

- [邮件绑定与渠道说明](https://www.pushplus.plus/doc/extend/mail.html)
- [发送参数和异步受理说明](https://www.pushplus.plus/doc/guide/api.html)

只需准备用户 Token 或消息 Token，并先验证绑定邮箱。官方邮件渠道无需邮件编码，默认推送渠道也不必修改，服务会明确指定 `mail`。请通过 Tailscale 网页填写 Token，不将其放入聊天、Git 或公开日志。

## 可选：真实 ScholarOne 自动登录诊断

仅在确需验证真实登录时运行（会访问官方期刊）：

```bash
.venv/bin/python deploy/check_scholarone.py --account-id <账号内部ID>
```

诊断只读生产数据库中的已保存账号，在隔离临时目录中从零 Cookie 自动登录，再复用加密会话抓取第二次。它只输出成功状态和稿件数量，不更新生产数据、不发通知、不输出稿件编号或秘密，退出后清理临时资料。安全验证或认证失败会返回非零退出码。

## 账号密码库

导航中的“密码库”集中展示现有期刊账号；支持搜索、平台与归档筛选。BMC 账号只显示一条，不按文章重复；EM、ScholarOne 同用户名的不同期刊仍各自保留。列表和账号编辑表单不返回密码。

点击查看或复制密码前，输入当前门户密码（独立部署则为 journalcheck 管理密码）。每个登录会话独立解锁 5 分钟，查询不会延长有效期；可立即锁定。明文仅经受保护的 POST 接口返回所选账号，最多显示 30 秒，关闭窗口、切页或页面进入后台时清除展示。退出／修改管理密码使会话及解锁权限失效；其它已打开标签页通过后续鉴权与轮询清除显示。复制成功后剪贴板由设备管理，不承诺自动擦除系统剪贴板。

编辑复用原账号配置，空密码保留原值，修改登录凭据后暂停并要求验证。归档账号可以查看；删除账号后不能再取回当前凭据，已有备份按原保留策略处理。查看／复制记录只含时间、操作和账号关联，不含秘密。密码、解锁密码不保存到浏览器持久存储、URL、日志或 CSV；响应禁止缓存。

这仍是服务端加密保管：后台需要原密钥解密才能自动检查。拥有服务器用户权限和密钥的人能访问凭据。

### 门户配套更新

DS 的门户源代码独立于本仓库。`deploy/portal-vault.patch` 包含本功能所需的最小门户改动及合成测试：会话级解锁表、Origin／CSRF／密码验证、签名上下文中的短期查看权限，以及两条受认证的路由。不复制门户密码散列至 journalcheck。

更新前备份门户代码、Caddyfile、门户数据库和 journalcheck 数据库／密钥。对**尚未应用**此扩展的兼容门户目录先检查补丁（已有改动时不要重复应用）：

```bash
patch --dry-run -p1 -d /path/to/server-portal < deploy/portal-vault.patch
patch -p1 -d /path/to/server-portal < deploy/portal-vault.patch
```

运行门户认证测试、journalcheck 测试及 Caddy 配置校验。验证后重启 `server-portal-auth`、`journalcheck-web`，再重启 `server-portal` 使新增路由生效；worker 无需重启。现有 DS 的 Caddy 管理接口关闭，重启会让现有网页连接短暂重连。回滚时恢复原三份门户模块和 Caddyfile、上一版 journalcheck 网页源码，再重启对应服务；新增解锁表可保留，原账号和后台抓取不依赖该表。

额外验证命令：

```bash
.venv/bin/python -m pytest tests/test_vault.py -q
.venv/bin/python tests/browser_vault.py
```

浏览器集成测试需要相邻的 `server-portal` 源码及其 Caddy 二进制；它启动隔离端口及合成数据库，不使用生产账号。
