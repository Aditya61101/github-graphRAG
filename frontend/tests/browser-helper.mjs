// Native Chromium/CDP test harness: no additional application or test libraries.
import { spawn } from "node:child_process";
import { access, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

export async function openBrowser() {
  const candidates = [
    process.env.ONBOARDING_TEST_BROWSER,
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
  ].filter(Boolean);
  let executable;
  for (const candidate of candidates) {
    try {
      await access(candidate);
      executable = candidate;
      break;
    } catch {
      /* try next */
    }
  }
  if (!executable)
    throw new Error(
      "Install Chromium or set ONBOARDING_TEST_BROWSER to run the onboarding browser tests."
    );
  const profile = await mkdtemp(
    path.join(tmpdir(), "decisionguard-onboarding-browser-")
  );
  const child = spawn(
    executable,
    [
      "--headless=new",
      "--remote-debugging-port=0",
      `--user-data-dir=${profile}`,
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-background-networking",
    ],
    { windowsHide: true, stdio: ["ignore", "ignore", "pipe"] }
  );
  let socket;
  try {
    const endpoint = await new Promise((resolve, reject) => {
      let output = "";
      const timeout = setTimeout(
        () =>
          reject(new Error(`Browser startup timed out: ${output.slice(-500)}`)),
        15000
      );
      child.once("error", (error) => {
        clearTimeout(timeout);
        reject(error);
      });
      child.once("exit", (code) => {
        clearTimeout(timeout);
        reject(new Error(`Browser exited: ${code}`));
      });
      child.stderr.on("data", (data) => {
        output += data;
        const match = output.match(/DevTools listening on (ws:\/\/[^\s]+)/);
        if (match) {
          clearTimeout(timeout);
          resolve(match[1]);
        }
      });
    });
    socket = new WebSocket(endpoint);
    await new Promise((resolve, reject) => {
      socket.addEventListener("open", resolve, { once: true });
      socket.addEventListener("error", reject, { once: true });
    });
    let sequence = 0;
    const pending = new Map();
    socket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      const call = pending.get(message.id);
      if (call) {
        pending.delete(message.id);
        clearTimeout(call.timeout);
        if (message.error)
          call.reject(new Error(JSON.stringify(message.error)));
        else call.resolve(message.result);
      }
    });
    const send = (method, params = {}, sessionId) =>
      new Promise((resolve, reject) => {
        const id = ++sequence;
        const timeout = setTimeout(() => {
          pending.delete(id);
          reject(new Error(`CDP timeout: ${method}`));
        }, 15000);
        pending.set(id, { resolve, reject, timeout });
        socket.send(
          JSON.stringify({
            id,
            method,
            params,
            ...(sessionId ? { sessionId } : {}),
          })
        );
      });
    const { targetId } = await send("Target.createTarget", {
      url: "about:blank",
    });
    const { sessionId } = await send("Target.attachToTarget", {
      targetId,
      flatten: true,
    });
    const call = (method, params) => send(method, params, sessionId);
    let committedLoader;
    socket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (
        message.sessionId === sessionId &&
        message.method === "Page.frameNavigated" &&
        !message.params.frame.parentId
      ) {
        committedLoader = message.params.frame.loaderId;
      }
    });
    await call("Page.enable");
    await call("Runtime.enable");
    await call("Page.bringToFront");
    const evaluate = async (expression) => {
      const result = await call("Runtime.evaluate", {
        expression,
        returnByValue: true,
        awaitPromise: true,
      });
      if (result.exceptionDetails)
        throw new Error(result.exceptionDetails.text);
      return result.result.value;
    };
    const waitFor = async (expression) => {
      const deadline = Date.now() + 15000;
      while (Date.now() < deadline) {
        try {
          if (await evaluate(expression)) return;
        } catch {
          /* navigation in progress */
        }
        await delay(40);
      }
      throw new Error(
        `UI timeout: ${expression}\n${await evaluate("document.body.innerText")}`
      );
    };
    return {
      call,
      evaluate,
      waitFor,
      navigate: async (url) => {
        // Page.navigate acknowledges initiation before the new document commits.
        // Do not let a test match the previous page's onboarding buttons.
        const result = await call("Page.navigate", { url });
        if (result.errorText) throw new Error(result.errorText);
        const deadline = Date.now() + 15000;
        while (result.loaderId && committedLoader !== result.loaderId) {
          if (Date.now() > deadline)
            throw new Error("Navigation did not commit");
          await delay(25);
        }
      },
      click: async (label) => {
        await waitFor(
          `Array.from(document.querySelectorAll('button')).some(b => b.innerText.trim() === ${JSON.stringify(label)} && !b.disabled)`
        );
        await evaluate(
          `Array.from(document.querySelectorAll('button')).find(b => b.innerText.trim() === ${JSON.stringify(label)}).click()`
        );
      },
      close: async () => {
        socket.close();
        child.kill();
        await delay(250);
        // Only remove the freshly created, verified temporary browser profile.
        const resolved = path.resolve(profile);
        const parent = path.resolve(tmpdir()) + path.sep;
        if (
          !resolved.startsWith(parent) ||
          !path
            .basename(resolved)
            .startsWith("decisionguard-onboarding-browser-")
        ) {
          throw new Error("Unexpected browser profile path");
        }
        await rm(resolved, {
          recursive: true,
          force: true,
          maxRetries: 10,
          retryDelay: 200,
        });
      },
    };
  } catch (error) {
    socket?.close();
    child.kill();
    throw error;
  }
}
