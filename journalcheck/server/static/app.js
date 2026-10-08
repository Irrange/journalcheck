(() => {
  'use strict';

  const base = (document.querySelector('meta[name="base-path"]')?.content || '/journal').replace(/\/$/, '');
  const gatewayMode = document.querySelector('meta[name="gateway-mode"]')?.content === 'true';
  const apiRoot = `${base}/api`;
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const state = { csrf: '', tab: 'submissions', accounts: [], submissions: [], archived: [], jobs: [], notifications: [], health: null, settings: null, loaded: false, timer: null, filters: { search: '', platform: '', status: '' }, management: null, activeJob: null, polling: false };
  const titles = { submissions: '投稿总览', accounts: '期刊与账号', vault: '账号密码库', jobs: '任务', notifications: '通知', archive: '归档', settings: '设置' };

  function node(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  function setText(target, value) { const n = typeof target === 'string' ? $(target) : target; if (n) n.textContent = value == null ? '' : String(value); }
  function show(el, yes = true) { if (el) el.classList.toggle('hidden', !yes); }
  function button(label, action, cls = 'button secondary', extra = {}) {
    const b = node('button', cls, label); b.type = 'button'; b.dataset.action = action;
    Object.entries(extra).forEach(([k, v]) => b.dataset[k] = v);
    return b;
  }
  function cell(value, cls = '') { return node('td', cls, value == null || value === '' ? '—' : value); }
  function safeUrl(raw) {
    try { const u = new URL(raw, window.location.href); return ['http:', 'https:'].includes(u.protocol) ? u.href : ''; }
    catch { return ''; }
  }
  function safeSpringerSubmissionUrl(raw) {
    const href=safeUrl(raw);if(!href)return '';
    try { const u=new URL(href);return u.protocol==='https:'&&u.hostname==='submission.springernature.com'&&u.pathname.startsWith('/submission-details/')?href:''; }
    catch { return ''; }
  }
  function link(label, raw, cls = '') {
    const href = safeUrl(raw);
    if (!href) return node('span', cls, label || '—');
    const a = node('a', cls, label || href); a.href = href; a.target = '_blank'; a.rel = 'noopener noreferrer'; return a;
  }
  function date(raw) {
    if (!raw) return '—';
    if (/^\d{4}-\d{2}-\d{2}$/.test(String(raw))) return String(raw).replaceAll('-', '/');
    const d = new Date(raw); if (Number.isNaN(d.valueOf())) return String(raw);
    return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(d);
  }
  function age(raw) {
    if (!raw) return '停留时间未知';
    if (/^\d{4}-\d{2}-\d{2}$/.test(String(raw))) {
      const parts = new Intl.DateTimeFormat('en-CA', {timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date());
      const map = Object.fromEntries(parts.map(p=>[p.type,p.value]));
      const today = Date.parse(`${map.year}-${map.month}-${map.day}`);
      return `已停留 ${Math.max(0,Math.floor((today-Date.parse(raw))/86400000))} 天`;
    }
    const d = new Date(raw); if (Number.isNaN(d.valueOf())) return '停留时间未知';
    const ms = Math.max(0, Date.now() - d.valueOf());
    const days = Math.floor(ms / 86400000), hours = Math.floor(ms / 3600000) % 24;
    if (days) return `已停留 ${days} 天 ${hours} 小时`;
    const mins = Math.floor(ms / 60000); return mins < 60 ? `已停留 ${mins} 分钟` : `已停留 ${Math.floor(mins / 60)} 小时 ${mins % 60} 分钟`;
  }
  function platformName(v) { return ({ aha: 'AHA Journals', em: 'Editorial Manager', scholarone: 'ScholarOne', bmc: 'BMC / Springer Nature' })[v] || v || '未知平台'; }
  function sourceName(v) { return ({ observed: '首次观察', platform: '投稿系统记录' })[v] || '首次观察'; }
  function fieldName(v) { return ({ status: '稿件状态', status_date: '状态日期', reviewer_invited: '已邀请审稿人', reviewer_accepted: '已接受邀请', review_reports_received: '已收到审稿报告', review_comments: '审稿意见' })[v] || '稿件信息'; }
  function eventKindName(v) { return ({ baseline: '建立首次记录', changed: '稿件信息更新', new_submission: '发现新稿件', missing: '暂未在列表中找到' })[v] || '状态记录'; }
  function jobStatusName(v) { return ({ queued: '排队中', running: '运行中', completed: '已完成', partial: '部分完成', failed: '失败' })[v] || v || '未知'; }
  function statusPill(status, extra = '') { return node('span', `status-pill ${statusClass(status)} ${extra}`.trim(), status || '未知'); }
  function statusClass(s) {
    const v = String(s || '').toLowerCase();
    if (/running|运行中/.test(v)) return 'active';
    if (/accept|complete|published|已接收|已发表|完成/.test(v)) return 'good';
    if (/reject|withdraw|error|fail|拒稿|撤稿|失败|错误/.test(v)) return 'bad';
    if (/review|审稿|revision|修改/.test(v)) return 'active';
    return 'neutral';
  }
  function empty(message) { return node('div', 'empty-state', message); }
  function panel(title, hint = '') {
    const p = node('section', 'panel'); const head = node('div', 'panel-heading');
    const group = node('div'); group.append(node('h2', '', title)); if (hint) group.append(node('p', 'muted', hint));
    head.append(group); p.append(head); return { panel: p, heading: head };
  }
  function table(headers) {
    const wrap = node('div', 'table-wrap'); const t = node('table'); const thead = node('thead'); const tr = node('tr');
    headers.forEach(h => tr.append(node('th', '', h))); thead.append(tr); t.append(thead);
    const body = node('tbody'); t.append(body); wrap.append(t); return { wrap, body };
  }
  function formLabel(labelText, input, hint) {
    const l = node('label'); l.append(node('span', '', labelText), input); if (hint) l.append(node('small', 'field-hint', hint)); return l;
  }
  function input(name, value = '', type = 'text') { const i = node('input'); i.name = name; i.type = type; if (value != null) i.value = value; return i; }
  function accountLinkCount(account) { return Array.isArray(account.submission_urls) ? account.submission_urls.length : account.submission_url ? 1 : 0; }
  function proxyInputValue(raw) {
    if (!raw) return '';
    try { const u = new URL(raw); if (u.username || u.password) return ''; }
    catch { if (/^[^\s/@:]+:[^\s/@]+@/.test(raw)) return ''; }
    return raw;
  }
  function checkbox(name, checked, labelText) {
    const l = node('label', 'check-label'); const i = node('input'); i.type = 'checkbox'; i.name = name; i.checked = !!checked; l.append(i, node('span', '', labelText)); return l;
  }
  function flash(message, kind = 'success') {
    const box = $('#global-message'); if (!box) return;
    box.className = `global-message ${kind}`; box.textContent = message; show(box);
    window.clearTimeout(flash.timeout); flash.timeout = window.setTimeout(() => show(box, false), 5500);
  }
  async function request(path, options = {}) {
    const method = (options.method || 'GET').toUpperCase();
    const headers = new Headers(options.headers || {});
    if (options.body !== undefined) headers.set('Content-Type', 'application/json');
    if (!['GET', 'HEAD', 'OPTIONS'].includes(method) && state.csrf) headers.set('X-CSRF-Token', state.csrf);
    let response;
    try {
      response = await fetch(`${apiRoot}${path}`, { ...options, method, headers, credentials: 'same-origin' });
    } catch (err) { throw new Error('无法连接服务，请检查网页服务是否运行。'); }
    if (response.status === 401 && !['/session', '/login', '/password'].includes(path)) {
      if (gatewayMode) { window.location.assign('/portal-auth/logout'); throw new Error('登录状态已失效，正在退出。'); }
      showWorkspace(false); throw new Error('登录状态已失效，请重新登录。');
    }
    const type = response.headers.get('content-type') || '';
    const data = type.includes('application/json') ? await response.json().catch(() => ({})) : await response.text().catch(() => '');
    if (!response.ok) {
      const detail = typeof data === 'object' && data ? data.detail : '';
      throw new Error(detail || `请求失败（HTTP ${response.status}）`);
    }
    return data;
  }
  const send = (path, method, data = {}) => request(path, { method, body: JSON.stringify(data) });
  function showWorkspace(on) { if(!on)window.JournalVault?.hide(); show($('#workspace'), on); show($('#auth-view'), !on); }
  function setAuthMode(changePassword, message = '') {
    showWorkspace(false);
    show($('#login-form'), !changePassword); show($('#password-form'), changePassword);
    setText('#auth-title', changePassword ? '首次登录：修改密码' : '期刊稿件追踪');
    setText('#auth-intro', changePassword ? '首次登录前请先修改管理密码。' : '登录以查看投稿、审稿进度和提醒。');
    $('#auth-error').textContent = message; show($('#auth-error'), !!message);
  }
  async function checkSession() {
    try {
      const s = await request('/session'); state.csrf = s.csrf || '';
      if (gatewayMode) { showWorkspace(true); await loadTab(location.hash.slice(1) || 'submissions'); return; }
      if (!s.authenticated) { setAuthMode(false); return; }
      if (s.must_change_password) { setAuthMode(true); return; }
      showWorkspace(true); await loadTab(location.hash.slice(1) || 'submissions');
    } catch (e) { if (!gatewayMode) setAuthMode(false, e.message); }
  }
  $('#login-form').addEventListener('submit', async e => {
    e.preventDefault(); const form = new FormData(e.currentTarget); const btn = e.currentTarget.querySelector('button'); btn.disabled = true;
    try {
      const s = await send('/login', 'POST', { password: form.get('password') }); state.csrf = s.csrf || state.csrf;
      if (s.must_change_password) { const oldPassword=form.get('password');setAuthMode(true);$('#password-form [name="old_password"]').value=oldPassword; }
      else { showWorkspace(true); await loadTab('submissions'); }
      $('#login-form').reset();
    } catch (err) { setAuthMode(false, err.message); }
    finally { btn.disabled = false; }
  });
  $('#password-form').addEventListener('submit', async e => {
    e.preventDefault(); const form = new FormData(e.currentTarget), btn = e.currentTarget.querySelector('button'); btn.disabled = true;
    try { await send('/password', 'POST', { old_password: form.get('old_password'), new_password: form.get('new_password') }); e.currentTarget.reset();setAuthMode(false, '密码已修改，请使用新密码重新登录。'); $('#login-form [name="password"]').value = ''; }
    catch (err) { $('#auth-error').textContent = err.message; show($('#auth-error')); }
    finally { btn.disabled = false; }
  });
  $('#logout-button').addEventListener('click', async () => {
    window.JournalVault?.hide();
    if (gatewayMode) { window.location.assign('/portal-auth/logout'); return; }
    try { await send('/logout', 'POST', {}); } catch {} stopPolling();state.activeJob=null;state.csrf = ''; $('#login-form').reset();$('#password-form').reset();accountForm.elements.password.value='';setAuthMode(false, '已退出登录。');
  });

  async function loadTab(tab, quiet = false) {
    if (tab.startsWith('accounts/')) { const id=tab.split('/')[1]; state.management={...(state.management||{origin:'accounts',scroll:0,section:'articles'}),id}; tab='accounts'; }
    if (!Object.hasOwn(titles, tab)) tab = 'submissions';
    if(tab!=='vault')window.JournalVault?.hide();
    state.tab = tab; history.replaceState(null, '', tab==='accounts'&&state.management?`#accounts/${state.management.id}`:`#${tab}`); setText('#page-title', titles[tab]);
    $$('#primary-nav a').forEach(a => a.classList.toggle('selected', a.dataset.tab === tab));
    $$('.view').forEach(v => show(v, v.id === `view-${tab}`));
    if (!quiet) $('#view-' + tab).replaceChildren(node('div', 'loading', '正在加载…'));
    try {
      if (tab === 'submissions') await Promise.all([loadSubmissions(false), loadHealth(), loadAccounts()]);
      if (tab === 'archive') await loadSubmissions(true);
      if (tab === 'accounts') { await Promise.all([loadSubmissions(false),loadHealth()]); await loadAccounts(); }
      if (tab === 'vault') await window.JournalVault.show();
      if (tab === 'jobs') await loadJobs();
      if (tab === 'notifications') await Promise.all([loadNotifications(), loadSettings()]);
      if (tab === 'settings') await Promise.all([loadSettings(), loadHealth()]);
      state.loaded = true; setText('#last-updated', `更新于 ${date(new Date().toISOString())}`);
    } catch (err) { if (!quiet) { const v = $('#view-' + tab); v.replaceChildren(empty(err.message)); } }
    if (tab === 'jobs'||state.activeJob) startPolling(); else stopPolling();
  }
  $$('#primary-nav a').forEach(a => a.addEventListener('click', e => { e.preventDefault(); state.management=null; loadTab(a.dataset.tab); }));
  window.addEventListener('hashchange', () => { if (!$('#workspace').classList.contains('hidden')) loadTab(location.hash.slice(1)); });

  async function loadSubmissions(archived) {
    const data = await request(`/submissions?archived=${archived ? 'true' : 'false'}`);
    if (archived) { state.archived = data.items || []; renderArchive(); }
    else { state.submissions = data.items || []; renderSubmissions(); setText('#nav-active-count', state.submissions.length || ''); }
  }
  function journalTitle(account, submissions=state.submissions) {
    if(account.platform==='bmc')return account.name||account.username||'BMC 账号';
    if(account.journal_name)return account.journal_name;
    if(account.platform==='scholarone'){
      const generic=/^(?:AGEING|Author Center|Author Dashboard|Author Main Menu|Author Resources|Manuscript Central|ScholarOne(?: Manuscripts?)?|ScholarOne Portal|Dashboard|Home|Main Menu|Live Manuscripts|Submitted Manuscripts|Manuscripts Awaiting Approval|Manuscripts with Decisions|Awaiting Author Approval|Awaiting Reviewer Scores|New Submissions|Submissions|Queues?)$/i;
      const usable=value=>typeof value==='string'&&value.trim()&&!generic.test(value.trim());
      const identified=submissions.find(r=>r.account_id===account.id&&usable(r.metadata?.journal_name));
      if(identified)return identified.metadata.journal_name.trim();
      const fetched=submissions.find(r=>r.account_id===account.id&&r.source&&r.source!==account.name&&usable(r.source));
      if(fetched)return fetched.source.trim();
      const code=String(account.journal_code||'').trim();
      let ageing=code.toLowerCase()==='ageing';
      try{ageing=ageing||new URL(account.base_url).pathname.split('/').filter(Boolean).some(part=>part.toLowerCase()==='ageing');}catch{}
      if(ageing)return 'Age and Ageing';
      return (code||'ScholarOne 期刊').toUpperCase();
    }
    if(account.platform==='em'){
      const identified=submissions.find(r=>r.account_id===account.id&&r.metadata?.journal_name);
      if(identified)return identified.metadata.journal_name;
      const generic=/^(?:EM|Editorial Manager|Submissions(?: .*?)?|Author Main Menu|Live Manuscripts|New Submissions|BMC|Springer Nature)$/i;
      const fetched=submissions.find(r=>r.account_id===account.id&&r.source&&r.source!==account.name&&!generic.test(r.source));
      if(fetched)return fetched.source;
    }
    if(account.platform==='em')return (account.journal_code||'EM 期刊').toUpperCase();
    if(account.name&&!account.name.includes('@'))return account.name;
    try{return new URL(account.base_url).hostname;}catch{return 'AHA Journals';}
  }
  function sourceGroups(includeArchived=false) {
    const groups=new Map();
    for(const account of state.accounts){
      if(!includeArchived&&account.archived)continue;
      let identity=account.id;
      if(account.platform==='em'||account.platform==='scholarone')identity=(account.journal_code||account.base_url||account.id).toLowerCase();
      if(account.platform==='aha')identity=account.base_url||account.id;
      const key=`${account.platform}:${identity}`;
      if(!groups.has(key))groups.set(key,{key,platform:account.platform,title:journalTitle(account),accounts:[]});
      const group=groups.get(key);group.accounts.push(account);
      if(account.journal_name)group.title=account.journal_name;
    }
    return [...groups.values()].sort((a,b)=>['bmc','em','scholarone','aha'].indexOf(a.platform)-['bmc','em','scholarone','aha'].indexOf(b.platform));
  }
  function accountStatus(account) {
    if(account.archived)return statusPill('已归档');
    if(account.last_error)return statusPill(account.enabled?'监测中 · 需处理':'需要处理','bad');
    if(account.enabled)return statusPill('监测中','good');
    if(account.platform==='bmc'&&!accountLinkCount(account))return statusPill('待添加文章');
    return statusPill(account.baseline||account.last_success_at?'已暂停':'待验证');
  }
  function accountActions(account,compact=false) {
    const actions=node('div','source-actions');
    actions.append(button(account.platform==='bmc'?'管理账号':compact?'管理登录账号':'管理期刊','manage-account','button secondary small-button',{id:account.id}));
    if(account.platform==='scholarone'&&!account.archived)actions.append(button('手动登录／更新会话','browser-session','button secondary small-button',{id:account.id}));
    if(account.platform==='bmc'&&!account.archived)actions.append(button('添加文章','add-target','button primary small-button',{id:account.id}));
    if(!compact&&!account.archived){
      const refresh=button(account.enabled?'刷新此账号':'验证并启用',account.enabled?'refresh-account':'verify-account','button quiet small-button',{id:account.id});
      refresh.disabled=account.platform==='bmc'&&!accountLinkCount(account);actions.append(refresh);
    }
    return actions;
  }
  function renderSubmissions() {
    const view=$('#view-submissions');view.replaceChildren();
    const summary=node('div','summary-strip');
    [['当前稿件',state.submissions.length],['期刊与账号',sourceGroups().length],['后台检查服务',state.health?.worker==='ok'?'在线':state.health?.worker==='stale'?'延迟':'未知']].forEach(([title,value])=>{const card=node('div','summary-card');card.append(node('span','muted',title),node('strong','',value));summary.append(card);});view.append(summary);
    const toolbar=node('div','overview-toolbar');
    const search=input('search',state.filters.search,'search');search.placeholder='搜索标题、编号、期刊或账号';search.setAttribute('aria-label','搜索稿件');
    const platform=node('select');platform.setAttribute('aria-label','按平台筛选');
    [['','全部平台'],['bmc','BMC / Springer Nature'],['em','Editorial Manager'],['scholarone','ScholarOne'],['aha','AHA Journals']].forEach(([value,label])=>{const option=node('option','',label);option.value=value;platform.append(option);});platform.value=state.filters.platform;
    const status=node('select');status.setAttribute('aria-label','按状态筛选');const all=node('option','','全部状态');all.value='';status.append(all);
    [...new Set(state.submissions.map(r=>r.status).filter(Boolean))].sort().forEach(value=>{const option=node('option','',value);option.value=value;status.append(option);});status.value=state.filters.status;
    toolbar.append(search,platform,status,button('导出 CSV','export-current','button secondary'));view.append(toolbar);
    const host=node('div','source-groups');host.id='overview-groups';view.append(host);
    search.addEventListener('input',()=>{state.filters.search=search.value;renderOverviewGroups();});
    platform.addEventListener('change',()=>{state.filters.platform=platform.value;renderOverviewGroups();});
    status.addEventListener('change',()=>{state.filters.status=status.value;renderOverviewGroups();});renderOverviewGroups();
  }
  function matchesSubmission(row,account) {
    const q=state.filters.search.trim().toLowerCase();
    return (!state.filters.status||row.status===state.filters.status)&&(!q||[row.title,row.manuscript_number,row.source,account.name,account.username,journalTitle(account)].join(' ').toLowerCase().includes(q));
  }
  function renderOverviewGroups() {
    const host=$('#overview-groups');if(!host)return;host.replaceChildren();
    for(const group of sourceGroups()){
      if(state.filters.platform&&group.platform!==state.filters.platform)continue;
      const accounts=group.accounts.filter(a=>state.submissions.some(r=>r.account_id===a.id&&matchesSubmission(r,a)));
      if(!accounts.length)continue;
      const box=panel(group.title,group.platform==='bmc'?'BMC / Springer Nature · 账号下管理多篇文章':`${platformName(group.platform)} · 按期刊管理`);box.panel.classList.add('source-group');box.panel.dataset.group=group.key;
      for(const account of accounts){
        const section=node('section','account-section');section.dataset.accountId=account.id;
        const head=node('div','source-heading');const meta=node('div','source-meta');
        meta.append(node('span','source-identity',group.platform==='bmc'?`${account.login_method==='orcid'?'ORCID':'邮箱密码'} · ${account.username}`:`登录账号：${account.username}`),accountStatus(account));
        head.append(meta,accountActions(account));section.append(head);
        if(account.last_error)section.append(node('p','notice error',`${account.last_error} 最近有效稿件信息继续保留。`));
        const rows=state.submissions.filter(r=>r.account_id===account.id&&matchesSubmission(r,account));
        const list=node('div','article-list');rows.forEach(row=>list.append(articleCard(row,account)));
        if(account.platform==='bmc'&&!state.filters.status){
          for(const target of account.targets||[]){
            if(state.submissions.some(r=>r.account_id===account.id&&trackingUrl(r)===target.url))continue;
            if(state.filters.search&&!target.url.toLowerCase().includes(state.filters.search.toLowerCase())&&![account.name,account.username].join(' ').toLowerCase().includes(state.filters.search.toLowerCase()))continue;
            // An archived submission stays in the account's articles area, rather than appearing as an unrecognized link.
            if(target.baseline&&!target.last_error)continue;
            list.append(articleCard(null,account,target));
          }
        }
        if(!list.children.length)list.append(empty(state.filters.search||state.filters.status?'没有符合筛选条件的文章。':account.platform==='bmc'?(accountLinkCount(account)?'暂无当前稿件；可在账号内查看归档文章。':'账号已保存，点击“添加文章”开始追踪。'):'验证并启用后，自动获取该期刊的投稿列表。'));
        section.append(list);box.panel.append(section);
      }host.append(box.panel);
    }
    if(!host.children.length){host.append(empty(state.accounts.length?(state.submissions.length?'没有符合筛选条件的稿件。':'暂无当前稿件；期刊和账号可在“期刊与账号”中管理，有新稿件后会自动显示。'):'还没有监测来源，先添加 BMC 账号或自动获取稿件的期刊。'),button('添加期刊／账号','new-account','button primary'));}
  }
  function trackingUrl(row){return row?.metadata?.tracking_url||row?.detail_url||'';}
  function articleCard(row,account,target=null,management=false) {
    target=target||(account.targets||[]).find(t=>t.url===trackingUrl(row));
    const card=node('article','article-card');if(target)card.dataset.targetId=target.id;if(row)card.dataset.submissionId=row.id;
    const main=node('div','article-main');const title=row?button(row.title||'未命名稿件','detail','text-button',{id:row.id}):node('strong','','待识别稿件');
    main.append(title,node('span','subline',row?[row.source,row.manuscript_number].filter(Boolean).join(' · '):'首次成功检查后自动获取标题与状态'));
    if(row?.missing)main.append(node('p','notice warning','暂未在有效列表中找到，保留最后记录。'));
    if(target?.last_error)main.append(node('p','article-error',target.last_error));
    if(!row&&target)main.append(link(target.url,target.url,'article-url'));
    const metrics=node('div','article-metrics');
    const status=node('div','article-status');status.append(node('span','metric-label','当前状态'),statusPill(row?.status||(target?.last_error?'检查失败':'待检查')));if(row?.archived)status.append(statusPill(row.archive_reason?`已归档 · ${row.archive_reason}`:'已归档'));metrics.append(status);
    const dwell=node('div');dwell.append(node('span','metric-label','状态停留'),node('span','',row?age(row.status_since):'—'));if(row)dwell.append(node('small','subline',sourceName(row.status_since_source)));metrics.append(dwell);
    const reviewers=node('div');reviewers.append(node('span','metric-label','审稿进展'),node('span','',row?reviewerSummary(row):'未提供'));metrics.append(reviewers);
    const checked=node('div');checked.append(node('span','metric-label','最近成功检查'),node('span','',date(target?.last_success_at||row?.last_success_at)));metrics.append(checked);
    const actions=node('div','article-actions');if(row)actions.append(button('查看历史','detail','button quiet small-button',{id:row.id}));
    if(management&&target&&!account.archived)actions.append(button('修改链接','edit-target','button secondary small-button',{id:account.id,target:target.id}),button('停止追踪','remove-target','button quiet small-button',{id:account.id,target:target.id}));
    if(row&&!management)actions.append(button('归档','archive-submission','button quiet small-button',{id:row.id}));
    const url=safeUrl(target?.url||row?.detail_url);if(url)actions.append(link('官方页面 ↗',url,'button quiet small-button'));
    card.append(main,metrics,actions);return card;
  }
  function submissionRow(row, archived) {
    const tr = node('tr'); const main = node('td','primary-cell'); const title = node('button','text-button',row.title || '未命名稿件'); title.dataset.action = 'detail'; title.dataset.id = row.id; main.append(title, node('span','subline',`${row.account_name || '未知账号'} · ${row.manuscript_number || '编号未知'}`)); tr.append(main);
    const st = node('td'); st.append(statusPill(row.status)); if(row.archive_reason)st.append(node('small','subline',row.archive_reason)); st.append(node('small','subline',`时间：${sourceName(row.status_since_source)}`)); tr.append(st);
    const dwell = node('td'); dwell.append(node('span','',age(row.status_since)), node('small','subline',date(row.status_since))); tr.append(dwell);
    tr.append(cell(date(row.last_success_at || row.first_seen_at)));
    tr.append(cell(reviewerSummary(row)));
    const actions = node('td','row-actions'); actions.append(button(archived ? '恢复' : '详情', archived ? 'restore-submission' : 'detail', 'button quiet small-button', { id: row.id }));
    if (!archived) actions.append(button('归档','archive-submission','button quiet small-button',{id:row.id})); tr.append(actions); return tr;
  }
  function reviewerSummary(r) {
    const meta=r.metadata||{},last=meta.last_known_counts||{};
    const part=(key,label,displayKey)=>{const current=r[key];if(current!==null&&current!==undefined)return `${label} ${meta[displayKey]||current}`;if(last[key]!==null&&last[key]!==undefined)return `${label} ${meta[displayKey]||last[key]}（上次记录）`;return `${label} 未提供`;};
    return [part('reviewer_invited','邀请','reviewer_invited_display'),part('reviewer_accepted','接受','reviewer_accepted_display'),part('review_reports_received','报告','review_reports_received_display')].join(' · ');
  }
  async function renderDetail(id) {
    const dialog = $('#submission-dialog'), host = $('#submission-detail'); host.replaceChildren(node('div','loading','正在读取稿件详情…')); dialog.showModal();
    try {
      const result = await request(`/submissions/${encodeURIComponent(id)}`); const row = result.submission || {};
      host.replaceChildren(); const header = node('div','dialog-heading'); const titleWrap = node('div'); titleWrap.append(node('span','eyebrow','MANUSCRIPT'),node('h2','',row.title || '稿件详情'),node('p','muted',`${row.account_name || '未知账号'} · ${row.manuscript_number || '编号未知'}`));
      const close = node('button','icon-button','×'); close.type='button'; close.dataset.closeDialog='submission-dialog'; close.setAttribute('aria-label','关闭'); header.append(titleWrap,close); host.append(header);
      if(state.accounts.some(a=>a.id===row.account_id))host.append(button(row.platform==='bmc'?'管理所属账号':'管理所属期刊','detail-management','button secondary small-button',{id:row.account_id}));
      const grid = node('div','detail-grid');
      const sinceSource=sourceName(row.status_since_source);
      [[ '当前状态',row.status||'未知'],[row.status_since_source==='platform'?'平台记录日期':'首次观察时间',date(row.status_date||row.status_since)],[ '状态停留',age(row.status_since)],['时间来源',sinceSource],['首次发现',date(row.first_seen_at)],['最近成功检查',date(row.last_success_at)],['投稿日期',date(row.submission_date)],['审稿进度',reviewerSummary(row)]].forEach(([k,v])=>{const x=node('div','detail-item');x.append(node('span','muted',k),node('strong','',v));grid.append(x);}); host.append(grid);
      const url = safeUrl(row.detail_url); if (url) { const box=node('p','detail-link'); box.append(link('打开投稿系统中的稿件 ↗',url)); host.append(box); }
      const meta = row.metadata || {};
      const counts=[row.reviewer_invited,row.reviewer_accepted,row.review_reports_received];
      const lastKnown=meta.last_known_counts||{};
      if(counts.some((v,i)=>(v===null||v===undefined)&&lastKnown[['reviewer_invited','reviewer_accepted','review_reports_received'][i]]==null)) host.append(node('p','notice warning',counts.every((v,i)=>(v===null||v===undefined)&&lastKnown[['reviewer_invited','reviewer_accepted','review_reports_received'][i]]==null)?'审稿人数统计未提供。':'部分审稿人数统计未提供。'));
      if (meta.stale_counts || meta.counts_stale) host.append(node('p','notice warning','部分审稿人数来自上次成功记录，本次页面未提供。'));
      if (row.notes) appendTextSection(host,'备注',row.notes);
      if (row.review_comments) appendCommentsSection(host,'审稿意见',row.review_comments);
      const events=node('section','detail-section'); events.append(node('h3','','状态时间线'));
      const list=node('ol','timeline'); (result.events || []).forEach(ev=>{const li=node('li','timeline-item'); const top=node('div','timeline-head'); top.append(node('strong','',eventKindName(ev.kind)),node('time','',date(ev.created_at))); li.append(top); renderEventDetails(li,ev); list.append(li);});
      if (!(result.events || []).length) events.append(empty('暂无历史事件。')); else events.append(list); host.append(events);
    } catch (err) { host.replaceChildren(node('div','dialog-heading',err.message),button('关闭','close-detail','button secondary')); }
  }
  function appendTextSection(host,title,text) { const sec=node('section','detail-section'); sec.append(node('h3','',title),node('p','pre-line',Array.isArray(text)?text.join('\n'):text)); host.append(sec); }
  function appendCommentsSection(host,title,comments) {
    const sec=node('section','detail-section');sec.append(node('h3','',title));
    (Array.isArray(comments)?comments:[comments]).forEach(item=>{
      const text=typeof item==='string'?item:(item&&typeof item==='object'?(item.comment||item.text||item.content||'审稿意见记录'):String(item));
      sec.append(node('p','comment-block',text));
    });host.append(sec);
  }
  function displayFieldValue(field,value) {
    if(value===null||value===undefined||value==='')return '未提供';
    if(field==='review_comments')return Array.isArray(value)?(value.length?`${value.length} 条审稿意见`:'无新增意见'):'审稿意见已记录';
    if(typeof value==='boolean')return value?'是':'否';
    if(typeof value==='object')return '信息已更新';
    return String(value);
  }
  function renderEventDetails(host,ev) {
    const payload=ev.payload||{};
    if(ev.kind==='missing'){host.append(node('p','event-note',payload.description||'本次检查未在有效稿件列表中找到该稿件，保留最后有效记录。'));return;}
    const fields=Array.isArray(payload.fields)?payload.fields:[],before=payload.before||{},after=payload.after||{};
    if(!fields.length){host.append(node('p','event-note',ev.kind==='baseline'?'记录了稿件首次可见信息。':'本次记录未包含可展示的字段变化。'));return;}
    fields.forEach(field=>{
      const item=node('div','event-change');item.append(node('strong','',fieldName(field)));
      if(!payload.before||ev.kind==='baseline'||ev.kind==='new_submission')item.append(node('span','event-values',displayFieldValue(field,after[field])));
      else if(field==='review_comments'&&JSON.stringify(before[field]||null)===JSON.stringify(after[field]||null))item.append(node('span','event-values','审稿意见内容已更新。'));
      else item.append(node('span','event-values',`${displayFieldValue(field,before[field])} → ${displayFieldValue(field,after[field])}`));
      host.append(item);
      if(field==='review_comments'&&after[field])appendCommentsSection(host,'本次记录的审稿意见',after[field]);
    });
  }
  function renderArchive() {
    const view=$('#view-archive'); view.replaceChildren(); const p=panel('归档稿件','归档项目仍可查看详情，也可以恢复到当前投稿列表。');
    const controls=node('div','toolbar');controls.append(node('span','muted','历史稿件记录可下载为 CSV。'),button('导出历史 CSV','export-history','button secondary'));view.append(controls);
    const t=table(['稿件 / 期刊账号','状态','归档前更新时间','审稿进度','']); state.archived.forEach(row=>{const tr=node('tr'); const c=node('td','primary-cell'); const b=node('button','text-button',row.title||'未命名稿件'); b.dataset.action='detail'; b.dataset.id=row.id;c.append(b,node('span','subline',`${row.account_name||'未知账号'} · ${row.manuscript_number||'编号未知'}`));tr.append(c);const s=node('td');s.append(statusPill(row.status));tr.append(s,cell(date(row.last_success_at||row.first_seen_at)),cell(reviewerSummary(row)));const a=node('td','row-actions');a.append(button('恢复','restore-submission','button secondary small-button',{id:row.id}));tr.append(a);t.body.append(tr);});
    p.panel.append(t.wrap); if(!state.archived.length)p.panel.append(empty('暂无归档稿件。')); view.append(p.panel);
  }

  async function loadAccounts() {
    const d=await request('/accounts');state.accounts=d.items||[];
    if(state.tab==='accounts')await renderAccounts();
    if(state.tab==='submissions')renderSubmissions();
  }
  async function renderAccounts() {
    if(state.management){await renderManagement();return;}
    const view=$('#view-accounts');view.replaceChildren();
    const intro=panel('期刊与账号','BMC 在账号内添加文章；EM、ScholarOne 和 AHA 从期刊投稿列表自动获取文章。');intro.heading.append(button('添加期刊／账号','new-account','button primary'));view.append(intro.panel);
    for(const group of sourceGroups(true)){
      const box=panel(group.title,platformName(group.platform));box.panel.classList.add('source-group');
      for(const account of group.accounts){
        const card=node('div','management-card');card.dataset.accountId=account.id;
        const heading=node('div','source-heading');const meta=node('div','source-meta');meta.append(node('strong','',account.platform==='bmc'?account.name:account.username),accountStatus(account));heading.append(meta,accountActions(account,true));card.append(heading);
        card.append(node('p','muted small',`${account.platform==='bmc'?(account.login_method==='orcid'?'ORCID':'邮箱密码')+' · '+account.username:account.name} · ${account.platform==='bmc'?accountLinkCount(account)+' 篇文章':state.submissions.filter(r=>r.account_id===account.id).length+' 篇当前稿件'}`));
        card.append(node('p','muted small',`最近成功检查：${date(account.last_success_at)}`));
        if(account.last_error)card.append(node('p','notice error',account.last_error));box.panel.append(card);
      }view.append(box.panel);
    }
    if(!state.accounts.length)view.append(empty('先保存登录资料，再添加文章或验证期刊。'));
  }
  async function openManagement(id,section='articles') {
    if(!state.management||state.management.id!==id)state.management={id,section,origin:state.tab,scroll:window.scrollY};else state.management.section=section;
    await loadTab(`accounts/${id}`);window.scrollTo(0,0);
  }
  function browserLoginUrl(raw){
    try{
      const url=new URL(raw,window.location.href);
      return url.origin===window.location.origin&&url.pathname.startsWith(`${base}/browser-login/`)?url.href:'';
    }catch{return '';}
  }
  async function refreshBrowserSession(id,target){
    try{
      const data=await request(`/accounts/${encodeURIComponent(id)}/browser-session`);
      if(!target?.isConnected)return;
      const messages={
        idle:'尚未启动浏览器登录。',
        starting:'正在准备安全浏览器登录…',
        active:'请在新标签页完成官方验证和登录。',
        saved:'会话已保存，可以验证并启用。',
        expired:'浏览器登录会话已过期，请重新登录并更新会话。',
        interrupted:'浏览器登录会话已中断，请重新启动登录。',
        cancelled:'浏览器登录会话已取消。',
        failed:'浏览器登录未完成，请重新启动登录。',
      };
      target.textContent=data.message||messages[data.status]||`浏览器会话：${data.status||'未知状态'}`;
    }catch(err){if(target?.isConnected)target.textContent=`无法读取会话状态：${err.message}`;}
  }
  let browserSessionRefreshTimer=null;
  function refreshVisibleBrowserSession(){
    if(document.hidden||state.tab!=='accounts'||!state.management)return;
    const target=$('.browser-session-state');
    if(target)refreshBrowserSession(target.dataset.accountId,target);
  }
  function scheduleBrowserSessionRefresh(){
    window.clearTimeout(browserSessionRefreshTimer);
    browserSessionRefreshTimer=window.setTimeout(refreshVisibleBrowserSession,120);
  }
  window.addEventListener('focus',scheduleBrowserSessionRefresh);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)scheduleBrowserSessionRefresh();});
  async function startBrowserSession(id){
    const popup=window.open('about:blank','_blank');
    if(!popup){flash('浏览器阻止了新标签页，请允许此站点打开标签页后重试。','error');return;}
    popup.opener=null;
    try{
      const data=await send(`/accounts/${encodeURIComponent(id)}/browser-session`,'POST',{});
      const url=browserLoginUrl(data.url);
      if(!url)throw new Error('服务器返回了无效的浏览器登录地址。');
      popup.location.href=url;
      const target=$(`.browser-session-state[data-account-id="${CSS.escape(String(id))}"]`);
      if(target)target.textContent=data.message||'请在新标签页完成官方安全验证和登录。';
      else flash(data.message||'浏览器登录页面已在新标签页打开。');
    }catch(err){popup.close();throw err;}
  }
  async function renderManagement() {
    const selection=state.management;if(!selection)return;
    const data=await request(`/accounts/${encodeURIComponent(selection.id)}`);
    if(state.management!==selection||state.tab!=='accounts')return;
    const account=data.account,rows=data.submissions||[];const view=$('#view-accounts');view.replaceChildren();setText('#page-title',account.platform==='bmc'?'BMC 账号管理':journalTitle(account,rows));
    const back=button(selection.origin==='submissions'?'返回投稿总览':'返回期刊与账号','back-management','button quiet');view.append(back);
    const box=panel(journalTitle(account,rows),`${platformName(account.platform)} · ${account.username}`);box.panel.classList.add('management-detail');box.panel.dataset.accountId=account.id;
    const stateBox=node('div','source-meta');stateBox.append(accountStatus(account));box.heading.append(stateBox);
    const group=sourceGroups(true).find(g=>g.accounts.some(a=>a.id===account.id));
    if(account.platform!=='bmc'&&group?.accounts.length>1){const siblings=node('div','account-switcher');siblings.append(node('span','muted small','该期刊的登录账号'));group.accounts.forEach(a=>siblings.append(button(a.username,'manage-account',`button ${a.id===account.id?'primary':'secondary'} small-button`,{id:a.id})));box.panel.append(siblings);}
    if(state.activeJob&&(!state.activeJob.account_id||state.activeJob.account_id===account.id))box.panel.append(node('p','notice task-notice',`后台任务${jobStatusName(state.activeJob.status)}；可以继续查看和管理文章。`));
    if(account.archived)box.panel.append(node('p','notice warning','账号已归档，可以编辑登录资料；恢复后才能添加文章和启用检查。'));
    if(account.last_error)box.panel.append(node('p','notice error',account.last_error));
    const controls=node('div','management-controls');
    if(account.archived)controls.append(button('恢复账号','restore-account','button primary',{id:account.id}));
    else {
      if(account.platform==='bmc')controls.append(button('添加文章','add-target','button primary',{id:account.id}));
      if(account.platform==='scholarone')controls.append(button('手动登录／更新会话','browser-session','button secondary',{id:account.id}));
      if(account.enabled)controls.append(button('刷新此账号','refresh-account','button secondary',{id:account.id}),button('暂停检查','pause-account','button quiet',{id:account.id}));
      else {const verify=button('验证并启用','verify-account','button secondary',{id:account.id});verify.disabled=account.platform==='bmc'&&!accountLinkCount(account);controls.append(verify);}
    }
    controls.append(button('编辑登录资料','edit-account','button secondary',{id:account.id}));
    const more=node('details','more-actions');more.append(node('summary','','更多操作'));const menu=node('div','more-menu');if(!account.archived)menu.append(button('归档账号','archive-account','button quiet',{id:account.id}));menu.append(button('删除账号','delete-account','button quiet danger-button',{id:account.id}));more.append(menu);controls.append(more);box.panel.append(controls);
    box.panel.append(node('p','muted small',`最近尝试：${date(account.last_attempt_at)} · 最近成功：${date(account.last_success_at)}`));
    if(account.platform==='scholarone'&&!account.archived){
      box.panel.append(node('p','browser-session-help','点击“验证并启用”即可自动登录并获取稿件。后台复用已保存会话，失效后尝试自动重新登录；仅当平台要求额外验证时使用“手动登录／更新会话”。检查失败会保留已有稿件。'));
      const sessionState=node('p','browser-session-state muted small','正在读取浏览器会话状态…');sessionState.dataset.accountId=account.id;box.panel.append(sessionState);
      refreshBrowserSession(account.id,sessionState);
    }
    const tabs=node('div','detail-tabs');[['articles','文章'],['login','登录设置']].forEach(([key,title])=>tabs.append(button(title,'management-section',`button ${selection.section===key?'selected':''}`,{section:key})));box.panel.append(tabs);
    if(selection.section==='login'){
      const grid=node('div','login-summary');[['账号名称',account.name],['登录身份',account.username],['登录方式',account.platform==='bmc'?(account.login_method==='orcid'?'ORCID':'邮箱和密码'):'用户名和密码'],['密码',account.password_configured?'已配置':'未配置']].forEach(([label,value])=>{const item=node('div','detail-item');item.append(node('span','muted',label),node('strong','',value));grid.append(item);});box.panel.append(grid);
      if(account.platform!=='bmc')box.panel.append(link('打开期刊投稿系统 ↗',account.base_url,'detail-link'));
      box.panel.append(node('p','muted small','修改登录资料后需要重新验证；修改名称或期刊显示名称不暂停监测。'));
    }else {
      const articles=node('div','article-list');
      if(account.platform==='bmc'){
        const used=new Set();for(const target of account.targets||[]){const row=rows.find(r=>trackingUrl(r)===target.url);if(row)used.add(row.id);articles.append(articleCard(row,account,target,true));}
        const history=rows.filter(r=>!used.has(r.id));if(history.length){const archived=node('details','account-history');archived.append(node('summary','',`历史与已停止追踪 · ${history.length} 篇`));history.forEach(row=>archived.append(articleCard(row,account,null,true)));articles.append(archived);}
      }else rows.forEach(row=>articles.append(articleCard(row,account,null,true)));
      if(!articles.children.length)articles.append(empty(account.platform==='bmc'?'账号已保存。添加文章链接后，点击“验证并启用”。':'验证并启用后，将自动获取该期刊的投稿列表。'));box.panel.append(articles);
    }
    view.append(box.panel);
  }
  const accountForm=$('#account-form');
  function updatePlatformFields() {
    const platform=accountForm.elements.platform.value;
    const method=accountForm.elements.login_method.value||'password';
    const visible={aha:['.field-username','.field-password','.field-base-url','.field-journal-name'],em:['.field-username','.field-password','.field-base-url','.field-journal-name'],scholarone:['.field-username','.field-password','.field-base-url','.field-journal-name'],bmc:['.field-username','.field-password','.field-login-method']}[platform]||[];
    $$('.platform-field',accountForm).forEach(el=>{const on=visible.some(sel=>el.matches(sel));show(el,on);$$('input,select,textarea',el).forEach(field=>field.disabled=!on);});
    accountForm.elements.platform.disabled=!!accountForm.elements.id.value;
    setText('#username-label',platform==='bmc'?(method==='orcid'?'ORCID iD':'登录邮箱'):'登录用户名');
    setText('#password-label',platform==='bmc'&&method==='orcid'?'ORCID 密码':'登录密码');
    accountForm.elements.username.placeholder=platform==='bmc'?(method==='orcid'?'0000-0000-0000-0000':'name@example.org'):'';
    accountForm.elements.password.required=accountForm.dataset.passwordConfigured!=='true';
    accountForm.elements.base_url.required=platform!=='bmc';
    setText('#base-url-label',platform==='em'?'EM 期刊链接':platform==='scholarone'?'ScholarOne 期刊链接':'期刊投稿网址');
    setText('#base-url-help',platform==='em'?'粘贴该期刊主页或登录页链接，自动提取期刊代码。':platform==='scholarone'?'粘贴 ScholarOne 登录网址，期刊代码将自动识别。':'');
    accountForm.elements.base_url.placeholder=platform==='em'?'https://www.editorialmanager.com/ghrpj/default2.aspx':platform==='scholarone'?'https://mc.manuscriptcentral.com/ageing':'https://…';
    setText('#password-help',accountForm.elements.id.value?'留空保留已保存密码。':'密码加密保存，页面不会回显。');
    setText('#account-flow-help',platform==='bmc'?'保存登录资料后，在账号内添加文章。':'保存后点击“验证并启用”，自动读取该期刊的稿件列表。');
  }
  accountForm.elements.platform.addEventListener('change',updatePlatformFields);
  accountForm.elements.login_method.addEventListener('change',updatePlatformFields);
  function openAccount(row={}) {
    accountForm.reset();accountForm.elements.platform.disabled=false;accountForm.elements.id.value=row.id||'';
    accountForm.dataset.passwordConfigured=row.password_configured?'true':'false';
    for(const key of ['name','platform','username','base_url','journal_name','login_method'])if(row[key]!=null)accountForm.elements[key].value=row[key];
    setText('#account-dialog-title',row.id?'编辑登录资料':'添加期刊／账号');setText('#account-form-error','');show($('#account-form-error'),false);updatePlatformFields();$('#account-dialog').showModal();
  }
  accountForm.addEventListener('submit',async e=>{
    e.preventDefault();const id=accountForm.elements.id.value,platform=accountForm.elements.platform.value;const data={};
    for(const key of ['name','username'])data[key]=accountForm.elements[key].value.trim();data.name=data.name||data.username;data.password=accountForm.elements.password.value;
    if(platform==='bmc')data.login_method=accountForm.elements.login_method.value;
    else {data.base_url=accountForm.elements.base_url.value.trim();data.journal_name=accountForm.elements.journal_name.value.trim();}
    if(!id)data.platform=platform;
    const submit=accountForm.querySelector('[type=submit]');submit.disabled=true;
    try {const saved=await send(id?`/accounts/${encodeURIComponent(id)}`:'/accounts',id?'PATCH':'POST',data);accountForm.elements.password.value='';$('#account-dialog').close();await loadAccounts();await loadSubmissions(false);flash(id?(saved.enabled?'资料已更新，监测继续。':'资料已保存；确认文章后验证并启用。'):'账号已保存。');if(!id)await openManagement(saved.id);}
    catch(err){setText('#account-form-error',err.message);show($('#account-form-error'));}finally{submit.disabled=false;}
  });
  $('#account-dialog').addEventListener('close',()=>{accountForm.elements.password.value='';window.JournalVault?.changed();});
  document.addEventListener('vault-edit',async e=>{try{await loadAccounts();const row=state.accounts.find(a=>a.id===e.detail);if(row)openAccount(row);}catch{flash('账号读取失败。','error');}});
  const targetForm=$('#target-form');
  function openTarget(account,target=null) {
    targetForm.reset();targetForm.elements.account_id.value=account.id;targetForm.elements.target_id.value=target?.id||'';targetForm.elements.urls.value=target?.url||'';
    setText('#target-dialog-title',target?'修改文章链接':'添加文章');setText('#target-dialog-intro',`${account.name} · ${account.username}`);
    setText('#target-help',target?'仅填写一个链接。更换后停止旧链接，旧稿件与历史保留。':'每行一个，可批量添加；首次成功检查建立记录，不发送变化通知。');
    setText('#target-submit',target?'保存链接':'添加文章');show($('#target-form-error'),false);$('#target-dialog').showModal();
  }
  targetForm.addEventListener('submit',async e=>{
    e.preventDefault();const id=targetForm.elements.account_id.value,target=targetForm.elements.target_id.value,urls=targetForm.elements.urls.value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);const submit=targetForm.querySelector('[type=submit]');submit.disabled=true;
    try {if(target&&urls.length!==1)throw new Error('修改时请仅填写一个文章链接。');await send(`/accounts/${encodeURIComponent(id)}/targets${target?'/'+encodeURIComponent(target):''}`,target?'PATCH':'POST',target?{url:urls[0]}:{urls});$('#target-dialog').close();await loadAccounts();await loadSubmissions(false);flash(target?'链接已更新，旧稿件历史保留。':'文章已添加；启用中的账号会在下次检查时读取。');}
    catch(err){setText('#target-form-error',err.message);show($('#target-form-error'));}finally{submit.disabled=false;}
  });

  async function loadJobs() { const d=await request('/jobs');state.jobs=d.items||[];if(state.tab==='jobs')renderJobs(); }
  function summarizeJobResults(results) {
    if(!Array.isArray(results)||!results.length)return '尚无账号结果。';
    const ok=results.filter(x=>x.ok),failed=results.filter(x=>!x.ok),items=results.reduce((n,x)=>n+(Number(x.count)||0),0),changes=results.reduce((n,x)=>n+(Number(x.changes)||0),0);
    const parts=[`${ok.length} 个账号成功`,`${items} 篇稿件`,`${changes} 项变化`];
    const perAccount=results.map(x=>x.ok?`${x.account_name||'未命名账号'}：成功，${Number(x.count)||0} 篇稿件，${Number(x.changes)||0} 项变化`:`${x.account_name||'未命名账号'}：${x.count?'部分成功':'失败'}，${x.count?`${x.count} 篇稿件已保存，`:''}${x.message||x.category||'检查失败'}`);
    if(failed.length)parts.push(`${failed.length} 个账号失败`);
    return `${parts.join(' · ')}。${perAccount.join('；')}`;
  }
  function renderJobs() {
    const view=$('#view-jobs');view.replaceChildren();const p=panel('后台任务','任务进度会自动更新。刷新任务不会在此页面自动重复触发。');const t=table(['任务','状态','开始时间','完成时间','进度','结果 / 错误']);
    state.jobs.forEach(j=>{const tr=node('tr');tr.append(cell(({refresh:'稿件刷新',verify:'账号验证',backup:'备份'})[j.kind]||j.kind||'任务'));const s=node('td');s.append(statusPill(jobStatusName(j.status)));tr.append(s,cell(date(j.started_at||j.created_at)),cell(date(j.finished_at)));let progress='—';if(j.total!=null&&j.total>0)progress=`${j.progress??0} / ${j.total} 个账号`;else if(j.progress!=null)progress=String(j.progress);tr.append(cell(progress));const result=node('td');if(j.error)result.append(node('span','error-text',j.error));else result.append(node('span','small',summarizeJobResults(j.results)));tr.append(result);t.body.append(tr);});
    p.panel.append(t.wrap);if(!state.jobs.length)p.panel.append(empty('暂无任务记录。'));view.append(p.panel);
  }
  function startPolling(){
    stopPolling();state.timer=window.setInterval(async()=>{
      if(state.polling)return;state.polling=true;
      try {
        await loadJobs();
        if(state.activeJob){const job=state.jobs.find(j=>j.id===state.activeJob.id);if(job){state.activeJob=job;if(!['queued','running'].includes(job.status)){state.activeJob=null;await Promise.all([loadAccounts(),loadSubmissions(false)]);flash(`后台检查${jobStatusName(job.status)}。`,job.status==='failed'?'error':'success');}else if(state.tab==='accounts'&&state.management)await renderManagement();}}
        if(state.tab!=='jobs'&&!state.activeJob)stopPolling();
      }catch{}finally{state.polling=false;}
    },7000);
  }
  async function queueCheck(path,data={}) {
    const job=await send(path,'POST',data);state.activeJob=job;startPolling();
    flash(job.reused?'已有检查任务正在执行，请等待完成；未重复创建任务。':'检查已交给后台，可以继续管理文章。');
    if(state.tab==='accounts'&&state.management)await renderManagement();
  }
  function stopPolling(){if(state.timer){clearInterval(state.timer);state.timer=null;}}

  async function loadNotifications(){const d=await request('/notifications');state.notifications=d.items||[];renderNotifications();}
  function renderNotifications(){
    const view=$('#view-notifications');view.replaceChildren();const p=panel('通知记录','PushPlus 已受理表示请求进入发送队列；实际邮件结果请在 PushPlus 消息列表和收件箱确认。');
    const testBox=node('div','inline-actions');testBox.append(node('span','muted','发送测试通知：'),button('PushPlus 邮件','test-pushplus','button secondary small-button'));p.heading.append(testBox);
    const t=table(['通知内容','渠道','状态','尝试次数','计划重试','创建时间','最近错误','']);state.notifications.forEach(n=>{const tr=node('tr');const content=node('td');const detail=node('details');detail.append(node('summary','',n.payload?.subject||'通知'),node('p','pre-line',n.payload?.body||''));content.append(detail);tr.append(content);tr.append(cell(n.channel==='pushplus'?'PushPlus 邮件':'旧渠道历史记录'));const s=node('td');s.append(statusPill(({pending:'等待发送',sending:'发送请求中',accepted:'PushPlus 已受理',sent:'历史发送成功',failed:'发送失败',cancelled:'旧渠道已停用'})[n.status]||n.status));if(n.receipt_id)s.append(node('small','subline',`流水号：${n.receipt_id}`));tr.append(s,cell(n.attempts??'—'),cell(date(n.next_attempt_at)),cell(date(n.created_at)),cell(n.last_error||'—'));const a=node('td','row-actions');if(n.channel==='pushplus'&&n.status==='failed')a.append(button('重试','retry-notification','button quiet small-button',{id:n.id}));tr.append(a);t.body.append(tr);});p.panel.append(t.wrap);if(!state.notifications.length)p.panel.append(empty('暂无通知记录。'));
    view.append(p.panel,renderNotificationSettings());
  }
  function renderNotificationSettings(){
    const p=panel('PushPlus 邮件配置','请先在 PushPlus 个人资料中绑定并验证收件邮箱。消息发送给该 Token 所属账号的已绑定邮箱。');const form=node('form','settings-form');form.dataset.form='notification';const s=state.settings||{};
    form.append(checkbox('pushplus_enabled',s.pushplus_enabled!==false,'启用 PushPlus 邮件通知'));
    const token=input('pushplus_token','','password');token.autocomplete='new-password';
    form.append(formLabel('PushPlus Token',token,s.pushplus_token_configured?'已配置；留空保留原 Token。':'填写用户 Token 或消息 Token；加密保存，不回显。'));
    form.append(formLabel('邮件编码（可选）',input('pushplus_option',s.pushplus_option||''),'使用 PushPlus 官方邮件渠道时留空；自定义邮件渠道填写其编码。'));
    const actions=node('div','form-actions');const save=node('button','button primary','保存通知设置');save.type='submit';actions.append(save);form.append(actions);form.addEventListener('submit',saveSettings);p.panel.append(form);return p.panel;
  }

  async function loadSettings(){state.settings=await request('/settings');if(state.tab==='settings')renderSettings();if(state.tab==='notifications')renderNotifications();}
  function renderSettings(){
    const view=$('#view-settings');view.replaceChildren();const p=panel('服务设置','设置自动检查频率和出站代理。留空的密码字段会保留现有凭据。');const s=state.settings||{};const form=node('form','settings-form');form.dataset.form='settings';
    const interval=node('select');interval.name='interval_minutes';[30,60,120,360].forEach(v=>{const option=node('option','',`${v} 分钟`);option.value=String(v);option.selected=Number(s.interval_minutes??60)===v;interval.append(option);});form.append(formLabel('自动刷新间隔',interval));form.append(checkbox('auto_refresh',s.auto_refresh,'启用自动刷新'));
    ['http_proxy','https_proxy','no_proxy'].forEach(k=>{
      const configured=s[`${k}_configured`];
      const hint=(k!=='no_proxy'&&configured)?'已配置（含认证信息时不会回显）；留空保留现有代理。':'';
      form.append(formLabel(({http_proxy:'HTTP 代理',https_proxy:'HTTPS 代理',no_proxy:'不使用代理的地址'})[k],input(k,k==='no_proxy'?(s[k]||''):proxyInputValue(s[k])),hint));
    });
    const notify=node('p','setting-summary');notify.textContent=`PushPlus 邮件：${s.pushplus_token_configured?'Token 已配置':'Token 未配置'}，${s.pushplus_enabled?'已启用':'已暂停'}。请在“通知”页管理。`;form.append(notify);
    const actions=node('div','form-actions');const save=node('button','button primary','保存设置');save.type='submit';actions.append(save);form.append(actions);form.addEventListener('submit',saveSettings);p.panel.append(form);view.append(p.panel);
    const health=state.health;if(health){const h=panel('服务健康状态');const grid=node('div','health-grid');const items=[['网页服务',health.web||'未知'],['后台检查服务',health.worker||'未知'],['后台检查服务上次活动',date(health.worker_seen_at)],['最近备份',date(health.last_backup_at)],['已启用账号',health.accounts_enabled??'未知'],['当前稿件',health.submissions_active??'未知']];items.forEach(([k,v])=>{const x=node('div','health-item');x.append(node('span','muted',k),node('strong','',v));grid.append(x);});h.panel.append(grid);view.append(h.panel);}
  }
  async function saveSettings(e){e.preventDefault();const f=new FormData(e.currentTarget),isNotify=e.currentTarget.dataset.form==='notification';let data={};
    if(isNotify){['pushplus_token','pushplus_option'].forEach(k=>data[k]=String(f.get(k)||'').trim());data.pushplus_enabled=f.has('pushplus_enabled');}
    else {data.interval_minutes=Number(f.get('interval_minutes'));data.auto_refresh=f.has('auto_refresh');['http_proxy','https_proxy','no_proxy'].forEach(k=>data[k]=String(f.get(k)||'').trim());}
    const btn=e.currentTarget.querySelector('[type="submit"]');btn.disabled=true;try{await send('/settings','PUT',data);await loadSettings();flash('设置已保存。');}catch(err){flash(err.message,'error');}finally{btn.disabled=false;}
  }
  async function loadHealth(){state.health=await request('/health');const dot=$('#worker-dot');dot.className=`health-dot ${state.health.worker==='ok'?'online':state.health.worker==='stale'?'stale':''}`;setText('#worker-label',state.health.worker==='ok'?'后台检查服务在线':state.health.worker==='stale'?'后台检查服务响应延迟':'后台检查服务状态未知');if(state.tab==='submissions')renderSubmissions();if(state.tab==='settings')renderSettings();}

  document.addEventListener('click',async e=>{
    const close=e.target.closest('[data-close-dialog]');if(close){const d=$(`#${close.dataset.closeDialog}`);if(d?.open)d.close();return;}
    const b=e.target.closest('[data-action]');if(!b)return;const action=b.dataset.action,id=b.dataset.id;b.disabled=true;
    try{
      if(action==='new-account')openAccount();
      else if(action==='manage-account')await openManagement(id);
      else if(action==='detail-management'){$('#submission-dialog').close();await openManagement(id);}
      else if(action==='back-management'){const previous=state.management;state.management=null;await loadTab(previous?.origin||'accounts');window.scrollTo(0,previous?.scroll||0);}
      else if(action==='management-section'){state.management.section=b.dataset.section;await renderManagement();}
      else if(action==='browser-session')await startBrowserSession(id);
      else if(action==='add-target'){const row=state.accounts.find(a=>a.id===id);if(row)openTarget(row);}
      else if(action==='edit-target'){const row=state.accounts.find(a=>a.id===id);const target=row?.targets.find(t=>t.id===b.dataset.target);if(target)openTarget(row,target);}
      else if(action==='remove-target'){
        if(window.confirm('停止追踪此文章后，不再检查这个链接。稿件和历史会保留在归档中；其他文章继续监测。')){
          await request(`/accounts/${encodeURIComponent(id)}/targets/${encodeURIComponent(b.dataset.target)}`,{method:'DELETE'});await loadAccounts();await loadSubmissions(false);flash('已停止追踪，稿件历史保留。');
        }
      }
      else if(action==='refresh-account')await queueCheck('/refresh',{account_id:id});
      else if(action==='edit-account'){const row=state.accounts.find(x=>String(x.id)===String(id));if(row)openAccount(row);}
      else if(action==='detail')await renderDetail(id);
      else if(action==='close-detail')$('#submission-dialog').close();
      else if(action==='export-current'||action==='export-history'){const a=document.createElement('a');a.href=`${apiRoot}/exports/${action==='export-current'?'current':'history'}.csv`;a.download='';document.body.append(a);a.click();a.remove();}
      else if(action==='verify-account')await queueCheck(`/accounts/${encodeURIComponent(id)}/verify`);
      else if(action==='pause-account'){await send(`/accounts/${encodeURIComponent(id)}/pause`,'POST',{});flash('账号已暂停。');await loadAccounts();}
      else if(action==='archive-account'){await send(`/accounts/${encodeURIComponent(id)}/archive`,'POST',{});flash('账号已归档。');await loadAccounts();await loadSubmissions(false);}
      else if(action==='restore-account'){await send(`/accounts/${encodeURIComponent(id)}/restore`,'POST',{});flash('账号已恢复。');await loadAccounts();}
      else if(action==='delete-account'){
        const confirmed=window.confirm('删除账号会移除其登录凭据并停止监测；已有稿件和历史记录将保留在归档中。确定删除此账号吗？');
        if(confirmed){await request(`/accounts/${encodeURIComponent(id)}`,{method:'DELETE'});flash('账号已删除，稿件与历史记录已保留。');state.management=null;await loadAccounts();await loadSubmissions(false);}
      }
      else if(action==='enable-account')await queueCheck(`/accounts/${encodeURIComponent(id)}/enable`);
      else if(action==='archive-submission'){await send(`/submissions/${encodeURIComponent(id)}/archive`,'POST',{});flash('稿件已归档。');await loadSubmissions(false);if(state.management)await renderManagement();}
      else if(action==='restore-submission'){await send(`/submissions/${encodeURIComponent(id)}/restore`,'POST',{});flash('稿件已恢复。');if(state.tab==='archive')await loadSubmissions(true);else await loadSubmissions(false);}
      else if(action==='test-pushplus'){await send('/notifications/test','POST',{channel:'pushplus'});flash('测试通知已加入发送队列。');await loadNotifications();}
      else if(action==='retry-notification'){await send(`/notifications/${encodeURIComponent(id)}/retry`,'POST',{});flash('通知已加入重试队列。');await loadNotifications();}
    }catch(err){flash(err.message,'error');}
    finally{b.disabled=false;}
  });
  $('#refresh-button').addEventListener('click',async e=>{const b=e.currentTarget;b.disabled=true;try{await queueCheck('/refresh');}catch(err){flash(err.message,'error');}finally{b.disabled=false;}});
  document.addEventListener('submit',e=>{if(e.target.matches('[data-form="notification"],[data-form="settings"]'))return;});
  document.addEventListener('change',e=>{if(e.target.name==='platform')updatePlatformFields();});
  checkSession();
})();
