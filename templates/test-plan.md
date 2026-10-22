# Test plan: <feature or release>

- Ticket / spec: #<N> / <docs/specs/<n>.md>
- Author: <name or agent role>
- Date: <YYYY-MM-DD>

## 1. Scope
- In scope: <features, components, platforms>
- Out of scope: <...>

## 2. Risks driving the testing
| Risk | Likelihood | Impact | Covered by |
|---|---|---|---|
| | L/M/H | L/M/H | <test level / charter> |

## 3. Test levels
| Level | What | Tooling | Owner | Required in CI? |
|---|---|---|---|---|
| Unit | Domain logic, edge cases | <framework> | Engineer | yes |
| Integration | DB, queues, external adapters | <testcontainers / fakes> | Engineer | yes |
| Contract | API schema, consumer/provider | <OpenAPI diff / Pact> | Engineer | <yes/no> |
| End-to-end | Critical user journeys | <Playwright / Detox / XCUITest> | QA | <smoke: yes> |
| Accessibility | WCAG 2.2 AA | <axe / pa11y> | QA | <yes/no> |
| Performance | Budgets from the spec | <k6 / Lighthouse> | QA | <yes/no> |
| Security | SAST, SCA, secrets, DAST baseline | <CodeQL/Semgrep, OSV, gitleaks, ZAP> | CI | yes |

## 4. Environments
| Environment | URL | Data | Notes |
|---|---|---|---|
| Local | | fixtures | |
| Preview | <per-PR> | seeded test data | |
| Staging | | anonymised | |

## 5. Test data
Where the data comes from, how it is seeded and reset, and confirmation that no production personal data is used.

## 6. Entry and exit criteria
- Entry: <build green, feature flag available, spec approved>
- Exit: <all ACs traced to passing tests, no open high/critical defects, coverage >= <n>% on changed files, performance budgets met>

## 7. Traceability
| Acceptance criterion | Test | Level | Status |
|---|---|---|---|
| | | | pass / fail / not run |

## 8. Exploratory charters
| Charter | Time box | Tester | Notes / bugs |
|---|---|---|---|
| Explore <area> with <resources> to discover <information> | 30 min | | |
