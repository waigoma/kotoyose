import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { runInNewContext } from 'node:vm';

const asset = (name) => readFileSync(new URL(`../child-app/src/web/${name}`, import.meta.url), 'utf8');
const html = asset('index.html');
const script = asset('app.js');
const providersScript = asset('providers.js');
const emptyPolicy = {
  workspace_roots: [], adapters: [], mcp: {}, logins: [], codex_sandbox_max: 'workspace-write', local_llm: {},
  session_import_sources: [], session_import_origin_hint: null,
};
const status = {
  link: { connection: 'connected', runner: false, work: 0 },
  enrolled: true,
  version: 'test',
  runner_bytes: 0,
};
const emptyComponents = { providers: [], installed: [], needs_reinstall: [] };

class Element {
  children = [];
  listeners = new Map();
  attributes = new Map();
  classList = { toggle() {} };
  hidden = false;
  disabled = false;
  textContent = '';

  append(...children) {
    for (const child of children) {
      if (child instanceof Element) {
        child.remove();
        child.parentElement = this;
      }
      this.children.push(child);
    }
  }
  remove() {
    if (this.parentElement) {
      this.parentElement.children = this.parentElement.children.filter((child) => child !== this);
      this.parentElement = undefined;
    }
  }
  replaceChildren(...children) {
    for (const child of [...this.children]) if (child instanceof Element) child.remove();
    this.children = [];
    this.append(...children);
  }
  setAttribute(name, value) { this.attributes.set(name, value); }
  removeAttribute(name) { this.attributes.delete(name); }
  toggleAttribute(name, enabled) {
    if (enabled) this.setAttribute(name, '');
    else this.removeAttribute(name);
  }
  addEventListener(name, callback) { this.listeners.set(name, callback); }
  focus() {}
  querySelectorAll(tag) {
    return this.children.flatMap((child) => child instanceof Element
      ? [...(child.tagName === tag ? [child] : []), ...child.querySelectorAll(tag)]
      : []);
  }
}

function browser(reply, confirm = () => false) {
  const elements = new Map(Array.from(html.matchAll(/\bid="([^"]+)"/g),
    ([, id]) => [id, new Element()]));
  const progress = elements.get('update-progress');
  Object.defineProperty(progress, 'value', {
    get: () => Number(progress.attributes.get('value') ?? 0),
    set: (value) => progress.setAttribute('value', String(value)),
  });
  elements.get('closed').hidden = true;
  elements.get('policy-inputs').disabled = true;
  elements.get('connection-enrollment').append(elements.get('enroll-form'));
  const tabs = Array.from(html.matchAll(/<button\b[^>]*data-page="([^"]+)"[^>]*>/g), ([markup, page]) => {
    const id = markup.match(/\bid="([^"]+)"/)?.[1];
    return Object.assign(elements.get(id) ?? new Element(), { dataset: { page } });
  });
  const pages = tabs.map(({ dataset: { page: id } }) =>
    Object.assign(elements.get(id), { id, hidden: id !== 'connection' }));
  const select = (page) => tabs.find((tab) => tab.dataset.page === page).listeners.get('click')();
  const intervals = new Map();
  const requests = [];
  const timeouts = [];
  const location = { hash: '#open-code', pathname: '/' };
  const document = new Element();
  document.visibilityState = 'visible';
  document.getElementById = (id) => elements.get(id);
  document.createElement = (tagName) => Object.assign(new Element(), { tagName });
  document.querySelectorAll = (selector) => {
    if (selector === 'button[data-page]') return tabs;
    if (selector === '.page') return pages;
    return [];
  };
  let timerId = 0;

  const testViews = {};
  runInNewContext(script + providersScript + "\ntestViews.declarations = declarationCards;", {
    testViews,
    document,
    window: { confirm },
    location,
    TextDecoderStream,
    history: { replaceState: () => { location.hash = ''; } },
    setInterval: (callback, milliseconds) => {
      intervals.set(++timerId, { callback, milliseconds });
      return timerId;
    },
    clearInterval: (id) => intervals.delete(id),
    setTimeout: (callback) => timeouts.push(callback),
    fetch: async (path, options) => {
      const request = {
        path,
        method: options.method ?? 'GET',
        authorization: options.headers.Authorization,
        credentials: options.credentials,
        body: options.body && JSON.parse(options.body),
      };
      requests.push(request);
      let statusCode;
      let data;
      try {
        [statusCode, data] = reply(request);
      } catch (error) {
        if (request.path === '/api/provider-declarations') {
          [statusCode, data] = [200, { host_fingerprint: '', offers: [] }];
        } else if (request.path === '/api/host-mcp' && request.method === 'GET') {
          [statusCode, data] = [200, { enabled: false }];
        } else {
          throw error;
        }
      }
      return { status: statusCode, ok: statusCode >= 200 && statusCode < 300, json: async () => data, body: data };
    },
  });

  return {
    elements, intervals, location, requests, timeouts,
    declarations: testViews.declarations,
    showWorkspace: () => select('workspace'),
    showProviders: () => select('providers'),
    showConnection: () => select('connection'),
    showSettings: () => select('settings'),
    visibility: (value) => {
      document.visibilityState = value;
      document.listeners.get('visibilitychange')();
    },
  };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

test('a visible Workspace tab heartbeats every minute and stops when hidden or closed', async () => {
  let sessionOpen = true;
  const ui = browser(({ path }) => {
    if (path === '/api/session') return [200, { token: 'session-token' }];
    if (path === '/api/components') return [200, emptyComponents];
    if (path === '/api/policy') {
      return [200, { revision: 'original', policy: emptyPolicy, available_adapters: [], warnings: [] }];
    }
    assert.equal(path, '/api/status');
    return sessionOpen ? [200, status] : [401, {}];
  });
  await flush();
  assert.equal(ui.location.hash, '');
  assert.deepEqual(ui.requests[0], {
    path: '/api/session', method: 'POST', authorization: undefined,
    credentials: 'omit', body: { code: 'open-code' },
  });
  ui.showWorkspace();
  await flush();
  ui.elements.get('policy-form').oninput();
  const policyReads = ui.requests.filter(({ path }) => path === '/api/policy').length;
  const [interval] = ui.intervals.values();
  assert.equal(interval.milliseconds, 60_000);
  await interval.callback();
  assert.deepEqual(ui.requests.at(-1), {
    path: '/api/status', method: 'GET', authorization: 'Bearer session-token',
    credentials: 'omit', body: undefined,
  });
  assert.equal(ui.requests.filter(({ path }) => path === '/api/policy').length, policyReads);

  ui.visibility('hidden');
  assert.equal(ui.intervals.size, 0);
  ui.visibility('visible');
  await flush();
  assert.equal(ui.intervals.size, 1);
  sessionOpen = false;
  await ui.intervals.values().next().value.callback();
  assert.equal(ui.intervals.size, 0);
  assert.equal(ui.elements.get('closed').hidden, false);
  ui.visibility('visible');
  assert.equal(ui.intervals.size, 0);
});

test('a broken policy shows its error and allows a fresh save using its revision', async () => {
  const ui = browser(({ path, method }) => {
    if (path === '/api/session') return [200, { token: 'session-token' }];
    if (path === '/api/components') return [200, emptyComponents];
    if (path === '/api/status') return [200, status];
    assert.equal(path, '/api/policy');
    return method === 'GET'
      ? [200, {
        error: 'Invalid policy file',
        revision: 'broken-file-revision',
        policy: emptyPolicy,
        available_adapters: [],
        warnings: [],
      }]
      : [200, { revision: 'repaired-file-revision', warnings: ['Drive root warning'] }];
  });
  await flush();
  ui.showWorkspace();
  await flush();
  assert.equal(ui.elements.get('policy-load-error').textContent, 'Invalid policy file');
  assert.equal(ui.elements.get('policy-inputs').disabled, false);
  ui.elements.get('policy-form').onsubmit({ preventDefault() {} });
  await flush();
  assert.deepEqual(ui.requests.at(-1).body, {
    revision: 'broken-file-revision', policy: emptyPolicy,
  });
  assert.equal(ui.elements.get('policy-load-error').textContent, '');
  assert.equal(ui.elements.get('policy-warnings').textContent, 'Drive root warning');
});

test('Workspace edits and clears the Local LLM allowlist', async () => {
  const local = { url: 'http://127.0.0.1:11434/v1', models: ['qwen3:4b'] };
  const ui = browser(({ path, method }) => {
    if (path === '/api/session') return [200, { token: 'session-token' }];
    if (path === '/api/components') return [200, emptyComponents];
    if (path === '/api/status') return [200, status];
    assert.equal(path, '/api/policy');
    return method === 'GET'
      ? [200, { revision: 'one', policy: { ...emptyPolicy, local_llm: { ollama: local } }, available_adapters: [], warnings: [] }]
      : [200, { revision: 'two', warnings: [] }];
  });
  await flush();
  ui.showWorkspace();
  await flush();
  const list = ui.elements.get('local-llm');
  const [existing] = list.children;
  assert.deepEqual(existing.querySelectorAll('input').map((input) => input.value),
    ['ollama', local.url, 'qwen3:4b']);
  existing.children[1].listeners.get('click')();
  ui.elements.get('add-local-llm').onclick();
  const [id, url, model] = list.querySelectorAll('input');
  id.value = 'studio';
  url.value = 'http://[::1]:1234/v1';
  model.value = 'loaded-model';
  const save = () => ui.elements.get('policy-form').onsubmit({ preventDefault() {} });
  save();
  await flush();
  assert.deepEqual(ui.requests.at(-1).body.policy.local_llm,
    { studio: { url: url.value, models: [model.value] } });
  list.children[0].children[1].listeners.get('click')();
  save();
  await flush();
  assert.deepEqual(ui.requests.at(-1).body.policy.local_llm, {});
});


