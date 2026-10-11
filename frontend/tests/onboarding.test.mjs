import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { createServer } from "vite";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router";

let server, service, client, flow, authFlow, Screen;
before(async () => {
  globalThis.localStorage = { getItem: () => "application-token" };
  server = await createServer({
    server: { middlewareMode: true, hmr: false },
    appType: "custom",
  });
  ({ authService: service } = await server.ssrLoadModule(
    "/src/api/auth-service.ts"
  ));
  ({ apiClient: client } = await server.ssrLoadModule(
    "/src/lib/api-client.ts"
  ));
  flow = await server.ssrLoadModule("/src/lib/onboarding-flow.ts");
  authFlow = await server.ssrLoadModule("/src/lib/auth-flow.ts");
  ({ OnboardingScreen: Screen } = await server.ssrLoadModule(
    "/src/components/onboarding/onboarding-screen.tsx"
  ));
});
after(async () => {
  await server?.close();
  delete globalThis.localStorage;
});

const incomplete = {
  onboarding_completed: false,
  onboarding_completed_at: null,
  next: "/install",
};
const complete = {
  onboarding_completed: true,
  onboarding_completed_at: "2026-10-11T00:00:00",
  next: "/projects",
};
const available = {
  ...incomplete,
  eligible: true,
  screen: "available_repositories",
  installation_discovery_status: "complete",
  installation_outcome: null,
  installations: ["alice", "example-org"].map((account_login, index) => ({
    id: String(index + 1),
    account_login,
    account_type: index ? "Organization" : "User",
    public_repository_count: 1,
    repository_selection: "selected",
    status: "ACTIVE",
    repositories: [
      {
        github_repository_id: String(index + 10),
        installation_id: String(index + 1),
        full_name: `${account_login}/visible`,
        repository_url: `https://github.com/${account_login}/visible`,
        name: "visible",
        is_private: false,
      },
    ],
  })),
};
const noRepositories = {
  ...available,
  eligible: false,
  screen: "install",
  installations: [],
};
function render(props = {}) {
  return renderToStaticMarkup(
    createElement(
      MemoryRouter,
      null,
      createElement(Screen, {
        username: "alice",
        data: noRepositories,
        isLoading: false,
        isRefreshing: false,
        busy: false,
        action: null,
        discoveryError: null,
        actionError: null,
        outcome: null,
        reconnect: false,
        onInstall() {},
        onComplete() {},
        onVerify() {},
        onRefresh() {},
        onReconnect() {},
        onLogout() {},
        ...props,
      })
    )
  );
}

test("onboarding API uses exact endpoints and browser cookies for discovery and verification", async () => {
  const calls = [];
  client.defaults.adapter = async (config) => {
    calls.push(config);
    return {
      data:
        config.url === "/auth/onboarding"
          ? available
          : { ...complete, installation_outcome: "success" },
      status: 200,
      statusText: "OK",
      headers: {},
      config,
    };
  };
  assert.deepEqual(await service.getOnboarding(), available);
  assert.equal((await service.completeOnboarding()).onboarding_completed, true);
  assert.equal(
    (await service.verifyInstallation()).installation_outcome,
    "success"
  );
  assert.deepEqual(
    calls.map((c) => [c.method, c.url]),
    [
      ["get", "/auth/onboarding"],
      ["post", "/auth/onboarding/complete"],
      ["post", "/auth/onboarding/installation/verify"],
    ]
  );
  assert.equal(calls[0].withCredentials, true);
  assert.equal(calls[2].withCredentials, true);
  assert.equal(calls[2].data, undefined);
  assert.ok(
    calls.every((c) => c.headers.Authorization === "Bearer application-token")
  );
});

test("failed completion remains a rejected operation", async () => {
  client.defaults.adapter = async () => {
    throw {
      isAxiosError: true,
      response: {
        status: 409,
        data: {
          detail: {
            outcome: "no_eligible_repositories",
            message: "Access changed",
          },
        },
      },
    };
  };
  await assert.rejects(service.completeOnboarding());
});

