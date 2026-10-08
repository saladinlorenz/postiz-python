const app = document.getElementById('app');
let token = localStorage.getItem('token') || '';
let view = 'launches';
let providers = [];
let integrations = [];
let posts = [];
let editingGroup = null;
let loadedGroup = null;
let selectedChannels = new Set();
let attachedMedia = [];
let channelByIntegration = {};
let channelOptions = {};
let twitchByIntegration = {};
const CHANNEL_PROVIDERS = { discord: 'Discord channel', pinterest: 'Board', youtube: 'Channel', slack: 'Slack channel' };
const TWITCH_COLORS = ['primary', 'blue', 'green', 'orange', 'purple'];

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(opts.headers || {}),
    },
  });
  const text = await res.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { data = { detail: text }; }
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

function toast(message, isError = false) {
  const el = document.createElement('div');
  el.className = 'toast';
  el.style.borderColor = isError ? 'var(--red)' : 'var(--green)';
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function initials(name) {
  return (name || '?').slice(0, 2).toUpperCase();
}

function fmtDate(iso) {
  const d = new Date(iso);
  return d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

// ---------- auth ----------

function renderAuth(mode = 'login') {
  app.innerHTML = `
    <div class="center">
      <div class="card" style="width:380px">
        <h1>${mode === 'login' ? 'Sign in' : 'Create account'}</h1>
        <form class="col" id="authForm">
          ${mode === 'register' ? `
            <div class="field"><label>Name</label><input name="name" placeholder="Your name" /></div>
            <div class="field"><label>Organization</label><input name="organization" placeholder="My organization" /></div>
          ` : ''}
          <div class="field"><label>Email</label><input name="email" type="email" required placeholder="you@example.com" /></div>
          <div class="field"><label>Password</label><input name="password" type="password" required minlength="8" placeholder="At least 8 characters" /></div>
          <div class="error-msg" id="authError"></div>
          <button class="btn" type="submit">${mode === 'login' ? 'Sign in' : 'Register'}</button>
          <button class="btn ghost" type="button" id="switchMode">
            ${mode === 'login' ? 'Create an account' : 'I already have an account'}
          </button>
        </form>
      </div>
    </div>`;

  document.getElementById('switchMode').onclick = () => renderAuth(mode === 'login' ? 'register' : 'login');
  document.getElementById('authForm').onsubmit = async (e) => {
    e.preventDefault();
    const body = Object.fromEntries(new FormData(e.target).entries());
    try {
      const data = await api(`/auth/${mode}`, { method: 'POST', body: JSON.stringify(body) });
      token = data.token;
      localStorage.setItem('token', token);
      await boot();
    } catch (err) {
      document.getElementById('authError').textContent = err.message;
    }
  };
}

// ---------- shell ----------

function shell() {
  app.innerHTML = `
    <header>
      <div class="row"><strong>Postiz Py</strong></div>
      <nav>
        <button data-view="launches" class="${view === 'launches' ? 'active' : ''}">Calendar</button>
        <button data-view="create" class="${view === 'create' ? 'active' : ''}">New post</button>
        <button data-view="accounts" class="${view === 'accounts' ? 'active' : ''}">Accounts</button>
      </nav>
      <button class="btn ghost" id="logout">Logout</button>
    </header>
    <main id="main"></main>`;

  document.querySelectorAll('nav button').forEach((b) => {
    b.onclick = () => { view = b.dataset.view; render(); };
  });
  document.getElementById('logout').onclick = () => {
    token = '';
    localStorage.removeItem('token');
    renderAuth();
  };
}

async function render() {
  document.querySelectorAll('nav button').forEach((b) => b.classList.toggle('active', b.dataset.view === view));
  const main = document.getElementById('main');
  if (view === 'accounts') return renderAccounts(main);
  if (view === 'create') return renderCreate(main);
  return renderLaunches(main);
}

// ---------- accounts ----------

async function renderAccounts(main) {
  integrations = await api('/integrations/list');
  providers = await api('/integrations/providers');
  const connected = new Set(integrations.map((i) => i.providerIdentifier));

  main.innerHTML = `
    <h1>Accounts</h1>
    <div class="grid">
      ${providers.map((p) => `
        <div class="card">
          <div class="integration">
            <div class="info">
              <div class="logo">${esc(initials(p.name))}</div>
              <div>
                <strong>${esc(p.name)}</strong>
                <div class="muted">${esc(p.description)}</div>
              </div>
            </div>
            <button class="btn ${connected.has(p.identifier) ? 'ghost' : ''}" data-connect="${p.identifier}">
              ${connected.has(p.identifier) ? 'Reconnect' : 'Connect'}
            </button>
          </div>
        </div>`).join('')}
    </div>
    <h1 style="margin-top:28px">Connected</h1>
    ${integrations.length ? `<div class="col">${integrations.map((i) => `
      <div class="post-item">
        <div class="row">
          <div class="logo">${esc(initials(i.name))}</div>
          <div>
            <strong>${esc(i.name)}</strong>
            <div class="muted">${esc(i.provider.name)} · @${esc(i.username || '—')}</div>
            ${i.refreshNeeded ? '<div class="error-msg">Reconnection required</div>' : ''}
          </div>
        </div>
        <div class="row">
          ${i.tokenExpiration ? `<span class="muted">token ${fmtDate(i.tokenExpiration)}</span>` : ''}
          <button class="btn danger" data-disconnect="${i.id}">Disconnect</button>
        </div>
      </div>`).join('')}</div>` : '<div class="empty">No account connected yet</div>'}`;

  main.querySelectorAll('[data-connect]').forEach((b) => (b.onclick = () => connectProvider(b.dataset.connect)));
  main.querySelectorAll('[data-disconnect]').forEach((b) => (b.onclick = async () => {
    await api(`/integrations/${b.dataset.disconnect}`, { method: 'DELETE' });
    toast('Account disconnected');
    render();
  }));
}

async function connectProvider(identifier) {
  try {
    const data = await api(`/integrations/social/${identifier}`);
    if (identifier === 'telegram') {
      return telegramFlow(data.state);
    }
    if (identifier === 'mastodon') {
      return mastodonFlow();
    }
    if (data.credentials) {
      const fields = providers.find((p) => p.identifier === identifier).customFields || [];
      openCredentialsModal(identifier, fields);
      return;
    }
    localStorage.setItem('oauth_state', data.state);
    window.location.href = data.url;
  } catch (err) {
    toast(err.message, true);
  }
}

function telegramFlow(word) {
  const wrap = document.createElement('div');
  wrap.className = 'modal-bg';
  wrap.innerHTML = `
    <div class="card modal">
      <h2>Connect Telegram</h2>
      <div class="col">
        <div class="field">
          <label>Bot token (from @BotFather)</label>
          <input id="botToken" type="password" placeholder="123456:ABC-DEF..." />
        </div>
        <div class="muted">Then send this command to your bot or channel:</div>
        <code style="background:var(--bg);padding:8px 12px;border-radius:6px;display:block">/connect ${esc(word)}</code>
        <div class="muted" id="tgStatus">Waiting for the command...</div>
        <div class="error-msg" id="tgError"></div>
        <div class="row">
          <button class="btn ghost" id="cancel">Cancel</button>
        </div>
      </div>
    </div>`;
  document.body.appendChild(wrap);

  let offset = null;
  let stopped = false;
  const status = wrap.querySelector('#tgStatus');

  async function poll() {
    if (stopped) return;
    const botToken = wrap.querySelector('#botToken').value.trim();
    if (!botToken) { setTimeout(poll, 2000); return; }
    try {
      const res = await api('/integrations/telegram/poll', {
        method: 'POST',
        body: JSON.stringify({ word, botToken, offset }),
      });
      if (res.lastChatId) offset = res.lastChatId;
      if (res.chatId) {
        status.textContent = 'Chat found, connecting...';
        await api('/integrations/social-connect/telegram', {
          method: 'POST',
          body: JSON.stringify({ code: String(res.chatId), details: { botToken } }),
        });
        stopped = true;
        wrap.remove();
        toast('Telegram connected');
        render();
        return;
      }
    } catch (err) {
      wrap.querySelector('#tgError').textContent = err.message;
    }
    setTimeout(poll, 2500);
  }
  setTimeout(poll, 1500);
  wrap.querySelector('#cancel').onclick = () => { stopped = true; wrap.remove(); };
}

async function mastodonFlow() {
  const wrap = document.createElement('div');
  wrap.className = 'modal-bg';
  wrap.innerHTML = `
    <div class="card modal">
      <h2>Connect Mastodon</h2>
      <form class="col" id="mdForm">
        <div class="field">
          <label>Instance URL</label>
          <input name="instance" type="text" placeholder="https://mastodon.social" value="https://mastodon.social" required />
        </div>
        <div class="muted">You will be redirected to your instance to authorize Postiz.</div>
        <div class="error-msg" id="mdError"></div>
        <div class="row">
          <button class="btn" type="submit">Continue</button>
          <button class="btn ghost" type="button" id="cancel">Cancel</button>
        </div>
      </form>
    </div>`;
  document.body.appendChild(wrap);
  wrap.querySelector('#cancel').onclick = () => wrap.remove();
  wrap.querySelector('#mdForm').onsubmit = async (e) => {
    e.preventDefault();
    const instance = new FormData(e.target).get('instance').trim().replace(/\/+$/, '');
    try {
      const data = await api(`/integrations/social/mastodon?instance=${encodeURIComponent(instance)}`);
      localStorage.setItem('oauth_state', data.state);
      window.location.href = data.url;
    } catch (err) {
      wrap.querySelector('#mdError').textContent = err.message;
    }
  };
}

function openCredentialsModal(identifier, fields) {
  const wrap = document.createElement('div');
  wrap.className = 'modal-bg';
  wrap.innerHTML = `
    <div class="card modal">
      <h2>Connect ${esc(identifier)}</h2>
      <form class="col" id="credForm">
        ${fields.map((f) => `
          <div class="field">
            <label>${esc(f.label)}</label>
            <input name="${esc(f.key)}" type="${f.type === 'password' ? 'password' : 'text'}" required />
          </div>`).join('')}
        <div class="error-msg" id="credError"></div>
        <div class="row">
          <button class="btn" type="submit">Connect</button>
          <button class="btn ghost" type="button" id="cancel">Cancel</button>
        </div>
      </form>
    </div>`;
  document.body.appendChild(wrap);
  wrap.querySelector('#cancel').onclick = () => wrap.remove();
  wrap.querySelector('#credForm').onsubmit = async (e) => {
    e.preventDefault();
    const values = Object.fromEntries(new FormData(e.target).entries());
    try {
      await api(`/integrations/social-connect/${identifier}`, {
        method: 'POST',
        body: JSON.stringify({ code: btoa(JSON.stringify(values)), state: '' }),
      });
      wrap.remove();
      toast('Account connected');
      render();
    } catch (err) {
      wrap.querySelector('#credError').textContent = err.message;
    }
  };
}

// ---------- create ----------

async function renderCreate(main) {
  integrations = await api('/integrations/list');
  const params = new URLSearchParams(location.search);
  const prefill = params.get('group');
  const selected = selectedChannels;
  const media = attachedMedia;

  if (prefill && prefill !== loadedGroup) {
    loadedGroup = prefill;
    editingGroup = prefill;
    const groupPosts = await api(`/posts/group/${prefill}`);
    if (groupPosts.length) {
      selectedChannels = new Set(groupPosts.map((p) => p.integration.id));
      attachedMedia = groupPosts[0].image || [];
        channelByIntegration = {};
        twitchByIntegration = {};
        for (const p of groupPosts) {
          if (p.settings && p.settings.channel) channelByIntegration[p.integration.id] = p.settings.channel;
          if (p.settings && p.settings.messageType) {
            twitchByIntegration[p.integration.id] = {
              messageType: p.settings.messageType,
              announcementColor: p.settings.announcementColor || 'primary',
            };
          }
        }
    }
  }

  const channelable = (id) => {
    const integration = integrations.find((i) => i.id === id);
    return !!integration && !!CHANNEL_PROVIDERS[integration.providerIdentifier];
  };

  main.innerHTML = `
    <h1>${editingGroup ? 'Edit post' : 'New post'}</h1>
    ${integrations.length ? `
      <div class="col">
        <div class="field">
          <label>Channels</label>
          <div class="chips" id="chips">
            ${integrations.map((i) => `
              <button type="button" class="chip ${selected.has(i.id) ? 'on' : ''}" data-chip="${i.id}">
                ${esc(i.name)} <span class="muted">${esc(i.providerIdentifier)}</span>
              </button>`).join('')}
          </div>
        </div>
        <div id="channelPickers"></div>
        <div class="field">
          <label>Content</label>
          <textarea class="composer" id="content" placeholder="What do you want to publish?"></textarea>
          <div class="muted" id="counter">0 chars</div>
        </div>
        <div class="field">
          <label>Media</label>
          <div class="row" id="mediaRow">
            ${media.map((m, idx) => `
              <div style="position:relative">
                <div class="logo" title="${esc(m.path)}">${m.type === 'video' ? '🎬' : '🖼'}</div>
                <button class="btn danger" style="position:absolute;top:-6px;right:-6px;padding:2px 6px;font-size:11px" data-rmmedia="${idx}">×</button>
              </div>`).join('')}
            <label class="btn ghost" style="display:inline-block">
              + Add
              <input type="file" id="fileInput" accept="image/*,video/*" multiple style="display:none" />
            </label>
          </div>
        </div>
        <div class="row">
          <div class="field" style="flex:1">
            <label>Publish at</label>
            <input type="datetime-local" id="publishDate" />
          </div>
          <button class="btn" id="schedule">Schedule</button>
          <button class="btn ghost" id="now">Publish now</button>
        </div>
        <div class="error-msg" id="createError"></div>
      </div>` : '<div class="empty">Connect an account first in the Accounts tab</div>'}`;

  if (!integrations.length) return;

  const content = document.getElementById('content');
  content.oninput = () => { document.getElementById('counter').textContent = `${content.value.length} chars`; };
  document.getElementById('publishDate').value = toLocalInput(new Date(Date.now() + 3600000));

  async function renderChannelPickers() {
    const box = document.getElementById('channelPickers');
    const channelIds = [...selected].filter(channelable);
    const twitchIds = [...selected].filter((id) => {
      const integration = integrations.find((i) => i.id === id);
      return !!integration && integration.providerIdentifier === 'twitch';
    });
    if (!channelIds.length && !twitchIds.length) { box.innerHTML = ''; return; }
    const blocks = await Promise.all(channelIds.map(async (id) => {
      const integration = integrations.find((i) => i.id === id);
      if (!channelOptions[id]) {
        try {
          channelOptions[id] = await api(`/integrations/${id}/channels`);
        } catch (err) {
          channelOptions[id] = [{ id: '', name: err.message }];
        }
      }
      const list = channelOptions[id];
      const current = channelByIntegration[id] || (list[0] && list[0].id) || '';
      channelByIntegration[id] = current;
      return `
        <div class="field">
          <label>${esc(CHANNEL_PROVIDERS[integration.providerIdentifier] || 'Channel')} · ${esc(integration.name)}</label>
          <select data-pick="${esc(id)}">
            ${list.map((c) => `<option value="${esc(c.id)}" ${c.id === current ? 'selected' : ''}>${esc(c.name)}</option>`).join('')}
          </select>
        </div>`;
    }));
    box.innerHTML = blocks.join('');
    box.querySelectorAll('[data-pick]').forEach((select) => {
      select.onchange = () => { channelByIntegration[select.dataset.pick] = select.value; };
    });
    for (const id of twitchIds) {
      const integration = integrations.find((i) => i.id === id);
      twitchByIntegration[id] = twitchByIntegration[id] || {};
      const current = twitchByIntegration[id].messageType || 'message';
      const color = twitchByIntegration[id].announcementColor || 'primary';
      box.insertAdjacentHTML('beforeend', `
        <div class="field">
          <label>Twitch post type &mdash; ${esc(integration.name)}</label>
          <select data-twitch-type="${esc(id)}">
            <option value="message" ${current !== 'announcement' ? 'selected' : ''}>Chat message</option>
            <option value="announcement" ${current === 'announcement' ? 'selected' : ''}>Announcement</option>
          </select>
        </div>
        <div class="field">
          <label>Announcement color</label>
          <select data-twitch-color="${esc(id)}">
            ${TWITCH_COLORS.map((c) => `<option value="${c}" ${c === color ? 'selected' : ''}>${c}</option>`).join('')}
          </select>
        </div>`);
    }
    box.querySelectorAll('[data-twitch-type]').forEach((select) => {
      select.onchange = () => {
        const id = select.dataset.twitchType;
        twitchByIntegration[id] = twitchByIntegration[id] || {};
        twitchByIntegration[id].messageType = select.value;
      };
    });
    box.querySelectorAll('[data-twitch-color]').forEach((select) => {
      select.onchange = () => {
        const id = select.dataset.twitchColor;
        twitchByIntegration[id] = twitchByIntegration[id] || {};
        twitchByIntegration[id].announcementColor = select.value;
      };
    });
  }

  main.querySelectorAll('[data-chip]').forEach((c) => {
    c.onclick = () => {
      const id = c.dataset.chip;
      selected.has(id) ? selected.delete(id) : selected.add(id);
      if (!selected.has(id)) { delete channelByIntegration[id]; delete twitchByIntegration[id]; }
      c.classList.toggle('on');
      renderChannelPickers();
    };
  });
  renderChannelPickers();

  const fileInput = document.getElementById('fileInput');
  if (fileInput) {
    fileInput.onchange = async () => {
      const files = [...fileInput.files];
      fileInput.value = '';
      for (const file of files) {
        const form = new FormData();
        form.append('file', file);
        try {
          const res = await fetch('/media/upload', {
            method: 'POST',
            headers: token ? { Authorization: `Bearer ${token}` } : {},
            body: form,
          });
          const data = await res.json();
          if (!res.ok) throw new Error(data.detail || 'Upload failed');
          media.push({ id: data.id, path: data.path, type: data.type, alt: '' });
        } catch (err) {
          toast(err.message, true);
        }
      }
      if (files.length) render();
    };
  }
  main.querySelectorAll('[data-rmmedia]').forEach((b) => {
    b.onclick = () => { media.splice(Number(b.dataset.rmmedia), 1); render(); };
  });

  async function submit(publishNow) {
    const errorEl = document.getElementById('createError');
    errorEl.textContent = '';
    if (!selected.size) { errorEl.textContent = 'Select at least one channel'; return; }
    const twitchSettingsFor = (id) => {
      const integration = integrations.find((i) => i.id === id);
      if (!integration || integration.providerIdentifier !== 'twitch') return {};
      const saved = twitchByIntegration[id];
      return saved
        ? { messageType: saved.messageType || 'message', announcementColor: saved.announcementColor || 'primary' }
        : {};
    };
    const dateValue = publishNow ? new Date().toISOString() : new Date(document.getElementById('publishDate').value).toISOString();
    const body = {
      posts: [...selected].map((integrationId) => ({
        integrationId,
        content: content.value,
        publishDate: dateValue,
        group: editingGroup || undefined,
        image: media,
        settings: {
          ...(channelable(integrationId) && channelByIntegration[integrationId]
            ? { channel: channelByIntegration[integrationId] }
            : {}),
          ...twitchSettingsFor(integrationId),
        },
      })),
    };
    try {
      await api('/posts', { method: 'POST', body: JSON.stringify(body) });
      toast(publishNow ? 'Publishing started' : 'Scheduled');
      resetCreateState();
      view = 'launches';
      history.replaceState({}, '', '/launches');
      render();
    } catch (err) {
      errorEl.textContent = err.message;
    }
  }

  document.getElementById('schedule').onclick = () => submit(false);
  document.getElementById('now').onclick = () => submit(true);
}

function toLocalInput(date) {
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function resetCreateState() {
  editingGroup = null;
  loadedGroup = null;
  selectedChannels = new Set();
  attachedMedia = [];
  channelByIntegration = {};
  channelOptions = {};
  twitchByIntegration = {};
}

// ---------- launches ----------

async function renderLaunches(main) {
  posts = (await api('/posts')).filter((p) => !p.parentPostId);
  const groups = {};
  for (const p of posts) (groups[p.group] ||= []).push(p);

  main.innerHTML = `
    <div class="row" style="justify-content:space-between">
      <h1>Calendar</h1>
      <button class="btn" id="newPost">New post</button>
    </div>
    ${posts.length ? `<div class="col">${Object.values(groups).map((group) => {
      const first = group[0];
      return `
        <div class="post-item">
          <div class="content">
            <div class="row" style="margin-bottom:6px">
              <span class="badge ${first.state}">${first.state}</span>
              <span class="muted">${fmtDate(first.publishDate)}</span>
              ${group.map((g) => `<span class="muted">· ${esc(g.integration?.name || '?')}</span>`).join('')}
            </div>
            ${esc(first.content).replace(/\n/g, '<br>')}
            ${first.error ? `<div class="error-msg">${esc(first.error)}</div>` : ''}
            ${first.releaseURL ? `<div class="muted">↗ <a href="${esc(first.releaseURL)}" target="_blank">View post</a></div>` : ''}
          </div>
          <div class="col" style="align-items:flex-end">
            ${first.state === 'ERROR' ? `<button class="btn" data-retry="${first.id}">Retry</button>` : ''}
            <button class="btn ghost" data-edit="${first.group}">Edit</button>
            <button class="btn danger" data-delete="${first.id}">Delete</button>
          </div>
        </div>`;
    }).join('')}</div>` : '<div class="empty">Nothing scheduled yet</div>'}`;

  document.getElementById('newPost').onclick = () => { resetCreateState(); view = 'create'; history.replaceState({}, '', '/create'); render(); };
  main.querySelectorAll('[data-delete]').forEach((b) => (b.onclick = async () => {
    await api(`/posts/${b.dataset.delete}`, { method: 'DELETE' });
    render();
  }));
  main.querySelectorAll('[data-retry]').forEach((b) => (b.onclick = async () => {
    await api(`/posts/${b.dataset.retry}/now`, { method: 'POST' });
    toast('Retrying');
    render();
  }));
  main.querySelectorAll('[data-edit]').forEach((b) => (b.onclick = () => {
    editingGroup = b.dataset.edit;
    view = 'create';
    history.replaceState({}, '', `/launches?group=${editingGroup}`);
    render();
  }));
}

// ---------- oauth callback ----------

async function handleOAuthCallback() {
  const params = new URLSearchParams(location.search);
  const path = location.pathname;
  const match = path.match(/^\/integrations\/social\/([^/]+)$/);
  if (!match) return false;
  const provider = match[1];
  const code = params.get('code') || params.get('oauth_verifier');
  const deviceId = params.get('device_id');
  const state = params.get('state') || params.get('oauth_token');
  if (!code || !state) return false;
  try {
    const res = await api(`/integrations/social-connect/${provider}`, {
      method: 'POST',
      body: JSON.stringify({ code: deviceId ? `${code}&&&&${deviceId}` : code, state }),
    });
    const created = Array.isArray(res) ? res.length : 1;
    toast(created > 1 ? `${created} channels connected` : 'Account connected');
    history.replaceState({}, '', '/accounts');
    view = 'accounts';
    return true;
  } catch (err) {
    toast(err.message, true);
    history.replaceState({}, '', '/accounts');
    view = 'accounts';
    return true;
  }
}

async function boot() {
  try {
    await api('/auth/me');
  } catch {
    renderAuth();
    return;
  }
  const path = location.pathname;
  if (path.startsWith('/launches')) view = 'launches';
  else if (path.startsWith('/accounts')) view = 'accounts';
  else if (path.startsWith('/create')) view = 'create';

  const handled = await handleOAuthCallback();
  shell();
  render();
  if (handled) render();
}

boot();
