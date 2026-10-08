import RFB from '../browser-assets/core/rfb.js';

const base = document.querySelector('meta[name="base-path"]').content;
const identity = document.querySelector('meta[name="browser-session"]').content;
const status = document.querySelector('#status');
const buttons = [...document.querySelectorAll('button')];
let csrf, desktop, connected = false, ended = false;

async function request(path, action) {
  const response = await fetch(base + '/api/' + path, {
    method: action ? 'POST' : 'GET', credentials: 'same-origin', cache: 'no-store',
    headers: action ? {'X-CSRF-Token': csrf} : {},
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || '请求未完成，请返回账号管理重试。');
  return data;
}

async function poll() {
  try {
    const data = await request('browser-sessions/' + identity);
    status.textContent = data.message;
    if (data.status === 'active' && !connected) {
      desktop?.disconnect();
      document.querySelector('#desktop').replaceChildren();
      const url = new URL(base + '/browser-sessions/' + identity + '/socket', location.origin);
      url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
      desktop = new RFB(document.querySelector('#desktop'), url.href);
      desktop.scaleViewport = true;
      desktop.resizeSession = false;
      desktop.showDotCursor = true;
      connected = true;
      desktop.addEventListener('disconnect', event => {
        connected = false;
        if (!ended && !event.detail.clean) status.textContent = '浏览器连接中断，正在重新连接。';
      });
    }
    ended = !['starting', 'active'].includes(data.status);
    buttons.forEach(button => button.disabled = data.status !== 'active');
    if (ended) desktop?.disconnect();
  } catch (error) {
    status.textContent = error.message;
    ended = true;
    desktop?.disconnect();
    buttons.forEach(button => button.disabled = true);
  }
  if (!ended) setTimeout(poll, 2000);
}

for (const button of buttons) button.addEventListener('click', async () => {
  buttons.forEach(item => item.disabled = true);
  try {
    await request('browser-sessions/' + identity + '/' + button.id, true);
    status.textContent = '操作已提交，正在等待浏览器确认。';
  } catch (error) {
    status.textContent = error.message;
  }
});

try {
  const session = await request('session');
  if (!session.authenticated) throw new Error('请先在管理网页登录。');
  csrf = session.csrf;
  poll();
} catch (error) {
  status.textContent = error.message;
  buttons.forEach(button => button.disabled = true);
}
