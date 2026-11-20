// Machine: read-only snapshot of host execution settings (drivers, cells, clients, rooms capacity, policy).

import { api } from "../api.js";
import { dot, el, errorLine, pathText, rowMeta, responsiveTable } from "../dom.js";
import { bytesLabel, count } from "../format.js";

function statCard(label, value, sub) {
  return el("section", { class: "card stat-card" },
    el("span", { class: "stat-label" }, label),
    el("span", { class: "stat-value" }, value),
    sub ? el("span", { class: "stat-sub" }, sub) : null);
}

function summaryGrid(machine) {
  const container = machine.agent_container;
  const sanctionText = machine.sanction_enforcement || "off";
  const roomsText = container ? `${count(container.max_containers)} max` : "Off";
  const roomsSub = container
    ? rowMeta(bytesLabel(container.workspace_memory_bytes), `${container.cpus} CPUs`, `${count(container.pids_limit)} PIDs`)
    : "No agent_container configured";
  return el("div", { class: "stat-grid" },
    statCard("Execution config", machine.enabled ? "Active" : "Disabled",
      machine.native_worker ? `worker: ${machine.native_worker}` : "No native worker"),
    statCard("Sanction enforcement", sanctionText,
      sanctionText === "all" ? "Blocks agent & chat" : sanctionText === "agent" ? "Blocks agent only" : "Warn only"),
    statCard("Rooms capacity", roomsText, roomsSub));
}

function roomsCard(machine) {
  const c = machine.agent_container;
  return el("section", { class: "card", "aria-label": "Rooms capacity" },
    el("div", { class: "card-head" },
      el("h2", { class: "card-title" }, "Agent rooms capacity"),
      el("span", { class: "mono small muted" }, c ? `${count(c.max_containers)} containers` : "Disabled")),
    el("div", { class: "card-body" },
      !c
        ? el("p", { class: "muted" }, "Agent container rooms are not configured on this machine.")
        : el("dl", { class: "kv" },
          el("dt", {}, "Max containers"),
          el("dd", { class: "mono" }, count(c.max_containers)),
          el("dt", {}, "Workspace memory"),
          el("dd", { class: "mono" }, bytesLabel(c.workspace_memory_bytes)),
          el("dt", {}, "CPUs"),
          el("dd", { class: "mono" }, String(c.cpus)),
          el("dt", {}, "PIDs limit"),
          el("dd", { class: "mono" }, count(c.pids_limit)),
          el("dt", {}, "Min free disk"),
          el("dd", { class: "mono" }, bytesLabel(c.min_free_disk_bytes)),
          el("dt", {}, "Agent root"),
          el("dd", {}, pathText(c.agent_root)),
          el("dt", {}, "Home root"),
          el("dd", {}, pathText(c.home_root)),
          el("dt", {}, "Socket root"),
          el("dd", {}, pathText(c.egress_socket_root)))));
}

function driverPanel(name, install) {
  const cells = Object.entries(install.cells || {});
  const href = `#/providers/${encodeURIComponent(name)}`;
  return el("section", { class: "family-section machine-driver" },
    el("div", { class: "family-head" },
      el("div", { class: "row-main" },
        el("a", { class: "row-title", href }, name),
        rowMeta(install.cli_version
          ? el("span", { class: "mono" }, install.cli_version) : install.cli_version_note),
        pathText(install.cli_binary))),
    cells.length ? el("details", { class: "machine-cells family-drivers" },
      el("summary", { class: "family-drivers-summary" },
        el("span", { class: "family-drivers-chevron", "aria-hidden": "true" }, "⌄"), "Cells",
        el("span", { class: "family-drivers-count mono" }, String(cells.length))),
      el("ul", { class: "driver-list" }, cells.map(([cellName, cell]) => {
        const egress = cell.codex_egress_limits;
        const limits = egress
          ? rowMeta(`Egress: ${bytesLabel(egress.total_bytes)} total`,
            `${egress.total_connections} connections`, `${bytesLabel(egress.request_bytes)} request`,
            `${bytesLabel(egress.response_bytes)} response`)
          : null;
        return el("li", { class: "driver-item" },
          el("span", { class: "driver-item-name" }, cellName),
          pathText(cell.rootfs),
          cell.rootfs_digest_prefix
            ? el("span", { class: "mono" }, `${cell.rootfs_digest_prefix}…`) : null,
          limits);
      }))) : null);
}

