/* Credentials remain server-side until a deliberate, audited reveal request. */
(() => {
  'use strict';
  const base=document.querySelector('meta[name="base-path"]').content;
  const gateway=document.querySelector('meta[name="gateway-mode"]').content==='true';
  const view=document.querySelector('#view-vault');
  const unlock=document.querySelector('#vault-unlock-dialog');
  const secret=document.querySelector('#vault-secret-dialog');
  const form=document.querySelector('#vault-unlock-form');
  const output=document.querySelector('#vault-secret-value');
  let active=false, generation=0, until=0, pending=null, shownId=null, timeout=null, ticker=null;
  let rows=[], search='', platform='', archived='all', channel=null;
  const node=(tag,cls,text)=>{const el=document.createElement(tag);el.className=cls||'';if(text!==undefined)el.textContent=text;return el;};
  const button=(text,action,id)=>{const b=node('button','button secondary small-button',text);b.type='button';b.dataset.vaultAction=action;if(id)b.dataset.id=id;return b;};
  const label=p=>({bmc:'BMC / Springer Nature',em:'Editorial Manager',scholarone:'ScholarOne',aha:'AHA Journals'})[p]||p;
  async function api(path,method='GET',body,portal=false){
    const headers={};
    if(method!=='GET'){
      const session=await fetch(portal?'/portal-auth/session':base+'/api/session',{credentials:'same-origin',cache:'no-store'}).then(r=>r.json());
      headers['X-CSRF-Token']=session.csrf||'';headers['Content-Type']='application/json';
    }
    const response=await fetch((portal?'/portal-auth':base+'/api')+path,{method,headers,credentials:'same-origin',cache:'no-store',...(body===undefined?{}:{body:JSON.stringify(body)})});
    const data=await response.json().catch(()=>({}));
    if(!response.ok){if(response.status===401||response.status===403)clearSecret();throw new Error(data.detail||'请求未完成，请重新登录后重试。');}
    return data;
  }
  function message(text){const el=document.querySelector('#vault-message');if(el)el.textContent=text;}
  function renderAudit(items){
    const history=document.querySelector('#vault-audit');if(!history)return;history.replaceChildren(node('summary','', '最近查看与复制记录'));
    for(const item of items)history.append(node('p','small',`${new Date(item.created_at).toLocaleString('zh-CN')} · ${item.action==='vault_copy'?'复制密码':'查看密码'} · ${item.account_name||'已删除账号'}`));
    if(!items.length)history.append(node('p','muted','暂无记录。'));
  }
  function clearSecret(){generation++;clearTimeout(timeout);timeout=null;output.value='';shownId=null;if(secret.open)secret.close();}
  function clearAll(){clearSecret();form.reset();pending=null;if(unlock.open)unlock.close();}
  function status(){
    const locked=Date.now()/1000>=until;
    const el=document.querySelector('#vault-state');if(el)el.textContent=locked?'已锁定':`已解锁 · ${Math.ceil((until-Date.now()/1000)/60)} 分钟内自动锁定`;
    const b=document.querySelector('#vault-lock-toggle');if(b){b.textContent=locked?'解锁密码库':'立即锁定';b.dataset.vaultAction=locked?'unlock':'lock';}
    if(locked&&shownId)clearSecret();
  }
  async function sync(){const result=await api('/vault/status');until=result.unlocked?result.expires_at:0;status();if(!result.unlocked)clearSecret();}
  function renderRows(){
    const list=document.querySelector('#vault-list');if(!list)return;list.replaceChildren();
    const q=search.toLowerCase().trim();
    for(const row of rows){
      if(platform&&row.platform!==platform||archived==='active'&&row.archived||archived==='archived'&&!row.archived)continue;
      if(q&&![row.name,row.journal_name,row.username,label(row.platform)].join(' ').toLowerCase().includes(q))continue;
      const card=node('article','vault-card');card.dataset.accountId=row.id;
      const title=row.platform==='bmc'?row.name:row.journal_name||row.journal_code?.toUpperCase()||row.name;
      card.append(node('h3','',title),node('p','muted',`${label(row.platform)} · ${row.login_method==='orcid'?'ORCID':'账号密码'}${row.archived?' · 已归档':''}`));
      const user=node('div','vault-login');user.append(node('span','metric-label','登录账号'),node('strong','',row.username));
      card.append(user,node('p','vault-mask','密码：••••••••'));
      const actions=node('div','source-actions');
      for(const [text,action] of [['复制账号','username'],['查看密码','view'],['复制密码','copy'],['编辑','edit']])actions.append(button(text,action,row.id));
      const raw=row.platform==='bmc'?(row.login_method==='orcid'?'https://orcid.org/signin':'https://submission.springernature.com'):row.base_url;
      try{const u=new URL(raw);if(u.protocol==='https:'){const a=node('a','button quiet small-button','官方网站 ↗');a.href=u.href;a.target='_blank';a.rel='noopener noreferrer';actions.append(a);}}catch{}
      card.append(actions);list.append(card);
    }
    if(!list.children.length)list.append(node('p','empty-state','没有符合条件的账号。先在“期刊与账号”添加账号即可在这里查看。'));
  }
  async function render(){
    clearAll();const epoch=generation;
    const [accounts,auth,audit]=await Promise.all([api('/accounts'),api('/vault/status'),api('/vault/audit')]);
    if(!active||epoch!==generation)return;
    rows=accounts.items||[];until=auth.unlocked?auth.expires_at:0;view.replaceChildren();
    const panel=node('section','panel');const heading=node('div','panel-heading');
    const intro=node('div');intro.append(node('h2','','账号密码库'),node('p','muted','账号与监测配置共用同一份加密凭据。查看前验证密码，解锁有效期 5 分钟。'));
    const toggle=button('解锁密码库','unlock');toggle.id='vault-lock-toggle';heading.append(intro,toggle);panel.append(heading);
    const state=node('p','notice');state.id='vault-state';panel.append(state);
    const filters=node('div','vault-filters');
    const query=node('input');query.type='search';query.placeholder='搜索账号、期刊或用户名';query.setAttribute('aria-label','搜索密码库');query.value=search;
    query.addEventListener('input',()=>{search=query.value;renderRows();});filters.append(query);
    const select=node('select');select.setAttribute('aria-label','密码库平台');
    for(const p of ['', 'bmc','em','scholarone','aha']){const o=node('option','',p?label(p):'全部平台');o.value=p;select.append(o);}select.value=platform;select.onchange=()=>{platform=select.value;renderRows();};filters.append(select);
    const archivedSelect=node('select');archivedSelect.setAttribute('aria-label','密码库归档筛选');
    for(const [value,text] of [['all','全部账号'],['active','未归档'],['archived','已归档']]){const o=node('option','',text);o.value=value;archivedSelect.append(o);}archivedSelect.value=archived;archivedSelect.onchange=()=>{archived=archivedSelect.value;renderRows();};filters.append(archivedSelect);panel.append(filters);
    const note=node('p','notice');note.id='vault-message';note.setAttribute('role','status');panel.append(note);
    const list=node('div','vault-list');list.id='vault-list';panel.append(list);view.append(panel);renderRows();status();
    const history=node('details','panel');history.id='vault-audit';view.append(history);renderAudit(audit.items||[]);
  }
  async function perform(action,id){
    clearSecret();const epoch=generation;
    if(action==='edit'){document.dispatchEvent(new CustomEvent('vault-edit',{detail:id}));return;}
    const data=await api(`/vault/accounts/${encodeURIComponent(id)}/reveal`,'POST',{purpose:action});
    if(epoch!==generation||!active||document.hidden){data.password='';return;}
    api('/vault/audit').then(audit=>{if(active)renderAudit(audit.items||[]);}).catch(()=>{});
    if(action==='copy'){
      try{await navigator.clipboard.writeText(data.password);message('密码已复制到剪贴板。');}catch{message('浏览器未允许复制，请使用“查看密码”后手动复制。');}
      finally{data.password='';}return;
    }
    shownId=id;output.value=data.password;data.password='';
    document.querySelector('#vault-secret-title').textContent=rows.find(r=>r.id===id)?.name||'账号密码';secret.showModal();
    timeout=setTimeout(clearSecret,Math.max(0,Math.min(30000,(data.visible_until-Date.now()/1000)*1000)));
  }
  async function action(name,id){
    if(name==='hide'){clearSecret();return;}
    if(name==='cancel'){clearAll();return;}
    if(name==='lock'){
      until=0;clearAll();status();channel?.postMessage('lock');
      await api('/vault/lock','POST',{},gateway);message('密码库已锁定。');return;
    }
    if(name==='username'){const row=rows.find(r=>r.id===id);await navigator.clipboard.writeText(row.username);message('账号已复制。');return;}
    if(name==='copy-visible'){id=shownId;name='copy';if(!id)return;}
    if(name==='unlock'){clearAll();document.querySelector('#vault-unlock-error').textContent='';unlock.showModal();return;}
    await sync();
    if(Date.now()/1000>=until){pending={action:name,id};form.reset();document.querySelector('#vault-unlock-error').textContent='';unlock.showModal();return;}
    await perform(name,id);
  }
  document.addEventListener('click',async e=>{
    const b=e.target.closest('[data-vault-action]');if(!b)return;b.disabled=true;
    try{await action(b.dataset.vaultAction,b.dataset.id);}catch{message('操作未完成，请重新解锁或检查浏览器的剪贴板权限。');}finally{b.disabled=false;}
  });
  form.addEventListener('submit',async e=>{
    e.preventDefault();const submit=form.querySelector('[type=submit]');submit.disabled=true;const epoch=generation;
    const password=form.elements.password.value;form.reset();
    try{
      const result=await api('/vault/unlock','POST',{password},gateway);
      if(epoch!==generation||!active||document.hidden){await api('/vault/lock','POST',{},gateway);return;}
      until=result.expires_at;const next=pending;pending=null;unlock.close();status();message('已解锁，5 分钟后自动锁定。');
      if(next)await perform(next.action,next.id);
    }catch(err){document.querySelector('#vault-unlock-error').textContent=err.message;}finally{submit.disabled=false;}
  });
  unlock.addEventListener('close',()=>{form.reset();pending=null;});
  unlock.addEventListener('cancel',clearAll);secret.addEventListener('cancel',clearSecret);
  secret.addEventListener('close',()=>{output.value='';shownId=null;clearTimeout(timeout);});
  window.addEventListener('pagehide',clearAll);
  document.addEventListener('visibilitychange',()=>{if(document.hidden)clearAll();else if(active)sync().catch(()=>{until=0;clearAll();status();});});
  try{channel=new BroadcastChannel('journalcheck-vault');channel.onmessage=()=>{until=0;clearAll();status();};}catch{}
  window.JournalVault={
    async show(){active=true;await render();clearInterval(ticker);let polls=0;ticker=setInterval(()=>{status();if(++polls%5===0)sync().catch(()=>{until=0;clearAll();status();});},1000);},
    hide(){active=false;clearInterval(ticker);clearAll();},
    clear:clearAll,
    changed(){if(active)render().catch(()=>message('账号已保存，请重新打开密码库查看。'));}
  };
})();
