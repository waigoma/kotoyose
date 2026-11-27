'use strict';

let componentsBusy = false;
let componentsLoading = false;
let repairPollPending = false;
let providerVisit = 0;
const providerCards = new Map();
const declarationCards = new Map();
let componentView = { providers: [], installed: [], needs_reinstall: [] };
let declarationView = { offers: [] };
const loginStates = new Map();
const startingLogins = new Set();
const cancellingLogins = new Set();
const pollingLogins = new Set();
const loginStatusText = {
  checking: text.loading,
  logged_in: text.loggedIn,
  logged_out: text.loggedOut,
  running: text.loginStarting,
  cancelled: text.loginCancelled,
  unavailable: text.loginUnavailable,
  failed: text.loginFailed,
  unknown: text.loginUnknown,
};

function element(tag, className, label) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (label) node.textContent = label;
  return node;
}

function checkBox(label, onChange) {
  const input = element('input');
  input.type = 'checkbox';
  input.onchange = () => onChange(input.checked);
  const wrapper = element('label', 'check');
  wrapper.append(input, label);
  return { input, wrapper };
}

function matchesLogin(provider, name) {
  const key = name.toLowerCase();
  return key === (provider.provider ?? provider.id) || provider.aliases.includes(key);
}

function updateProviderPolicyControls() {
  for (const { provider, adapter, quota, unavailable } of providerCards.values()) {
    adapter.checked = savedPolicy?.adapters.includes(provider.driver) ?? false;
    quota.checked = savedPolicy?.logins.some((name) => matchesLogin(provider, name)) ?? false;
    const canUse = availableAdapters.includes(provider.driver);
    const readonly = closed || !revision || policyBusy || Boolean(policyError);
    adapter.disabled = readonly || (!adapter.checked && !canUse);
    quota.disabled = readonly;
    unavailable.hidden = provider.packages.length > 0 && (!revision || canUse);
  }
  for (const entry of declarationCards.values()) {
    // The next-step button depends on the saved policy (Use on this child / In use).
    entry.refreshPrimary?.();
    const { provider, adapter } = entry;
    if (!adapter) continue;
    adapter.checked = savedPolicy?.adapters.includes(provider.driver) ?? false;
    adapter.disabled = closed || !revision || policyBusy || Boolean(policyError)
      || (!adapter.checked && !availableAdapters.includes(provider.driver));
  }
}

function changeProviderPolicy(provider, key, enable) {
  if (closed || !savedPolicy || policyError || policyBusy) return;
  const adapter = key === 'adapters';
  const matches = (name) => adapter ? name === provider.driver : matchesLogin(provider, name);
  const entries = savedPolicy[key].filter((name) => !matches(name));
  if (enable) entries.push(adapter ? provider.driver : (provider.provider ?? provider.id));
  operate('provider-policy-message', null, () => editPolicy(async () => {
    await savePolicy({ ...savedPolicy, [key]: entries });
    message('provider-policy-message', text.policySaved);
  }));
}

function renderPackageDetails(provider, installed, versions, canManage, needsRepair) {
  const details = element('details');
  const summary = versions.length ? text.packageSummary(versions.join(' · ')) : text.packageDetails;
  const packages = element('ul', 'component-details');
  for (const pkg of provider.packages) {
    packages.append(element('li', '', `${pkg} · ${installed?.packages[pkg] ?? text.notInstalled}`));
  }
  if (versions.length && !installed.sdk_importable) {
    packages.append(element('li', '', text.sdkUnavailable));
  }
  const controls = element('div', 'actions');
  if (canManage && versions.length) {
    if (!needsRepair) controls.append(button(text.update, () => changeComponent(provider.id, 'install')));
    controls.append(button(text.remove, () => changeComponent(provider.id, 'remove')));
  }
  details.append(element('summary', '', summary), packages, controls);
  return details;
}

