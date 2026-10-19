# Run ForgeFlow on a new laptop

Extract **ForgeFlow-Final.zip** completely. Open the **ForgeFlow** folder inside it. Do not run files from inside the ZIP viewer.

This package includes the latest platform and saved generated project source. It contains no personal API keys, platform accounts, databases, uploads, or original run history. Some included projects are unfinished drafts. Register your own local account and import the projects below.

## 1. Install prerequisites

- Python 3.12 or newer. On Windows select **Add Python to PATH**, then reopen PowerShell.
- Git.
- For the recommended Docker mode: Docker Desktop running Linux containers. Linux can use Docker Engine and the Compose plugin.
- Internet for the first installation. Node.js is not required; compiled styles are included.

## 2. Windows setup with Docker

Open PowerShell inside the extracted ForgeFlow folder. Keep Docker Desktop running. Run one command at a time:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts/setup.py
.\.venv\Scripts\python.exe scripts/manage.py start
```

If `python` is not found, try `py -3` for the first command. The launcher builds the runner image and starts PostgreSQL automatically. The first run may take several minutes. You do not need an API key to start the platform or preview saved source.

Open **http://127.0.0.1:8791** and register your own username and password.

In a second PowerShell window in the ForgeFlow folder, import the included projects:

```powershell
.\.venv\Scripts\python.exe scripts/import_projects.py --username friend
```

Replace `friend` with the username you just registered. Refresh the dashboard. Open a project and click **Start draft preview**. Imported source is not automatically marked tested. An empty or unfinished project may not have a runnable preview.

## 3. Windows alternative without Docker

Use a separate extraction if you already set up Docker mode. This alternative uses SQLite and runs generated code directly under your laptop account; use trusted projects and prompts.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts/setup.py --local
.\.venv\Scripts\python.exe scripts/manage.py start --local
```

Register at http://127.0.0.1:8791, then import into the same mode:

```powershell
.\.venv\Scripts\python.exe scripts/import_projects.py --username friend --local
```

Docker and no-Docker modes have separate account databases. Use one mode per extracted copy. Setup never overwrites an existing `.env`.

## 4. macOS / Linux

Use the same steps with `python3` and `.venv/bin/python`:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python scripts/setup.py
```

For no-Docker mode, use `scripts/setup.py --local` instead. Open `.env` and change `BROWSER_CHANNEL=msedge` to `BROWSER_CHANNEL=` (empty). On Linux, browser libraries may require `.venv/bin/python -m playwright install-deps chromium`.

```bash
# Docker must be running for this mode:
.venv/bin/python scripts/manage.py start
.venv/bin/python scripts/import_projects.py --username friend

# For the no-Docker mode, use these instead:
.venv/bin/python scripts/manage.py start --local
.venv/bin/python scripts/import_projects.py --username friend --local
```

Register before importing. The platform URL is the same: http://127.0.0.1:8791.

## 5. Add your own DeepSeek API key

Open the private `.env` created by setup. It is configured for DeepSeek; fill in:

```env
LLM_API_KEY=your_own_deepseek_key
```

Save and restart ForgeFlow. GPT is disabled (`GPT_ENABLED=false`). Keep your `.env` private.

Setup, importing, opening previews, and downloading source do not call an AI model. Submitting a new request or pressing **Resume saved work** uses your provider credits. The distributed defaults pause work after configured call/token/time limits and retain the source. A token budget is not an exact dollar cap. Do not repeatedly resume the same unresolved problem.

## 6. Start and stop later

Windows Docker mode: double-click **Start ForgeFlow.cmd** or **Stop ForgeFlow.cmd**. Or run:

```powershell
.\.venv\Scripts\python.exe scripts/manage.py start
.\.venv\Scripts\python.exe scripts/manage.py stop
```

No-Docker mode: use the **without Docker** shortcuts, or append `--local` to both commands. On macOS/Linux replace the Python path with `.venv/bin/python`.

Keep Docker running when using Docker mode. Closing the browser does not stop the server. Stop preserves your project files and database. To stop PostgreSQL as well, run `docker compose stop database`; the data remains stored.

## 7. Start manually with Uvicorn for a presentation

Complete the first setup and at least one normal start first, then stop the launcher to free port 8791.

```powershell
# Docker mode only:
docker compose up -d database

# Both modes, with their corresponding .env created by setup:
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8791
```

Open localhost:8791. Press **Ctrl+C** in that terminal to stop Uvicorn. Use one worker. On macOS/Linux use `.venv/bin/python`. You must keep this laptop running for its localhost website to work.

## Common issues

- **Port already used:** stop the other copy from its own folder, or add `--port 8792` to both launcher start and stop, then visit localhost:8792.
- **No projects:** register first, use the matching username, and include `--local` when importing into no-Docker mode.
- **Browser checks unavailable:** Windows normally uses Edge. Install Chromium with `.\.venv\Scripts\python.exe -m playwright install chromium` and set `BROWSER_CHANNEL=` if needed.
- **API call fails:** check the provider key, selected model access, balance, and provider limits. Saved previews are independent of AI calls.
- **Preview/review pending:** open the draft and check the reported issues. **Download latest draft** includes current source; **Last validated ZIP** is the earlier tested checkpoint if one exists.

Back up your workspace and local database before reinstalling. Never share your private `.env` with an API key.
