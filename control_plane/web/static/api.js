/**
 * API Client & Mock Data Fixtures
 */

import { t, fmtClockTime } from "./i18n.js";

const params = typeof location !== "undefined" ? new URLSearchParams(location.search) : new URLSearchParams();
export const IS_MOCK = params.get("mock") === "1" || (typeof location !== "undefined" && location.protocol === "file:");
if (typeof window !== "undefined") {
  window.__mockFail = params.get("fail") === "1";
}

export const SAMPLE_DECKS = {
  solar: [
    "# 10-Junction Solar Cell TCAD Deck",
    "# Layer Thickness Parameters (um)",
    "set t_total1 = 0.551",
    "set t_total2 = 0.820",
    "set t_total3 = 1.250",
    "set t_total4 = 1.640",
    "set t_total5 = 2.100",
    "set junction_depth = $t_total1 * 0.5",
    "",
    "# Mesh & Environmental Parameters",
    "set mesh_bias = 1.0",
    "set substrate_doping = 1.0e16",
    "set ambient_temperature = 300.0",
    "",
    "# Measurement Extracts",
    "extract name=\"voc\" max(v.\"anode\")",
    "extract name=\"jsc\" max(i.\"cathode\")",
    "extract name=\"pmpp\" max(v.\"anode\"*i.\"cathode\")"
  ].join("\n"),

  cmos: [
    "# CMOS Inverter SPICE Simulation Deck",
    "# Transistor Channel Widths (nm)",
    "set wn_width_nm = 120.0",
    "set wp_width_nm = 240.0",
    "set width_ratio = $wp_width_nm / $wn_width_nm",
    "set channel_length_nm = 45.0",
    "",
    "# Operating Voltages & Temperatures",
    "set supply_voltage_v = 0.9",
    "set load_cap_ff = 15.0",
    "set temperature_k = 300.0",
    "",
    "# Measurement Extracts",
    "extract name=\"tpdr\" max(time)",
    "extract name=\"power_uw\" max(i.\"vdd\"*v.\"vdd\")"
  ].join("\n"),

  plasma: [
    "# SPIS Spacecraft Plasma Charging Deck",
    "# Ambient Plasma Parameters",
    "set plasma_density_m3 = 1.0e8",
    "set electron_temp_ev = 2.5",
    "set ion_temp_ev = 0.1",
    "set te_ti_ratio = a / b",
    "set bias_potential_v = 0.0",
    "set grid_resolution_mm = 5.0",
    "",
    "# Measurement Extracts",
    "extract name=\"floating_pot_v\" max(v.\"probe\")"
  ].join("\n")
};

function simpleHash(str) {
  let h = 0;
  for (let i = 0; i < str.length; i++) {
    h = ((h << 5) - h) + str.charCodeAt(i);
    h |= 0;
  }
  let hex = (h >>> 0).toString(16);
  while (hex.length < 8) hex = "0" + hex;
  return hex + "a0b1c2d3e4f5061728394a5b6c7d8e9f" + hex;
}

