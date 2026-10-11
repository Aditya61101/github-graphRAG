import assert from "node:assert/strict";
import { after, before, beforeEach, test } from "node:test";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { createServer } from "vite";
import { openBrowser } from "./browser-helper.mjs";

let browser, server, base, fixture, screenshots;
const token = `e30.${Buffer.from(JSON.stringify({ sub: "usr_1", exp: Date.now() / 1000 + 3600 })).toString("base64url")}.test`;
const completed = {
  onboarding_completed: true,
  onboarding_completed_at: "2026-10-11T00:00:00",
  next: "/projects",
};
function setup() {
  return {
    completion: {
      onboarding_completed: false,
      onboarding_completed_at: null,
      next: "/install",
    },
    eligible: true,
    outcome: null,
    discoveryFailure: false,
    completionFailure: false,
    completionUnconfirmed: false,
    verificationOutcome: "success",
    discoveryDelay: 0,
    calls: [],
  };
}
function groups() {
  return ["alice", "example-org"].map((account_login, i) => ({
    id: String(i + 1),
    account_login,
    account_type: i ? "Organization" : "User",
    status: "ACTIVE",
    repository_selection: "selected",
    public_repository_count: 1,
    repositories: [
      {
        github_repository_id: String(i + 10),
        installation_id: String(i + 1),
        full_name: `${account_login}/accessible-repository`,
        name: "accessible-repository",
        repository_url: `https://github.com/${account_login}/accessible-repository`,
        is_private: false,
      },
    ],
  }));
}
before(async () => {
  fixture = setup();
  server = await createServer({
    server: { host: "127.0.0.1", port: 0, hmr: false },
    define: {
      "import.meta.env.VITE_BACKEND_URL": JSON.stringify("/__onboarding_api"),
    },
    plugins: [
      {
        name: "onboarding-test-api",
        configureServer(vite) {
          vite.middlewares.use(
            "/__onboarding_api",
            async (request, response) => {
              fixture.calls.push([request.method, request.url]);
              response.setHeader("Content-Type", "application/json");
              const json = (data, status = 200) => {
                response.statusCode = status;
                response.end(JSON.stringify(data));
              };
              if (request.url === "/auth/me") {
                return json({
                  ...fixture.completion,
                  user: {
                    id: "usr_1",
                    username: "alice",
                    email: null,
                    avatar_url: null,
                    onboarding_completed_at:
                      fixture.completion.onboarding_completed_at,
                  },
                  reconnect_required: false,
                  installation_onboarding_required:
                    !fixture.completion.onboarding_completed,
                  installation_discovery_status: "not_requested",
                  installations: [],
                });
              }
              if (request.url === "/auth/onboarding") {
                if (fixture.discoveryDelay)
                  await new Promise((resolve) =>
                    setTimeout(resolve, fixture.discoveryDelay)
                  );
                if (fixture.discoveryFailure)
                  return json(
                    {
                      detail: {
                        outcome: "temporary_failure",
                        message: "GitHub temporarily unavailable",
                      },
                    },
                    503
                  );
                return json({
                  ...fixture.completion,
                  eligible: fixture.eligible,
                  screen: fixture.eligible
                    ? "available_repositories"
                    : "install",
                  installation_discovery_status: "complete",
                  installation_outcome: fixture.outcome,
                  installations: fixture.eligible ? groups() : [],
                });
              }
              if (request.url === "/auth/onboarding/complete") {
                if (fixture.completionFailure)
                  return json(
                    {
                      detail: {
                        outcome: "no_eligible_repositories",
                        message: "Repository access changed. Try again.",
                      },
                    },
                    409
                  );
                if (!fixture.completionUnconfirmed)
                  fixture.completion = completed;
                return json(fixture.completion);
              }
              if (request.url === "/auth/onboarding/installation/verify") {
                fixture.outcome = fixture.verificationOutcome;
                if (fixture.outcome === "success")
                  fixture.completion = completed;
                return json({
                  ...fixture.completion,
                  installation_outcome: fixture.outcome,
                });
              }
              if (request.url === "/auth/github/install") {
                return json({
                  installation_url:
                    base + "/__github_install?state=backend-bound-state",
                });
              }
              // Let the unmodified dashboard render after navigation, without external APIs.
              if (request.url === "/repositories") return json([]);
              return json({ detail: "Unexpected test request" }, 404);
            }
          );
        },
      },
    ],
  });
  await server.listen();
  base = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await openBrowser();
  await browser.call("Page.addScriptToEvaluateOnNewDocument", {
    source: `localStorage.setItem('token', ${JSON.stringify(token)}); localStorage.setItem('theme', 'light');`,
  });
  screenshots = await mkdtemp(
    path.join(tmpdir(), "decisionguard-onboarding-qa-")
  );
});
beforeEach(async () => {
  fixture = setup();
  await browser.call("Emulation.setDeviceMetricsOverride", {
    width: 1280,
    height: 900,
    deviceScaleFactor: 1,
    mobile: false,
  });
});
after(async () => {
  await browser?.close();
  await server?.close();
});
const bodyContains = (text) =>
  `document.body.innerText.includes(${JSON.stringify(text)})`;