function driversCard(machine) {
  const entries = Object.entries(machine.drivers || {}).sort(([a], [b]) => a.localeCompare(b));
  return el("section", { class: "families-list", "aria-label": "Machine drivers" },
    el("div", { class: "section-heading" },
      el("h2", { class: "card-title" }, "Drivers and rootfs cells"),
      el("span", { class: "small muted mono" }, String(entries.length))),
    entries.length
      ? entries.map(([name, install]) => driverPanel(name, install))
      : el("p", { class: "card empty-line" }, "No drivers declared in machine settings."));
}

function clientsCard(machine) {
  const clients = machine.clients || [];
  return el("section", { class: "card", "aria-label": "Clients' ceilings" },
    el("div", { class: "card-head" },
      el("h2", { class: "card-title" }, "Clients' ceilings"),
      el("span", { class: "mono small muted" }, String(clients.length))),
    clients.length === 0
      ? el("p", { class: "card-body muted" }, "No clients configured.")
      : responsiveTable(el("table", {},
          el("thead", {}, el("tr", {},
            el("th", {}, "Client"),
            el("th", {}, "Places"),
            el("th", {}, "Max surface"),
            el("th", { class: "num" }, "Concurrent"),
            el("th", {}, "Models"),
            el("th", {}, "Limits"))),
          el("tbody", {}, clients.map((client) => {
            const limits = [
              client.max_run_seconds !== null && client.max_run_seconds !== undefined ? `${client.max_run_seconds}s` : null,
              client.max_memory_bytes !== null && client.max_memory_bytes !== undefined ? bytesLabel(client.max_memory_bytes) : null,
              client.max_cpus !== null && client.max_cpus !== undefined ? `${client.max_cpus} CPU` : null,
              client.max_processes !== null && client.max_processes !== undefined ? `${client.max_processes} PIDs` : null,
            ].filter(Boolean);
            return el("tr", {},
              el("td", { class: "item-name" }, client.name),
              el("td", {}, (client.places || []).join(", ")),
              el("td", {}, client.max_surface || ""),
              el("td", { class: "mono small num" }, count(client.max_concurrent_runs)),
              el("td", {}, (client.models || []).length === 0
                ? el("span", { class: "muted small" }, "None")
                : el("div", { class: "chips" }, client.models.map((m) => el("span", { class: "chip" }, m)))),
              el("td", {}, rowMeta(limits)));
          })))));
}

export async function renderMachine(main, { refresh, notice } = {}) {
  const status = el("p", { class: "small muted", role: "status" }, notice);
  const reload = el("button", { class: "btn", type: "button" }, "Reload config.yml");
  reload.addEventListener("click", async () => {
    if (reload.disabled || !window.confirm("Reload config.yml from disk?")) return;
    reload.disabled = true;
    reload.textContent = "Reloading…";
    status.className = "small muted";
    status.textContent = "";
    try {
      await api.reloadConfig();
      if (main.isConnected) await refresh({ notice: "Configuration reloaded." });
    } catch (error) {
      status.className = "small error";
      status.textContent = `Could not reload config.yml: ${error.message}`;
    } finally {
      reload.disabled = false;
      reload.textContent = "Reload config.yml";
    }
  });

  main.append(
    el("header", { class: "page-head" },
      el("div", {},
        el("h1", { class: "page-title" }, "Machine"),
        el("p", { class: "page-sub" },
          "Read-only view of host execution settings. These are edited in the machine settings file (KOTOYOSE_EXECUTION_CONFIG).")),
      el("div", { class: "stack" }, reload, status)));

  let machine;
  try {
    machine = await api.machine();
  } catch (error) {
    if (!main.isConnected) return;
    main.append(el("section", { class: "card card-body" },
      errorLine(`Could not load machine settings: ${error.message}`)));
    return;
  }
  if (!main.isConnected) return;

  main.append(
    summaryGrid(machine),
    driversCard(machine),
    clientsCard(machine),
    roomsCard(machine),
  );
}