function cliChoice(provider, installed) {
  const choice = checkBox(text.useInstalledCli, (checked) => {
    choice.input.checked = !checked;
    changeComponent(provider.id, { use_cli: checked ? 'system' : 'bundled' });
  });
  choice.input.checked = installed.choice === 'system';
  choice.input.disabled = !installed.system_cli && !choice.input.checked;
  const bundled = provider.packages.length
    ? (installed.bundled_cli ? text.bundledCli : text.bundledMissing)
    : (installed.bundled_cli ? text.managedCli : text.managedCliMissing);
  const system = installed.system_cli
    ? [text.cliFound, installed.system_version].filter(Boolean).join(' · ')
    : text.noSystemCli;
  const info = element('p', 'hint', `${bundled} · ${system}`);
  const field = element('div', 'form');
  field.append(choice.wrapper, info);
  return field;
}

function renderManagedCli(provider, installed) {
  const details = element('details');
  details.append(element('summary', '', text.managedCli));
  if (!installed?.managed_available) {
    details.append(element('p', 'hint', text.cliPlatformUnavailable));
    return details;
  }
  details.append(element('p', 'hint', provider.managed.version));
  if (installed.bundled_cli) {
    const controls = element('div', 'actions');
    controls.append(button(text.remove, () => changeComponent(provider.id, 'remove')));
    details.append(controls);
  }
  return details;
}

function primaryInstall(provider, status, label, hint) {
  const control = button(label, () => changeComponent(provider.id, 'install'));
  control.className += ' btn-primary';
  const line = element('div', 'status-row');
  line.append(element('span', 'row-status', status), control);
  const action = element('div', 'form');
  action.append(line, element('p', 'hint', hint));
  return action;
}

function operationPanel() {
  const panel = element('div', 'form component-operation');
  const result = element('p', 'component-result');
  result.setAttribute('role', 'status');
  const details = element('details', 'component-output');
  details.hidden = true;
  const summary = element('summary', '', text.componentOutput);
  const preview = element('span', 'component-output-preview');
  summary.append(preview);
  const log = element('pre', 'component-output-log');
  log.tabIndex = 0;
  details.append(summary, log);
  panel.append(result, details);
  return { panel, result, details, preview, log };
}

function setOperationResult(operation, ok, reason) {
  operation.result.textContent = ok ? text.componentSuccess(reason) : text.componentFailure(reason);
  operation.result.toggleAttribute('data-error', !ok);
}

function showAutoRepair(operation, phase) {
  if (operation.autoRepair === phase) return;
  operation.autoRepair = phase;
  if (!phase && !operation.showingAutoRepair) return;
  operation.showingAutoRepair = Boolean(phase);
  operation.result.textContent = {
    pending: text.autoRepairPending,
    installing: text.autoReinstallRunning,
    done: text.autoRepairDone,
    failed: text.autoRepairFailed,
  }[phase] ?? '';
  operation.result.toggleAttribute('data-error', phase === 'failed');
}

function autoRepairsPending() {
  return Object.values(componentView.auto_repair ?? {}).some((phase) => phase === 'pending' || phase === 'installing');
}

async function applyCardChange(operation, task) {
  operation.showingAutoRepair = false;
  operation.result.textContent = text.applyingComponent;
  operation.result.removeAttribute('data-error');
  operation.details.open = false;
  operation.details.hidden = true;
  operation.preview.textContent = '';
  operation.log.textContent = '';
  try {
    const { ok, message: reason } = await task();
    setOperationResult(operation, ok, reason);
    return ok;
  } catch (error) {
    setOperationResult(operation, false, error.message);
    return false;
  }
}

function namesOf(provider) {
  return [provider.id, provider.provider, provider.profile, ...(provider.aliases || [])].filter(Boolean);
}

function offerMatches(offer, provider) {
  const names = namesOf(provider);
  return names.includes(offer.id) || (offer.profile_id && names.includes(offer.profile_id));
}

function unavailableOnThisChild(provider, probe) {
  if (!probe) return false;
  if (provider.packages.length > 0) return !probe.turns_supported;
  return !probe.managed_available;
}

function primaryButton(label, action) {
  const control = button(label, action);
  control.className += ' btn-primary';
  return control;
}