test('the sidebar opens connection details and tracks enrollment, connection, retries and stop', async () => {
  let current = { ...status, enrolled: false, link: { connection: 'unregistered' } };
  const ui = browser(({ path }) => {
    if (path === '/api/session') return [200, { token: 'session-token' }];
    if (path === '/api/components') return [200, emptyComponents];
    if (path === '/api/policy') return [200, policyRead()];
    assert.equal(path, '/api/status');
    return [200, current];
  });
  const get = (id) => ui.elements.get(id);
  await flush();
  assert.equal(get('connection').hidden, false);
  assert.equal(get('connection-enrollment').hidden, false);
  assert.equal(get('enrollment-reason').hidden, true);
  assert.equal(get('enroll-submit').textContent, 'Enroll');
  assert.equal(get('connection-card').hidden, true);
  assert.equal(get('connection-state').textContent, 'Not enrolled');
  assert.equal(get('connection-dot').className, 'dot');
  assert.equal(get('connection-address').hidden, true);
  assert.equal(get('re-enroll').hidden, true);

  current = { ...status, runner_bytes: 5 * 1048576, link: {
    ...status.link, host: 'desktop.example:9443', host_version: 'host-test', runner: true,
  } };
  const poll = () => ui.intervals.values().next().value.callback();
  await poll();
  assert.equal(get('connection').hidden, false);
  assert.equal(get('connection-state').textContent, 'Connected');
  assert.equal(get('connection-dot').className, 'dot positive');
  assert.equal(get('connection-address').textContent, 'desktop.example:9443');
  assert.equal(get('connection-address').hidden, false);
  assert.equal(get('connection-link').attributes.get('aria-current'), 'page');
  assert.equal(get('connection-enrollment').hidden, true);
  assert.equal(get('connection-card').hidden, false);
  assert.equal(get('connection-summary').textContent, 'Connected · desktop.example:9443');
  assert.equal(get('versions').textContent, 'Child test · Host host-test');
  assert.equal(get('memory').textContent, '5.0 MiB');
  assert.equal(get('last-error').hidden, true);
  assert.equal(get('re-enroll').hidden, false);
  assert.equal(get('enroll-form').parentElement, get('settings-enrollment'));

  current = { ...status, link: { connection: 'waiting', error: 'Host is offline.' } };
  await poll();
  assert.equal(get('connection-state').textContent, 'Reconnecting');
  assert.equal(get('connection-dot').className, 'dot warning');
  assert.equal(get('last-error').textContent, 'Host is offline.');

  current = { ...status, link: { connection: 'stopped', error: 'child revoked' } };
  ui.showProviders();
  await poll();
  assert.equal(get('connection').hidden, false);
  assert.equal(get('connection-state').textContent, 'Not connected');
  assert.equal(get('connection-dot').className, 'dot negative');
  assert.equal(get('connection-enrollment').hidden, false);
  assert.equal(get('connection-card').hidden, false);
  assert.equal(get('re-enroll').hidden, true);
  assert.equal(get('enrollment-reason').hidden, false);
  assert.equal(get('enrollment-reason').textContent, 'child revoked');
  assert.equal(get('enroll-submit').textContent, 'Enroll again');
  assert.equal(get('enroll-form').parentElement, get('connection-enrollment'));
  assert.match(html, /data-page="settings"[\s\S]*?<div class="sidebar-foot">\s*<button[^>]*id="connection-link"/);
  assert.doesNotMatch(html, /data-page="home"/);
  assert.match(html, /id="enrollment-reason"[^>]*role="alert"[^>]*>[\s\S]*?<form id="enroll-form"/);
});

test('initial landing uses Providers for enrolled links and enrollment details for stopped links', async () => {
  for (const connection of ['connected', 'trying', 'waiting', 'stopped']) {
    const ui = browser(({ path }) => {
      if (path === '/api/session') return [200, { token: 'session-token' }];
      if (path === '/api/status') return [200, { ...status, link: { ...status.link, connection } }];
      if (path === '/api/policy') return [200, policyRead()];
      if (path === '/api/components') return [200, emptyComponents];
      assert.fail(path);
    });
    await flush();
    const stopped = connection === 'stopped';
    assert.equal(ui.elements.get('providers').hidden, stopped, connection);
    assert.equal(ui.elements.get('connection').hidden, !stopped, connection);
    assert.equal(ui.elements.get('connection-enrollment').hidden, !stopped, connection);
  }
});

test('status recovery keeps the selected page and new enrollment needs open Connection', async () => {
  const stopped = { ...status, link: { connection: 'stopped' } };
  let current = status;
  const ui = browser(({ path }) => {
    if (path === '/api/session') return [200, { token: 'session-token' }];
    if (path === '/api/status') return [200, current];
    if (path === '/api/policy') return [200, policyRead()];
    if (path === '/api/components') return [200, emptyComponents];
    assert.fail(path);
  });
  const poll = async () => {
    await ui.intervals.values().next().value.callback();
    await flush();
  };
  const visible = (page) => !ui.elements.get(page).hidden;

  await flush();
  ui.showWorkspace();
  current = stopped;
  await poll();
  assert.equal(visible('connection'), true);

  current = status;
  await poll();
  assert.equal(visible('connection'), true);
  assert.equal(ui.elements.get('connection-enrollment').hidden, true);

  current = stopped;
  await poll();
  ui.showWorkspace();
  await poll();
  assert.equal(visible('workspace'), true);

  current = status;
  await poll();
  assert.equal(visible('workspace'), true);
});

test('enrollment leaves Connection only after the API succeeds', async () => {
  let enrolled = false;
  const ui = browser(({ path, body }) => {
    if (path === '/api/session') return [200, { token: 'session-token' }];
    if (path === '/api/status') {
      return [200, enrolled ? status : { ...status, enrolled: false, link: { connection: 'unregistered' } }];
    }
    if (path === '/api/policy') return [200, policyRead()];
    if (path === '/api/components') return [200, emptyComponents];
    assert.equal(path, '/api/enroll');
    if (body.code === 'invalid') return [400, { error: 'Invalid code' }];
    assert.equal(body.code, 'valid');
    enrolled = true;
    return [200, { message: 'Enrolled' }];
  });
  const get = (id) => ui.elements.get(id);
  const submit = async (code) => {
    get('enroll-code').value = code;
    get('enroll-form').onsubmit({ preventDefault() {}, submitter: get('enroll-submit') });
    await flush();
  };

  await flush();
  await submit('invalid');
  assert.equal(get('connection').hidden, false);
  assert.equal(get('enroll-message').textContent, 'Invalid code');
  await submit('valid');
  assert.equal(get('connection').hidden, true);
  assert.equal(get('providers').hidden, false);
  assert.equal(get('enroll-code').value, '');
});

test('Settings fetches logs on demand and exposes the Windows repair log folder', async () => {
  const line = '2026-11-23 0:00:00 +00:00:00 connection stopped: child revoked';
  const ui = browser(({ path, method }) => {
    if (path === '/api/session') return [200, { token: 'session-token' }];
    if (path === '/api/status') return [200, status];
    if (path === '/api/components') return [200, emptyComponents];
    if (path === '/api/policy') return [200, policyRead()];
    if (path === '/api/settings') return [200, { revision: 'test', autostart: null, can_open_logs: true }];
    if (path === '/api/open-logs') {
      assert.equal(method, 'POST');
      return [200, {}];
    }
    assert.equal(path, '/api/logs');
    return [200, [line]];
  });
  await flush();
  ui.showSettings();
  await flush();
  assert.equal(ui.requests.some(({ path }) => path === '/api/logs'), false);
  const logPanel = ui.elements.get('log-panel');
  logPanel.open = true;
  logPanel.listeners.get('toggle')({ currentTarget: logPanel });
  await flush();
  assert.equal(ui.elements.get('logs').textContent, line);
  assert.equal(ui.elements.get('repair-log-hint').hidden, false);
  const openFolder = ui.elements.get('open-logs');
  assert.equal(openFolder.hidden, false);
  await openFolder.onclick({ currentTarget: openFolder });
  assert.equal(ui.requests.at(-1).path, '/api/open-logs');
});

test('Workspace approves and revokes the reviewed host MCP version', async () => {
  const fingerprint = 'a'.repeat(64);
  const version = 'b'.repeat(64);
  let approved = {};
  const ui = browser(({ path, method, body }) => {
    if (path === '/api/session') return [200, { token: 'session-token' }];
    if (path === '/api/status') return [200, status];
    if (path === '/api/components') return [200, emptyComponents];
    if (path === '/api/policy') return [200, policyRead()];
    assert.equal(path, '/api/host-mcp');
    if (method === 'POST') {
      assert.deepEqual(body, { fingerprint, name: 'search', version, approve: !approved.search });
      approved = body.approve ? { search: version } : {};
      return [200, { saved: true }];
    }
    return [200, { enabled: true, approved, catalog: {
      fingerprint, offers: [{ name: 'search', description: 'Search documents', tools: ['lookup'], version }],
    } }];
  });
  await flush();
  ui.showWorkspace();
  await flush();
  assert.equal(ui.elements.get('host-mcp-card').hidden, false);
  const button = () => ui.elements.get('host-mcp-offers').querySelectorAll('button')[0];
  assert.equal(button().textContent, 'Approve this version');
  button().listeners.get('click')();
  await flush();
  assert.equal(button().textContent, 'Revoke approval');
  button().listeners.get('click')();
  await flush();
  assert.equal(button().textContent, 'Approve this version');
});

