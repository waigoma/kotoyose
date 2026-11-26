// Display helpers. Times are shown in the browser's local zone, as "MM/DD HH:MM".

const pad = (n) => String(n).padStart(2, "0");

export function timeLabel(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso);
  return `${pad(date.getMonth() + 1)}/${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function count(value) {
  return value === null || value === undefined ? "—" : Number(value).toLocaleString("en-US");
}

export function placeGateMessage(places) {
  const options = places.some((place) => place.startsWith("child:")) ? [...places, "child"] : places;
  return `Hidden by client places. Allow ${options.join(" or ")}.`;
}

export function bytesLabel(bytes) {
  if (bytes === null || bytes === undefined) return "—";
  if (bytes === "disabled") return "disabled";
  const num = Number(bytes);
  if (!Number.isFinite(num) || num < 0) return String(bytes);
  if (num >= 1024 ** 3) return `${(num / 1024 ** 3).toFixed(num % 1024 ** 3 === 0 ? 0 : 1)} GiB`;
  if (num >= 1024 ** 2) return `${(num / 1024 ** 2).toFixed(num % 1024 ** 2 === 0 ? 0 : 1)} MiB`;
  if (num >= 1024) return `${(num / 1024).toFixed(num % 1024 === 0 ? 0 : 1)} KiB`;
  return `${num} B`;
}

export function hourLabel(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso);
  return `${pad(date.getHours())}:00`;
}

export function clockLabel(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso);
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

export function durationLabel(ms) {
  if (ms === null || ms === undefined) return "—";
  const num = Number(ms);
  if (!Number.isFinite(num)) return "—";
  const clamped = Math.max(0, num);
  if (clamped < 1000) return `${Math.round(clamped)}ms`;
  const totalSec = clamped / 1000;
  if (totalSec < 60) return `${totalSec.toFixed(1)}s`;
  const mins = Math.floor(totalSec / 60);
  const secs = Math.floor(totalSec % 60);
  if (mins < 60) return `${mins}m ${secs}s`;
  const hrs = Math.floor(mins / 60);
  return `${hrs}h ${mins % 60}m`;
}

export function statusTone(status) {
  if (status === "succeeded" || status === "completed") return "positive";
  if (status === "failed" || status === "error") return "negative";
  if (status === "in_progress" || status === "running") return "ring-positive";
  return "";
}

export function tokenPair(tokensIn, tokensOut, tokensSource) {
  if (tokensIn === null && tokensOut === null) return "—";
  if (tokensIn === undefined && tokensOut === undefined) return "—";
  const prefix = tokensSource === "estimated" ? "~" : "";
  const inText = tokensIn === null || tokensIn === undefined ? "?" : count(tokensIn);
  const outText = tokensOut === null || tokensOut === undefined ? "?" : count(tokensOut);
  return `${prefix}${inText} → ${outText}`;
}

export function throughputLabel(item) {
  if (!item || item.output_tokens_per_second === null || item.output_tokens_per_second === undefined) {
    return "—";
  }
  const rate = Number(item.output_tokens_per_second);
  if (!Number.isFinite(rate)) return "—";
  const prefix = item.tokens_source === "estimated" ? "~" : "";
  return `${prefix}${rate.toFixed(1)} tok/s`;
}

export function shortId(value, length = 12) {
  if (!value) return "—";
  const text = String(value);
  return text.length > length ? `${text.slice(0, length)}…` : text;
}

/** The sanction of a route, as the console shows it. */
export const SANCTION = {
  official: { label: "Official", tone: "positive" },
  acknowledged: { label: "Acknowledged", tone: "ring-positive" },
  needs_ack: { label: "Needs acknowledgement", tone: "ring-warning" },
};

export function sanctionOf(status) {
  return SANCTION[status] || { label: status || "Unknown", tone: "" };
}

/** Candidate states in a pool view that still block a call. */
export const BLOCKED_STATES = new Set(["limited", "auth_expired"]);

/** Status dot tone for connection auth health. */
export function authTone(auth) {
  if (auth === "ok") return "positive";
  if (auth === "expiring") return "warning";
  if (auth === "expired" || auth === "logged_out" || auth === "login_required") return "negative";
  return "";
}

/** Human-readable auth state summary ("expires in N days", "expired", "logged out", "login required", "unknown"). */
export function authLabel(state) {
  const auth = state && state.auth ? state.auth : "unknown";
  if (auth === "expired") return "expired";
  if (auth === "logged_out") return "logged out";
  if (auth === "login_required") return "login required";
  if (auth === "unknown") return "unknown";
  if (state && state.expires_at) {
    const diffMs = new Date(state.expires_at).getTime() - Date.now();
    if (Number.isFinite(diffMs)) {
      if (diffMs <= 0) return "expired";
      const hours = Math.floor(diffMs / 3600000);
      if (hours >= 24) {
        const days = Math.ceil(diffMs / 86400000);
        return `expires in ${days} ${days === 1 ? "day" : "days"}`;
      }
      return `expires in ${Math.max(1, hours)}h`;
    }
  }
  return auth === "expiring" ? "expiring soon" : "ok";
}

/** Relative reset label ("3h 10m") falling back to formatted timestamp. */
export function resetLabel(iso) {
  if (!iso) return "—";
  const targetMs = new Date(iso).getTime();
  if (!Number.isFinite(targetMs)) return String(iso);
  const diffSec = Math.max(0, Math.floor((targetMs - Date.now()) / 1000));
  const days = Math.floor(diffSec / 86400);
  const hours = Math.floor((diffSec % 86400) / 3600);
  const mins = Math.floor((diffSec % 3600) / 60);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${mins}m`;
  if (mins > 0) return `${mins}m`;
  return timeLabel(iso);
}

/** Pick the index of the tightest (highest used_percent) window in a quota list. */
export function tightestWindowIndex(quota) {
  if (!Array.isArray(quota) || quota.length === 0) return -1;
  let bestIdx = 0;
  let bestVal = -Infinity;
  for (let i = 0; i < quota.length; i++) {
    const val = Number(quota[i] && quota[i].used_percent);
    if (Number.isFinite(val) && val > bestVal) {
      bestVal = val;
      bestIdx = i;
    }
  }
  return bestIdx;
}

export function trainingRiskLabel(policy) {
  return {
    trains: "Trains on input",
    unknown: "Unknown — treated as training",
  }[policy] || "";
}