function declarationStep(offer) {
  if (offer.status !== 'approved') {
    if (offer.capability === 'unrestricted') return 'review-unrestricted';
    if (['read', 'write', 'mcp'].includes(offer.capability)) return 'review';
    return null;
  }
  // A data provider's policy adapter is its declaration id (see renderDeclarationCard), not its driver.
  const enabled = savedPolicy?.adapters.includes(offer.id) ?? false;
  if (!offer.installed && offer.can_install) return 'install';
  if (offer.installed && offer.local_login) {
    const state = loginStates.get(offer.id)?.state;
    if (state === 'logged_in') return enabled ? null : 'use';
    if (['logged_out', 'failed', 'cancelled', 'unknown'].includes(state)) return 'login';
    return null;
  }
  if (offer.installed) return enabled ? null : 'use';
  return null;
}

function renderDeclarationPrimary(entry, offer) {
  if (entry.confirming) return;
  const primary = entry.primary;
  primary.replaceChildren();
  entry.loginButton = null;
  const endpoint = `provider-declarations/${offer.id}`;
  const step = declarationStep(offer);
  const hint = offer.capability && !(savedPolicy?.adapters.includes(offer.id) ?? false)
    ? text.declarationStep(step) || (offer.status === 'approved' && offer.installed && offer.local_login
      ? text.declarationCheckingLogin : '') : '';
  if (hint) primary.append(textLine('p', 'hint declaration-step', hint));
  if (step === 'review') {
    primary.append(primaryButton(text.reviewApproval, () => entry.run(
      () => api(endpoint, { sha256: offer.sha256, capability: 'read' }), text.declarationApproved)));
  } else if (step === 'review-unrestricted') {
    let nonce;
    const reviewPanel = element('div', 'unrestricted-review');
    const warning = textLine('p', 'unrestricted-warning', '');
    warning.hidden = true;
    const confirm = checkBox(text.unrestrictedConfirm(offer.display_name || offer.id), (checked) => {
      approval.disabled = !nonce || !checked;
    });
    const reset = () => {
      nonce = undefined;
      reviewPanel.hidden = true;
      warning.hidden = true;
      confirm.wrapper.hidden = true;
      confirm.input.checked = false;
      approval.hidden = true;
      approval.disabled = true;
      approval.className = approval.className.replace(' btn-primary', '');
      if (!review.className.includes('btn-primary')) review.className += ' btn-primary';
      entry.confirming = false;
    };
    const review = primaryButton(text.reviewUnrestrictedApproval, () => entry.run(async () => {
      reset();
      const challenge = await api(`${endpoint}/confirmation`, { sha256: offer.sha256 });
      entry.confirming = true;
      reviewPanel.hidden = false;
      nonce = challenge.nonce;
      warning.textContent = challenge.warning;
      warning.hidden = false;
      confirm.wrapper.hidden = false;
      approval.hidden = false;
      review.className = review.className.replace(' btn-primary', '');
      if (!approval.className.includes('btn-primary')) approval.className += ' btn-primary';
    }, text.confirmWithin, { reload: false }));
    const approval = button(text.approveUnrestricted, () => {
      if (!nonce || !confirm.input.checked) return undefined;
      return entry.run(() => {
        const confirmation = nonce;
        reset();
        return api(endpoint, { sha256: offer.sha256, capability: 'unrestricted', nonce: confirmation });
      }, text.declarationApproved);
    });
    const reviewActions = element('div', 'actions');
    reviewActions.append(approval, button(text.cancelLogin, reset));
    reviewPanel.append(warning, confirm.wrapper, reviewActions);
    reset();
    primary.append(review, reviewPanel);
  } else if (step === 'install') {
    primary.append(primaryButton(text.install, () => entry.run(
      () => api(`${endpoint}/install`, { sha256: offer.sha256 }), text.declarationInstalled)));
  } else if (step === 'login') {
    entry.loginButton = primaryButton(text.logIn, () => startLogin(offer.id));
    primary.append(entry.loginButton);
  } else if (step === 'use') {
    primary.append(primaryButton(text.useOnChild, () => changeProviderPolicy(entry.provider, 'adapters', true)));
  } else if (offer.status === 'approved' && (savedPolicy?.adapters.includes(offer.id) ?? false)) {
    primary.append(
      textLine('span', 'status-ok', text.inUseOnChild),
      button(text.stopUsingOnChild, () => changeProviderPolicy(entry.provider, 'adapters', false)),
    );
  }
  primary.disabled = closed || Boolean(entry.operation.busy);
}