test("completion cache updates require an explicit server confirmation", () => {
  const current = {
    ...incomplete,
    user: { id: "alice", onboarding_completed_at: null },
    installations: [],
    reconnect_required: false,
    installation_onboarding_required: true,
  };
  assert.throws(
    () => authFlow.mergeOnboardingCompletion(current, incomplete),
    /not been confirmed/
  );
  assert.throws(() =>
    authFlow.mergeOnboardingCompletion(current, {
      ...complete,
      onboarding_completed_at: null,
    })
  );
  const next = authFlow.mergeOnboardingCompletion(current, complete);
  assert.equal(next.onboarding_completed_at, complete.onboarding_completed_at);
  assert.equal(
    next.user.onboarding_completed_at,
    complete.onboarding_completed_at
  );
  assert.equal(next.installation_onboarding_required, false);
  assert.equal(current.onboarding_completed, false);
});

test("B1 uses existing card styling and the installation action", () => {
  const html = render();
  assert.match(html, /data-slot="card"/);
  assert.match(html, /bg-muted/);
  assert.match(html, /Install DecisionGuard on GitHub/);
  assert.doesNotMatch(
    html,
    /Continue with available repositories|Connect more repositories/
  );
});

test("B2 renders multiple accessible installation groups and exactly the two main actions", () => {
  const html = render({ data: available });
  assert.match(html, /Available repositories to protect/);
  assert.match(html, /alice/);
  assert.match(html, /example-org/);
  assert.equal(
    (html.match(/data-slot="collapsible-trigger"/g) ?? []).length,
    2
  );
  assert.equal((html.match(/aria-expanded="false"/g) ?? []).length, 2);
  assert.match(html, /Continue with available repositories/);
  assert.match(html, /Connect more repositories/);
  assert.doesNotMatch(
    html,
    /Install DecisionGuard on GitHub|Check access again|Enable Monitoring|Index/
  );
});

test("loading and discovery errors cannot render an empty repository result", () => {
  for (const props of [
    { data: undefined, isLoading: true },
    { discoveryError: "GitHub temporarily unavailable", data: available },
  ]) {
    const html = render(props);
    assert.doesNotMatch(
      html,
      /No accessible public repositories were found|Continue with available repositories/
    );
    assert.match(html, /role="status"|role="alert"/);
    assert.match(html, /Sign out/);
  }
});

for (const [outcome, message] of [
  ["cancelled", /was cancelled/],
  ["pending_approval", /awaiting organization approval/],
  [
    "no_eligible_repositories",
    /No accessible public repositories were granted/,
  ],
  ["temporary_failure", /temporarily unavailable/],
]) {
  test(`${outcome} has accurate messaging and a recovery action`, () => {
    const html = render({ outcome });
    assert.match(html, message);
    assert.match(html, /Check access again/);
    if (outcome === "temporary_failure")
      assert.match(html, /Retry installation verification/);
    if (outcome === "cancelled" || outcome === "pending_approval")
      assert.doesNotMatch(html, /Retry installation verification/);
  });
}

test("structured GitHub errors stay distinct from cancellation and empty discovery", () => {
  for (const outcome of [
    "temporary_failure",
    "reauthorization_required",
    "no_eligible_repositories",
  ]) {
    const error = {
      isAxiosError: true,
      response: {
        status: 409,
        data: { detail: { outcome, message: "Provider status" } },
      },
    };
    assert.deepEqual(flow.getOnboardingError(error), {
      outcome,
      message: "Provider status",
    });
  }
  assert.equal(
    flow.parseInstallationOutcome("https://unexpected.example"),
    null
  );
  assert.equal(flow.parseInstallationOutcome("__proto__"), null);
});

test("failed completion displays an error while preserving the available actions", () => {
  const html = render({
    data: available,
    actionError: "Server could not confirm onboarding",
  });
  assert.match(html, /role="alert"/);
  assert.match(html, /Server could not confirm onboarding/);
  assert.match(html, /Continue with available repositories/);
  assert.match(html, /Connect more repositories/);
});
