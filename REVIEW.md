# Repository review

## Fixed

- Removed a duplicated JavaScript lint block at the end of the WebSocket handler
  that referenced undefined variables; sessions now close explicitly.
- Removed execution from JavaScript linting. Typing code no longer triggers its
  filesystem writes, network requests, or infinite loops during syntax checks.
- Centralized subprocess cleanup: lint and compilation timeouts kill process groups
  and reap the parent. Interactive runs stop on disconnect, interrupt, timeout, or
  excessive output. Per-run temporary directories clean up generated files.
- Moved synchronous AI requests off the event loop. Missing AI credentials no longer
  prevent startup, and provider failures return an error HTTP status.
- Resolved the UI relative to the backend file and removed the hardcoded local port
  from same-origin frontend requests.
- Prevented stale lint responses and old socket events from modifying newer state;
  guarded AI fixes against overwriting edits made after requesting suggestions.
- Sandboxed both HTML preview paths, escaped AI error messages, and accepted C++
  code-fence labels when extracting suggested fixes.
- Aligned Python dependency metadata with FastAPI and excluded environment/key
  files from Docker's build context.

## Improvement priorities

1. **Isolate untrusted compilation and execution.** Run each job in a separate,
   disposable sandbox with no API credentials, restricted filesystem/network access,
   and enforced memory/process limits. The API and compiler processes currently
   share host privileges; changing working directories does not fix this. Process
   groups also cannot contain malicious code that creates a new session.
2. **Add admission controls.** `/ws`, `/lint`, and `/analyze` have no authentication,
   rate limit, job queue, concurrency budget, or application-level code-size limit.
   Apply these before public deployment, restrict CORS and WebSocket origins, and
   bound compiler/linter output as well as runtime output.
3. **Improve static diagnostics.** Python and JavaScript blank invalid lines and
   retry, which can produce artificial follow-up errors. Prefer one accurate syntax
   result or a parser with error recovery; use a static JavaScript linter for undefined
   names. Java class detection uses regex and needs coverage for modifiers, records,
   comments, and multiple classes.
4. **Make builds reproducible and tested.** Pin and regularly audit Python and CDN
   dependencies, replace the floating Docker Node install, add regression tests to CI,
   and deploy only frontend assets to GitHub Pages instead of the entire repository.
   The npm manifest still describes the old Flask project and depends on `easyjre`,
   although the backend uses system Java; remove it after confirming no external
   deployment relies on it.
5. **Separate UI concerns and improve feedback.** Split the large inline script into
   editor, diagnostics, execution, and AI modules. Report exit status and connection
   failures visibly, add browser tests, and support pasting multiline terminal input.
   Ship frontend assets as package data if distributing a Python wheel.

## Validation limits

Tests cover local API/runtime behavior and frontend state transitions. They do not
validate a live NVIDIA response, deployed Render/GitHub Pages sites, a Docker image
build, or a full browser session with CDN-loaded Monaco/XTerm.