function renderDeclarationCard(offer, payload, previous) {
  const operation = previous && previous.sha256 === offer.sha256 ? previous.operation : operationPanel();
  const card = element('article', 'card provider-card');
  const mark = element('span', 'provider-mark is-cli', (offer.display_name || offer.id || '?').slice(0, 1).toUpperCase());
  mark.setAttribute('aria-hidden', 'true');
  const title = element('div', 'provider-title');
  title.append(element('h2', 'card-title', offer.display_name || offer.id || ''), element('span', 'row-sub mono', offer.driver || offer.id || ''));
  const heading = element('div', 'provider-heading');
  heading.append(mark, title);
  const header = element('div', 'card-head');
  header.append(heading);
  const content = element('div', 'card-body form');
  const primary = element('fieldset', 'actions');
  const details = element('details');
  details.open = previous?.details?.open ?? false;
  const info = element('div', 'form');
  const offersWrite = offer.capability === 'write' || offer.capability === 'mcp';
  info.append(
    textLine('p', '', offer.publisher || ''),
    textLine('p', 'hint', text.hostFingerprint(payload.host_fingerprint || '')),
    textLine('p', 'hint', text.declarationSha(offer.sha256 || '')),
    textLine('p', '', text.declarationStatus(offer.status || 'pending')),
    textLine('p', '', text.declarationPlatform(offer.platform || '')),
    textLine('p', '', text.declarationBinary(offer.binary || '')),
    textLine('p', '', text.declarationVersion(offer.version || '')),
    textLine('p', '', offer.rights || ''),
  );
  if (offer.approved_capability) info.append(textLine('p', '', text.approvedCapability(offer.approved_capability)));
  if (offersWrite) info.append(textLine('p', '', text.declarationWriteRights));
  if (offer.capability === 'mcp') info.append(textLine('p', '', text.declarationMcpRights));
  for (const artifact of offer.artifacts || []) {
    info.append(textLine('p', '', `${artifact.platform || ''} ${artifact.url || ''} ${artifact.sha256 || ''}`));
  }
  const network = offer.network || {};
  info.append(textLine('p', '', `download ${((network.download_hosts) || []).join(' ')} login ${((network.login_hosts) || []).join(' ')} runtime ${((network.runtime_hosts) || []).join(' ')}`));
  info.append(textLine('p', '', `output_guard_env ${(offer.output_guard_env || []).join(' ')}`));
  const commands = element('ul');
  for (const command of offer.commands || []) {
    const argv = Array.isArray(command.argv) ? command.argv.join(' ') : '';
    commands.append(textLine('li', '', `${command.operation || ''}: ${argv}`));
  }
  info.append(element('h4', '', text.commandsHeading), commands);
  const actions = element('fieldset', 'actions');
  const provider = {
    // The policy adapter for a data provider is its declaration id (the host advertises it so), not the driver.
    id: offer.id, driver: offer.id, aliases: [],
    local_login: Boolean(offer.installed && offer.local_login),
  };
  const entry = { provider, operation, actions, primary, sha256: offer.sha256, details, declaration: true };
  const endpoint = `provider-declarations/${offer.id}`;
  entry.run = async (request, reason, { reload = true } = {}) => {
    if (operation.busy || closed) return;
    operation.busy = true;
    actions.disabled = true;
    primary.disabled = true;
    try {
      const ok = await applyCardChange(operation, async () => {
        await request();
        return { ok: true, message: reason };
      });
      if (ok && reload) {
        await loadHostDeclarations();
        if (declarationCards.get(offer.id)?.provider.local_login) checkLogin(offer.id, providerVisit);
      }
    } finally {
      operation.busy = false;
      const current = declarationCards.get(offer.id);
      if (current?.operation === operation) {
        current.actions.disabled = closed;
        current.primary.disabled = closed;
      }
    }
  };
  const approve = (label, capability) => button(label, () => entry.run(
    () => api(endpoint, { sha256: offer.sha256, capability }), text.declarationApproved));
  if (offer.status === 'approved') {
    actions.append(button(text.revoke, () => entry.run(async () => {
      await api(endpoint, undefined, 'DELETE');
      loginStates.delete(offer.id);
    }, text.declarationRevoked)));
    if (offersWrite && offer.approved_capability === 'read') actions.append(approve(text.approveWrite, 'write'));
    if (offer.capability === 'mcp' && ['read', 'write'].includes(offer.approved_capability)) {
      actions.append(approve(text.approveMcp, 'mcp'));
    }
  }
  details.append(element('summary', '', text.declarationDetails), info, actions);
  content.append(primary);
  if (provider.local_login) {
    const login = element('div', 'form');
    const adapter = checkBox(text.useOnChild, (enable) => changeProviderPolicy(provider, 'adapters', enable));
    Object.assign(entry, { login, adapter: adapter.input, cliAvailable: true });
    renderLogin(entry);
    details.append(adapter.wrapper);
    content.append(login);
  }
  entry.refreshPrimary = () => renderDeclarationPrimary(entry, offer);
  entry.refreshPrimary();
  content.append(details, operation.panel);
  card.append(header, content);
  actions.disabled = closed || Boolean(operation.busy);
  return { card, entry };
}

