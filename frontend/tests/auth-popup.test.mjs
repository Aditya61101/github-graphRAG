import assert from "node:assert/strict";
import { before, after, beforeEach, test } from "node:test";
import { createServer } from "vite";

let server, auth, listeners, intervals, timeouts, popup, forms;
const origin = "https://api.example.test";
const token = `e30.${Buffer.from(JSON.stringify({ sub: "alice", exp: Date.now() / 1000 + 3600 })).toString("base64url")}.test`;
const message = {
  type: "decisionguard.auth",
  token,
  next: "/projects",
  installation_outcome: null,
};
before(async () => {
  server = await createServer({
    server: { middlewareMode: true },
    appType: "custom",
    define: { "import.meta.env.VITE_BACKEND_URL": JSON.stringify(origin) },
  });
  auth = await server.ssrLoadModule("/src/lib/auth-popup.ts");
});
beforeEach(() => {
  listeners = new Set();
  intervals = new Set();
  timeouts = new Set();
  forms = [];
  popup = {
    name: "popup",
    closed: false,
    close() {
      this.closed = true;
    },
  };
  globalThis.window = {
    location: { origin: "https://website.example.test" },
    open: () => popup,
    addEventListener: (type, fn) => listeners.add(fn),
    removeEventListener: (type, fn) => listeners.delete(fn),
    setInterval: (fn) => {
      intervals.add(fn);
      return fn;
    },
    clearInterval: (fn) => intervals.delete(fn),
    setTimeout: (fn) => {
      timeouts.add(fn);
      return fn;
    },
    clearTimeout: (fn) => timeouts.delete(fn),
  };
  globalThis.document = {
    createElement: (tag) => ({
      tag,
      append(input) {
        this.input = input;
      },
      remove() {
        this.removed = true;
      },
      submit() {
        this.submitted = {
          action: this.action,
          method: this.method,
          target: this.target,
          enctype: this.enctype,
          proof: this.input.value,
        };
      },
    }),
    body: { append: (form) => forms.push(form) },
  };
});
after(async () => {
  await server.close();
  delete globalThis.window;
  delete globalThis.document;
});
const emit = (data = message, eventOrigin = origin, source = popup) => {
  for (const listener of listeners)
    listener({ data, origin: eventOrigin, source });
};
test("only active exact-origin exact-window valid completions are consumed once and cleaned up", async () => {
  const flow = auth.openAuthPopup();
  emit(message, "https://evil.example");
  emit(message, origin, {});
  for (const data of [
    null,
    {},
    { ...message, token: "bad" },
    { ...message, next: "https://evil.example" },
    { ...message, installation_outcome: "unknown" },
    { ...message, installation_outcome: undefined },
  ])
    emit(data);
  assert.equal(listeners.size, 1);
  emit();
  emit();
  assert.deepEqual(await flow.result, message);
  assert.equal(listeners.size, 0);
  assert.equal(intervals.size, 0);
  assert.equal(timeouts.size, 0);
  assert.equal(popup.closed, true);
});
test("blocked popups report actionable error", () => {
  window.open = () => null;
  assert.throws(() => auth.openAuthPopup(), /Allow popups/);
  assert.equal(listeners.size, 0);
});
for (const mode of ["closed", "cancelled", "unmount", "expired"])
  test(`${mode} popup rejects and cleans listeners`, async () => {
    const flow = auth.openAuthPopup();
    const rejected = assert.rejects(
      flow.result,
      mode === "expired" ? /expired/ : /cancelled/
    );
    if (mode === "closed") {
      popup.closed = true;
      [...intervals][0]();
    } else if (mode === "expired") [...timeouts][0]();
    else flow.cancel();
    await rejected;
    assert.equal(listeners.size, 0);
    assert.equal(intervals.size, 0);
  });
const handoff = () => ({
  flow: "post-handoff-v1",
  method: "POST",
  action: origin + "/auth/github/install/continue",
  fields: { handoff: "single-use-proof" },
  expires_in: 120,
});
test("installation continuation is a targeted form POST with body proof and immediate removal", () => {
  auth.submitHandoff(handoff(), popup, origin);
  assert.deepEqual(forms[0].submitted, {
    action: handoff().action,
    method: "POST",
    target: "popup",
    enctype: "application/x-www-form-urlencoded",
    proof: "single-use-proof",
  });
  assert.equal(forms[0].removed, true);
  assert.equal(forms[0].input.value, "");
  assert.equal(forms[0].submitted.action.includes("proof"), false);
});
for (const changes of [
  { action: "https://evil.example/auth/github/install/continue" },
  { action: origin + "/wrong" },
  { action: origin + "/auth/github/install/continue?handoff=proof" },
  { action: origin + "/auth/github/install/continue#proof" },
  { action: "http://api.example.test/auth/github/install/continue" },
  { method: "GET" },
  { flow: "legacy" },
  { fields: {} },
  { expires_in: 0 },
])
  test(`untrusted or expired handoff rejected: ${JSON.stringify(changes)}`, () => {
    assert.throws(() =>
      auth.submitHandoff({ ...handoff(), ...changes }, popup, origin)
    );
    assert.equal(forms.length, 0);
  });

test("installation account mismatch and changed sessions cannot apply a completion", () => {
  const other = `e30.${Buffer.from(JSON.stringify({ sub: "bob", exp: Date.now() / 1000 + 3600 })).toString("base64url")}.test`;
  assert.doesNotThrow(() =>
    auth.validateAuthSession(message, token, token, true)
  );
  assert.throws(
    () =>
      auth.validateAuthSession(
        { ...message, token: other },
        token,
        token,
        true
      ),
    /account mismatch/
  );
  assert.throws(
    () => auth.validateAuthSession(message, token, other, true),
    /session changed/
  );
  assert.throws(
    () => auth.validateAuthSession(message, null, null, true),
    /account mismatch/
  );
  assert.doesNotThrow(() =>
    auth.validateAuthSession({ ...message, token: other }, token, token, false)
  );
});