const pathIs = (value) =>
  `window.location.pathname === ${JSON.stringify(value)}`;
async function login(outcome = null) {
  await browser.navigate(
    base +
      "/auth/success?" +
      new URLSearchParams({
        token,
        next: "/projects",
        ...(outcome ? { installation_outcome: outcome } : {}),
      })
  );
}

test("first-time login ignores next hint and loads B1 only after confirmed empty discovery", async () => {
  fixture.eligible = false;
  fixture.discoveryDelay = 500;
  await login();
  await browser.waitFor(bodyContains("Checking repositories accessible"));
  assert.equal(
    await browser.evaluate(
      bodyContains("No accessible public repositories were found")
    ),
    false
  );
  await browser.waitFor(bodyContains("Install DecisionGuard on GitHub"));
  assert.equal(await browser.evaluate(pathIs("/install")), true);
  assert.ok(
    fixture.calls.findIndex((c) => c[1] === "/auth/me") <
      fixture.calls.findIndex((c) => c[1] === "/auth/onboarding")
  );
});

test("B2 accordion expands with pointer and keyboard and lists only supplied accessible repositories", async () => {
  await login();
  await browser.waitFor(bodyContains("Available repositories to protect"));
  await browser.evaluate(
    "document.querySelectorAll('[data-slot=collapsible-trigger]')[0].click()"
  );
  await browser.waitFor(
    "document.querySelectorAll('[data-slot=collapsible-trigger]')[0].getAttribute('aria-expanded') === 'true'"
  );
  await browser.waitFor(bodyContains("alice/accessible-repository"));
  await browser.evaluate(
    "document.querySelectorAll('[data-slot=collapsible-trigger]')[1].focus()"
  );
  await browser.call("Input.dispatchKeyEvent", {
    type: "keyDown",
    key: "Enter",
    code: "Enter",
    windowsVirtualKeyCode: 13,
    text: "\r",
  });
  await browser.call("Input.dispatchKeyEvent", {
    type: "keyUp",
    key: "Enter",
    code: "Enter",
    windowsVirtualKeyCode: 13,
    text: "\r",
  });
  await browser.waitFor(
    "document.querySelectorAll('[data-slot=collapsible-trigger]')[1].getAttribute('aria-expanded') === 'true'"
  );
  await browser.waitFor(bodyContains("example-org/accessible-repository"));
  assert.equal(
    await browser.evaluate(
      "document.querySelectorAll('[role=region] a').length"
    ),
    2
  );
  assert.equal(
    fixture.calls.some((c) => c[1] === "/repositories"),
    false
  );
  await browser.call("Input.dispatchKeyEvent", {
    type: "keyDown",
    key: " ",
    code: "Space",
    windowsVirtualKeyCode: 32,
  });
  await browser.call("Input.dispatchKeyEvent", {
    type: "keyUp",
    key: " ",
    code: "Space",
    windowsVirtualKeyCode: 32,
  });
  await browser.waitFor(
    "document.querySelectorAll('[data-slot=collapsible-trigger]')[1].getAttribute('aria-expanded') === 'false'"
  );
  const image = await browser.call("Page.captureScreenshot", { format: "png" });
  await writeFile(
    path.join(screenshots, "available-desktop.png"),
    Buffer.from(image.data, "base64")
  );
});

test("continue updates auth cache before entering the unmodified projects route without a guard loop", async () => {
  await login();
  await browser.waitFor(bodyContains("Continue with available repositories"));
  await browser.click("Continue with available repositories");
  await browser.waitFor(pathIs("/projects"));
  await browser.waitFor(bodyContains("Your projects"));
  assert.equal(
    fixture.calls.filter((c) => c[1] === "/auth/onboarding/complete").length,
    1
  );
  assert.equal(await browser.evaluate(pathIs("/projects")), true);
});

test("failed or unconfirmed completion never navigates to projects", async () => {
  for (const unconfirmed of [false, true]) {
    fixture = setup();
    fixture.completionFailure = !unconfirmed;
    fixture.completionUnconfirmed = unconfirmed;
    await login();
    await browser.waitFor(bodyContains("Continue with available repositories"));
    await browser.click("Continue with available repositories");
    await browser.waitFor(
      bodyContains(
        unconfirmed
          ? "Onboarding has not been confirmed"
          : "Repository access changed"
      )
    );
    assert.equal(await browser.evaluate(pathIs("/install")), true);
    assert.equal(fixture.completion.onboarding_completed, false);
  }
});

