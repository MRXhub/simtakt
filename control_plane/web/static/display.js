import { state } from "./state.js";
import { t } from "./i18n.js";

// Display labels never replace the identifiers sent to the server.
export function entityName(value, kind = "record") {
  const id = String(value || "");
  const lists = {study: state.studiesList, problem: state.problemsList, schema: state.schemasList, package: state.packagesList};
  const list = lists[kind] || [];
  const index = list.findIndex(row => [row.study_id, row.problem_id, row.revision, row.artifact_id, row.package_name].includes(id));
  const row = index >= 0 ? list[index] : null;
  const name = row?.metadata?.display_name || row?.display_name || row?.problem_hint || row?.schema?.problem_hint || row?.package_name;
  if (name) return String(name).replace(/^(study[:.]|problem[:.]|package[.:]|pkg[.:-])/, "");
  if (kind === "problem" && row?.parameter_schema_revision) return entityName(row.parameter_schema_revision, "schema");
  if (kind === "schema" && row?.source_package?.artifact_id) return entityName(row.source_package.artifact_id, "package");
  const readable = id.replace(/^(study[:.]|problem[:.]|package[.:]|pkg[.:-])/, "");
  if (readable && !/sha256:|[a-f0-9]{16,}|[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}/i.test(readable)) return readable;
  return t("entity_" + kind) + (index >= 0 ? ` ${index + 1}` : "");
}

export function problemValue(problem) {
  return JSON.stringify([problem.problem_id, problem.problem_revision || problem.revision]);
}
export function problemLabel(problem) {
  const versions = state.problemsList.filter(p => p.problem_id === problem.problem_id);
  const name = entityName(problem.parameter_schema_revision, "schema");
  const index = versions.findIndex(p => problemValue(p) === problemValue(problem));
  return name + (versions.length > 1 ? ` · v${index + 1}` : "");
}
