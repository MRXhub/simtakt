import { postJSON } from "./api.js";

// Each server write is idempotent. A retry reuses the same draft identity.
// Only the final Problem response means the complete template is available.
export async function saveTemplate(doc, config) {
  async function post(path, body) {
    const result = await postJSON(path, body);
    if (!result?.ok || !result.data) throw new Error(result?.data?.error || "Request failed");
    return result.data;
  }
  const schema = structuredClone(doc);
  delete schema.extract_names;
  const registered = await post("/api/schemas", schema);
  if (!registered.revision) throw new Error("Missing schema revision");
  const built = await post("/api/contracts/build", {kind: "problem", spec: {
    problem_id: config.problemId,
    parameter_schema_revision: registered.revision,
    constraint_revision: config.constraintRevision,
    metric_schema_revision: config.metricRevision,
    simulation_capabilities: config.capabilities
  }});
  if (!built.contract?.revision) throw new Error("Missing template contract");
  await post("/api/problems", built.contract);
  return {schemaRevision: registered.revision, problem: built.contract};
}