test('Workspace shows a published stdio command as information only', async () => {
  const fingerprint = 'a'.repeat(64);
  const version = 'b'.repeat(64);
  const ui = browser(({ path, method }) => {
    if (path === '/api/session') return [200, { token: 'session-token' }];
    if (path === '/api/status') return [200, status];
    if (path === '/api/components') return [200, emptyComponents];
    if (path === '/api/policy') return [200, policyRead()];
    assert.equal(path, '/api/host-mcp');
    assert.equal(method, 'GET');
    return [200, {
      enabled: true, approved: {}, catalog: {
        fingerprint, offers: [{
          name: 'files', description: 'Host files', tools: ['write'], version,
          command: '/opt/kotoyose/host-only-stdio',
        }],
      },
    }];
  });
  await flush();
  ui.showWorkspace();
  await flush();
  const details = ui.elements.get('host-mcp-offers').querySelectorAll('p')
    .find((node) => node.className === 'hint host-mcp-details').textContent;
  assert.match(details, /\/opt\/kotoyose\/host-only-stdio/);
  assert.match(details, /does not run it/);
});

test('Workspace shows stdio args, cwd, and env names without secret values', async () => {
  const fingerprint = 'a'.repeat(64);
  const version = 'b'.repeat(64);
  const ui = browser(({ path, method }) => {
    if (path === '/api/session') return [200, { token: 'session-token' }];
    if (path === '/api/status') return [200, status];
    if (path === '/api/components') return [200, emptyComponents];
    if (path === '/api/policy') return [200, policyRead()];
    assert.equal(path, '/api/host-mcp');
    assert.equal(method, 'GET');
    return [200, {
      enabled: true, approved: {}, catalog: {
        fingerprint, offers: [{
          name: 'files', description: 'Host files', tools: ['write'], version,
          command: '/opt/kotoyose/host-only-stdio',
          args: ['--serve', '--root', '/var/lib/kotoyose'],
          cwd: '/srv/host-files',
          env_names: ['TOKEN', 'VISIBLE'],
          env: { TOKEN: 'secret-value-must-not-appear' },
        }],
      },
    }];
  });
  await flush();
  ui.showWorkspace();
  await flush();
  const details = ui.elements.get('host-mcp-offers').querySelectorAll('p')
    .find((node) => node.className === 'hint host-mcp-details').textContent;
  assert.match(details, /--serve/);
  assert.match(details, /\/srv\/host-files/);
  assert.match(details, /TOKEN/);
  assert.match(details, /VISIBLE/);
  assert.doesNotMatch(details, /secret-value-must-not-appear/);
});

test('Workspace shows all tools and a version-changed marker', async () => {
  const fingerprint = 'a'.repeat(64);
  const version = 'b'.repeat(64);
  const previous = 'c'.repeat(64);
  const ui = browser(({ path, method }) => {
    if (path === '/api/session') return [200, { token: 'session-token' }];
    if (path === '/api/status') return [200, status];
    if (path === '/api/components') return [200, emptyComponents];
    if (path === '/api/policy') return [200, policyRead()];
    assert.equal(path, '/api/host-mcp');
    assert.equal(method, 'GET');
    return [200, {
      enabled: true, approved: { search: previous }, catalog: {
        fingerprint, offers: [{ name: 'search', description: 'Search documents', tools: [], all_tools: true, version }],
      },
    }];
  });
  await flush();
  ui.showWorkspace();
  await flush();
  const details = ui.elements.get('host-mcp-offers').querySelectorAll('p')
    .find((node) => node.className === 'hint host-mcp-details').textContent;
  assert.match(details, /All tools/);
  assert.match(details, /Version changed/);
});

test('Workspace shows Kotoyose delegation and the delegate names', async () => {
  const fingerprint = 'a'.repeat(64);
  const version = 'b'.repeat(64);
  const delegates = [...Array.from({ length: 65 }, (_, index) => `reader-${index}`), 'long-delegate-'.repeat(50)];
  const ui = browser(({ path, method }) => {
    if (path === '/api/session') return [200, { token: 'session-token' }];
    if (path === '/api/status') return [200, status];
    if (path === '/api/components') return [200, emptyComponents];
    if (path === '/api/policy') return [200, policyRead()];
    assert.equal(path, '/api/host-mcp');
    assert.equal(method, 'GET');
    return [200, {
      enabled: true, approved: {}, catalog: {
        fingerprint, offers: [{
          name: 'kotoyose', description: 'Kotoyose delegation', tools: delegates, all_tools: false, version,
        }],
      },
    }];
  });
  await flush();
  ui.showWorkspace();
  await flush();
  const details = ui.elements.get('host-mcp-offers').querySelectorAll('p')
    .find((node) => node.className === 'hint host-mcp-details').textContent;
  assert.match(details, /Kotoyose delegation/);
  assert.ok(details.includes(`Delegates: ${delegates.join(', ')}`));
});

