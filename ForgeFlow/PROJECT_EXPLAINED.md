# Understanding your project

## What is it?

Imagine a small software team. You tell a team lead what you need. A developer writes code. A tester checks it. When a test fails, the developer fixes the problem. Once the work passes checks, someone saves a version and shows you the app.

ForgeFlow builds that workflow around AI models. You use one local website to describe the application, watch the work, see failures, and open the result.

For example, you ask for a bookstore. The AI writes Python backend files and HTML, CSS, and JavaScript frontend files on your laptop. The platform runs tests, starts the application, and saves a Git checkpoint. Later you say, "Add a wishlist." It reads and modifies the same project.

## What each technology does

| Name | Plain explanation |
| --- | --- |
| FastAPI | The Python web server that receives button clicks and chat requests. |
| Jinja2 | Fills HTML pages with project names, status, and other data. |
| Tailwind/CSS | Controls how the dashboard looks. |
| JavaScript | Updates the page and sends requests without full page reloads. |
| PostgreSQL | A durable notebook storing accounts, requests, tests, and history. |
| LLM | The external AI model that plans and writes code. |
| LangGraph | A flowchart in code that decides which agent acts next. |
| LangChain tools | Named operations the Developer can request, such as reading a file. |
| Pytest | Runs executable checks and returns pass/fail results. |
| Docker | Runs generated programs in a restricted container. |
| Git | Saves versions so you can inspect how the project changed. |
| WebSocket | A connection that delivers activity updates while agents work. |

The platform backend and the generated application's backend are different programs. ForgeFlow might run on port 8791; the bookstore runs on another local port.

## What you built versus what the AI provider supplies

You are not training a new language model. Gemini or another provider supplies the model. Your engineering work connects it to controlled tools, separate agent roles, persistent state, tests, bounded retries, version history, and a usable website.

This is mainly an AI application engineering and backend project. It teaches APIs, orchestration, authentication, databases, testing, subprocesses, Docker, Git, and frontend integration. It does not demonstrate training neural networks or researching a new ML algorithm.

## Why build this when Codex can write code?

Coding assistants already have powerful development capabilities. This project is valuable as a focused system you can understand, explain, and improve. Your useful contribution is the visible workflow: what was requested, what changed, which tests failed, which repairs happened, what was validated, and where the checkpoint was saved.

Do not claim it is better than established coding assistants. Explain how you designed a small reliable workflow around an existing model and how you prevent misleading success reports.

## Improvements over the starting project

- Real provider calls drive implementation rather than selecting a fixed application template.
- Existing files are inspected and updated for follow-up requests.
- Executable test results determine success; an AI saying "passed" is insufficient.
- Previous tests are preserved separately for regression checks.
- Development steps, repair iterations, calls, tokens, and time all have limits.
- Failed runs remain visibly failed and cannot create successful checkpoints.
- Actual diffs and Git commit IDs provide evidence of changes.
- A clean dashboard shows live progress and explains failures.
- Docker confines execution; users can only access their own projects.

## Advantages

You can see the full development process in one place. It encourages testing, incremental changes, and version history. Code is local and available in an editor. A failed attempt can still leave useful files and an explanation. It is a strong learning project because its components connect to real software engineering tasks.

## Disadvantages

AI can misunderstand requirements or produce incorrect code. Tests can also be incomplete or incorrectly written. Provider quotas, outages, and latency affect live generation. Complex applications may exceed the budget. Maintaining secure execution is difficult. The first version supports a deliberately limited web stack, and generated applications still need human review.

Saved previews can be demonstrated without further AI calls, but new AI work needs a working provider. Preview example data currently resets on restart. Local Git backup is different from publishing to GitHub.

## Is it a good fresher project?

Yes, if you can demonstrate and explain the implementation honestly. A small working system with evidence is more convincing than a long feature list. Be ready to open the code for one request, one failed test, the repair, and the resulting commit.

Good interview discussion points include why there are separate agents, why an LLM cannot decide test success alone, how retries stop, how users are isolated, how paths are validated, and how a wishlist request preserves the original checkout.

A reasonable resume description is: "Built a local AI development platform with FastAPI, LangGraph, PostgreSQL, Docker execution, executable regression testing, and Git checkpoints." Add performance or success-rate numbers only after measuring them.

## A sensible next step

After the localhost workflow is reliable, add automated browser scenarios and a durable background worker. Then design deployment and isolation for a live website. Improve one measured weakness at a time instead of adding many agent names.
