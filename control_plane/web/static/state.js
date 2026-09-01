/**
 * Shared Application State
 * Single source of truth across views, navigation, and language switches.
 */

const defaultSchemaDoc = {
  kind: "parameter-schema",
  problem_hint: "ten-junction-thickness-tcad",
  parameters: [
    { name: "t_total1", type: "float", unit: "um", role: "variable", bounds: { min: 0.1, max: 5.0 }, default: 0.551, deck_line: 3 },
    { name: "t_total2", type: "float", unit: "um", role: "variable", bounds: { min: 0.1, max: 5.0 }, default: 0.820, deck_line: 4 },
    { name: "t_total3", type: "float", unit: "um", role: "variable", bounds: { min: 0.1, max: 5.0 }, default: 1.250, deck_line: 5 },
    { name: "t_total4", type: "float", unit: "um", role: "variable", bounds: { min: 0.1, max: 5.0 }, default: 1.640, deck_line: 6 },
    { name: "t_total5", type: "float", unit: "um", role: "variable", bounds: { min: 0.1, max: 5.0 }, default: 2.100, deck_line: 7 },
    { name: "ambient_temperature", type: "float", unit: "K", role: "fixed", value: 300.0, deck_line: 12 }
  ],
  extracts: [
    { name: "voc", expression: 'max(v."anode")' },
    { name: "jsc", expression: 'max(i."cathode")' },
    { name: "pmpp", expression: 'max(v."anode"*i."cathode")' }
  ],
  extract_names: ["voc", "jsc", "pmpp"]
};

export const state = {
  // Connectivity & Server Info
  health: null,
  healthOk: false,
  writes: false,
  unreachable: false,
  lastRenderAt: null,
  helpEnabled: false,
  lookupValue: "",

  // Current Route
  route: { name: "overview", id: null, step: 1 },

  // Entity Lists Cache (populated by GET /api/<entities>)
  packagesList: [],
  schemasList: [],
  problemsList: [],
  studiesList: [],
  evaluationsList: [],

  // Cached Payloads for detail / operational views
  overview: null,
  algorithms: null,
  algorithm: null,
  algorithmId: null,
  capacity: null,
  shapes: null,
  study: null,
  studyId: null,
  studyStatusFilter: null,
  expandedAttempts: {},
  problem: null,
  problemId: null,
  schema: null,
  schemaRev: null,

  // Package Landing Flow
  packagesDeckText: "",
  packagesPackageName: "",
  packagesSchemaRev: "",
  packagesJobId: null,
  packagesJobStatus: null,
  packagesJobLogs: [],
  packagesParsed: null,
  packagesJobTimer: null,
  expandedPackageManifests: {},

  // Compose Multi-step Flow (1: Package, 2: Schema, 3: Problem, 4: Study, 5: Candidate)
  composeStep: 5,

  // Persistent Schema Draft State (Survives card/route/language switches)
  schemaDraft: {
    mode: "form", // 'form' | 'json'
    doc: JSON.parse(JSON.stringify(defaultSchemaDoc)),
    rawJson: JSON.stringify(defaultSchemaDoc, null, 2),
    jsonError: null,
    registeredRevision: null,
    registeredAt: null
  },

  // Candidate Designer Persistent State
  candidateDesigner: {
    schemaRev: "sha256:mock-schema-ten-junction-v1",
    schemaDoc: null,
    params: {
      t_total1: 0.551,
      t_total2: 0.820,
      t_total3: 1.250,
      t_total4: 1.640,
      t_total5: 2.100
    },
    rawJson: JSON.stringify({
      t_total1: 0.551,
      t_total2: 0.820,
      t_total3: 1.250,
      t_total4: 1.640,
      t_total5: 2.100
    }, null, 2),
    jsonError: null,
    mode: "form", // 'form' | 'json'
    validation: { valid: true, issues: [] },

    // Evaluation Controls (persisted across renders and language switch)
    problemId: "problem:sched-opt",
    problemRev: "sha256:7a8b9c0d1e2f",
    fidelity: "2",
    requestedOutputs: ["voc", "jsc", "pmpp"],
    customExtractInput: "",
    evidenceProfile: "standard-metrics-v1",
    studyId: "study:search-exp-041",
    independence: "normal", // 'normal' | 'independent'
    priority: "normal", // select: 'low' | 'normal' | 'high', POSTED VERBATIM AS TEXT!
    replicateKey: "",

    // Built Contracts & Results
    candidateContract: null,
    requestContract: null,
    previewResult: null,
    serverValidationResult: null
  }
};