export const mockSchemas = {
  "sha256:mock-schema-ten-junction-v1": {
    kind: "parameter-schema",
    revision: "sha256:mock-schema-ten-junction-v1",
    problem_hint: "ten-junction-thickness-tcad",
    source_package: {
      artifact_id: "pkg.ten-junction-tcad",
      revision: "sha256:a1b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef0123456789abcdef0"
    },
    parameters: [
      { name: "t_total1", type: "float", unit: "um", role: "variable", bounds: { min: 0.1, max: 5.0 }, default: 0.551, deck_line: 3 },
      { name: "t_total2", type: "float", unit: "um", role: "variable", bounds: { min: 0.1, max: 5.0 }, default: 0.820, deck_line: 4 },
      { name: "t_total3", type: "float", unit: "um", role: "variable", bounds: { min: 0.1, max: 5.0 }, default: 1.250, deck_line: 5 },
      { name: "t_total4", type: "float", unit: "um", role: "variable", bounds: { min: 0.1, max: 5.0 }, default: 1.640, deck_line: 6 },
      { name: "t_total5", type: "float", unit: "um", role: "variable", bounds: { min: 0.1, max: 5.0 }, default: 2.100, deck_line: 7 },
      { name: "mesh_bias", type: "float", role: "fixed", value: 1.0, deck_line: 10 },
      { name: "substrate_doping", type: "float", role: "fixed", value: 1.0e16, deck_line: 11 },
      { name: "ambient_temperature", type: "float", unit: "K", role: "fixed", value: 300.0, deck_line: 12 }
    ],
    extracts: [
      { name: "voc", expression: "max(v.\"anode\")" },
      { name: "jsc", expression: "max(i.\"cathode\")" },
      { name: "pmpp", expression: "max(v.\"anode\"*i.\"cathode\")" }
    ],
    extract_names: ["voc", "jsc", "pmpp"]
  },
  "sha256:mock-schema-cmos-inverter-v1": {
    kind: "parameter-schema",
    revision: "sha256:mock-schema-cmos-inverter-v1",
    problem_hint: "cmos-inverter-delay-opt",
    source_package: {
      artifact_id: "pkg.cmos-inverter-hspice",
      revision: "sha256:b2c3d4e5f6a708192a3b4c5d6e7f809123456789abcdef0123456789abcdef1"
    },
    parameters: [
      { name: "wn_width_nm", type: "float", unit: "nm", role: "variable", bounds: { min: 50, max: 1000 }, default: 120.0, deck_line: 3 },
      { name: "wp_width_nm", type: "float", unit: "nm", role: "variable", bounds: { min: 50, max: 2000 }, default: 240.0, deck_line: 4 },
      { name: "channel_length_nm", type: "float", unit: "nm", role: "fixed", value: 45.0, deck_line: 5 },
      { name: "supply_voltage_v", type: "float", unit: "V", role: "variable", bounds: { min: 0.6, max: 1.2 }, default: 0.9, deck_line: 8 },
      { name: "load_cap_ff", type: "float", unit: "fF", role: "fixed", value: 15.0, deck_line: 9 },
      { name: "temperature_k", type: "float", unit: "K", role: "fixed", value: 300.0, deck_line: 10 }
    ],
    extracts: [
      { name: "tpdr", expression: "max(time)" },
      { name: "power_uw", expression: "max(i.\"vdd\"*v.\"vdd\")" }
    ],
    extract_names: ["tpdr", "power_uw"]
  },
  "sha256:mock-schema-spis-plasma-v1": {
    kind: "parameter-schema",
    revision: "sha256:mock-schema-spis-plasma-v1",
    problem_hint: "spis-plasma-charging",
    source_package: {
      artifact_id: "pkg.spis-spacecraft-charging",
      revision: "sha256:c3d4e5f6a7b8091a2b3c4d5e6f70819223456789abcdef0123456789abcdef2"
    },
    parameters: [
      { name: "plasma_density_m3", type: "float", unit: "m^-3", role: "variable", bounds: { min: 1e6, max: 1e12 }, default: 1.0e8, deck_line: 3 },
      { name: "electron_temp_ev", type: "float", unit: "eV", role: "variable", bounds: { min: 0.5, max: 20.0 }, default: 2.5, deck_line: 4 },
      { name: "ion_temp_ev", type: "float", unit: "eV", role: "fixed", value: 0.1, deck_line: 5 },
      { name: "bias_potential_v", type: "float", unit: "V", role: "variable", bounds: { min: -100.0, max: 100.0 }, default: 0.0, deck_line: 6 },
      { name: "grid_resolution_mm", type: "float", unit: "mm", role: "fixed", value: 5.0, deck_line: 7 }
    ],
    extracts: [
      { name: "floating_pot_v", expression: "max(v.\"probe\")" }
    ],
    extract_names: ["floating_pot_v"]
  }
};