test("returning users and successful installation returns go directly to projects without B2 discovery", async () => {
  for (const outcome of [null, "success"]) {
    fixture = setup();
    fixture.completion = completed;
    fixture.eligible = false;
    await login(outcome);
    await browser.waitFor(pathIs("/projects"));
    await browser.waitFor(bodyContains("Your projects"));
    assert.equal(
      fixture.calls.some((c) => c[1] === "/auth/onboarding"),
      false
    );
  }
});

test("a forged success hint cannot complete onboarding", async () => {
  await login("success");
  await browser.waitFor(bodyContains("Available repositories to protect"));
  assert.equal(await browser.evaluate(pathIs("/install")), true);
  assert.equal(
    fixture.calls.some((c) => c[0] === "POST"),
    false
  );
  assert.equal(
    await browser.evaluate("location.search.includes('token=')"),
    false
  );
});

test("cancellation with existing grants stays recoverable and connect-more uses the backend URL", async () => {
  fixture.outcome = "cancelled";
  await login("cancelled");
  await browser.waitFor(bodyContains("GitHub installation was cancelled"));
  assert.equal(await browser.evaluate(pathIs("/install")), true);
  assert.equal(
    fixture.calls.some((c) => c[0] === "POST"),
    false
  );
  assert.equal(
    await browser.evaluate(bodyContains("Retry installation verification")),
    false
  );
  await browser.click("Connect more repositories");
  await browser.waitFor(pathIs("/__github_install"));
  assert.equal(
    await browser.evaluate("new URLSearchParams(location.search).get('state')"),
    "backend-bound-state"
  );
});

test("pending approval and no eligible repositories expose accurate recovery", async () => {
  for (const outcome of ["pending_approval", "no_eligible_repositories"]) {
    fixture = setup();
    fixture.eligible = false;
    fixture.outcome = outcome;
    await login(outcome);
    await browser.waitFor(
      bodyContains(
        outcome === "pending_approval"
          ? "awaiting organization approval"
          : "No accessible public repositories were granted"
      )
    );
    assert.equal(await browser.evaluate(pathIs("/install")), true);
    assert.equal(
      await browser.evaluate(bodyContains("Check access again")),
      true
    );
    assert.equal(fixture.completion.onboarding_completed, false);
  }
});

test("temporary discovery and return failures remain distinct from B1 and retry verifies server outcome", async () => {
  fixture.discoveryFailure = true;
  fixture.outcome = "temporary_failure";
  fixture.verificationOutcome = "temporary_failure";
  await login("temporary_failure");
  await browser.waitFor(bodyContains("Unable to discover repositories"));
  assert.equal(
    await browser.evaluate(
      bodyContains("No accessible public repositories were found")
    ),
    false
  );
  await browser.click("Retry installation verification");
  await browser.waitFor(
    "Array.from(document.querySelectorAll('button')).some(b => b.textContent.trim() === 'Retry installation verification' && !b.disabled)"
  );
  assert.equal(await browser.evaluate(pathIs("/install")), true);
  fixture.verificationOutcome = "success";
  await browser.click("Retry installation verification");
  await browser.waitFor(pathIs("/projects"));
  assert.equal(
    fixture.calls.filter((c) => c[1] === "/auth/onboarding/installation/verify")
      .length,
    2
  );
});

test("B2 remains usable on a narrow viewport in dark mode", async () => {
  await browser.call("Emulation.setDeviceMetricsOverride", {
    width: 360,
    height: 800,
    deviceScaleFactor: 1,
    mobile: false,
  });
  await login();
  await browser.waitFor(bodyContains("Available repositories to protect"));
  await browser.evaluate(
    "document.documentElement.classList.add('dark'); document.querySelector('[data-slot=collapsible-trigger]').click()"
  );
  await browser.waitFor(bodyContains("alice/accessible-repository"));
  assert.equal(
    await browser.evaluate(
      "document.documentElement.scrollWidth <= window.innerWidth"
    ),
    true
  );
  assert.equal(
    await browser.evaluate(
      "document.querySelector('main').scrollWidth <= window.innerWidth"
    ),
    true
  );
  const image = await browser.call("Page.captureScreenshot", {
    format: "png",
    captureBeyondViewport: true,
  });
  await writeFile(
    path.join(screenshots, "available-mobile-dark.png"),
    Buffer.from(image.data, "base64")
  );
  console.log(`Onboarding visual checks: ${screenshots}`);
});