test('all settings copy resolves from the English string table', () => {
  const html = asset('index.html');
  const withoutMarkup = html.replace(/<!--[^]*?-->/g, '').replace(/<[^>]+>/g, '');
  assert.equal(withoutMarkup.trim(), '');
  const strings = runInNewContext(script.slice(0, script.indexOf('for (const element')) + '\ntext;');
  const keys = [
    ...Array.from(html.matchAll(/data-(?:text|aria)="([^"]+)"/g), ([, key]) => key),
    ...Array.from((script + providersScript).matchAll(/\btext\.([a-zA-Z]+)\b/g), ([, key]) => key),
  ];
  for (const key of new Set(keys)) {
    assert.ok((typeof strings[key] === 'string' && strings[key]) || typeof strings[key] === 'function', key);
  }
  const japanese = /[\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Han}]/u;
  const installer = readFileSync(new URL('../child-app/installer/setup.nsi', import.meta.url), 'utf8');
  const rust = ['messages', 'web', 'components', 'tray', 'autostart', 'parts', 'policy', 'state'].map((name) =>
    readFileSync(new URL(`../child-app/src/${name}.rs`, import.meta.url), 'utf8').split('#[cfg(test)]')[0]);
  for (const source of [html, script, providersScript, asset('style.css'), installer, ...rust]) {
    assert.doesNotMatch(source.replace(/^\s*(?:;|\/\/).*$/gm, '').replace(/\/\*[^]*?\*\//g, ''), japanese);
  }
});

const component = {
    providers: [{
      id: 'claude', name: 'Claude', initial: 'A', tone: 'claude', driver: 'claude.agent-sdk',
      packages: ['claude-agent-sdk'], aliases: ['anthropic'], quota: true, local_login: true,
      login: { cli_name: 'Claude Code', command: ['claude', 'auth', 'login', '--claudeai'], status: ['auth', 'status', '--json'] },
    }],
    installed: [{
      id: 'claude', packages: { 'claude-agent-sdk': '1' }, system_cli: true, system_version: null,
      bundled_cli: true, choice: 'bundled', turns_supported: true, sdk_importable: true,
    }],
    can_manage_packages: true,
    needs_reinstall: ['claude'],
    error: null,
};

const policyRead = (policy = emptyPolicy, revision = 'original') =>
  ({ revision, policy, available_adapters: ['claude.agent-sdk'], warnings: [] });

function insideDetails(node) {
  for (let ancestor = node.parentElement; ancestor; ancestor = ancestor.parentElement) {
    if (ancestor.tagName === 'details') return true;
  }
  return false;
}

const findButton = (card, label) => card.querySelectorAll('button').find((item) => item.textContent === label);
const cardResult = (card) => card.querySelectorAll('p').find((item) => item.className === 'component-result');

test('Providers collapses package details and runs CLI version checks only on Check', async () => {
  const ui = browser(({ path }) => {
    if (path === '/api/session') return [200, { token: 'session-token' }];
    if (path === '/api/status') return [200, status];
    if (path === '/api/components' || path === '/api/components/check') return [200, component];
    if (path === '/api/policy') return [200, policyRead()];
    if (path === '/api/login/status/claude') return [200, { state: 'logged_out' }];
    assert.fail(path);
  });
  await flush();
  ui.showProviders();
  await flush();
  assert.equal(ui.requests.filter(({ path }) => path === '/api/components').length, 1);
  assert.equal(ui.requests.some(({ path }) => path === '/api/components/check'), false);
  const [card] = ui.elements.get('component-rows').querySelectorAll('article');
  const repair = findButton(card, 'Repair');
  assert.match(repair.className, /\bbtn-primary\b/);
  assert.equal(insideDetails(repair), false);
  assert.ok(repair.parentElement.children.some((element) => element.textContent === 'Needs reinstall'));
  assert.equal(card.querySelectorAll('details')[0].open, false);
  ui.elements.get('refresh-components').onclick();
  await flush();
  assert.equal(ui.requests.filter(({ path }) => path === '/api/components/check').length, 1);
});

test('Providers polls automatic reinstall and offers Repair after a failure', async () => {
  for (const outcome of ['done', 'failed']) {
    let phase = 'pending';
    const ui = browser(({ path, method, body }) => {
      if (path === '/api/session') return [200, { token: 'session-token' }];
      if (path === '/api/status') return [200, status];
      if (path === '/api/policy') return [200, policyRead()];
      if (path === '/api/login/status/claude') return [200, { state: 'logged_out' }];
      if (path === '/api/components' && method === 'POST') {
        assert.equal(body.provider, 'claude');
        phase = undefined;
        return [200, new ReadableStream({ start(controller) {
          const result = { event: 'result', ok: true, message: 'Reinstalled manually.' };
          controller.enqueue(new TextEncoder().encode(JSON.stringify(result) + '\n'));
          controller.close();
        } })];
      }
      if (path === '/api/components') return [200, {
        ...component,
        installed: phase === 'installing' ? [] : component.installed,
        can_manage_packages: phase !== 'installing',
        needs_reinstall: phase && phase !== 'done' ? ['claude'] : [],
        auto_repair: phase ? { claude: phase } : {},
      }];
      assert.fail(path);
    });
    await flush();
    ui.showProviders();
    await flush();
    const card = () => ui.elements.get('component-rows').querySelectorAll('article')[0];
    assert.match(cardResult(card()).textContent, /Waiting to reinstall/);
    phase = 'installing';
    await ui.timeouts.shift()();
    assert.match(cardResult(card()).textContent, /Reinstalling automatically/);
    assert.equal(ui.elements.get('refresh-components').disabled, true);
    phase = outcome;
    await ui.timeouts.shift()();
    assert.equal(ui.timeouts.length, 0);
    assert.equal(ui.elements.get('refresh-components').disabled, false);
    if (outcome === 'done') {
      assert.match(cardResult(card()).textContent, /Reinstalled automatically/);
      assert.equal(findButton(card(), 'Repair'), undefined);
    } else {
      assert.match(cardResult(card()).textContent, /Automatic reinstall failed/);
      await findButton(card(), 'Repair').listeners.get('click')();
      assert.match(cardResult(card()).textContent, /Reinstalled manually/);
    }
  }
});

test('SDK install is visible when missing and installed packages put Update and Remove in details', async () => {
  let installed = false;
  const ui = browser(({ path }) => {
    if (path === '/api/session') return [200, { token: 'session-token' }];
    if (path === '/api/status') return [200, status];
    if (path === '/api/policy') return [200, policyRead()];
    if (path === '/api/login/status/claude') return [200, { state: 'logged_out' }];
    if (path === '/api/components' || path === '/api/components/check') return [200, {
      ...component, needs_reinstall: [],
      installed: installed ? component.installed : [{
        ...component.installed[0], packages: { 'claude-agent-sdk': null }, sdk_importable: false,
      }],
    }];
    assert.fail(path);
  });
  await flush();
  const card = () => ui.elements.get('component-rows').querySelectorAll('article')[0];
  const install = findButton(card(), 'Install');
  assert.match(install.className, /\bbtn-primary\b/);
  assert.equal(insideDetails(install), false);
  assert.ok(install.parentElement.children.some((item) => item.textContent === 'Not installed'));
  assert.ok(card().querySelectorAll('p').some((item) => item.textContent.includes('Installs claude-agent-sdk')));
  const login = findButton(card(), 'Log in');
  assert.match(login.className, /\bbtn-primary\b/);
  assert.equal(insideDetails(login), false);

  installed = true;
  await ui.elements.get('refresh-components').onclick();
  assert.equal(findButton(card(), 'Install'), undefined);
  assert.equal(findButton(card(), 'Repair'), undefined);
  assert.equal(insideDetails(findButton(card(), 'Update')), true);
  assert.equal(insideDetails(findButton(card(), 'Remove')), true);
  assert.equal(card().querySelectorAll('details')[0].open, false);
});

test('a CLI-only catalog row shows its pin and manual login while child turns are unavailable', async () => {
  let installed = false;
  const provider = {
    id: 'example', name: 'Example', initial: 'E', tone: 'cli', driver: 'cli.acp',
    packages: [], aliases: [], local_login: false, quota: false,
    managed: { version: '1.2.3' }, login: { cli_name: 'Example', command: ['example', 'sign-in'] },
  };
  const ui = browser(({ path }) => {
    if (path === '/api/session') return [200, { token: 'session-token' }];
    if (path === '/api/status') return [200, status];
    if (path === '/api/components' || path === '/api/components/check') return [200, {
      ...component, providers: [provider],
      installed: [{ id: provider.id, choice: 'bundled', managed_available: true, bundled_cli: installed }],
    }];
    if (path === '/api/policy') return [200, policyRead()];
    assert.fail(path);
  });
  await flush();
  ui.showProviders();
  await flush();
  const rows = ui.elements.get('component-rows');
  assert.ok(rows.querySelectorAll('p').some((p) => p.textContent === 'Child turns are not enabled for this provider.'));
  assert.ok(rows.querySelectorAll('p').some((p) => p.textContent.includes('example sign-in')));
  assert.ok(rows.querySelectorAll('p').some((p) => p.textContent === '1.2.3'));
  const install = findButton(rows, 'Install');
  assert.match(install.className, /\bbtn-primary\b/);
  assert.equal(insideDetails(install), false);
  assert.ok(install.parentElement.children.some((item) => item.textContent === 'Managed CLI not installed'));
  assert.equal(rows.querySelectorAll('input').length, 1);

  installed = true;
  await ui.elements.get('refresh-components').onclick();
  assert.equal(findButton(rows, 'Install'), undefined);
  assert.equal(insideDetails(findButton(rows, 'Remove')), true);
});

test('package progress and its result stay in the changed card and reset on its next action', async () => {
  const second = {
    ...component.providers[0], id: 'other', name: 'Other', driver: 'other.sdk', local_login: false,
  };
  const requests = [];
  let output;
  const ui = browser(({ path, method, body }) => {
    if (path === '/api/session') return [200, { token: 'session-token' }];
    if (path === '/api/status') return [200, status];
    if (path === '/api/policy') return [200, policyRead()];
    if (path === '/api/login/status/claude') return [200, { state: 'logged_out' }];
    assert.equal(path, '/api/components');
    if (method === 'POST') {
      requests.push(body.provider);
      return [200, new ReadableStream({ start(controller) { output = controller; } })];
    }
    return [200, {
      ...component, providers: [component.providers[0], second],
      installed: [...component.installed, { ...component.installed[0], id: second.id }],
    }];
  });
  await flush();
  const cards = () => ui.elements.get('component-rows').querySelectorAll('article');
  const click = (index, label) => findButton(cards()[index], label).listeners.get('click')();
  const progress = (index) => cards()[index].querySelectorAll('details').find((node) => node.className === 'component-output');
  const emit = (...events) => output.enqueue(new TextEncoder().encode(events.map((event) => JSON.stringify(event) + '\n').join('')));
  const finish = (ok, message) => {
    emit({ event: 'result', ok, message });
    output.close();
  };

  const install = click(0, 'Repair');
  await flush();
  assert.equal(cardResult(cards()[0]).textContent, 'Applying change…');
  assert.equal(cardResult(cards()[1]).textContent, '');
  emit(...Array.from({ length: 106 }, (_, i) => ({ event: 'progress', message: `Line ${i + 1}` })));
  await flush();
  assert.equal(progress(0).hidden, false);
  assert.equal(progress(0).open, false);
  assert.equal(progress(0).querySelectorAll('span')[0].textContent, 'Line 102\nLine 103\nLine 104\nLine 105\nLine 106');
  const lines = progress(0).querySelectorAll('pre')[0].textContent.split('\n');
  assert.equal(lines.length, 100);
  assert.equal(lines[0], 'Line 7');
  assert.equal(progress(1).hidden, true);
  progress(0).open = true;
  finish(false, 'pip failed.');
  await install;
  assert.equal(progress(0).open, true);
  assert.equal(cardResult(cards()[0]).textContent, 'Failed. pip failed.');
  assert.ok(cardResult(cards()[0]).attributes.has('data-error'));

  const update = click(1, 'Update');
  await flush();
  finish(true, 'Packages updated.');
  await update;
  assert.equal(cardResult(cards()[0]).textContent, 'Failed. pip failed.');
  assert.equal(cardResult(cards()[1]).textContent, 'Succeeded. Packages updated.');
  assert.equal(cardResult(cards()[1]).attributes.has('data-error'), false);

  const retry = click(0, 'Repair');
  await flush();
  assert.equal(progress(0).hidden, true);
  assert.equal(progress(0).open, false);
  assert.equal(progress(0).querySelectorAll('pre')[0].textContent, '');
  finish(true, 'Packages repaired.');
  await retry;
  assert.equal(cardResult(cards()[0]).textContent, 'Succeeded. Packages repaired.');
  assert.deepEqual(requests, ['claude', 'other', 'claude']);
  assert.equal(ui.elements.get('components-message').textContent, '');
});

test('provider toggles save driver and quota names independently of a Workspace draft', async () => {
  let policy = { ...emptyPolicy, adapters: ['custom.driver'], logins: ['Anthropic', 'grok'] };
  let revision = 'original';
  let conflict = false;
  const ui = browser(({ path, method, body }) => {
    if (path === '/api/session') return [200, { token: 'session-token' }];
    if (path === '/api/status') return [200, status];
    if (path === '/api/components') return [200, component];
    if (path === '/api/login/status/claude') return [200, { state: 'logged_out' }];
    assert.equal(path, '/api/policy');
    if (method === 'GET') return [200, policyRead(policy, revision)];
    assert.equal(body.revision, revision);
    if (conflict) return [409, { error: 'The file changed.' }];
    policy = body.policy;
    revision += '-saved';
    return [200, { revision, warnings: [] }];
  });
  await flush();
  ui.showWorkspace();
  await flush();
  ui.elements.get('codex-sandbox').value = 'read-only';
  ui.elements.get('policy-form').oninput();
  ui.showProviders();
  await flush();
  const [adapter, quota] = ui.elements.get('component-rows').querySelectorAll('input');
  assert.equal(adapter.checked, false);
  assert.equal(quota.checked, true);
  adapter.checked = true;
  adapter.onchange();
  await flush();
  assert.deepEqual(policy.adapters, ['custom.driver', 'claude.agent-sdk']);
  assert.equal(policy.codex_sandbox_max, 'workspace-write');
  quota.checked = false;
  quota.onchange();
  await flush();
  assert.deepEqual(policy.logins, ['grok']);
  quota.checked = true;
  quota.onchange();
  await flush();
  assert.deepEqual(policy.logins, ['grok', 'claude']);
  conflict = true;
  adapter.checked = false;
  adapter.onchange();
  await flush();
  assert.equal(adapter.checked, true);
  assert.equal(ui.elements.get('provider-policy-message').textContent, 'The file changed.');

  conflict = false;
  ui.showWorkspace();
  assert.equal(ui.elements.get('codex-sandbox').value, 'read-only');
  ui.elements.get('policy-form').onsubmit({ preventDefault() {} });
  await flush();
  assert.equal(policy.codex_sandbox_max, 'read-only');
  assert.deepEqual(policy.adapters, ['custom.driver', 'claude.agent-sdk']);
  ui.showConnection();
  ui.showWorkspace();
  await flush();
  assert.equal(ui.elements.get('codex-sandbox').value, 'read-only');
});

test('login checks once on entering Providers and polls only the active login', async () => {
  let progresses = 0;
  const ui = browser(({ path, method, body }) => {
    if (path === '/api/session') return [200, { token: 'session-token' }];
    if (path === '/api/status') return [200, status];
    if (path === '/api/policy') return [200, policyRead()];
    if (path === '/api/components') return [200, component];
    if (path === '/api/login/status/claude') {
      assert.equal(method, 'POST');
      return [200, { state: 'logged_out' }];
    }
    if (path === '/api/login/claude' && method === 'POST') {
      assert.deepEqual(body, {});
      return [200, { state: 'running' }];
    }
    assert.equal(path, '/api/login/claude');
    return [200, ++progresses === 1
      ? { state: 'running', url: 'https://claude.ai/authorize', needs_code: true }
      : { state: 'logged_in' }];
  });
  await flush();
  ui.showProviders();
  await flush();
  const checkCount = () => ui.requests.filter(({ path }) => path === '/api/login/status/claude').length;
  assert.equal(checkCount(), 1);
  await ui.intervals.values().next().value.callback();
  assert.equal(checkCount(), 1);
  const cards = ui.elements.get('component-rows');
  cards.querySelectorAll('button').find((button) => button.textContent === 'Log in').listeners.get('click')();
  await flush();
  ui.timeouts.shift()();
  await flush();
  assert.equal(cards.querySelectorAll('a')[0].href, 'https://claude.ai/authorize');
  assert.equal(cards.querySelectorAll('form').length, 1);
  ui.timeouts.shift()();
  await flush();
  assert.equal(cards.querySelectorAll('form').length, 0);
  assert.ok(cards.querySelectorAll('span').some((span) => span.textContent === 'Logged in'));
  assert.equal(ui.timeouts.length, 0);
  assert.equal(checkCount(), 1);
  ui.showConnection();
  ui.showProviders();
  await flush();
  assert.equal(checkCount(), 2);
});

test('authorization code can be retried and login can be cancelled', async () => {
  const prompt = { state: 'running', url: 'https://claude.ai/authorize', needs_code: true };
  let view = prompt;
  const ui = browser(({ path, method, body, authorization }) => {
    if (path === '/api/session') return [200, { token: 'session-token' }];
    if (path === '/api/status') return [200, status];
    if (path === '/api/policy') return [200, policyRead()];
    if (path === '/api/components') return [200, component];
    if (path === '/api/login/status/claude') return [200, { state: 'logged_out' }];
    if (path === '/api/login/claude/code') {
      assert.equal(body.code, 'wrong-code');
      view = { ...prompt, needs_code: false };
      return [200, null];
    }
    assert.equal(path, '/api/login/claude');
    if (method === 'POST') return [200, { state: 'running' }];
    if (method === 'DELETE') {
      assert.equal(authorization, 'Bearer session-token');
      assert.equal(body, undefined);
      return [200, { state: 'cancelled' }];
    }
    return [200, view];
  });
  await flush();
  ui.showProviders();
  await flush();
  const cards = ui.elements.get('component-rows');
  const click = (text) => cards.querySelectorAll('button')
    .find((button) => button.textContent === text).listeners.get('click')();
  const poll = async () => {
    ui.timeouts.shift()();
    await flush();
  };
  click('Log in');
  await flush();
  assert.ok(cards.querySelectorAll('button').some((button) => button.textContent === 'Cancel'));
  await poll();
  const [form] = cards.querySelectorAll('form');
  const [input] = form.querySelectorAll('input');
  input.value = ' wrong-code ';
  form.onsubmit({ preventDefault() {} });
  await flush();
  assert.equal(input.value, '');
  await poll();
  assert.equal(cards.querySelectorAll('form').length, 0);
  view = prompt;
  await poll();
  assert.equal(cards.querySelectorAll('form').length, 1);
  click('Cancel');
  await flush();
  assert.ok(cards.querySelectorAll('span').some((span) => span.textContent === 'Login cancelled'));
  assert.equal(cards.querySelectorAll('form').length, 0);
  await poll();
  assert.equal(ui.timeouts.length, 0);
});

test('the installer defaults to the base runtime and sign-in, with an optional finish-page launch', () => {
  const installer = readFileSync(new URL('../child-app/installer/setup.nsi', import.meta.url), 'utf8');
  assert.match(installer, /^!insertmacro MUI_LANGUAGE "English"$/m);
  assert.match(installer, /^InstallDir "\$\{PRODUCT_ROOT\}"$/m);
  assert.match(installer, /^!define PRODUCT_ROOT "\$LOCALAPPDATA\\Programs\\\$\{PRODUCT\}"$/m);
  assert.match(installer, /^RequestExecutionLevel user$/m);
  assert.match(installer, /^!insertmacro MUI_PAGE_DIRECTORY$/m);
  assert.match(installer, /^Section "Core runtime" SEC_BASE\s+SectionIn RO\s+SectionEnd$/m);
  assert.match(installer, /^Section "Start at sign-in" SEC_AUTOSTART\s+SectionEnd$/m);
  assert.deepEqual(Array.from(installer.matchAll(/^Section "([^"]+)" SEC_/gm), ([, name]) => name),
    ['Core runtime', 'Start at sign-in']);
  assert.doesNotMatch(installer, /NOCLAUDE|UnselectSection|MUI_FINISHPAGE_RUN_NOTCHECKED/);
  assert.match(installer, /^!define MUI_FINISHPAGE_RUN_TEXT "Launch now"$/m);
  assert.match(installer, /Setup cannot install in the drive root/);
  assert.match(installer, /exceed Windows path limits/);
  assert.match(installer, /No uninstalls it, then installs in the folder you chose/);
  assert.match(installer, /kotoyose-child-app\.exe/);
  assert.match(installer, /kotoyose-child-background\.exe/);
});

test('a stored catalog error is shown and a matching declaration replaces its built-in card', async () => {
  const ui = browser(({ path }) => {
    if (path === '/api/session') return [200, { token: 'session-token' }];
    if (path === '/api/status') return [200, status];
    if (path === '/api/policy') return [200, policyRead()];
    if (path === '/api/components') return [200, {
      providers: [
        component.providers[0],
        { id: 'cursor', name: 'Cursor', initial: 'C', tone: 'cli', driver: 'cli.print', packages: [], aliases: [], quota: false },
      ],
      installed: [
        component.installed[0],
        { id: 'cursor', packages: {}, system_cli: false, bundled_cli: false, choice: 'bundled', turns_supported: false, sdk_importable: false, managed_available: false },
      ],
      can_manage_packages: true,
      needs_reinstall: [],
    }];
    if (path === '/api/provider-declarations') {
      return [200, {
        error: 'provider storage ACL grants access outside the current user',
        host_fingerprint: 'ab'.repeat(32),
        offers: [{ id: 'claude', profile_id: 'claude', capability: 'read', sha256: 'cd'.repeat(32), display_name: 'Claude', status: 'pending' }],
      }];
    }
    assert.fail(path);
  });
  await flush();
  ui.showProviders();
  await flush();
  const rows = ui.elements.get('component-rows');
  const cards = rows.querySelectorAll('article');
  assert.equal(cards.length, 1);
  assert.equal(cards[0].querySelectorAll('h2')[0].textContent, 'Claude');
  assert.equal(insideDetails(findButton(cards[0], 'Review approval')), false);
  assert.equal(findButton(cards[0], 'Repair'), undefined);
  const unavailable = rows.querySelectorAll('details').find((node) => node.className === 'unavailable-providers');
  assert.equal(unavailable.open, false);
  assert.match(unavailable.querySelectorAll('summary')[0].textContent, /Not available on this child/);
  assert.match(unavailable.querySelectorAll('li')[0].textContent, /Cursor/);
  const notice = ui.elements.get('host-declarations');
  assert.equal(notice.querySelectorAll('p')[0].textContent, 'provider storage ACL grants access outside the current user');
  assert.equal(notice.querySelectorAll('p')[0].attributes.get('data-error'), '');
  assert.equal(notice.querySelectorAll('h2').length, 0);
});

for (const displayName of ['Grok Build', '  gRoK bUiLd  ']) {
  test(`a host offer named "${displayName}" hides only the matching unavailable built-in`, async () => {
    const providers = [
      { id: 'grok', name: 'Grok Build' },
      { id: 'antigravity', name: 'Antigravity' },
      { id: 'cursor', name: 'Cursor' },
    ].map((provider) => ({
      ...provider, initial: 'G', tone: 'cli', driver: 'cli.acp',
      packages: [], aliases: [], quota: false,
    }));
    const ui = browser(({ path }) => {
      if (path === '/api/session') return [200, { token: 'session-token' }];
      if (path === '/api/status') return [200, status];
      if (path === '/api/policy') return [200, policyRead()];
      if (path === '/api/components') return [200, {
        providers,
        installed: providers.map(({ id }) => ({
          id, packages: {}, system_cli: false, bundled_cli: false,
          choice: 'bundled', turns_supported: false, sdk_importable: false,
          managed_available: false,
        })),
        can_manage_packages: true, needs_reinstall: [],
      }];
      if (path === '/api/provider-declarations') return [200, {
        offers: [{
          id: 'grok-build', display_name: displayName, capability: 'read',
          sha256: 'cd'.repeat(32), status: 'pending',
        }],
      }];
      assert.fail(path);
    });
    await flush();
    ui.showProviders();
    await flush();
    const rows = ui.elements.get('component-rows');
    assert.deepEqual(rows.querySelectorAll('article').map((card) =>
      card.querySelectorAll('h2')[0].textContent), [displayName]);
    const unavailable = rows.querySelectorAll('details')
      .find((node) => node.className === 'unavailable-providers');
    assert.match(unavailable.querySelectorAll('summary')[0].textContent, /Not available on this child/);
    const names = unavailable.querySelectorAll('li').map((node) => node.textContent);
    assert.equal(names.some((name) => /Grok Build/i.test(name)), false);
    assert.ok(names.some((name) => /Antigravity/.test(name)));
    assert.ok(names.some((name) => /Cursor/.test(name)));
  });
}

test('host declarations render only the validated preview for this machine', async () => {
  const poisoned = JSON.stringify({
    identity: { display_name: 'Spoofed', publisher: 'Other OS' },
    platforms: {
      'windows-x64': {
        install: { binary: 'spoof.exe', urls: ['https://evil.example/spoof.exe'], sha256: 'ff'.repeat(32) },
      },
    },
    operations: { login: { argv: ['--evil'] }, status: { argv: ['--evil'] } },
    acp: { argv: ['--not-the-preview'] },
  });
  const ui = browser(({ path }) => {
    if (path === '/api/session') return [200, { token: 'session-token' }];
    if (path === '/api/status') return [200, status];
    if (path === '/api/policy') return [200, policyRead()];
    if (path === '/api/components') return [200, emptyComponents];
    if (path === '/api/provider-declarations') {
      return [200, {
        host_fingerprint: 'ab'.repeat(32),
        offers: [{
          id: 'fake-cli',
          capability: 'read',
          sha256: 'cd'.repeat(32),
          status: 'pending',
          document: poisoned,
          display_name: 'Fake <img src=x>',
          publisher: 'Example & Co',
          platform: 'linux-x64',
          binary: 'fake-cli',
          version: '1.2.3',
          artifacts: [{
            platform: 'linux-x64', url: 'https://example.com/fake-cli', sha256: `sha256:${'aa'.repeat(32)}`,
          }],
          commands: [
            { operation: 'version', argv: ['fake-cli', '--version'] },
            { operation: 'login', argv: ['fake-cli', 'login'] },
            { operation: 'acp', argv: ['fake-cli', 'acp', '--model', '{model}'] },
          ],
          network: { download_hosts: ['example.com'], login_hosts: [], runtime_hosts: ['api.example.com'] },
          output_guard_env: ['FAKE_GUARD'],
          rights: 'The approved CLI runs with your user rights. Network and file access are not sandboxed on this child.',
        }],
      }];
    }
    assert.fail(path);
  });
  for (let attempt = 0; attempt < 6; attempt += 1) await flush();
  const texts = [];
  const walk = (node) => {
    if (!node || typeof node !== 'object') return;
    if (typeof node.textContent === 'string' && node.textContent) texts.push(node.textContent);
    for (const child of node.children || []) walk(child);
  };
  walk(ui.elements.get('component-rows'));
  const shown = texts.join('\n');
  assert.match(shown, /Fake <img src=x>/);
  assert.match(shown, /Example & Co/);
  assert.match(shown, new RegExp('ab'.repeat(32)));
  assert.match(shown, /https:\/\/example.com\/fake-cli/);
  assert.match(shown, /fake-cli --version/);
  assert.match(shown, /fake-cli login/);
  assert.match(shown, /fake-cli acp --model \{model\}/);
  assert.match(shown, /1\.2\.3/);
  assert.match(shown, /FAKE_GUARD/);
  assert.match(shown, /api\.example\.com/);
  assert.match(shown, /not sandboxed/);
  assert.doesNotMatch(shown, /spoof\.exe/);
  assert.doesNotMatch(shown, /--evil/);
  assert.doesNotMatch(shown, /--not-the-preview/);
  assert.equal(providersScript.includes('innerHTML'), false);
  assert.equal(providersScript.includes('offer.document'), false);
  assert.equal(providersScript.includes('declarationCommands'), false);
  const renderer = providersScript.slice(
    providersScript.indexOf('function renderDeclarationCard'),
    providersScript.indexOf('async function loadHostDeclarations'),
  );
  assert.equal(renderer.includes('JSON.parse'), false);
});

test('an approved declaration can retry installation and open its device login', async () => {
  const sha256 = 'ab'.repeat(32);
  let approved = false;
  let installed = false;
  let installAttempts = 0;
  const ui = browser(({ path, method, body }) => {
    if (path === '/api/session') return [200, { token: 'session-token' }];
    if (path === '/api/status') return [200, status];
    if (path === '/api/components') return [200, emptyComponents];
    if (path === '/api/policy') {
      return [200, { ...policyRead(), available_adapters: installed ? ['grok-build'] : [] }];
    }
    if (path === '/api/provider-declarations') {
      return [200, { offers: [{
        id: 'grok-build', capability: 'read', sha256, display_name: 'Grok Build',
        status: approved ? 'approved' : 'pending', can_install: approved, installed, local_login: true,
      }, {
        id: 'other-build', capability: 'read', sha256: 'cd'.repeat(32), display_name: 'Other Build',
      }] }];
    }
    if (path === '/api/provider-declarations/grok-build') {
      assert.equal(method, 'POST');
      assert.deepEqual(body, { sha256, capability: 'read' });
      approved = true;
      return [200, {}];
    }
    if (path === '/api/provider-declarations/grok-build/install') {
      assert.equal(method, 'POST');
      assert.deepEqual(body, { sha256 });
      if (++installAttempts === 1) return [422, { error: 'Artifact download failed (DNS lookup failed)' }];
      if (installAttempts === 2) return [422, { error: 'artifact SHA-256 mismatch' }];
      installed = true;
      return [200, { installed }];
    }
    if (path === '/api/login/status/grok-build') return [200, { state: 'logged_out' }];
    if (path === '/api/login/grok-build' && method === 'POST') return [200, { state: 'running' }];
    assert.equal(path, '/api/login/grok-build');
    return [200, { state: 'running', url: 'https://auth.x.ai/device', device_code: 'ABCD-1234' }];
  });
  await flush();
  const cards = () => ui.elements.get('component-rows').querySelectorAll('article');
  const card = () => cards()[0];
  const click = (label) => findButton(card(), label).listeners.get('click')();
  assert.equal(findButton(card(), 'Install'), undefined);
  assert.equal(insideDetails(findButton(card(), 'Review approval')), false);
  await click('Review approval');
  assert.equal(cardResult(card()).textContent, 'Succeeded. The host declaration was approved.');
  assert.equal(findButton(card(), 'Log in'), undefined);
  await click('Install');
  assert.match(cardResult(card()).textContent, /^Failed\..*DNS lookup failed/);
  await click('Install');
  assert.match(cardResult(card()).textContent, /^Failed\..*SHA-256 mismatch/);
  await click('Install');
  await flush();
  assert.equal(cardResult(card()).textContent, 'Succeeded. The managed CLI was installed.');
  assert.equal(cardResult(cards()[1]).textContent, '');
  assert.equal(ui.elements.get('components-message').textContent, '');
  assert.equal(findButton(card(), 'Install'), undefined);
  assert.equal(card().querySelectorAll('input')[0].disabled, false);
  click('Log in');
  await flush();
  ui.timeouts.shift()();
  await flush();
  assert.equal(card().querySelectorAll('a')[0].href, 'https://auth.x.ai/device');
  assert.ok(card().querySelectorAll('p').some((item) => item.textContent.includes('ABCD-1234')));
  assert.equal(card().querySelectorAll('form').length, 0);
});

for (const maximum of ['write', 'mcp']) test(`a ${maximum} declaration offers explicit capability upgrades`, async () => {
  const sha256 = 'ab'.repeat(32);
  let approvedCapability = null;
  const approvals = [];
  const ui = browser(({ path, method, body }) => {
    if (path === '/api/session') return [200, { token: 'session-token' }];
    if (path === '/api/status') return [200, status];
    if (path === '/api/components') return [200, emptyComponents];
    if (path === '/api/policy') return [200, policyRead()];
    if (path === '/api/provider-declarations') {
      return [200, { offers: [{
        id: 'grok-build', display_name: 'Grok Build', sha256, capability: maximum,
        status: approvedCapability ? 'approved' : 'pending', approved_capability: approvedCapability,
      }] }];
    }
    assert.equal(path, '/api/provider-declarations/grok-build');
    assert.equal(method, 'POST');
    approvals.push(body);
    approvedCapability = body.capability;
    return [200, {}];
  });
  await flush();
  const card = () => ui.elements.get('component-rows').querySelectorAll('article')[0];
  const click = (label) => findButton(card(), label).listeners.get('click')();
  const review = findButton(card(), 'Review approval');
  assert.equal(insideDetails(review), false);
  assert.match(review.className, /\bbtn-primary\b/);
  assert.equal(findButton(card(), 'Approve write'), undefined);
  assert.equal(findButton(card(), 'Approve MCP'), undefined);
  assert.ok(card().querySelectorAll('p').some((p) => /work folder.*without network.*home folder.*Read stays available/.test(p.textContent)));
  await click('Review approval');
  assert.equal(insideDetails(findButton(card(), 'Approve write')), true);
  await click('Approve write');
  assert.deepEqual(approvals, [{ sha256, capability: 'read' }, { sha256, capability: 'write' }]);
  assert.ok(card().querySelectorAll('p').some((p) => p.textContent === 'Approved capability write'));
  assert.equal(findButton(card(), 'Approve write'), undefined);
  if (maximum === 'mcp') {
    assert.ok(card().querySelectorAll('p').some((p) => /only the MCP servers you register in Workspace/.test(p.textContent)));
    await click('Approve MCP');
    assert.deepEqual(approvals.at(-1), { sha256, capability: 'mcp' });
    assert.ok(card().querySelectorAll('p').some((p) => p.textContent === 'Approved capability mcp'));
  }
  assert.equal(findButton(card(), 'Approve MCP'), undefined);
});

for (const [id, name] of [['grok-build', 'Grok Build'], ['agy-cli', 'Antigravity'], ['cursor-cli', 'Cursor']])
test(`Windows unrestricted approval for ${name} needs a server warning and a fresh one-time nonce`, async () => {
  const sha256 = 'ab'.repeat(32);
  const warning = `${name} runs with your rights on this PC. Tool limits and the work folder are not enforced.`;
  const reviews = [];
  const approvals = [];
  const ui = browser(({ path, method, body }) => {
    if (path === '/api/session') return [200, { token: 'session-token' }];
    if (path === '/api/status') return [200, status];
    if (path === '/api/components') return [200, emptyComponents];
    if (path === '/api/policy') return [200, policyRead()];
    if (path === '/api/provider-declarations') {
      return [200, { offers: [{
        id, display_name: name, sha256, capability: 'unrestricted', platform: 'windows-x64',
        status: approvals.length === 2 ? 'approved' : 'pending', can_install: approvals.length === 2,
        rights: warning,
      }] }];
    }
    assert.equal(method, 'POST');
    if (path === `/api/provider-declarations/${id}/confirmation`) {
      reviews.push(body);
      return [200, { nonce: `nonce-${reviews.length}`, warning: `${warning} Review ${reviews.length}.` }];
    }
    assert.equal(path, `/api/provider-declarations/${id}`);
    approvals.push(body);
    if (approvals.length === 1) return [409, { error: 'Review the warning again.' }];
    return [200, {}];
  });
  await flush();
  const card = () => ui.elements.get('component-rows').querySelectorAll('article')[0];
  assert.ok(card().querySelectorAll('label').some((node) => node.children.includes(`I understand ${name} will run with my rights on this PC.`)));
  const confirm = card().querySelectorAll('input')[0];
  const approve = findButton(card(), 'Approve unrestricted');
  const review = findButton(card(), 'Review unrestricted approval');
  assert.ok(card().querySelectorAll('p').some((p) => p.textContent.includes('the work folder are not enforced')));
  for (const label of ['Approve', 'Approve read', 'Approve write', 'Approve MCP', 'Install']) {
    assert.equal(findButton(card(), label), undefined);
  }
  assert.equal(Boolean(confirm.checked), false);
  assert.equal(approve.disabled, true);
  assert.equal(approve.hidden, true);
  await approve.listeners.get('click')();
  assert.deepEqual(approvals, []);
  await review.listeners.get('click')();
  assert.deepEqual(reviews, [{ sha256 }]);
  assert.ok(card().querySelectorAll('p').some((p) => !p.hidden && p.textContent.endsWith('Review 1.')));
  assert.equal(approve.hidden, false);
  assert.equal(approve.disabled, true);
  confirm.checked = true;
  confirm.onchange();
  assert.equal(approve.disabled, false);
  confirm.checked = false;
  confirm.onchange();
  assert.equal(approve.disabled, true);
  confirm.checked = true;
  confirm.onchange();
  await approve.listeners.get('click')();
  assert.equal(approve.hidden, true);
  await approve.listeners.get('click')();
  assert.equal(approvals.length, 1);
  await review.listeners.get('click')();
  assert.equal(confirm.checked, false);
  confirm.checked = true;
  confirm.onchange();
  await approve.listeners.get('click')();
  await flush();
  assert.deepEqual(approvals, ['nonce-1', 'nonce-2'].map(nonce => ({ sha256, capability: 'unrestricted', nonce })));
  assert.ok(findButton(card(), 'Install'));
});

test('use on this child saves the declaration id, not its driver', async () => {
  // Regression: the card once saved offer.driver ("cli.acp"), which is never a detected adapter.
  const sha256 = 'ab'.repeat(32);
  let policy = { ...emptyPolicy };
  let revision = 'original';
  const ui = browser(({ path, method, body }) => {
    if (path === '/api/session') return [200, { token: 'session-token' }];
    if (path === '/api/status') return [200, status];
    if (path === '/api/components') return [200, emptyComponents];
    if (path === '/api/login/status/grok-build') return [200, { state: 'logged_in' }];
    if (path === '/api/provider-declarations') {
      return [200, { offers: [{
        id: 'grok-build', driver: 'cli.acp', capability: 'read', sha256, display_name: 'Grok Build',
        status: 'approved', approved_capability: 'read', installed: true, local_login: true,
      }] }];
    }
    assert.equal(path, '/api/policy');
    if (method === 'GET') return [200, { ...policyRead(policy, revision), available_adapters: ['grok-build'] }];
    policy = body.policy;
    revision += '-saved';
    return [200, { revision, warnings: [] }];
  });
  await flush();
  const card = ui.elements.get('component-rows').querySelectorAll('article')[0];
  await findButton(card, 'Use on this child').listeners.get('click')();
  await flush();
  assert.deepEqual(policy.adapters, ['grok-build']);
});

test('an enabled declaration shows that it is in use and can be stopped', async () => {
  const sha256 = 'ab'.repeat(32);
  let policy = { ...emptyPolicy, adapters: ['grok-build'] };
  let revision = 'original';
  const ui = browser(({ path, method, body }) => {
    if (path === '/api/session') return [200, { token: 'session-token' }];
    if (path === '/api/status') return [200, status];
    if (path === '/api/components') return [200, emptyComponents];
    if (path === '/api/login/status/grok-build') return [200, { state: 'logged_in' }];
    if (path === '/api/provider-declarations') {
      return [200, { offers: [{
        id: 'grok-build', driver: 'cli.acp', capability: 'read', sha256, display_name: 'Grok Build',
        status: 'approved', approved_capability: 'read', installed: true, local_login: true,
      }] }];
    }
    assert.equal(path, '/api/policy');
    if (method === 'GET') return [200, { ...policyRead(policy, revision), available_adapters: ['grok-build'] }];
    policy = body.policy;
    revision += '-saved';
    return [200, { revision, warnings: [] }];
  });
  await flush();
  const card = () => ui.elements.get('component-rows').querySelectorAll('article')[0];
  assert.equal(findButton(card(), 'Use on this child'), undefined);
  assert.ok(card().querySelectorAll('span').some((item) => item.textContent === 'In use on this child'));
  await findButton(card(), 'Stop using on this child').listeners.get('click')();
  await flush();
  assert.deepEqual(policy.adapters, []);
  assert.ok(findButton(card(), 'Use on this child'));
});

test('Settings handles app updates, progress, and a deferred restart', async () => {
  const offer = { version: '1.2.1', build_id: 'abcd', sha256: 'digest', size: 100 };
  let current = { ...status, can_update: true, update: { offer, phase: 'idle' } };
  let confirmed = 0;
  let output;
  const ui = browser(({ path, body }) => {
    if (path === '/api/session') return [200, { token: 'session-token' }];
    if (path === '/api/status') return [200, current];
    if (path === '/api/components') return [200, emptyComponents];
    if (path === '/api/policy') return [200, policyRead()];
    if (path === '/api/settings') return [200, { revision: 'test' }];
    if (path === '/api/update' && body?.action === 'download') {
      return [202, new ReadableStream({ start(controller) { output = controller; } })];
    }
    if (path === '/api/update' && body?.action === 'interrupt') return [202, {}];
    if (path === '/api/update') return [200, new ReadableStream({ start(controller) {
      controller.enqueue(Buffer.from(JSON.stringify(current.update) + '\n'));
      controller.close();
    } })];
    if (path === '/api/action') return [200, { message: 'Restarting Kotoyose Child.' }];
    throw new Error(path);
  }, () => ++confirmed > 1);
  await flush();
  ui.showSettings();
  await flush();
  const get = (id) => ui.elements.get(id);
  const tick = async () => {
    [...ui.intervals.values()].find((timer) => timer.milliseconds === 1000).callback();
    await flush();
  };
  const sendUpdate = async (view) => {
    current = { ...current, update: { offer, ...view } };
    output.enqueue(Buffer.from(JSON.stringify(current.update) + '\n'));
    await flush();
  };

  assert.equal(get('update-offer').textContent, 'Update available: 1.2.1 (abcd)');
  assert.equal(get('restart').hidden, true);
  assert.equal(ui.requests.some(({ path }) => path === '/api/update'), false);
  current = { ...current, update: { offer, phase: 'idle', retry_after: 12 } };
  await tick();
  assert.match(get('update-state').textContent, /Try again in 12 s/);
  assert.equal(get('update-start').disabled, true);
  assert.equal(get('update-offer').hidden, false);
  current = { ...current, update: { offer, phase: 'idle', retry_after: 0 } };
  await tick();
  assert.equal(get('update-start').disabled, false);
  get('update-defer').checked = true;
  const download = get('update-start').onclick({ currentTarget: get('update-start') });
  await flush();
  assert.equal(get('update-message').textContent, '');
  assert.deepEqual(ui.requests.find(({ path }) => path === '/api/update')?.body,
    { action: 'download', sha256: 'digest', defer: true });
  for (const received of [0, 25, 74, 100]) {
    await sendUpdate({ phase: 'downloading', received });
    assert.equal(get('update-progress').hidden, false);
    assert.equal(get('update-progress').max, offer.size);
    assert.equal(get('update-progress').value, received);
    assert.equal(get('update-state').textContent, `Downloading… ${received}%`);
  }
  await sendUpdate({ phase: 'verifying' });
  assert.equal(get('update-state').textContent, 'Verifying update…');
  assert.equal(get('update-progress').attributes.has('value'), false);

  await sendUpdate({ phase: 'waiting' });
  assert.equal(get('update-state').textContent, 'Waiting for active and queued work to finish…');
  for (const work of [2, 1]) {
    current = { ...current, link: { ...status.link, work } };
    await tick();
    assert.equal(get('update-state').textContent,
      `Waiting for active and queued work to finish (${work} ${work === 1 ? 'job' : 'jobs'})…`);
  }
  const interrupt = () => get('update-interrupt').onclick({ currentTarget: get('update-interrupt') });
  await interrupt();
  assert.equal(ui.requests.filter(({ body }) => body?.action === 'interrupt').length, 0);
  await interrupt();
  assert.equal(ui.requests.filter(({ body }) => body?.action === 'interrupt').length, 1);

  await sendUpdate({ phase: 'deferred' });
  output.close();
  await download;
  assert.equal(get('restart').hidden, false);
  assert.match(get('update-state').textContent, /Select Restart/);
  await get('restart').onclick({ currentTarget: get('restart') });
  assert.deepEqual(ui.requests.find(({ path }) => path === '/api/action').body, { restart: true, interrupt: true });
});

for (const loss of ['network', 'unauthorized']) {
  test(`an acknowledged update stays visible after ${loss} shutdown`, async () => {
    const offer = { version: '1.3.3', build_id: 'abcd', sha256: 'digest', size: 100 };
    let update = { offer, phase: 'idle' };
    let output;
    let stopped = false;
    const ui = browser(({ path, body }) => {
      if (path === '/api/session') return [200, { token: 'session-token' }];
      if (path === '/api/status') {
        if (stopped && loss === 'network') throw new Error('server closed');
        if (stopped) return [401, {}];
        return [200, { ...status, can_update: true, update }];
      }
      if (path === '/api/components') return [200, emptyComponents];
      if (path === '/api/policy') return [200, policyRead()];
      if (path === '/api/settings') return [200, { revision: 'test' }];
      assert.equal(path, '/api/update');
      if (body.action === 'acknowledge') {
        stopped = true;
        return [204, {}];
      }
      return [202, new ReadableStream({ start(controller) { output = controller; } })];
    });
    await flush();
    ui.showSettings();
    await flush();
    const get = (id) => ui.elements.get(id);
    const download = get('update-start').onclick({ currentTarget: get('update-start') });
    await flush();
    const send = (expected_shutdown) => {
      update = { offer, phase: 'installing', expected_shutdown };
      output.enqueue(Buffer.from(JSON.stringify(update) + '\n'));
    };
    send(false);
    await flush();
    assert.equal(get('update-state').textContent, 'Installing 1.3.3…');
    assert.equal(stopped, false);
    send(true);
    output.close();
    await download;
    assert.equal(stopped, true);
    assert.equal(ui.requests.filter(({ body }) => body?.action === 'acknowledge').length, 1);
    assert.match(get('update-state').textContent, /Installing 1\.3\.3.*open Settings in a new window.*This tab cannot reconnect/);
    assert.equal(get('update-progress').hidden, false);
    assert.equal(get('update-progress').attributes.has('value'), false);

    const [interval] = ui.intervals.values();
    assert.equal(interval.milliseconds, 1_000);
    await interval.callback();
    assert.equal(ui.intervals.size, 0);
    assert.equal(get('closed').hidden, true);
    assert.equal(get('status-message').attributes.has('data-error'), false);
    assert.match(get('update-state').textContent, /You can close it/);
    assert.equal(get('update-progress').hidden, false);
  });
}

test('Linux source shows manual instructions without an offer or install button', async () => {
  const update = { offer: null, phase: 'idle' };
  const ui = browser(({ path }) => {
    if (path === '/api/session') return [200, { token: 'session-token' }];
    if (path === '/api/status') return [200, { ...status, can_update: false, update }];
    if (path === '/api/components') return [200, emptyComponents];
    if (path === '/api/policy') return [200, policyRead()];
    throw new Error(path);
  });
  await flush();
  const get = (id) => ui.elements.get(id);
  assert.equal(get('update-card').hidden, false);
  assert.equal(get('update-start').hidden, true);
  assert.equal(get('restart').hidden, true);
  assert.equal(get('update-offer').hidden, true);
  assert.match(get('update-state').textContent, /Update by hand.*uv sync.*cargo build/);
});

test('Workspace edits and saves session history collection sources and origin label', async () => {
  let policy = {
    ...emptyPolicy,
    session_import_sources: ['claude'],
    session_import_origin_hint: null,
  };
  let revision = 'rev-1';
  const ui = browser(({ path, method, body }) => {
    if (path === '/api/session') return [200, { token: 'session-token' }];
    if (path === '/api/status') return [200, status];
    if (path === '/api/components') return [200, emptyComponents];
    if (path === '/api/policy' && method === 'GET') return [200, policyRead(policy, revision)];
    if (path === '/api/policy' && method === 'POST') {
      policy = body.policy;
      revision = 'rev-2';
      return [200, { revision, warnings: [] }];
    }
    throw new Error(`${method} ${path}`);
  });
  await flush();
  ui.showWorkspace();
  await flush();

  const boxes = ui.elements.get('session-import-sources').querySelectorAll('input');
  assert.deepEqual(boxes.map((box) => [box.value, box.checked]), [
    ['claude', true],
    ['codex', false],
    ['antigravity', false],
    ['cursor', false],
    ['grok', false],
  ]);
  assert.equal(ui.elements.get('session-import-origin-hint').value, '');

  boxes.find((box) => box.value === 'codex').checked = true;
  boxes.find((box) => box.value === 'cursor').checked = true;
  ui.elements.get('session-import-origin-hint').value = 'work';
  ui.elements.get('policy-form').onchange();
  ui.elements.get('policy-form').onsubmit({ preventDefault() {} });
  await flush();

  assert.deepEqual(policy.session_import_sources, ['claude', 'codex', 'cursor']);
  assert.equal(policy.session_import_origin_hint, 'work');

  // Policy with ["all"] renders all 5 source checkboxes as checked.
  policy = { ...policy, session_import_sources: ['all'], session_import_origin_hint: 'personal' };
  ui.showConnection();
  ui.showWorkspace();
  await flush();
  const reloaded = ui.elements.get('session-import-sources').querySelectorAll('input');
  assert.equal(reloaded.every((box) => box.checked), true);
  assert.equal(ui.elements.get('session-import-origin-hint').value, 'personal');
});


test('unrestricted review groups controls, cancels and recovers from confirmation errors', async () => {
  let attempts = 0;
  const ui = browser(({ path }) => {
    if (path === '/api/session') return [200, { token: 'session-token' }];
    if (path === '/api/status') return [200, status];
    if (path === '/api/components') return [200, emptyComponents];
    if (path === '/api/policy') return [200, policyRead()];
    if (path === '/api/provider-declarations') return [200, { offers: [{
      id: 'grok-build', capability: 'unrestricted', status: 'pending', sha256: 'ab',
    }] }];
    assert.equal(path, '/api/provider-declarations/grok-build/confirmation');
    if (++attempts === 1) return [500, { error: 'Confirmation unavailable' }];
    return [200, { nonce: 'fresh', warning: 'Runs with your rights.' }];
  });
  await flush();
  const card = () => ui.elements.get('component-rows').querySelectorAll('article')[0];
  const panel = () => card().querySelectorAll('div').find((node) => node.className === 'unrestricted-review');
  assert.equal(panel().hidden, true);
  await findButton(card(), 'Review unrestricted approval').listeners.get('click')();
  assert.match(cardResult(card()).textContent, /Confirmation unavailable/);
  assert.equal(Boolean(ui.declarations.get('grok-build').confirming), false);
  await findButton(card(), 'Review unrestricted approval').listeners.get('click')();
  const review = panel();
  assert.equal(review.hidden, false);
  assert.equal(review.querySelectorAll('p')[0].className, 'unrestricted-warning');
  assert.equal(review.querySelectorAll('p')[0].textContent, 'Runs with your rights.');
  const checkbox = review.querySelectorAll('input')[0];
  const approve = findButton(review, 'Approve unrestricted');
  assert.equal(approve.parentElement.className, 'actions');
  checkbox.checked = true;
  checkbox.onchange();
  assert.equal(approve.disabled, false);
  await findButton(review, 'Cancel').listeners.get('click')();
  assert.equal(review.hidden, true);
  assert.equal(checkbox.checked, false);
  assert.equal(approve.disabled, true);
  await findButton(card(), 'Review unrestricted approval').listeners.get('click')();
  assert.equal(panel().hidden, false);
  assert.equal(attempts, 3);
});

for (const matched of [true, false]) test(`unsupported declaration is unavailable (matched built-in: ${matched})`, async () => {
  const ui = browser(({ path }) => {
    if (path === '/api/session') return [200, { token: 'session-token' }];
    if (path === '/api/status') return [200, status];
    if (path === '/api/policy') return [200, policyRead()];
    if (path === '/api/components') return [200, { ...emptyComponents, providers: matched ? [{
      id: 'cursor', name: 'Cursor', driver: 'cli.print', packages: [], aliases: [],
    }] : [] }];
    if (path === '/api/provider-declarations') return [200, { offers: [{
      id: 'cursor-cli', profile_id: 'cursor', display_name: 'Cursor',
      capability: null, platform: 'windows-x64', status: 'pending',
    }] }];
    assert.fail(path);
  });
  await flush();
  const rows = ui.elements.get('component-rows');
  assert.equal(rows.querySelectorAll('article').length, 0);
  const group = rows.querySelectorAll('details').find((node) => node.className === 'unavailable-providers');
  assert.equal(group.querySelectorAll('summary')[0].textContent, 'Not available on this child');
  assert.deepEqual(group.querySelectorAll('li').map((node) => node.textContent), ['Cursor — Not available on windows-x64.']);
});