const mockPackageJobs = {};
const MIN = 60000, HOUR = 3600000, DAY = 86400000;
function mIso(msAgo) { return new Date(Date.now() - msAgo).toISOString(); }

export function validateCandidateLocally(schema, candidateParams) {
  const issues = [];
  if (!schema || !schema.parameters) {
    return { valid: false, issues: [{ name: "_schema", code: "no_schema", message: t("errNoSchemaLoaded") }] };
  }
  if (typeof candidateParams !== "object" || candidateParams === null || Array.isArray(candidateParams)) {
    return { valid: false, issues: [{ name: "_params", code: "invalid_params", message: t("errInvalidJson") }] };
  }

  const schemaParamMap = {};
  schema.parameters.forEach(p => { schemaParamMap[p.name] = p; });

  schema.parameters.forEach(p => {
    if (p.role === "variable") {
      if (!(p.name in candidateParams) || candidateParams[p.name] === undefined || candidateParams[p.name] === null || candidateParams[p.name] === "") {
        issues.push({
          name: p.name,
          code: "missing_parameter",
          message: t("issueMissing", { name: p.name })
        });
        return;
      }
      const val = candidateParams[p.name];
      if (p.type === "float") {
        if (typeof val !== "number" || isNaN(val) || !isFinite(val)) {
          issues.push({ name: p.name, code: "type_mismatch", message: t("issueType", { name: p.name, expected: "float" }) });
          return;
        }
      } else if (p.type === "int") {
        if (typeof val !== "number" || isNaN(val) || Math.floor(val) !== val) {
          issues.push({ name: p.name, code: "type_mismatch", message: t("issueType", { name: p.name, expected: "int" }) });
          return;
        }
      } else if (p.type === "bool") {
        if (typeof val !== "boolean") {
          issues.push({ name: p.name, code: "type_mismatch", message: t("issueType", { name: p.name, expected: "bool" }) });
          return;
        }
      } else if (p.type === "string") {
        if (typeof val !== "string") {
          issues.push({ name: p.name, code: "type_mismatch", message: t("issueType", { name: p.name, expected: "string" }) });
          return;
        }
      }

      if (typeof val === "number" && p.bounds) {
        if (typeof p.bounds.min === "number" && val < p.bounds.min) {
          issues.push({ name: p.name, code: "out_of_bounds", message: t("issueMin", { name: p.name, min: String(p.bounds.min), val: String(val) }) });
        }
        if (typeof p.bounds.max === "number" && val > p.bounds.max) {
          issues.push({ name: p.name, code: "out_of_bounds", message: t("issueMax", { name: p.name, max: String(p.bounds.max), val: String(val) }) });
        }
      }
    }
  });

  Object.keys(candidateParams).forEach(k => {
    if (!schemaParamMap[k]) {
      issues.push({ name: k, code: "unexpected_parameter", message: t("issueExtra", { name: k }) });
    }
  });

  return { valid: issues.length === 0, issues };
}

