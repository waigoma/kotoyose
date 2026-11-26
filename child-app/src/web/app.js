'use strict';

const text = {
  appName: "Kotoyose Child",
  menu: "Menu",
  settingsNavigation: "Settings navigation",
  connectionDetails: "Connection details",
  providers: "Providers",
  workspace: "Workspace",
  enrollAgain: "Enroll again",
  settings: "Settings",
  sessionClosed: "The Settings session ended. Open Settings again from the tray.",
  refresh: "Refresh",
  loading: "Loading…",
  applyingPolicy: "Applying policy",
  enrollmentHint: "Use an enrollment code issued in the host console.",
  childName: "Child name",
  enrollmentCode: "Enrollment code",
  enroll: "Enroll",
  reload: "Reload",
  workspaceFolders: "Workspace folders",
  noWorkspaceFolders: "No workspace folders added.",
  addWorkspaceFolder: "Add workspace folder",
  localLlms: "Local LLMs",
  noLocalLlms: "No local LLM connections allowed.",
  addLocalLlm: "Add local LLM",
  localLlmId: "Connection ID",
  localLlmUrl: "Loopback URL",
  localLlmModel: "Allowed model ID",
  addLocalLlmModel: "Add model",
  duplicateLocalLlm: "Local LLM connection IDs must be unique.",
  mcp: "MCP",
  hostMcpTitle: "Host MCP publications",
  hostMcpScope: "Tools execute on the host. Tool arguments go to the host and its configured MCP server, and results return to this child.",
  hostMcpEmpty: "No MCP publications from the enrolled host.",
  hostMcpTools: (tools) => `Configured tool preview: ${tools.join(', ')}`,
  hostMcpDelegates: (aliases) => aliases.length ? `Delegates: ${aliases.join(', ')}` : "No delegates configured.",
  hostMcpAllTools: "All tools",
  hostMcpToolsUpstream: "Tools are defined by the host MCP server.",
  hostMcpVersionChanged: "Version changed",
  hostMcpFingerprint: (value) => `Host fingerprint: ${value}`,
  hostMcpVersion: (value) => `Version: ${value}`,
  hostMcpCommand: (command) => `Host command (information only; this child does not run it): ${command}`,
  hostMcpArgs: (args) => `Arguments (information only): ${args.join(' ')}`,
  hostMcpCwd: (cwd) => `Working directory: ${cwd}`,
  hostMcpEnvNames: (names) => `Environment names: ${names.join(', ')}`,
  hostMcpApprove: "Approve this version",
  hostMcpRevoke: "Revoke approval",
  hostMcpApproved: "Approved. The host receives this on the next heartbeat.",
  hostMcpRevoked: "Approval revoked.",
  noMcp: "No MCP servers configured.",
  addMcp: "Add MCP server",
  savePolicy: "Save policy",
  policyHint: "Changes apply when active work finishes and the child reconnects.",
  runnerIdle: "Runner idle timeout",
  idleSeconds: "Idle timeout (seconds)",
  idleHint: "Default: 600 seconds. Set to 0 to stop the runner as soon as it finishes cleaning up.",
  saveIdle: "Save idle timeout",
  autostart: "Autostart",
  startAtSignIn: "Start at Windows sign-in",
  autostartHint: "Turning this on removes the old KotoyoseChild scheduled task if it exists.",
  appControls: "App controls",
  appUpdate: "App update",
  updateAvailable: (offer) => `Update available: ${offer.version} (${offer.build_id})`,
  updateManual: "Update by hand. In your checkout, switch to the new release, run uv sync and cargo build --locked --release --manifest-path child-app/Cargo.toml, then restart.",
  updateDefer: "Install on restart",
  updateDownloading: (received, size) => `Downloading… ${Math.floor(100 * received / size)}%`,
  updateVerifying: "Verifying update…",
  updateWaiting: (count) => count
    ? `Waiting for active and queued work to finish (${count} ${count === 1 ? text.workOne : text.workOther})…`
    : "Waiting for active and queued work to finish…",
  updateDeferred: "Update verified. Select Restart under App controls to install it.",
  updateInstalling: (version) => `Installing ${version}…`,
  updateHandoff: (version) => `${text.updateInstalling(version)} Kotoyose Child will restart and open Settings in a new window. This tab cannot reconnect. You can close it.`,
  updateInterrupt: "Interrupt and update",
  updateRetry: (seconds) => `The host is busy. Try again in ${seconds} s.`,
  updateError: (code) => ({
    download: "The host could not send this update. Try again or ask the host operator to check the publication.",
    disconnect: "The link disconnected. The downloaded update was discarded.",
    timeout: "The update download timed out. Try again.",
    verify: "The download did not match the offered installer. Ask the host operator to check the publication.",
    storage: "The installer could not be saved in the child state folder.",
    setup: "The app update failed. The previous build is still installed. Open the log folder to check repair.log.",
  })[code],
  restart: "Restart",
  stop: "Stop",
  recentLogs: "Recent logs",
  openLogFolder: "Open log folder",
  repairLogHint: "Open the log folder to find repair.log after running Setup or repair.",
  autoRepairPending: "Waiting to reinstall automatically.",
  autoReinstallRunning: "Reinstalling automatically…",
  autoRepairDone: "Reinstalled automatically.",
  autoRepairFailed: "Automatic reinstall failed. See Recent logs in Settings, then select Repair to try again.",
  connected: "Connected",
  reconnecting: "Reconnecting",
  notConnected: "Not connected",
  notEnrolled: "Not enrolled",
  connectionSummary: (state, host) => host ? `${state} · ${host}` : state,
  versionLine: (child, host) => `Child ${child} · Host ${host ?? "—"}`,
  connectionLost: "The Settings connection was lost.",
  reopenSettings: "Open Settings again from the tray.",
  running: "Runner running",
  idle: "Runner idle",
  noMemory: "Memory usage unavailable",
  remove: "Remove",
  variableName: "Variable name",
  value: "Value",
  show: "Show",
  hide: "Hide",
  name: "Name",
  mcpNameHint: "Use 1 to 64 ASCII letters, digits, '_', '.', or '-'.",
  command: "Command",
  description: "Description",
  argument: "Argument",
  addArgument: "Add argument",
  addVariable: "Add environment variable",
  duplicateMcpVariable: "MCP environment variable names must be unique.",
  absolutePath: "Absolute path",
  duplicateMcp: "MCP names must be unique.",
  noLogs: "No logs yet.",
  confirmReload: "Discard unsaved changes and reload the policy?",
  enrolling: "Enrolling…",
  policySaved: "Policy saved. The child will reconnect when active work finishes.",
  idleSaved: "Idle timeout saved. It takes effect the next time the runner starts.",
  packageUnavailable: "Install or repair the bundled Python runtime to manage packages.",
  install: "Install",
  installPackagesHint: (packages) => `Installs ${packages} and the bundled CLI.`,
  installManagedCliHint: (name, version) => `Installs the managed ${name} CLI (${version}).`,
  update: "Update",
  repair: "Repair",
  check: "Check",
  needsReinstall: "Needs reinstall",
  useInstalledCli: "Use the installed CLI",
  checkingComponents: "Checking components…",
  applyingComponent: "Applying change…",
  componentOutput: "Operation output",
  componentSuccess: (reason) => `Succeeded. ${reason}`,
  componentFailure: (reason) => `Failed. ${reason}`,
  approve: "Approve",
  approveRead: "Approve read",
  approveWrite: "Approve write",
  approveMcp: "Approve MCP",
  reviewApproval: "Review approval",
  reviewUnrestrictedApproval: "Review unrestricted approval",
  approveUnrestricted: "Approve unrestricted",
  unrestrictedConfirm: "I understand Grok will run with my rights on this PC.",
  confirmWithin: "Confirm within 60 seconds",
  notAvailableOnChild: "Not available on this child",
  noDeclarations: "No declarations have been offered by the linked host.",
  declarationDetails: "Declaration details (host, version, commands)",
  commandsHeading: "Commands",
  hostFingerprint: (value) => `Host ${value}`,
  declarationSha: (value) => `SHA-256 ${value}`,
  declarationStatus: (value) => `Status ${value}`,
  declarationPlatform: (value) => `Platform ${value}`,
  declarationBinary: (value) => `Binary ${value}`,
  declarationVersion: (value) => `Version ${value}`,
  approvedCapability: (value) => `Approved capability ${value}`,
  declarationWriteRights: "Write limits file changes to the work folder and Grok’s own state. Commands run without network access. Reads of your home folder outside the work folder are blocked except for Grok’s own state. Read stays available.",
  declarationMcpRights: "MCP also lets the provider use only the MCP servers you register in Workspace.",
  revoke: "Revoke",
  declarationApproved: "The host declaration was approved.",
  declarationInstalled: "The managed CLI was installed.",
  declarationRevoked: "The host declaration was revoked.",
  declarationLoadFailed: "Could not load declarations.",
  notInstalled: "Not installed",
  cliFound: "System CLI",
  noSystemCli: "System CLI not found",
  bundledCli: "Bundled CLI",
  bundledMissing: "Bundled CLI unavailable",
  sdkUnavailable: "SDK could not be imported",
  progressEnded: "The progress stream ended before a result was received. Refresh to check the installed packages.",
  codexSandbox: "Codex sandbox limit",
  sandboxRead: "read-only",
  sandboxWrite: "workspace-write",
  sandboxFull: "danger-full-access",
  useOnChild: "Use on this child",
  inUseOnChild: "In use on this child",
  stopUsingOnChild: "Stop using on this child",
  showQuota: "Share quota with host",
  adapterHint: "The host can check the CLI login state while this provider is enabled.",
  quotaHint: "On Linux, this also allows host-initiated CLI login.",
  adapterUnavailable: "Install the SDK and select an available CLI to use this provider.",
  packageDetails: "SDK packages",
  packageSummary: (version) => `SDK · ${version}`,
  logIn: "Log in",
  loggedIn: "Logged in",
  loggedOut: "Not logged in",
  loginStarting: "Login in progress",
  cancelLogin: "Cancel",
  loginCancelled: "Login cancelled",
  loginUnavailable: "The selected CLI is unavailable.",
  loginFailed: "Could not check local login.",
  loginUnknown: "Login status unchecked",
  repairPolicy: "Open Workspace to repair the policy before changing provider access.",
  loginInstructions: (name, command) => `Sign in with the official ${name} CLI using ${command}.`,
  childTurnsUnavailable: "Child turns are not enabled for this provider.",
  cliPlatformUnavailable: "Managed installation is unavailable on this OS.",
  managedCli: "Managed CLI",
  managedCliMissing: "Managed CLI not installed",
  loginLink: "Open the official sign-in page",
  loginDeviceCode: (code) => `Enter this device code on the sign-in page: ${code}`,
  authorizationCode: "Authorization code",
  submitCode: "Submit code",
  workOne: "job",
  workOther: "jobs",
  httpError: (status) => `The request failed (HTTP ${status}).`,
  exitInterrupt: (count) => `Restarting or stopping will interrupt ${count} active or queued ${count === 1 ? text.workOne : text.workOther}.`,
  confirmInterrupt: (count) => `Interrupt ${count} active or queued ${count === 1 ? text.workOne : text.workOther} and continue?`,
  memoryMiB: (bytes) => `${(bytes / 1048576).toFixed(1)} MiB`,
};