function unavailableGroup(providers) {
  const group = element('details', 'unavailable-providers');
  group.open = false;
  group.append(element('summary', '', text.notAvailableOnChild));
  const list = element('ul', 'component-details');
  for (const provider of providers) {
    const reason = provider.reason || (provider.packages?.length ? text.childTurnsUnavailable : text.cliPlatformUnavailable);
    list.append(element('li', '', `${provider.name} — ${reason}`));
  }
  group.append(list);
  return group;
}

function renderProviders(payload) {
  componentView = payload || componentView;
  renderProviderPage();
}

function renderProviderPage() {
  const { providers = [], installed = [], can_manage_packages: canManage, needs_reinstall: needsReinstall = [], auto_repair: repairs = {}, error } = componentView;
  const container = byId('component-rows');
  const previousCards = new Map(providerCards);
  const previousDeclarations = new Map(declarationCards);
  container.replaceChildren();
  providerCards.clear();
  declarationCards.clear();
  message('components-warning', error || (canManage ? '' : text.packageUnavailable));
  const offers = Array.isArray(declarationView.offers) ? declarationView.offers : [];
  const usedOffers = new Set();
  const unavailable = [];

  for (const provider of providers) {
    const offer = offers.find((item) => !usedOffers.has(item) && offerMatches(item, provider));
    if (offer) {
      usedOffers.add(offer);
      if (!offer.capability && offer.status !== 'approved') {
        unavailable.push({ name: offer.display_name || offer.id, reason: text.declarationPlatformUnavailable(offer.platform || '') });
        continue;
      }
      const built = renderDeclarationCard(offer, declarationView, previousDeclarations.get(offer.id));
      container.append(built.card);
      declarationCards.set(offer.id, built.entry);
      continue;
    }
    const probe = installed.find((row) => row.id === provider.id);
    if (unavailableOnThisChild(provider, probe)) {
      if (!offers.some((offer) => offer.display_name?.trim().toLowerCase() === provider.name.trim().toLowerCase())) {
        unavailable.push(provider);
      }
      continue;
    }
    const previous = previousCards.get(provider.id);
    const operation = previous?.operation ?? operationPanel();
    showAutoRepair(operation, repairs[provider.id]);
    const row = element('article', 'card provider-card');
    const mark = element('span', `provider-mark is-${provider.tone}`, provider.initial);
    mark.setAttribute('aria-hidden', 'true');
    const title = element('div', 'provider-title');
    title.append(element('h2', 'card-title', provider.name), element('span', 'row-sub mono', provider.driver));
    const heading = element('div', 'provider-heading');
    heading.append(mark, title);
    const header = element('div', 'card-head');
    header.append(heading);

    const login = element('div', 'form');
    const sdk = provider.packages.length > 0;
    const checks = element('div', 'checks');
    const adapter = checkBox(text.useOnChild, (enabled) => changeProviderPolicy(provider, 'adapters', enabled));
    adapter.wrapper.title = text.adapterHint;
    const quota = checkBox(text.showQuota, (enabled) => changeProviderPolicy(provider, 'logins', enabled));
    quota.wrapper.title = text.quotaHint;
    if (sdk) checks.append(adapter.wrapper);
    if (provider.quota) checks.append(quota.wrapper);
    const availability = element('p', 'hint', sdk ? text.adapterUnavailable : text.childTurnsUnavailable);
    const versions = provider.packages.map((pkg) => probe?.packages[pkg]).filter(Boolean);
    const needsRepair = needsReinstall.includes(provider.id)
      || (versions.length > 0 && (versions.length < provider.packages.length || !probe.sdk_importable));
    const details = sdk
      ? renderPackageDetails(provider, probe, versions, canManage, needsRepair)
      : renderManagedCli(provider, probe);
    details.open = previous?.details.open ?? false;
    const runtime = element('fieldset', 'form');
    if (sdk && canManage && (needsRepair || (probe && !versions.length))) {
      runtime.append(primaryInstall(provider,
        needsRepair ? text.needsReinstall : text.notInstalled,
        needsRepair ? text.repair : text.install,
        text.installPackagesHint(provider.packages.join(', '))));
    } else if (!sdk && probe?.managed_available && !probe.bundled_cli) {
      runtime.append(primaryInstall(provider, text.managedCliMissing, text.install,
        text.installManagedCliHint(provider.name, provider.managed.version)));
    }
    if (probe && (sdk || probe.managed_available || provider.local_login)) runtime.append(cliChoice(provider, probe));
    runtime.append(details);

    const body = element('div', 'card-body form');
    body.append(login, checks, availability, runtime, operation.panel);
    row.append(header, body);
    container.append(row);
    const cliAvailable = probe?.choice === 'system' ? probe.system_cli : probe?.bundled_cli;
    const card = {
      provider, login, details, runtime, cliAvailable, operation,
      adapter: adapter.input, quota: quota.input, unavailable: availability,
    };
    providerCards.set(provider.id, card);
    renderLogin(card);
  }
  for (const offer of offers) {
    if (usedOffers.has(offer)) continue;
    if (!offer.capability && offer.status !== 'approved') {
      unavailable.push({ name: offer.display_name || offer.id, reason: text.declarationPlatformUnavailable(offer.platform || '') });
      continue;
    }
    const built = renderDeclarationCard(offer, declarationView, previousDeclarations.get(offer.id));
    container.append(built.card);
    declarationCards.set(offer.id, built.entry);
  }
  if (unavailable.length) container.append(unavailableGroup(unavailable));
  updateProviderPolicyControls();
  updateProviderRuntimeControls();
}