function getMockPayload(path) {
  if (path === "/api/health") {
    const writes = params.get("readonly") === "1" ? false : true;
    return { status: "ok", time: mIso(0), project_root: "configured", uptime_seconds: 8432.1, writes_enabled: writes, demo: true };
  }
  if (path.startsWith("/api/overview")) {
    return {
      generated_at: mIso(0),
      study_count: 6,
      global: { queued: 18, recovering: 3, reconciling: 2 },
      studies: [
        {
          study_id: "study:search-exp-041", problem_id: "problem:sched-opt",
          problem_revision: 7, created_at: mIso(3 * DAY),
          algorithm_run_id: "run:0182", automation_profile: "standard",
          evaluation_count: 24,
          status_counts: { queued: 6, running: 2, qualifying: 1, recovering: 1, qualified: 12, ambiguous: 1, unresolved: 1 },
          active_count: 3, waiting_count: 8,
          oldest_wait: { evaluation_id: "eval:9f02c1", wait_reason: "license-denied", wait_since: mIso(47 * MIN) },
          last_activity_at: mIso(2 * MIN)
        },
        {
          study_id: "study:ablate-lr-012", problem_id: "problem:sched-opt",
          problem_revision: 7, created_at: mIso(2 * DAY),
          algorithm_run_id: "run:0179", automation_profile: "aggressive",
          evaluation_count: 15,
          status_counts: { requested: 2, deduplicating: 1, queued: 4, running: 1, qualified: 5, cancelled: 2 },
          active_count: 1, waiting_count: 7,
          oldest_wait: { evaluation_id: "eval:a41d88", wait_reason: "requeued-after:worker-crash", wait_since: mIso(9 * MIN) },
          last_activity_at: mIso(31000)
        },
        {
          study_id: "study:recon-check-003", problem_id: "problem:pipeline-sanity",
          problem_revision: 2, created_at: mIso(26 * HOUR),
          algorithm_run_id: "run:0184", automation_profile: "standard",
          evaluation_count: 6,
          status_counts: { recovering: 2, queued: 3, running: 1 },
          active_count: 1, waiting_count: 5,
          oldest_wait: { evaluation_id: "eval:b77e10", wait_reason: "reconciling", wait_since: null },
          last_activity_at: mIso(95000)
        }
      ]
    };
  }
  if (path === "/api/capacity") {
    return {
      license_pools: [
        { license_pool_id: "pool-a", license_sessions: 8, active: true, active_count: 5, license_sessions_in_use: 5, license_reserve: 1 },
        { license_pool_id: "pool-b", license_sessions: 4, active: true, active_count: 4, license_sessions_in_use: 4, license_reserve: 0 }
      ],
      targets: [
        { target_id: "demo-target-a", host_id: "demo-host-a", active: true, active_count: 3, max_active_sessions: 4, role: "formal" },
        { target_id: "demo-target-b", host_id: "demo-host-a", active: true, active_count: 1, max_active_sessions: 2, role: "trial" }
      ],
      global: { queued: 18, recovering: 3, reconciling: 2, stale_reconciling: 1 },
      snapshot: "unavailable"
    };
  }
  if (path === "/api/shapes") {
    return {
      shapes: [
        {
          task_class_key: "solve/std", target_id: "t-vm-01", profile_revision: 3,
          processors: 4, sample_count: 42, success_count: 39, failure_count: 3,
          successful_wall_samples: 39, successful_wall_mean_seconds: 128.4,
          successful_wall_stddev_seconds: 11.2,
          cpu_samples: 39, cpu_mean_seconds: 402.1, cpu_stddev_seconds: 30.5,
          busy_samples: 39, busy_mean_seconds: 118.0, busy_stddev_seconds: 9.8,
          rss_samples: 39, rss_mean_bytes: 734003200, rss_stddev_bytes: 52428800,
          budget: { max_wall_seconds: 600, command_timeout_seconds: 900 }
        }
      ]
    };
  }
  if (path === "/api/algorithms") {
    return {
      generated_at: mIso(0),
      algorithm_count: 2,
      algorithms: [
        {
          algorithm_run_id: "run:0182",
          algorithm_id: "algo:bayesian-gp-ucb",
          algorithm_revision: "sha256:4a8b29f0e1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8",
          problem_id: "problem:sched-opt",
          problem_revision: 7,
          status: "active",
          study_ids: ["study:search-exp-041"],
          study_count: 1,
          evaluation_count: 24,
          active_count: 3,
          event_count: 4,
          result_count: 0,
          created_at: mIso(3 * DAY),
          updated_at: mIso(2 * MIN),
          latest_event_at: mIso(2 * MIN)
        }
      ]
    };
  }
  if (path.startsWith("/api/algorithms/")) {
    const aid = decodeURIComponent(path.slice("/api/algorithms/".length));
    return {
      algorithm: {
        algorithm_run_id: aid,
        algorithm_id: "algo:bayesian-gp-ucb",
        algorithm_revision: "sha256:4a8b29f0e1c2d3e4",
        problem_id: "problem:sched-opt",
        problem_revision: 7,
        status: "active",
        configuration: { acquisition: "UCB", beta: 2.5 },
        study_ids: ["study:search-exp-041"],
        created_at: mIso(3 * DAY)
      },
      events: [
        { sequence: 1, event_key: "init-warmup", event_type: "warmup-completed", run_status: "active", created_at: mIso(3 * DAY) },
        { sequence: 2, event_key: "iter-0001", event_type: "candidate-proposed", run_status: "active", created_at: mIso(2 * DAY) }
      ],
      results: []
    };
  }
  if (path.startsWith("/api/studies/")) {
    const sid = decodeURIComponent(path.slice("/api/studies/".length));
    return {
      study: {
        study_id: sid, problem_id: "problem:sched-opt", problem_revision: 7,
        created_at: mIso(3 * DAY), automation_profile: "standard"
      },
      evaluations: [
        { evaluation_id: "eval:9f02c1", candidate_id: "cand:7f3a", fidelity: 2, priority: "normal", status: "queued", wait_reason: "license-denied", wait_since: mIso(47 * MIN) },
        { evaluation_id: "eval:r31c0d", candidate_id: "cand:91e0", fidelity: 3, priority: "high", status: "running", wait_reason: null, wait_since: null },
        { evaluation_id: "eval:okc319", candidate_id: "cand:55be", fidelity: 3, priority: "normal", status: "qualified", observation_id: "obs:99120", wait_reason: null, wait_since: null }
      ]
    };
  }
  if (path.startsWith("/api/problems/")) {
    const pid = decodeURIComponent(path.slice("/api/problems/".length));
    return {
      problem_id: pid,
      parameter_schema_revision: "sha256:mock-schema-ten-junction-v1",
      constraint_revision: "none",
      metric_schema_revision: "default",
      simulation_capabilities: ["tcad", "spis", "spice"],
      studies: [
        { study_id: "study:search-exp-041", problem_id: pid, problem_revision: 7, created_at: mIso(3 * DAY) },
        { study_id: "study:baseline-v5", problem_id: pid, problem_revision: 5, created_at: mIso(9 * DAY) }
      ],
      evaluations: [
        { evaluation_id: "eval:9f02c1", candidate_id: "cand:7f3a", fidelity: 2, priority: "normal", status: "queued" },
        { evaluation_id: "eval:okc319", candidate_id: "cand:55be", fidelity: 3, priority: "normal", status: "qualified" }
      ]
    };
  }
  if (path.startsWith("/api/schemas/")) {
    const rev = decodeURIComponent(path.slice("/api/schemas/".length));
    if (mockSchemas[rev]) return mockSchemas[rev];
    return { __http: 404, error: "ParameterSchema not found: " + rev };
  }

  // Frozen List GETs
  if (path === "/api/schemas") {
    return {
      items: Object.keys(mockSchemas).map(rev => ({
        revision: rev,
        kind: mockSchemas[rev].kind || "parameter-schema",
        parameter_count: (mockSchemas[rev].parameters || []).length,
        extract_names: mockSchemas[rev].extract_names || (mockSchemas[rev].extracts || []).map(e => e.name || e),
        registered_at: mIso(3 * DAY)
      }))
    };
  }
  if (path === "/api/packages") {
    return {
      items: [
        {
          package_name: "pkg-solar-cell-tcad",
          artifact_id: "pkg.ten-junction-tcad",
          revision: "sha256:a1b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef0123456789abcdef0",
          status: "registered",
          created_at: mIso(3 * DAY)
        },
        {
          package_name: "pkg-cmos-inverter",
          artifact_id: "pkg.cmos-inverter-hspice",
          revision: "sha256:b2c3d4e5f6a708192a3b4c5d6e7f809123456789abcdef0123456789abcdef1",
          status: "registered",
          created_at: mIso(2 * DAY)
        }
      ]
    };
  }
  if (path === "/api/problems") {
    return {
      items: [
        { problem_id: "problem:sched-opt", parameter_schema_revision: "sha256:mock-schema-ten-junction-v1", problem_revision: "sha256:7a8b9c0d1e2f" },
        { problem_id: "problem:cmos-timing", parameter_schema_revision: "sha256:mock-schema-cmos-inverter-v1", problem_revision: "sha256:8b9c0d1e2f3a" },
        { problem_id: "problem:plasma-charge", parameter_schema_revision: "sha256:mock-schema-spis-plasma-v1", problem_revision: "sha256:9c0d1e2f3a4b" }
      ]
    };
  }
  if (path === "/api/studies") {
    return {
      items: [
        { study_id: "study:search-exp-041", problem_id: "problem:sched-opt", problem_revision: 7 },
        { study_id: "study:ablate-lr-012", problem_id: "problem:sched-opt", problem_revision: 7 },
        { study_id: "study:recon-check-003", problem_id: "problem:pipeline-sanity", problem_revision: 2 }
      ]
    };
  }
  if (path === "/api/evaluations") {
    return {
      items: [
        { evaluation_id: "eval:9f02c1", candidate_id: "cand:7f3a", status: "queued", priority: "normal" },
        { evaluation_id: "eval:r31c0d", candidate_id: "cand:91e0", status: "running", priority: "high" },
        { evaluation_id: "eval:okc319", candidate_id: "cand:55be", status: "qualified", priority: "normal" }
      ]
    };
  }
  if (path.startsWith("/api/packages/jobs/")) {
    const jid = decodeURIComponent(path.slice("/api/packages/jobs/".length));
    const job = mockPackageJobs[jid] || {
      job_id: jid, status: "registered",
      logTail: ["[demo] Package job completed successfully."],
      package: { artifact_id: "pkg.mock-package", revision: "sha256:mock-package-rev" },
      error: null
    };
    return {
      status: job.status,
      log_tail: job.logTail || [],
      package: job.package,
      error: job.error
    };
  }

  return { __http: 404, error: "unknown path: " + path };
}