for (const element of document.querySelectorAll('[data-text]')) element.textContent = text[element.dataset.text];
for (const element of document.querySelectorAll('[data-aria]')) element.setAttribute('aria-label', text[element.dataset.aria]);

let token = '';
const byId = (id) => document.getElementById(id);
let activePage = 'connection';
let enrollmentNeeded;
let revision;
let settingsRevision;
let policyDirty = false;
let policyBusy = false;
let savedPolicy;
let policyError;
let availableAdapters = [];
let idleBusy = false;
let closed = false;
let heartbeatTimer;

function message(id, text = '', error = false) {
  const target = byId(id);
  target.textContent = text ?? '';
  target.toggleAttribute('data-error', error);
}

function stopHeartbeat() {
  clearInterval(heartbeatTimer);
  heartbeatTimer = undefined;
}

function closeSession() {
  token = '';
  closed = true;
  stopHeartbeat();
  document.querySelectorAll('main button, main input, main select, main textarea').forEach((control) => {
    control.disabled = true;
  });
}

function disconnected() {
  closeSession();
  byId('closed').hidden = !!appUpdateView?.expected_shutdown;
}

async function request(path, data, stream = false, method = data === undefined ? 'GET' : 'POST') {
  const headers = token ? { Authorization: `Bearer ${token}` } : {};
  if (stream) headers.Accept = 'application/x-ndjson';
  let response;
  try {
    response = await fetch(`/api/${path}`, data === undefined ? { method, headers, credentials: 'omit' } : {
      method,
      credentials: 'omit',
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
  } catch {
    if (!closed) disconnected();
    throw new Error(text.connectionLost);
  }
  if (response.status === 401 && !closed) disconnected();
  if (response.ok && stream) return response;
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.error || text.httpError(response.status));
  return result;
}

async function openSession() {
  const code = location.hash.slice(1);
  history.replaceState(null, '', location.pathname);
  if (!code) {
    disconnected();
    return;
  }
  try {
    const session = await request('session', { code });
    token = session.token;
  } catch (error) {
    disconnected();
    message('status-message', error.message, true);
  }
}

async function api(path, data, method) {
  await sessionReady;
  if (!token) throw new Error(text.reopenSettings);
  return request(path, data, false, method);
}

async function operate(id, target, task) {
  message(id);
  if (target) target.disabled = true;
  try {
    await task();
  } catch (error) {
    if (!appUpdateView?.expected_shutdown) message(id, error.message, true);
  } finally {
    if (target && !closed) target.disabled = false;
  }
}

function show(id, value) {
  const target = byId(id);
  if (target.textContent !== (value ?? '')) target.textContent = value ?? '';
  target.hidden = value == null;
}

let appUpdateRequestPending = false;
let appUpdateView;
let appUpdateStream;
let appUpdateSupported;
let appRestartNeeded;
let appUpdateWork = 0;

function updating(phase) {
  return ['requested', 'downloading', 'verifying', 'waiting', 'installing'].includes(phase);
}

function renderUpdate(update) {
  const { offer, phase, received, error, retry_after: retryAfter, expected_shutdown: handoff } = update ?? {};
  const newHandoff = handoff && !appUpdateView?.expected_shutdown;
  appUpdateView = update;
  byId('update-card').hidden = appUpdateSupported !== false && !offer && !error && !updating(phase);
  show('update-offer', offer ? text.updateAvailable(offer) : null);
  const canStart = !!offer && appUpdateSupported && phase === 'idle';
  byId('update-start').hidden = !canStart;
  byId('update-start').disabled = appUpdateRequestPending || !!retryAfter;
  byId('update-defer-field').hidden = !canStart;
  byId('update-interrupt').hidden = phase !== 'waiting' || !appUpdateWork;
  const progress = byId('update-progress');
  progress.hidden = !['downloading', 'verifying', 'installing'].includes(phase);
  if (phase === 'downloading' && offer) {
    progress.max = offer.size;
    progress.value = received;
  } else {
    progress.removeAttribute('value');
  }
  const handoffMessage = handoff ? text.updateHandoff(offer.version) : null;
  const next = {
    requested: text.loading,
    downloading: offer && text.updateDownloading(received, offer.size),
    verifying: text.updateVerifying,
    waiting: text.updateWaiting(appUpdateWork),
    deferred: text.updateDeferred,
    installing: handoffMessage ?? text.updateInstalling(offer?.version ?? ''),
  };
  show('update-state', appUpdateSupported === false ? text.updateManual :
    (canStart && retryAfter ? text.updateRetry(retryAfter) : (next[phase] ?? null)));
  show('update-handoff', activePage === 'settings' ? null : handoffMessage);
  message('update-error', text.updateError(error), !!error);
  const restart = byId('restart');
  const restartNeeded = phase === 'deferred' || (!updating(phase) && appRestartNeeded);
  if (restartNeeded && restart.hidden) byId('app-controls').open = true;
  restart.hidden = !restartNeeded;
  if (newHandoff) void api('update', { action: 'acknowledge' }).catch(() => {});
}

function watchUpdate(download) {
  appUpdateStream ??= operate('update-message', null, async () => {
    const response = await request('update', download, true);
    const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
    let pending = '';
    while (!closed) {
      const { value, done } = await reader.read();
      if (done || closed) break;
      const lines = (pending + value).split('\n');
      pending = lines.pop();
      for (const line of lines) renderUpdate(JSON.parse(line));
    }
  }).finally(() => { appUpdateStream = undefined; });
  return appUpdateStream;
}

function renderStatus({ link, enrolled, version, runner_bytes: bytes, update, can_update, restart_needed }) {
  if (closed) return link;
  const needsEnrollment = !enrolled || ['unregistered', 'stopped'].includes(link.connection);
  let state = text.notEnrolled;
  let tone = '';
  if (enrolled) {
    if (link.connection === 'connected') {
      state = text.connected;
      tone = ' positive';
    } else if (needsEnrollment) {
      state = text.notConnected;
      tone = ' negative';
    } else {
      state = text.reconnecting;
      tone = ' warning';
    }
  }
  message('connection-state', state);
  show('connection-address', link.host);
  byId('connection-dot').className = byId('details-dot').className = `dot${tone}`;

  const form = byId('enroll-form');
  const enrollment = byId(needsEnrollment ? 'connection-enrollment' : 'settings-enrollment');
  if (form.parentElement !== enrollment) enrollment.append(form);
  byId('connection-enrollment').hidden = !needsEnrollment;
  byId('re-enroll').hidden = !enrolled || needsEnrollment;
  message('enroll-submit', enrolled ? text.enrollAgain : text.enroll);
  byId('connection-card').hidden = !enrolled;
  byId('refresh-status').hidden = needsEnrollment;

  message('connection-summary', text.connectionSummary(state, link.host));
  message('versions', text.versionLine(version, link.host_version));
  show('last-error', needsEnrollment ? null : link.error);
  byId('applying-policy').hidden = !link.applying_policy;
  show('enrollment-reason', link.error);
  message('runner', link.runner ? text.running : text.idle);
  byId('runner-dot').className = link.runner ? 'dot positive' : 'dot';
  show('memory', link.runner ? (bytes == null ? text.noMemory : text.memoryMiB(bytes)) : null);
  message('interrupt-hint', link.work ? text.exitInterrupt(link.work) : '');
  appUpdateSupported = can_update;
  appRestartNeeded = restart_needed && update?.phase !== 'deferred';
  appUpdateWork = link.work;
  if (appUpdateStream || (appUpdateView?.expected_shutdown && !update?.error)) {
    renderUpdate(appUpdateView);
  } else {
    renderUpdate(update);
    if (updating(update?.phase) && !update.expected_shutdown) watchUpdate();
  }
  const navigate = !updating(appUpdateView?.phase) &&
    (enrollmentNeeded === undefined || (needsEnrollment && !enrollmentNeeded));
  enrollmentNeeded = needsEnrollment;
  if (navigate) {
    selectPage(tabs.find((tab) => tab.dataset.page === (needsEnrollment ? 'connection' : 'providers')));
  }
  return link;
}

let statusRequest;
function refreshStatus() {
  statusRequest ??= api('status').then(renderStatus).finally(() => { statusRequest = undefined; });
  return statusRequest;
}

function updateHeartbeat() {
  stopHeartbeat();
  if (closed || !token || document.visibilityState !== 'visible') return;
  const heartbeat = () => operate('status-message', null, refreshStatus);
  heartbeatTimer = setInterval(heartbeat, { connection: 5_000, settings: 1_000 }[activePage] ?? 60_000);
  heartbeat();
}

async function confirmInterruption() {
  const link = await refreshStatus();
  if (!link.work) return false;
  return window.confirm(text.confirmInterrupt(link.work)) ? true : null;
}

function field(text, value = '', { password = false, required = false, mono = false } = {}) {
  const label = document.createElement('label');
  label.className = 'field';
  const title = document.createElement('span');
  title.className = 'field-label';
  title.textContent = text;
  const input = document.createElement('input');
  input.className = mono ? 'input input-mono' : 'input';
  if (password) input.type = 'password';
  input.value = value;
  input.required = required;
  input.spellcheck = false;
  label.append(title, input);
  return { label, input };
}

function button(text, handler) {
  const element = document.createElement('button');
  element.type = 'button';
  element.className = 'btn btn-sm';
  element.textContent = text;
  element.addEventListener('click', handler);
  return element;
}

function listRow(container, fields, className = 'input-row') {
  const row = document.createElement('div');
  row.className = className;
  row.append(...fields, button(text.remove, () => {
    row.remove();
    policyDirty = true;
  }));
  container.append(row);
  return row;
}

function addText(container, label, value = '', options = {}) {
  const control = field(label, value, options);
  listRow(container, [control.label]);
  return control.input;
}

function addEnvironment(container, name = '', value = '') {
  const key = field(text.variableName, name, { required: true, mono: true });
  const secret = field(text.value, value, { password: true });
  const row = listRow(container, [key.label, secret.label]);
  const reveal = button(text.show, () => {
    secret.input.type = secret.input.type === 'password' ? 'text' : 'password';
    reveal.textContent = secret.input.type === 'password' ? text.show : text.hide;
  });
  row.insertBefore(reveal, row.lastElementChild);
  return key.input;
}

function addMcp(name = '', entry = { args: [] }) {
  const card = document.createElement('fieldset');
  card.className = 'card card-body mcp-entry';
  const legend = document.createElement('legend');
  legend.className = 'row-title';
  legend.textContent = name || text.mcp;
  const nameField = field(text.name, name, { required: true });
  nameField.input.addEventListener('input', () => { legend.textContent = nameField.input.value || text.mcp; });
  nameField.input.pattern = String.raw`[A-Za-z0-9_.\-]{1,64}`;
  nameField.input.title = text.mcpNameHint;
  const command = field(text.command, entry.command, { required: true, mono: true });
  const description = field(text.description, entry.description || '');
  const args = document.createElement('div');
  const env = document.createElement('div');
  args.className = env.className = 'form';
  for (const arg of entry.args || []) addText(args, text.argument, arg);
  for (const [key, value] of Object.entries(entry.env || {})) addEnvironment(env, key, value);
  const addArg = button(text.addArgument, () => {
    addText(args, text.argument).focus();
    policyDirty = true;
  });
  const addEnv = button(text.addVariable, () => {
    addEnvironment(env).focus();
    policyDirty = true;
  });
  const actions = document.createElement('div');
  actions.className = 'actions';
  actions.append(addArg, addEnv);
  card.append(legend, nameField.label, description.label, command.label, args, env, actions);
  listRow(byId('mcp'), [card], 'mcp-item');
  card.readEntry = () => {
    const variables = Object.create(null);
    for (const row of env.children) {
      const [key, value] = row.querySelectorAll('input');
      if (Object.hasOwn(variables, key.value)) throw new Error(text.duplicateMcpVariable);
      variables[key.value] = value.value;
    }
    return [nameField.input.value, {
      ...entry,
      command: command.input.value,
      description: description.input.value || null,
      args: Array.from(args.querySelectorAll('input'), (arg) => arg.value),
      env: variables,
    }];
  };
  return nameField.input;
}

async function editPolicy(task) {
  if (policyBusy) return;
  policyBusy = true;
  byId('policy-inputs').disabled = true;
  byId('reload-policy').disabled = true;
  updateProviderPolicyControls();
  try {
    await task();
  } finally {
    policyBusy = false;
    if (!closed) {
      byId('policy-inputs').disabled = !revision;
      byId('reload-policy').disabled = false;
      updateProviderPolicyControls();
    }
  }
}

function addLocalLlm(name = '', entry = { url: '', models: [''] }) {
  const card = document.createElement('fieldset');
  card.className = 'card card-body';
  const id = field(text.localLlmId, name, { required: true, mono: true });
  id.input.pattern = String.raw`[A-Za-z0-9_.\-]{1,64}`;
  const url = field(text.localLlmUrl, entry.url, { required: true, mono: true });
  url.input.placeholder = 'http://127.0.0.1:11434/v1';
  const models = document.createElement('div');
  models.className = 'form';
  const addModel = (model) => addText(models, text.localLlmModel, model, { required: true, mono: true });
  for (const model of entry.models) addModel(model);
  card.append(id.label, url.label, models, button(text.addLocalLlmModel, () => {
    addModel('').focus();
    policyDirty = true;
  }));
  listRow(byId('local-llm'), [card], 'mcp-item');
  card.readEntry = () => [id.input.value, {
    url: url.input.value,
    models: Array.from(models.querySelectorAll('input'), (input) => input.value),
  }];
  return id.input;
}

async function loadPolicy() {
  await editPolicy(async () => {
    const result = await api('policy');
    savedPolicy = result.policy;
    availableAdapters = result.available_adapters;
    for (const id of ['roots', 'mcp', 'local-llm']) byId(id).replaceChildren();
    for (const root of savedPolicy.workspace_roots) {
      addText(byId('roots'), text.absolutePath, root, { required: true, mono: true });
    }
    for (const [name, entry] of Object.entries(savedPolicy.mcp)) addMcp(name, entry || undefined);
    for (const [name, entry] of Object.entries(savedPolicy.local_llm ?? {})) addLocalLlm(name, entry);
    byId('codex-sandbox').value = savedPolicy.codex_sandbox_max ?? 'workspace-write';
    revision = result.revision;
    policyDirty = false;
    policyError = result.error;
    message('policy-load-error', policyError, Boolean(policyError));
    message('provider-policy-error', policyError && text.repairPolicy, Boolean(policyError));
    message('policy-warnings', result.warnings.join('\n'));
  });
}

function readPolicy() {
  const mcp = Object.create(null);
  for (const card of byId('mcp').querySelectorAll('.mcp-entry')) {
    const [name, entry] = card.readEntry();
    if (Object.hasOwn(mcp, name)) throw new Error(text.duplicateMcp);
    mcp[name] = entry;
  }
  const localLlm = Object.create(null);
  for (const row of byId('local-llm').children) {
    const [name, entry] = row.children[0].readEntry();
    if (Object.hasOwn(localLlm, name)) throw new Error(text.duplicateLocalLlm);
    localLlm[name] = entry;
  }
  return {
    ...savedPolicy,
    workspace_roots: Array.from(byId('roots').querySelectorAll('input'), (input) => input.value),
    codex_sandbox_max: byId('codex-sandbox').value,
    mcp,
    local_llm: localLlm,
  };
}

async function savePolicy(policy) {
  const result = await api('policy', { revision, policy });
  revision = result.revision;
  savedPolicy = policy;
  policyError = null;
  message('policy-load-error');
  message('provider-policy-error');
  message('policy-warnings', result.warnings.join('\n'));
}

async function loadHostMcp() {
  const { enabled, catalog, approved } = await api('host-mcp');
  if (closed || !enabled) return;
  byId('host-mcp-card').hidden = false;
  const offers = byId('host-mcp-offers');
  offers.replaceChildren();
  if (catalog.offers.length === 0) {
    const empty = document.createElement('p');
    empty.textContent = text.hostMcpEmpty;
    offers.append(empty);
  }
  for (const offer of catalog.offers) {
    const row = document.createElement('div');
    row.className = 'form';
    const title = document.createElement('strong');
    title.textContent = offer.name;
    const details = document.createElement('p');
    details.className = 'hint host-mcp-details';
    let tools = text.hostMcpToolsUpstream;
    if (offer.name === 'kotoyose') tools = text.hostMcpDelegates(offer.tools);
    else if (offer.all_tools) tools = text.hostMcpAllTools;
    else if (offer.tools.length) tools = text.hostMcpTools(offer.tools);
    const versionChanged = Object.hasOwn(approved, offer.name) && approved[offer.name] !== offer.version;
    details.textContent = [
      offer.description, tools, offer.command ? text.hostMcpCommand(offer.command) : "",
      offer.args && offer.args.length ? text.hostMcpArgs(offer.args) : "",
      offer.cwd ? text.hostMcpCwd(offer.cwd) : "",
      offer.env_names && offer.env_names.length ? text.hostMcpEnvNames(offer.env_names) : "",
      versionChanged ? text.hostMcpVersionChanged : "",
      text.hostMcpFingerprint(catalog.fingerprint), text.hostMcpVersion(offer.version),
    ].filter(Boolean).join(' · ');
    const isApproved = Object.hasOwn(approved, offer.name) && approved[offer.name] === offer.version;
    const label = isApproved ? text.hostMcpRevoke : text.hostMcpApprove;
    const action = button(label, () => operate('host-mcp-message', action, async () => {
      await api('host-mcp', { fingerprint: catalog.fingerprint, name: offer.name, version: offer.version, approve: !isApproved });
      await loadHostMcp();
      message('host-mcp-message', isApproved ? text.hostMcpRevoked : text.hostMcpApproved);
    }));
    row.append(title, details, action);
    offers.append(row);
  }
}

async function loadSettings() {
  await editIdle(async () => {
    const settings = await api('settings');
    if (closed) return;
    settingsRevision = settings.revision;
    byId('idle').value = settings.runner_idle_seconds ?? 600;
    message('idle-message', settings.settings_error, Boolean(settings.settings_error));
    byId('autostart-form').hidden = settings.autostart === null && !settings.autostart_error;
    byId('autostart').disabled = settings.autostart === null;
    byId('autostart').checked = settings.autostart === true;
    byId('open-logs').hidden = !settings.can_open_logs;
    byId('repair-log-hint').hidden = !settings.can_open_logs;
    message('autostart-message', settings.autostart_error, Boolean(settings.autostart_error));
  });
}

async function editIdle(task) {
  if (idleBusy) return;
  idleBusy = true;
  byId('idle-inputs').disabled = true;
  try {
    await task();
  } finally {
    idleBusy = false;
    if (!closed) byId('idle-inputs').disabled = !settingsRevision;
  }
}

async function loadLogs() {
  const logs = await api('logs');
  show('logs', logs.length ? logs.join('\n') : null);
  show('logs-empty', logs.length ? null : text.noLogs);
}

function showMenu(open) {
  byId('sidebar').classList.toggle('is-menu-open', open);
  byId('menu').setAttribute('aria-expanded', String(open));
}

const tabs = [...document.querySelectorAll('button[data-page]')];
function selectPage(tab) {
  if (closed) return;
  const page = tab.dataset.page;
  const changed = page !== activePage;
  activePage = page;
  document.querySelectorAll('.page').forEach((section) => { section.hidden = section.id !== page; });
  for (const item of tabs) {
    if (item === tab) item.setAttribute('aria-current', 'page');
    else item.removeAttribute('aria-current');
  }
  showMenu(false);
  show('update-handoff', page !== 'settings' && appUpdateView?.expected_shutdown
    ? text.updateHandoff(appUpdateView.offer.version) : null);
  updateHeartbeat();
  if (page === 'workspace') {
    if (!policyDirty) operate('policy-message', null, loadPolicy);
    operate('host-mcp-message', null, loadHostMcp);
  }
  if (page === 'providers' && changed) enterProviders();
  if (page === 'settings') {
    operate('idle-message', null, loadSettings);
  }
}

for (const tab of tabs) tab.addEventListener('click', () => selectPage(tab));
byId('menu').onclick = () => showMenu(byId('menu').getAttribute('aria-expanded') !== 'true');
byId('log-panel').addEventListener('toggle', (event) => {
  if (event.currentTarget.open && !closed) operate('logs-message', null, loadLogs);
});

byId('refresh-status').onclick = (event) => operate('status-message', event.currentTarget, refreshStatus);
byId('refresh-logs').onclick = (event) => operate('logs-message', event.currentTarget, loadLogs);
byId('open-logs').onclick = (event) => operate('logs-message', event.currentTarget, () => api('open-logs', {}));
byId('reload-policy').onclick = () => {
  if (!policyDirty || window.confirm(text.confirmReload)) {
    operate('policy-message', null, loadPolicy);
    operate('host-mcp-message', null, loadHostMcp);
  }
};

byId('policy-form').oninput = () => { policyDirty = true; };
byId('policy-form').addEventListener('invalid', (event) => {
  for (let parent = event.target.parentElement; parent; parent = parent.parentElement) {
    if (parent.tagName === 'DETAILS') parent.open = true;
  }
}, true);
byId('add-root').onclick = () => {
  addText(byId('roots'), text.absolutePath, '', { required: true, mono: true }).focus();
  policyDirty = true;
};
byId('add-mcp').onclick = () => {
  addMcp().focus();
  policyDirty = true;
};
byId('add-local-llm').onclick = () => {
  addLocalLlm().focus();
  policyDirty = true;
};

byId('enroll-form').onsubmit = (event) => {
  event.preventDefault();
  operate('enroll-message', event.submitter, async () => {
    const interrupt = await confirmInterruption();
    if (interrupt === null) return;
    const code = byId('enroll-code');
    message('enroll-message', text.enrolling);
    try {
      const result = await api('enroll', { code: code.value, name: byId('child-name').value, interrupt });
      message('enroll-message', result.message);
      await statusRequest?.catch(() => {});
      await refreshStatus();
      if (activePage === 'connection' && !enrollmentNeeded) {
        selectPage(tabs.find((tab) => tab.dataset.page === 'providers'));
      }
    } finally {
      code.value = '';
    }
  });
};
byId('policy-form').onsubmit = (event) => {
  event.preventDefault();
  operate('policy-message', null, () => editPolicy(async () => {
    await savePolicy(readPolicy());
    policyDirty = false;
    message('policy-message', text.policySaved);
  }));
};
byId('idle-form').onsubmit = (event) => {
  event.preventDefault();
  operate('idle-message', null, () => editIdle(async () => {
    const result = await api('settings', {
      revision: settingsRevision,
      runner_idle_seconds: byId('idle').valueAsNumber,
    });
    settingsRevision = result.revision;
    message('idle-message', text.idleSaved);
  }));
};
byId('autostart').onchange = (event) => {
  const checkbox = event.currentTarget;
  const enabled = checkbox.checked;
  operate('autostart-message', checkbox, async () => {
    try {
      message('autostart-message', (await api('autostart', { enabled })).message);
    } catch (error) {
      checkbox.checked = !enabled;
      throw error;
    }
  });
};

byId('update-start').onclick = async (event) => {
  const { offer, retry_after } = appUpdateView ?? {};
  if (!offer || appUpdateRequestPending || retry_after) return;
  const button = event.currentTarget;
  button.disabled = true;
  appUpdateRequestPending = true;
  try {
    await watchUpdate({ action: 'download', sha256: offer.sha256, defer: byId('update-defer').checked });
  } finally {
    appUpdateRequestPending = false;
    if (!closed) button.disabled = !!appUpdateView?.retry_after;
  }
};

byId('update-interrupt').onclick = (event) => operate('update-message', event.currentTarget, async () => {
  const link = await refreshStatus();
  if (!link.work || !window.confirm(text.exitInterrupt(link.work))) return;
  await api('update', { action: 'interrupt' });
});

for (const id of ['restart', 'stop']) {
  byId(id).onclick = (event) => operate('action-message', event.currentTarget, async () => {
    const interrupt = await confirmInterruption();
    if (interrupt === null) return;
    const appliesUpdate = id === 'restart' && appUpdateView?.phase === 'deferred';
    const result = await api('action', { restart: id === 'restart', interrupt });
    message('action-message', result.message);
    if (appliesUpdate) watchUpdate();
    else closeSession();
  });
}

const sessionReady = openSession();
document.addEventListener('visibilitychange', updateHeartbeat);
sessionReady.then(updateHeartbeat);
