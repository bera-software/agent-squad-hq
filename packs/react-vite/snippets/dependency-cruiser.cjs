// dependency-cruiser rules seeded by Agent Squad HQ (pack react-vite).
//
// Tool:  dependency-cruiser 18.5.0, pinned in devDependencies:
//          npm install --save-dev --save-exact dependency-cruiser@18.5.0
// Run:   npx --no-install depcruise --config .dependency-cruiser.cjs .
//        (the hub's "arch" gate runs exactly this whenever this file exists)
//
// TODO(owner): adjust the layer paths below to this project's layout, and
// record the layering in an ADR under docs/adr/. Loosen a rule here, in a
// reviewed PR, rather than suppressing it in code.
const fs = require("node:fs");

const TEST_FILE = "\\.(spec|test)\\.[cm]?[jt]sx?$|(^|/)(__tests__|__mocks__)/";

/** @type {import('dependency-cruiser').IConfiguration} */
module.exports = {
  forbidden: [
    {
      name: "no-circular",
      severity: "error",
      comment: "Circular dependencies make modules impossible to reason about, test or split.",
      from: {},
      to: { circular: true },
    },
    {
      name: "no-test-code-in-src",
      severity: "error",
      comment: "Production code must not import tests, mocks or fixtures.",
      from: { pathNot: [TEST_FILE, "(^|/)(test|tests|e2e|cypress|playwright)/"] },
      to: { path: TEST_FILE },
    },
    {
      name: "shared-not-to-features",
      severity: "error",
      comment:
        "Shared building blocks (components, hooks, lib, utils) must not import pages, routes or " +
        "features: dependencies point from the app towards shared code, never back.",
      from: { path: "(^|/)(components|ui|hooks|lib|utils)/" },
      to: { path: "(^|/)(pages|routes|features|views)/" },
    },
    {
      name: "not-to-unresolvable",
      severity: "warn",
      comment: "An import that cannot be resolved is a typo or a missing dependency.",
      from: {},
      to: { couldNotResolve: true },
    },
  ],
  options: {
    doNotFollow: { path: "node_modules" },
    exclude: { path: "(^|/)(node_modules|\\.next|\\.expo|dist|build|out|coverage|\\.squadhq)/" },
    tsPreCompilationDeps: true,
    ...(fs.existsSync("tsconfig.json") ? { tsConfig: { fileName: "tsconfig.json" } } : {}),
    enhancedResolveOptions: {
      exportsFields: ["exports"],
      conditionNames: ["import", "require", "node", "default", "types"],
    },
  },
};