export async function fetchJSON(path) {
  if (IS_MOCK) {
    return new Promise((resolve, reject) => {
      setTimeout(() => {
        if (typeof window !== "undefined" && window.__mockFail) {
          reject(new TypeError("mock: network failure"));
          return;
        }
        const payload = getMockPayload(path);
        if (payload && payload.__http && payload.__http !== 200) {
          resolve({ ok: false, status: payload.__http, data: payload });
        } else {
          resolve({ ok: true, status: 200, data: payload });
        }
      }, 30);
    });
  }

  try {
    const res = await fetch(path, { headers: { Accept: "application/json" } });
    const data = await res.json().catch(() => null);
    return { ok: res.ok, status: res.status, data };
  } catch (err) {
    return { ok: false, status: 0, error: err.message, data: null };
  }
}

export async function postJSON(path, body) {
  if (IS_MOCK) {
    return new Promise((resolve) => {
      setTimeout(() => {
        if (path === "/api/packages/parse") {
          const text = (body && body.deck_text) ? String(body.deck_text) : "";
          const lines = text.split(/\r?\n/);
          const paramsList = [];
          const extracts = [];
          const warnings = [];
          const nameCounts = {};
          const nameFirstLine = {};

          lines.forEach((line, idx) => {
            const trimmed = line.trim();
            if (!trimmed || trimmed.startsWith("#") || trimmed.startsWith("//")) return;

            const extMatch = trimmed.match(/^extract\s+name\s*=\s*(?:"([^"]+)"|'([^']+)'|([^\s\(\)]+))\s*(.*)$/i);
            if (extMatch) {
              const extName = extMatch[1] || extMatch[2] || extMatch[3];
              const extExpr = (extMatch[4] || "").trim();
              extracts.push({ name: extName, expression: extExpr, line: idx + 1 });
              return;
            }

            const setMatch = trimmed.match(/^set\s+([a-zA-Z0-9_]+)\s*=\s*(.*)$/i);
            if (setMatch) {
              const pName = setMatch[1];
              const rest = setMatch[2].trim();
              let commentIdx = -1;
              for (let ci = 0; ci < rest.length; ci++) {
                if (rest[ci] === "#" || (rest[ci] === "/" && rest[ci + 1] === "/")) {
                  commentIdx = ci;
                  break;
                }
              }
              const rawVal = (commentIdx >= 0 ? rest.slice(0, commentIdx) : rest).trim();
              if (!rawVal) return;

              nameCounts[pName] = (nameCounts[pName] || 0) + 1;
              if (nameCounts[pName] === 1) nameFirstLine[pName] = idx + 1;
              else warnings.push(`Line ${idx + 1}: Parameter '${pName}' conflicts with line ${nameFirstLine[pName]}`);

              const isPureNum = /^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/.test(rawVal);
              const isExpr = !isPureNum || rawVal.includes("$");
              const kind = isExpr ? "expression" : "numeric";

              paramsList.push({
                name: pName,
                kind,
                type: kind,
                value: kind === "numeric" ? Number(rawVal) : null,
                value_raw: rawVal,
                line: idx + 1,
                unique: true
              });
            }
          });

          resolve({
            ok: true,
            status: 200,
            data: { parameters: paramsList, extracts, warnings }
          });
        } else if (path === "/api/schemas") {
          const schemaDoc = body || {};
          const rev = "sha256:" + simpleHash(JSON.stringify(schemaDoc));
          mockSchemas[rev] = schemaDoc;
          resolve({ ok: true, status: 200, data: { revision: rev } });
        } else if (path === "/api/packages") {
          const pkgName = (body && body.package_name) ? String(body.package_name).trim() : "";
          const jobId = "job-pkg-" + Math.random().toString(36).slice(2, 10);
          const contentHash = "sha256:" + simpleHash(body?.deck_text || pkgName);
          mockPackageJobs[jobId] = {
            job_id: jobId,
            package_name: pkgName,
            content_hash: contentHash,
            status: "registered",
            logTail: [`[${fmtClockTime(new Date())}] [registered] Package recorded: pkg.${pkgName}`],
            package: { artifact_id: "pkg." + pkgName, revision: contentHash },
            error: null
          };
          resolve({ ok: true, status: 202, data: { job_id: jobId, content_hash: contentHash } });
        } else if (path === "/api/candidates/validate") {
          const schemaRev = body?.schema_revision;
          const candParams = body?.parameters || {};
          const schema = mockSchemas[schemaRev];
          const result = validateCandidateLocally(schema, candParams);
          resolve({ ok: true, status: 200, data: result });
        } else if (path === "/api/contracts/build") {
          const kind = body?.kind || "problem";
          const contract = Object.assign({}, body?.spec || {});
          if (kind === "problem") contract.problem_revision = "sha256:mock" + "0".repeat(58);
          if (kind === "candidate") contract.candidate_id = "cand:mock-" + Math.random().toString(36).slice(2, 8);
          if (kind === "evaluation_request") contract.evaluation_id = "eval:mock-" + Math.random().toString(36).slice(2, 8);
          resolve({ ok: true, status: 200, data: { contract } });
        } else if (path === "/api/problems") {
          resolve({ ok: true, status: 200, data: { problem: { problem_revision: "sha256:mock-registered" } } });
        } else if (path === "/api/studies") {
          resolve({ ok: true, status: 200, data: { study: body } });
        } else {
          resolve({ ok: true, status: 200, data: { status: "accepted", evaluation_id: "eval:mock-created" } });
        }
      }, 40);
    });
  }

  try {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body)
    });
    const data = await res.json().catch(() => null);
    return { ok: res.ok, status: res.status, data };
  } catch (err) {
    return { ok: false, status: 0, error: err.message, data: null };
  }
}
