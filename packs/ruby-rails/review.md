# Ruby on Rails — review rules

Apply only to this pack's files. Style is owned by RuboCop; static security
scanning by Brakeman.

## REQUEST CHANGES
- `permit!` or permitting privileged attributes (`role`, `admin`, `user_id`)
  from params (mass assignment).
- Unscoped `Model.find(params[:id])` on user-owned data (IDOR); action
  without a policy check.
- String interpolation into SQL fragments, `order`, `constantize`/`send` on
  user input.
- `html_safe`/`raw` on user content; `render inline:` with input.
- `skip_before_action :verify_authenticity_token` or `protect_from_forgery`
  weakened for cookie-authenticated actions.
- `Marshal.load`/`YAML.load` (unsafe) on untrusted data.
- Migration without schema file update, or an edited migration that ran.

## Security checklist (OWASP ASVS-aligned)
- `config.force_ssl`, secure cookie flags, `config.hosts` intact in
  production.
- Open redirects (`redirect_to params[:url]`) need `allow_other_host: false`
  or an allowlist.
- File uploads (Active Storage): content type/size validation; no
  user-controlled paths.
- SSRF on outbound HTTP to user URLs; timeouts set.
- Password hashing via `has_secure_password`/Devise; rate limiting on auth.
- Logs: `filter_parameters` covers new sensitive fields.
- `bundle exec bundle-audit check --update` clean for new gems.

## Observations (non-blocking)
- N+1 queries, callbacks hiding business logic, fat controllers, missing
  indexes for new foreign keys.

## Quality gates
- A failing `lint` check in `hub / ci-gate` is REQUEST CHANGES: cite the
  reported rule and file from the check output.
- New lint suppressions, or edits to the linter config that relax a rule, need an approved design; otherwise REQUEST CHANGES.
