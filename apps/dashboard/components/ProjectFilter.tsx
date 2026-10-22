/** GET form so filtering works without JavaScript and keeps a shareable URL. */
export function ProjectFilter({
  projects,
  selected,
  extra,
}: {
  projects: readonly string[];
  selected: string;
  extra?: Record<string, string>;
}) {
  return (
    <form className="toolbar" method="get" role="search" aria-label="Filter by project">
      {Object.entries(extra ?? {}).map(([k, v]) => (
        <input key={k} type="hidden" name={k} value={v} />
      ))}
      <div className="field">
        <label htmlFor="project-filter">Project</label>
        <select id="project-filter" name="project" defaultValue={selected}>
          <option value="">All projects</option>
          {projects.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
      </div>
      <button type="submit">Apply</button>
    </form>
  );
}
