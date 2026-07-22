export function createLatestOperation({ createController = () => new AbortController() } = {}) {
  let generation = 0;
  let active = null;

  return {
    start() {
      active?.controller.abort();
      generation += 1;
      const controller = createController();
      const currentGeneration = generation;
      active = { controller, generation: currentGeneration };
      return {
        signal: controller.signal,
        isCurrent: () => active?.generation === currentGeneration && !controller.signal.aborted,
      };
    },
    cancel() {
      generation += 1;
      active?.controller.abort();
      active = null;
    },
  };
}

export function createExportIntent(idSource = () => globalThis.crypto.randomUUID()) {
  let idempotencyKey = null;
  return {
    key() {
      if (!idempotencyKey) idempotencyKey = idSource();
      return idempotencyKey;
    },
    peek: () => idempotencyKey,
    succeed() {
      idempotencyKey = null;
    },
    reset() {
      idempotencyKey = null;
    },
  };
}

export function loadingReportState() {
  return { status: "loading", data: null, error: "" };
}

export function failedReportState() {
  return { status: "error", data: null, error: "报表加载失败，请检查网络后重试。" };
}

export function isTerminalExportFailure(record) {
  return record?.status === "failed";
}