function updateProviderRuntimeControls() {
  const loginBusy = startingLogins.size > 0 || [...loginStates.values()].some((view) => view.state === 'running');
  const changing = closed || componentsBusy || componentsLoading || autoRepairsPending();
  for (const { provider, runtime, loginButton, cancelButton } of [...providerCards.values(), ...declarationCards.values()]) {
    if (runtime) runtime.disabled = changing || loginBusy;
    if (loginButton) loginButton.disabled = changing || startingLogins.has(provider.id);
    if (cancelButton) cancelButton.disabled = closed || cancellingLogins.has(provider.id);
  }
  byId('refresh-components').disabled = changing;
}

function renderLogin(card) {
  const { provider, login } = card;
  const view = loginStates.get(provider.id) ?? { state: 'checking' };
  card.loginButton = null;
  card.cancelButton = null;
  if (!provider.local_login || view.state === 'unsupported') {
    login.replaceChildren();
    if (provider.login) {
      const { cli_name: name, command } = provider.login;
      login.append(element('p', 'hint', text.loginInstructions(name, command.join(' '))));
    }
    return;
  }
  const dot = element('span', view.state === 'logged_in' ? 'dot positive' : 'dot');
  dot.setAttribute('aria-hidden', 'true');
  const status = element('div', 'row-status');
  const statusLabel = view.state === 'unknown' && !card.cliAvailable ? text.loginUnavailable : loginStatusText[view.state];
  status.append(dot, element('span', '', statusLabel));
  const line = element('div', 'status-row');
  line.append(status);
  const signedInState = ['logged_in', 'logged_out'].includes(view.state);
  if (!card.declaration && (signedInState || (['unknown', 'failed', 'cancelled'].includes(view.state) && card.cliAvailable))) {
    card.loginButton = button(text.logIn, () => startLogin(provider.id));
    if (view.state === 'logged_out') card.loginButton.className += ' btn-primary';
    line.append(card.loginButton);
  }
  if (view.state === 'running') {
    card.cancelButton = button(text.cancelLogin, () => cancelLogin(provider.id));
    line.append(card.cancelButton);
  }
  login.replaceChildren(line);
  if (view.state !== 'running' || !view.url) return;

  const link = element('a', 'login-link', text.loginLink);
  link.href = view.url;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  login.append(link);
  if (view.device_code) login.append(element('p', 'hint', text.loginDeviceCode(view.device_code)));
  if (view.needs_code) login.append(authorizationForm(provider.id));
}

