import assert from "node:assert/strict";
import test from "node:test";

import {
  SCHEDULES,
  dispatch,
  dispatchRequest,
  runScheduled,
} from "../src/index.js";

const ENV = {
  GITHUB_OWNER: "xiuweidiao",
  GITHUB_REPO: "daily_stock_analysis",
  GITHUB_REF: "main",
  GITHUB_TOKEN: "test-token-never-log",
};

function response(status, requestId = "request-123") {
  return new Response(null, {
    status,
    headers: { "x-github-request-id": requestId },
  });
}

function recordedFetch(status = 204) {
  const calls = [];
  return {
    calls,
    fetch: async (url, init) => {
      calls.push({ url, init, body: JSON.parse(init.body) });
      return response(status);
    },
  };
}

test("premarket cron dispatches the existing market workflow on main", async () => {
  const recorder = recordedFetch();
  await runScheduled("40 23 * * 0-4", ENV, recorder.fetch);
  assert.equal(recorder.calls[0].body.ref, "main");
  assert.deepEqual(recorder.calls[0].body.inputs, {
    trigger_source: "cloudflare_cron",
    expected_slot: "07:40",
    phase: "premarket",
  });
});

test("midday cron dispatches phase midday", async () => {
  const recorder = recordedFetch();
  await runScheduled("40 3 * * 1-5", ENV, recorder.fetch);
  assert.equal(recorder.calls[0].body.inputs.phase, "midday");
  assert.equal(recorder.calls[0].body.inputs.expected_slot, "11:40");
});

test("close cron dispatches phase close", async () => {
  const recorder = recordedFetch();
  await runScheduled("10 7 * * 1-5", ENV, recorder.fetch);
  assert.equal(recorder.calls[0].body.inputs.phase, "close");
  assert.equal(recorder.calls[0].body.inputs.expected_slot, "15:10");
});

test("repair cron dispatches the existing repair workflow", async () => {
  const recorder = recordedFetch();
  await runScheduled("30 10 * * 1-5", ENV, recorder.fetch);
  assert.match(recorder.calls[0].url, /portfolio-snapshot-repair\.yml\/dispatches$/);
  assert.deepEqual(recorder.calls[0].body.inputs, {
    trigger_source: "cloudflare_cron",
    expected_slot: "18:30",
    days: "5",
  });
});

test("duplicate cron delivery produces identical idempotent workflow dispatches", async () => {
  const recorder = recordedFetch();
  await runScheduled("40 3 * * 1-5", ENV, recorder.fetch);
  await runScheduled("40 3 * * 1-5", ENV, recorder.fetch);
  assert.equal(recorder.calls.length, 2);
  assert.deepEqual(recorder.calls[0].body, recorder.calls[1].body);
});

test("all configured dispatches target existing workflows", () => {
  assert.deepEqual(
    new Set(Object.values(SCHEDULES).map((job) => job.workflow)),
    new Set(["portfolio-market-data.yml", "portfolio-snapshot-repair.yml"]),
  );
});

test("missing token fails before any network request", async () => {
  let called = false;
  await assert.rejects(
    dispatch(SCHEDULES["40 3 * * 1-5"], { ...ENV, GITHUB_TOKEN: "" }, async () => {
      called = true;
      return response(204);
    }),
    /missing required Worker configuration: GITHUB_TOKEN/,
  );
  assert.equal(called, false);
});

test("non-204 GitHub response is a failure with request id", async () => {
  await assert.rejects(
    dispatch(SCHEDULES["10 7 * * 1-5"], ENV, async () => response(403, "denied-1")),
    /status=403 request_id=denied-1/,
  );
});

test("network failure is sanitized and does not expose the token", async () => {
  await assert.rejects(
    dispatch(SCHEDULES["10 7 * * 1-5"], ENV, async () => {
      throw new Error(`request with ${ENV.GITHUB_TOKEN} failed`);
    }),
    (error) => {
      assert.match(error.message, /network failure/);
      assert.doesNotMatch(error.message, /test-token-never-log/);
      return true;
    },
  );
});

test("unknown cron is rejected without dispatching", async () => {
  await assert.rejects(
    runScheduled("1 2 3 4 5", ENV, async () => response(204)),
    /unsupported Cloudflare cron expression/,
  );
});

test("authorization is present only in the request header", () => {
  const request = dispatchRequest(SCHEDULES["30 10 * * 1-5"], ENV);
  assert.equal(request.init.headers.Authorization, `Bearer ${ENV.GITHUB_TOKEN}`);
  assert.doesNotMatch(request.url, /test-token/);
  assert.doesNotMatch(request.init.body, /test-token/);
});
