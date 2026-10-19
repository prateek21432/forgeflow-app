# Earlier project vs upgraded ForgeFlow


| Area | Earlier project | Upgraded project |
| --- | --- | --- |
| Building apps | Fixed shop template and special wishlist code. | Model plans and edits different small FastAPI/HTML/JavaScript apps; reads existing files before changes. |
| Agent workflow | Separate Python loop; demonstration LangGraph nodes. | Actual LangGraph stages for planning, development, testing, repair, review, checkpoint, and preview; saved state. |
| Repeated work | Repeated attempts could perform the same work. | Empty/repeated actions hand off; unchanged failed repairs stop with a reason; cancellation and configurable budgets. |
| Testing | Ran project pytest and extracted counts from text. | Independent acceptance tests, protected regression tests, structured JUnit counts, zero-test rejection, and failure reports. |
| Browser checks | No integrated browser verification. | Real Playwright checks for visible results, forms, keyboard use, and mobile overflow; explicit unsupported checks. |
| Resume | New request restarted the normal workflow. | Budget stops preserve partial development; Resume saved work continues the saved stage, reuses completed test-generation batches, and reruns validation. |
| Preview | Suggested a fixed URL and manual launch command. | Starts and monitors an actual local preview, checks HTTP response, and shows Open preview controls. |
| Checkpoints | Created after project tests passed. | Requires passing independent tests, review, working preview, and matching source hashes; push failures remain failures. |
| Git | Basic branch/commit and optional push. | Verified user-branch commits, local Git mirror backups, clearer push history, main-branch protection. |
| Models | Basic OpenRouter request function. | Shared structured-output gateway; custom compatible endpoints, DeepSeek, Gemini, Groq, OpenRouter, and Ollama configuration; same-provider backup keys, retries, streaming, usage tracking. |
| Isolation | Command/path restrictions and local execution. | Hardened path checks, secret separation, Docker snapshots, non-root execution, resource/time limits, process cleanup, and isolated test storage. |
| Accounts and state | Basic authentication; SQLite default. | Running PostgreSQL installation, stronger session/CSRF/ownership checks, richer persisted tasks, events, tools, tests, and commits. |
| Website | Basic dashboard/chat. | Updated responsive layout, live/replayed activity, current task, changes/diffs, risk estimates, test evidence, history, and preview controls. |
| Downloads | No completed-project export. | Download project ZIP from the completed Git checkpoint, excluding secrets/runtime data; downloadable run report. |
| Setup and documentation | Manual setup and short README. | Local setup/start/stop scripts, Docker/database setup, environment examples, beginner explanation, presentation guide, and validation record. |

**Checked so far:** 83 platform tests passed; saved BMI app passed 168 tests and three browser scenarios, completed a checkpoint, and launched a preview. Its final verification used one GPT review call and no DeepSeek calls. Budget pauses and repeated explicit resumes were tested using simulated models; shared-project import was tested without AI calls.

**Still limited:** arbitrary stacks/dependency installs, automatic upload/audio browser tests, public hosting, and the full live e-commerce → wishlist demonstration. GitHub push is configurable but not verified here; current backups use a local Git mirror. API keys are configured privately in `.env`, not entered through a browser secrets form. Preview runtime data resets when the preview is recreated.

**Optional setup:** Docker-free Start/Stop launchers use SQLite and local Python execution; Docker remains the default. The shared copy includes a six-project demo account, while new accounts start empty.
