// Display fields for a host data declaration. The caller renders these as text.

export function declarationSummary(row) {
  const source = row || {};
  return {
    title: String(source.display_name || source.id || ""),
    publisher: String(source.publisher || ""),
    sha256: String(source.sha256 || ""),
    commands: (source.commands || []).map((command) => (command.argv || []).join(" ")),
    artifacts: (source.artifacts || []).map((artifact) => `${artifact.url || ""} ${artifact.sha256 || ""}`),
  };
}