test("completed users can manage access through the unchanged create-project drawer", async () => {
  fixture.completion = completed;
  await login();
  await browser.waitFor(bodyContains("Your projects"));
  await browser.click("Create project");
  await browser.waitFor(
    "Array.from(document.querySelectorAll('a')).some(a => a.innerText.trim() === 'Manage GitHub repository access')"
  );
  await browser.evaluate(
    "Array.from(document.querySelectorAll('a')).find(a => a.innerText.trim() === 'Manage GitHub repository access').click()"
  );
  await browser.waitFor(pathIs("/install"));
  await browser.waitFor(bodyContains("Manage access on GitHub"));
  assert.equal(
    await browser.evaluate(
      bodyContains("Continue with available repositories")
    ),
    false
  );
  assert.equal(fixture.completion.onboarding_completed, true);
  await browser.click("Manage access on GitHub");
  await browser.waitFor(pathIs("/__github_install"));
  assert.equal(
    await browser.evaluate("new URLSearchParams(location.search).get('state')"),
    "backend-bound-state"
  );
  assert.equal(
    fixture.calls.some((c) => c[1] === "/auth/onboarding/complete"),
    false
  );
});

test("completed users retain completion while recovering from a cancelled access change", async () => {
  fixture.completion = completed;
  fixture.outcome = "cancelled";
  await login("cancelled");
  await browser.waitFor(bodyContains("GitHub installation was cancelled"));
  await browser.waitFor(bodyContains("Manage access on GitHub"));
  assert.equal(await browser.evaluate(pathIs("/install")), true);
  assert.equal(fixture.completion.onboarding_completed, true);
  assert.equal(
    fixture.calls.some((c) => c[0] === "POST"),
    false
  );
  await browser.click("Back to projects");
  await browser.waitFor(pathIs("/projects"));
  await browser.waitFor(bodyContains("Your projects"));
});

test("completed users can recover from missing access and failed discovery without restarting onboarding", async () => {
  fixture.completion = completed;
  fixture.eligible = false;
  fixture.discoveryFailure = true;
  await browser.navigate(base + "/install");
  await browser.waitFor(bodyContains("Unable to discover repositories"));
  assert.equal(
    await browser.evaluate(
      bodyContains("No accessible public repositories were found")
    ),
    false
  );
  fixture.discoveryFailure = false;
  await browser.click("Check access again");
  await browser.waitFor(bodyContains("Manage access on GitHub"));
  assert.equal(await browser.evaluate(pathIs("/install")), true);
  assert.equal(
    await browser.evaluate(
      bodyContains("Continue with available repositories")
    ),
    false
  );
  await browser.click("Back to projects");
  await browser.waitFor(pathIs("/projects"));
  assert.equal(fixture.completion.onboarding_completed, true);
  assert.equal(
    fixture.calls.some((c) => c[1] === "/auth/onboarding/complete"),
    false
  );
});

test("B1 install and B2 connect-more enter projects after a confirmed successful return", async () => {
  for (const eligible of [false, true]) {
    fixture = setup();
    fixture.eligible = eligible;
    await login();
    await browser.click(
      eligible ? "Connect more repositories" : "Install DecisionGuard on GitHub"
    );
    await browser.waitFor(pathIs("/__github_install"));
    fixture.completion = completed;
    fixture.outcome = "success";
    const discoveriesBeforeReturn = fixture.calls.filter(
      (c) => c[1] === "/auth/onboarding"
    ).length;
    await login("success");
    await browser.waitFor(pathIs("/projects"));
    await browser.waitFor(bodyContains("Your projects"));
    assert.equal(
      fixture.calls.filter((c) => c[1] === "/auth/onboarding").length,
      discoveriesBeforeReturn
    );
    assert.equal(
      fixture.calls.some((c) => c[1] === "/auth/onboarding/complete"),
      false
    );
  }
});

test("completed status alone cannot make a failed verification successful", async () => {
  fixture.completion = completed;
  fixture.outcome = "temporary_failure";
  fixture.verificationOutcome = "temporary_failure";
  await login("temporary_failure");
  await browser.waitFor(bodyContains("Retry installation verification"));
  await browser.click("Retry installation verification");
  await browser.waitFor(
    "Array.from(document.querySelectorAll('button')).some(b => b.innerText.trim() === 'Retry installation verification' && !b.disabled)"
  );
  assert.equal(await browser.evaluate(pathIs("/install")), true);
  fixture.verificationOutcome = "success";
  await browser.click("Retry installation verification");
  await browser.waitFor(pathIs("/projects"));
  assert.equal(
    fixture.calls.filter((c) => c[1] === "/auth/onboarding/installation/verify")
      .length,
    2
  );
});