function authorizationForm(provider) {
  const form = element('form', 'input-row login-form');
  form.autocomplete = 'off';
  const code = field(text.authorizationCode, '', { password: true, required: true });
  code.input.maxLength = 512;
  const submit = element('button', 'btn btn-sm', text.submitCode);
  form.append(code.label, submit);
  form.onsubmit = (event) => {
    event.preventDefault();
    operate('components-message', submit, async () => {
      try {
        await api(`login/${provider}/code`, { code: code.input.value.trim() });
      } finally {
        code.input.value = '';
      }
    });
  };
  return form;
}

function setLogin(provider, view) {
  if (JSON.stringify(loginStates.get(provider)) === JSON.stringify(view)) return;
  loginStates.set(provider, view);
  const card = providerCards.get(provider) || declarationCards.get(provider);
  if (card?.login) renderLogin(card);
  if (card?.refreshPrimary) card.refreshPrimary();
  updateProviderRuntimeControls();
}

async function watchLogin(provider) {
  if (pollingLogins.has(provider)) return;
  pollingLogins.add(provider);
  try {
    while (!closed && loginStates.get(provider)?.state === 'running') {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      const previous = loginStates.get(provider);
      if (closed || previous?.state !== 'running' || cancellingLogins.has(provider)) continue;
      const view = await api(`login/${provider}`);
      if (loginStates.get(provider) === previous && !cancellingLogins.has(provider)) setLogin(provider, view);
    }
  } catch (error) {
    setLogin(provider, { state: 'failed' });
    message('components-message', error.message, true);
  } finally {
    pollingLogins.delete(provider);
  }
}

function startLogin(provider) {
  if (componentsBusy || componentsLoading || closed || startingLogins.has(provider)) return;
  startingLogins.add(provider);
  updateProviderRuntimeControls();
  operate('components-message', null, async () => {
    try {
      setLogin(provider, await api(`login/${provider}`, {}));
      watchLogin(provider);
    } catch (error) {
      setLogin(provider, { state: 'failed' });
      throw error;
    } finally {
      startingLogins.delete(provider);
      updateProviderRuntimeControls();
    }
  });
}

function cancelLogin(provider) {
  if (closed || cancellingLogins.has(provider)) return;
  cancellingLogins.add(provider);
  updateProviderRuntimeControls();
  operate('components-message', null, async () => {
    try {
      setLogin(provider, await api(`login/${provider}`, undefined, 'DELETE'));
    } finally {
      cancellingLogins.delete(provider);
      updateProviderRuntimeControls();
    }
  });
}

async function checkLogin(provider, visit) {
  if (pollingLogins.has(provider) || startingLogins.has(provider)) return;
  setLogin(provider, { state: 'checking' });
  try {
    const view = await api(`login/status/${provider}`, {});
    if (visit !== providerVisit) return;
    setLogin(provider, view);
    if (view.state === 'running') watchLogin(provider);
  } catch (error) {
    if (visit !== providerVisit) return;
    setLogin(provider, { state: 'failed' });
    message('components-message', error.message, true);
  }
}

async function loadComponents(endpoint = 'components') {
  renderProviders(await api(endpoint));
}

function refreshComponents(endpoint) {
  if (componentsBusy || componentsLoading) return;
  componentsLoading = true;
  updateProviderRuntimeControls();
  return operate('components-message', null, async () => {
    try {
      message('components-message', text.checkingComponents);
      await loadComponents(endpoint);
      message('components-message');
    } finally {
      componentsLoading = false;
      updateProviderRuntimeControls();
    }
  });
}

