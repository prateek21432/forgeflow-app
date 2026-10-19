/* Plain browser code: model text is always inserted as text, never HTML. */
(() => {
  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => [...document.querySelectorAll(selector)];
  const csrf = $('meta[name="csrf-token"]')?.content || "";
  const projectId = document.body.dataset.projectId;
  let toastTimer;

  function toast(message) {
    const box = $("#toast");
    box.textContent = message;
    box.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      box.hidden = true;
    }, 7000);
  }

  function element(tag, text = "", className = "") {
    const node = document.createElement(tag);
    node.textContent = text;
    node.className = className;
    return node;
  }

  async function request(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: {
        "X-CSRF-Token": csrf,
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...options.headers,
      },
    });
    if (response.status === 401) {
      location.href = "/login";
      throw new Error("Sign in again to continue.");
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok)
      throw new Error(
        typeof data.detail === "string"
          ? data.detail
          : "The request could not be completed.",
      );
    return data;
  }

  $$("[data-auth-tab]").forEach((button) =>
    button.addEventListener("click", () => {
      $$("[data-auth-tab]").forEach((tab) =>
        tab.setAttribute("aria-selected", String(tab === button)),
      );
      $$(".auth-form").forEach((form) => {
        form.hidden = form.id !== button.dataset.authTab;
      });
    }),
  );

  $$("[data-panel]").forEach((button) =>
    button.addEventListener("click", () => {
      $$("[data-panel]").forEach((tab) =>
        tab.setAttribute("aria-selected", String(tab === button)),
      );
      $$(".tab-panel").forEach((panel) => {
        panel.hidden = panel.id !== button.dataset.panel;
      });
      if (button.dataset.panel === "files-panel")
        loadFiles().catch((error) => toast(error.message));
    }),
  );

  let lastState = null;
  let modelUpdate = Promise.resolve();
  const modelChoice = $("#model-profile");
  if (modelChoice) {
    let savedChoice = modelChoice.value;
    modelChoice.addEventListener("change", () => { modelUpdate = (async () => {
      const next = modelChoice.value;
      modelChoice.disabled = true;
      try {
        await request("/settings/model", { method: "POST", body: JSON.stringify({profile: next}) });
        savedChoice = next;
        toast("Model selected for your next request or Resume.");
      } catch (error) {
        modelChoice.value = savedChoice;
        toast(error.message);
      } finally {
        modelChoice.disabled = Boolean(lastState?.active_run_id);
      }
    })(); });
  }
  if (!projectId) return;
  const base = "/projects/" + projectId;
  const seenEvents = new Set();
  let eventCursor = 0;
  let stateSignature = "";
  let submitting = false;
  let closed = false;
  let latestRunId = null;
  const activity = $("#activity");
  const followLive = $("#follow-live");
  let scrollFrame;
  let progressWasBusy = false;
  let completedUntil = 0;

  function followActivity() {
    if (!followLive.checked) return;
    cancelAnimationFrame(scrollFrame);
    scrollFrame = requestAnimationFrame(() => {
      activity.scrollTop = activity.scrollHeight;
    });
  }

  function fitConversation() {
    const layout = $(".project-layout");
    const top = layout.getBoundingClientRect().top + window.scrollY;
    const height = Math.max(240, (window.visualViewport?.height || innerHeight) - top - 16);
    layout.style.setProperty("--conversation-height", height + "px");
    followActivity();
  }

  followLive.addEventListener("change", followActivity);
  window.addEventListener("resize", fitConversation);
  window.visualViewport?.addEventListener("resize", fitConversation);
  new ResizeObserver(fitConversation).observe($(".project-heading"));
  document.fonts.ready.then(fitConversation);
  fitConversation();

  function renderProgress(data) {
    const popup = $("#progress-popup");
    const busy = Boolean(data.active_run_id);
    if (modelChoice) modelChoice.disabled = busy;
    if (progressWasBusy && !busy && data.status === "completed") {
      completedUntil = Date.now() + 4000;
      setTimeout(() => {
        if (lastState) renderProgress(lastState);
      }, 4100);
    }
    if (busy || data.status !== "completed") completedUntil = 0;
    progressWasBusy = busy;
    const stages = ["planning", "developing", "testing", "validating", "completed"];
    const stage = data.status === "queued" ? "planning"
      : data.status === "checkpointing" ? "validating" : data.status;
    const index = stages.indexOf(stage);
    popup.hidden = !busy && Date.now() >= completedUntil;
    $("#progress-title").textContent = busy ? "Project in progress" : "Project ready";
    if (index >= 0) {
      popup.style.setProperty("--stage", index);
      const descriptions = ["Understanding your request", "Building your application",
        "Testing and repairing", "Verifying before release", "Your preview is ready"];
      $("#progress-description").textContent = data.status === "checkpointing"
        ? "Saving the verified checkpoint" : descriptions[index];
      $$('[data-stage]').forEach((node, position) => {
        node.classList.toggle("active", position === index);
        node.classList.toggle("done", position < index);
        if (position === index) node.setAttribute("aria-current", "step");
        else node.removeAttribute("aria-current");
      });
    }
    fitConversation();
  }

  function filterActivity() {
    const showAll = $("#activity-filter").value === "all";
    $$("#activity .event").forEach((item) => {
      item.hidden =
        !showAll &&
        latestRunId !== null &&
        item.dataset.runId !== String(latestRunId);
    });
  }

  $("#activity-filter").addEventListener("change", () => {
    filterActivity();
    followActivity();
  });

  function addEvent(event) {
    if (seenEvents.has(event.id)) return;
    seenEvents.add(event.id);
    $("#conversation-empty")?.remove();
    const activity = $("#activity");
    const item = element(
      "article",
      "",
      "event" +
        (event.agent === "You" ? " user" : "") +
        (event.kind === "error" || event.kind === "tool_error" ? " error" : ""),
    );
    const avatar = element("span", event.agent.slice(0, 1), "event-avatar");
    item.dataset.runId = event.run_id;
    const body = element("div", "", "event-content");
    const meta = element("div", "", "event-meta");
    const timestamp = new Date(event.created_at);
    meta.append(
      element("span", event.agent, "event-agent"),
      element(
        "span",
        timestamp.toLocaleTimeString([], {
          hour: "2-digit",
          minute: "2-digit",
        }),
        "event-time",
      ),
    );
    body.append(meta, element("p", event.message, "event-message"));
    if (event.details?.output) {
      const details = element("details");
      details.append(
        element("summary", "View terminal output"),
        element("pre", event.details.output),
      );
      body.append(details);
    }
    if (event.kind === "browser_finished") {
      const details = element("details");
      details.append(element("summary", "View browser checks"));
      for (const check of event.details?.cases || []) {
        details.append(element("p", `${check.status}: ${check.name}`));
        if (check.reason) details.append(element("pre", check.reason));
      }
      for (const limitation of event.details?.limitations || []) {
        details.append(element("p", "Unverified: " + limitation));
      }
      if (event.details?.error) details.append(element("pre", event.details.error));
      body.append(details);
    }
    item.append(avatar, body);
    activity.append(item);
    filterActivity();
    followActivity();
  }

  async function loadEvents() {
    const firstLoad = eventCursor === 0;
    const rows = await request(base + "/events?after=" + eventCursor);
    rows.forEach(addEvent);
    if (rows.length) eventCursor = rows[rows.length - 1].id;
    if (rows.length === 200) await loadEvents();
    if (firstLoad) followActivity();
  }

  function badge(text, status = text) {
    const node = element("span", text.replaceAll("_", " "), "badge");
    node.dataset.status = status;
    return node;
  }

  function renderState(data) {
    lastState = data;
    const busy = Boolean(data.active_run_id);
    $("#resume-verification").hidden = !data.can_resume_verification;
    $("#resume-verification").disabled = busy || submitting;
    $("#project-status").textContent = !busy && data.preview_is_draft && data.demo_url
      ? "Preview ready — review pending" : data.status.replaceAll("_", " ");
    $("#draft-notice").hidden = busy || !data.preview_is_draft || !data.draft_download_url;
    $("#project-status").dataset.status = data.status;
    $("#send-request").disabled = busy || submitting;
    $("#message").disabled = busy;
    $("#cancel-run").hidden = !busy;
    $("#run-hint").textContent = busy
      ? "Working. You can stop after the current operation."
      : "Your files stay in this project.";
    renderProgress(data);
    const current = data.runs[0];
    latestRunId = current?.id ?? null;
    filterActivity();
    followActivity();
    $("#current-task").textContent = current?.request || "No request yet.";
    $("#current-task").title = current?.request || "";
    $("#iteration").textContent = current
      ? "Attempt " + current.iteration
      : "—";
    const plan = $("#plan");
    plan.replaceChildren();
    if (current?.plan) {
      const list = element("ul", "", "plan-list");
      (current.plan.tasks || []).forEach((task) => list.append(element("li", task)));
      plan.append(list);
      $("#risk").textContent = current.plan.risk || "Not assessed";
      $("#risk").dataset.status = current.plan.risk || "";
      $("#risk-reason").textContent = current.plan.risk_reason || "No change-impact assessment was recorded for this run.";
      $("#affected-files").replaceChildren(
        ...(current.plan.affected_files || []).map((file) => element("span", file)),
      );
    }
    const summary = $("#test-summary");
    summary.replaceChildren();
    const latest = data.tests[0];
    if (latest) {
      [
        ["Passed", latest.passed, "pass"],
        ["Failed", latest.failed, "fail"],
        ["Skipped", latest.skipped, ""],
      ].forEach(([label, count, color]) => {
        const metric = element("div", "", "test-metric " + color);
        metric.append(
          element("strong", String(count)),
          element("small", label),
        );
        summary.append(metric);
      });
    } else summary.append(element("span", "No tests run yet.", "muted"));

    const changes = current?.changes || [];
    $("#change-count").textContent = changes.length;
    const changePanel = $("#changes-panel");
    changePanel.replaceChildren();
    if (!changes.length)
      changePanel.append(
        element("p", "No file changes recorded yet.", "empty-line"),
      );
    changes.forEach((change) => {
      const details = element("details", "", "diff-card");
      const heading = element("summary");
      heading.append(
        element("span", change.path),
        element(
          "span",
          "+" + change.added + " / −" + change.removed,
          "diff-count",
        ),
      );
      details.append(heading, element("pre", change.diff));
      changePanel.append(details);
    });
    renderTests(data.tests);
    renderHistory(data);
    const download = $("#download-report");
    const projectDownload = $("#download-project");
    const latestDownload = data.preview_is_draft ? data.draft_download_url : data.download_url;
    projectDownload.hidden = !latestDownload;
    if (latestDownload) projectDownload.href = latestDownload;
    projectDownload.textContent = data.preview_is_draft ? "Download latest draft" : "Download project ZIP";
    projectDownload.title = data.preview_is_draft ? "Latest saved files; verification is incomplete" : "Latest validated checkpoint";
    const validatedDownload = $("#download-validated");
    validatedDownload.hidden = !data.preview_is_draft || !data.download_url;
    if (data.download_url) validatedDownload.href = data.download_url;
    download.hidden = !current;
    if (current) download.href = base + "/report/" + current.id;
    const url = data.demo_url;
    $("#open-demo").hidden = !url;
    $("#start-demo").hidden = Boolean(url);
    $("#start-demo").disabled = busy || !data.can_start_preview;
    $("#start-demo").textContent = data.preview_is_draft ? "Start draft preview" : "Start preview";
    $("#stop-demo").hidden = !url;
    if (url) $("#open-demo").href = url;
    $("#preview-state").textContent = url
      ? (data.preview_is_draft ? "Draft preview — checks need attention. " : "Running at ") + url
      : "Preview is stopped.";
    const frame = $("#demo-frame");
    frame.hidden = !url;
    if (url && frame.dataset.url !== url) {
      frame.src = url;
      frame.dataset.url = url;
    }
    if (!url) {
      frame.removeAttribute("src");
      delete frame.dataset.url;
    }
  }

  function renderTests(tests) {
    const panel = $("#tests-panel");
    panel.replaceChildren();
    if (!tests.length)
      panel.append(element("p", "No tests have been run.", "empty-line"));
    tests.forEach((test) => {
      const section = element("section", "", "test-run");
      const heading = element(
        "h3",
        test.phase + " · run " + test.run_id + " · ",
      );
      heading.append(badge(test.status));
      section.append(heading);
      test.cases.forEach((testCase) => {
        const row = element("div", "", "test-case");
        row.append(badge(testCase.status), element("code", testCase.name));
        if (testCase.reason) row.append(element("pre", testCase.reason));
        section.append(row);
      });
      if (!test.cases.length)
        section.append(
          element(
            "p",
            "No test cases collected. This cannot count as a pass.",
            "muted",
          ),
        );
      const output = element("details");
      output.append(
        element("summary", "View full test output"),
        element("pre", test.output),
      );
      section.append(output);
      panel.append(section);
    });
  }

  function renderHistory(data) {
    const panel = $("#history-panel");
    panel.replaceChildren();
    if (!data.checkpoints.length)
      panel.append(element("p", "No validated checkpoints yet.", "empty-line"));
    data.checkpoints.forEach((cp) => {
      const row = element("article", "", "checkpoint-row");
      const body = element("div");
      body.append(
        element("strong", "CP-" + String(cp.id).padStart(3, "0")),
        element("p", cp.description),
        element(
          "code",
          cp.commit.slice(0, 12) +
            " · " +
            cp.branch +
            " · " +
            cp.test_count +
            " tests",
        ),
      );
      const git = data.git.find((entry) => entry.commit === cp.commit);
      row.append(
        body,
        badge(
          git?.push_status === "local_mirror"
            ? "Local backup"
            : git?.push_status || "Saved",
        ),
      );
      if (git?.error) body.append(element("p", git.error));
      panel.append(row);
    });
    if (data.runs.length) panel.append(element("h3", "Run history", "mt-6"));
    data.runs.forEach((run) => {
      const details = element("details", "", "diff-card");
      const heading = element("summary");
      heading.append(
        element("span", "#" + run.id + " · " + run.request.slice(0, 100)),
        badge(run.status),
      );
      details.append(
        heading,
        element("pre", run.error || run.summary || "Run in progress."),
      );
      panel.append(details);
    });
  }

  async function refresh() {
    if (closed) return;
    try {
      await loadEvents();
      const data = await request(base + "/api/state");
      const signature = JSON.stringify(data);
      if (signature !== stateSignature) {
        stateSignature = signature;
        renderState(data);
      }
    } catch (error) {
      $("#connection").textContent = "Reconnecting…";
      $("#connection").classList.remove("online");
    } finally {
      if (!closed) setTimeout(refresh, 2500);
    }
  }

  function connect() {
    if (closed) return;
    const socket = new WebSocket(
      (location.protocol === "https:" ? "wss://" : "ws://") +
        location.host +
        "/ws/projects/" +
        projectId,
    );
    socket.onopen = () => {
      $("#connection").textContent = "Live updates";
      $("#connection").classList.add("online");
      loadEvents().catch(() => {});
    };
    socket.onmessage = (event) => {
      try {
        addEvent(JSON.parse(event.data));
      } catch {
        /* Polling restores malformed/missed events. */
      }
    };
    socket.onclose = () => {
      if (!closed) setTimeout(connect, 4000);
    };
    socket.onerror = () => socket.close();
  }

  $("#chat").addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = $("#message").value.trim();
    if (!message || submitting || lastState?.active_run_id) return;
    submitting = true;
    $("#send-request").disabled = true;
    try {
      await modelUpdate;
      await request(base + "/chat", {
        method: "POST",
        body: JSON.stringify({ message }),
      });
      $("#message").value = "";
      renderState(await request(base + "/api/state"));
      await loadEvents();
    } catch (error) {
      toast(error.message);
    } finally {
      submitting = false;
      $("#send-request").disabled = Boolean(lastState?.active_run_id);
    }
  });

  $$("[data-prompt]").forEach((button) =>
    button.addEventListener("click", () => {
      $("#message").value = button.dataset.prompt;
      $("#message").focus();
    }),
  );

  $("#cancel-run").addEventListener("click", async () => {
    try {
      await request(base + "/cancel", { method: "POST" });
      toast("Stop requested. Waiting for the current operation to finish.");
    } catch (error) {
      toast(error.message);
    }
  });

  $("#resume-verification").addEventListener("click", async () => {
    if (submitting || lastState?.active_run_id) return;
    submitting = true;
    $("#resume-verification").disabled = true;
    try {
      await modelUpdate;
      await request(base + "/resume-verification", { method: "POST" });
      renderState(await request(base + "/api/state"));
      await loadEvents();
    } catch (error) {
      toast(error.message);
    } finally {
      submitting = false;
      $("#resume-verification").disabled = Boolean(lastState?.active_run_id);
    }
  });

  ["start", "stop"].forEach((action) =>
    $("#" + action + "-demo").addEventListener("click", async (event) => {
      event.currentTarget.disabled = true;
      try {
        await request(base + "/demo/" + action, { method: "POST" });
        renderState(await request(base + "/api/state"));
      } catch (error) {
        toast(error.message);
      } finally {
        $("#" + action + "-demo").disabled = false;
      }
    }),
  );

  async function loadFiles() {
    const data = await request(base + "/files");
    const list = $("#file-list");
    list.replaceChildren();
    if (!data.files.length)
      list.append(element("p", "No source files yet.", "muted"));
    data.files.forEach((path) => {
      const button = element("button", path);
      button.addEventListener("click", async () => {
        try {
          $("#file-content").textContent = (
            await request(base + "/files?path=" + encodeURIComponent(path))
          ).content;
        } catch (error) {
          toast(error.message);
        }
      });
      list.append(button);
    });
  }

  window.addEventListener("beforeunload", () => {
    closed = true;
  });
  refresh();
  connect();
})();