function watchAutoRepairs() {
  if (closed || activePage !== 'providers' || repairPollPending || !autoRepairsPending()) return;
  repairPollPending = true;
  setTimeout(async () => {
    repairPollPending = false;
    if (closed || activePage !== 'providers') return;
    await refreshComponents();
    if (closed || activePage !== 'providers') return;
    if (autoRepairsPending()) {
      watchAutoRepairs();
    } else {
      await operate('provider-policy-message', null, refreshProviderPolicy);
      loadProviderLogins(providerVisit);
    }
  }, 1000);
}

function textLine(tag, className, value) {
  const node = element(tag, className);
  node.textContent = value || '';
  return node;
}

function renderHostDeclarations(payload) {
  declarationView = payload && typeof payload === 'object' ? payload : { offers: [] };
  const host = document.getElementById('host-declarations');
  if (host) {
    host.replaceChildren();
    const offers = Array.isArray(declarationView.offers) ? declarationView.offers : [];
    if (declarationView.error) {
      const note = textLine('p', 'hint', declarationView.error);
      note.setAttribute('role', 'status');
      note.setAttribute('data-error', '');
      host.append(note);
    } else if (!offers.length) {
      host.append(textLine('p', 'hint', text.noDeclarations));
    }
  }
  renderProviderPage();
}

async function loadHostDeclarations() {
  try {
    renderHostDeclarations(await api('provider-declarations'));
  } catch (error) {
    message('declaration-message', error.message || text.declarationLoadFailed, true);
    return;
  }
  message('declaration-message');
  if ([...declarationCards.values()].some((card) => card.adapter) && revision) {
    await operate('provider-policy-message', null, async () => {
      availableAdapters = (await api('policy')).available_adapters;
      updateProviderPolicyControls();
    });
  }
}

async function enterProviders() {
  const visit = ++providerVisit;
  if (!policyDirty) operate('provider-policy-message', null, loadPolicy);
  await refreshComponents();
  await loadHostDeclarations();
  if (closed || activePage !== 'providers' || visit !== providerVisit) return;
  if (autoRepairsPending()) watchAutoRepairs();
  else loadProviderLogins(visit);
}

function loadProviderLogins(visit) {
  for (const { provider } of [...providerCards.values(), ...declarationCards.values()]) {
    if (provider.local_login && (declarationCards.has(provider.id) || provider.login.status.length)) checkLogin(provider.id, visit);
    else if (provider.local_login) setLogin(provider.id, { state: 'unknown' });
  }
}

async function refreshProviderPolicy() {
  if (policyDirty) {
    availableAdapters = (await api('policy')).available_adapters;
    updateProviderPolicyControls();
  } else {
    await loadPolicy();
  }
}

async function streamComponentChange(operation, provider, change, interrupt) {
  const response = await request('components', { provider, change, interrupt }, true);
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  const lines = [];
  let pending = '';
  let result;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    pending += value;
    const events = pending.split('\n');
    pending = events.pop();
    for (const line of events) {
      const event = JSON.parse(line);
      if (event.event === 'result') result = event;
      else if (event.event === 'progress') lines.push(event.message);
    }
    if (lines.length > 100) lines.splice(0, lines.length - 100);
    operation.details.hidden = lines.length === 0;
    operation.preview.textContent = lines.slice(-5).join('\n');
    operation.log.textContent = lines.join('\n');
    operation.log.scrollTop = operation.log.scrollHeight;
  }
  if (!result) throw new Error(text.progressEnded);
  return result;
}

async function changeComponent(provider, change) {
  if (componentsBusy || componentsLoading || closed) return;
  const operation = providerCards.get(provider).operation;
  componentsBusy = true;
  updateProviderRuntimeControls();
  try {
    const interrupt = await confirmInterruption();
    if (interrupt === null) return;
    if (await applyCardChange(operation, () => streamComponentChange(operation, provider, change, interrupt))) {
      setLogin(provider, { state: 'unknown' });
    }
    if (closed) return;
    await operate('components-message', null, async () => {
      await loadComponents();
      await refreshProviderPolicy();
    });
  } catch (error) {
    setOperationResult(operation, false, error.message);
  } finally {
    componentsBusy = false;
    updateProviderRuntimeControls();
  }
}

byId('refresh-components').onclick = () => refreshComponents('components/check');
