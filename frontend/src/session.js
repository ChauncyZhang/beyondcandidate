import { ApiError, apiClient } from "./apiClient.js";

const INITIAL_STATE = Object.freeze({
  status: "bootstrapping",
  user: null,
  role: null,
  submitting: false,
  loggingOut: false,
  error: null,
});

export function mapServerRoles(roles = []) {
  const values = new Set(Array.isArray(roles) ? roles : []);
  if (values.has("system_admin")) return "系统管理员";
  if (values.has("recruiting_admin")) return "招聘管理员";
  if (values.has("recruiter")) return "HR 招聘专员";
  if (values.has("hiring_manager")) return "用人经理";
  if (values.has("interviewer")) return "面试官";
  return null;
}

export function getSessionMessage(error) {
  if (error === "expired") return "登录状态已过期，请重新登录。";
  if (error === "authentication") return "登录信息不正确或账号暂不可用，请核对后重试。";
  if (error === "locked") return "登录失败次数过多，账号已临时锁定。";
  if (error === "unavailable") return "服务暂时无法连接，请稍后重试。";
  if (error === "logout_failed") return "退出失败，请稍后重试。";
  return "";
}

export function getSessionIdentity(user, role) {
  const displayName = typeof user?.display_name === "string" ? user.display_name.trim() : "";
  return { name: displayName || "当前用户", title: role || "未配置角色" };
}

function anonymousState(error = null, submitting = false, retryAfterSeconds = null) {
  const state = { status: "anonymous", user: null, role: null, submitting, loggingOut: false, error };
  if (error === "locked" && Number.isInteger(retryAfterSeconds) && retryAfterSeconds > 0) {
    state.retryAfterSeconds = retryAfterSeconds;
  }
  return state;
}

function authenticatedState(user, { loggingOut = false, error = null } = {}) {
  const role = mapServerRoles(user?.roles);
  return { status: role ? "authenticated" : "forbidden", user, role, submitting: false, loggingOut, error };
}

function errorKind(error) {
  if (error instanceof ApiError && (error.kind === "unavailable" || error.status >= 500)) return "unavailable";
  if (error instanceof ApiError && error.code === "account_temporarily_locked") return "locked";
  return "authentication";
}

export function createSessionController(client) {
  let state = INITIAL_STATE;
  let bootstrapPromise = null;
  let logoutPromise = null;
  let bootstrapped = false;
  let sessionEpoch = Number.isInteger(client.getAuthEpoch?.()) ? client.getAuthEpoch() : 0;
  const listeners = new Set();

  function setState(next) {
    state = Object.freeze(next);
    for (const listener of listeners) listener();
  }

  function handleUnauthorized(requestEpoch = sessionEpoch) {
    if (requestEpoch !== sessionEpoch) return;
    if (state.status === "authenticated" || state.status === "forbidden") {
      setState(anonymousState("expired"));
    }
  }

  const unregisterUnauthorized = client.setUnauthorizedHandler?.(handleUnauthorized);

  return {
    getSnapshot() {
      return state;
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    dispose() {
      if (typeof unregisterUnauthorized === "function") unregisterUnauthorized();
    },
    bootstrap() {
      if (bootstrapped) return Promise.resolve(state);
      if (bootstrapPromise) return bootstrapPromise;
      bootstrapPromise = (async () => {
        try {
          const user = await client.getMe();
          setState(authenticatedState(user));
        } catch (error) {
          if (error instanceof ApiError && error.status === 401) {
            setState(anonymousState());
          } else {
            setState(anonymousState("unavailable"));
          }
        } finally {
          bootstrapped = true;
          bootstrapPromise = null;
        }
        return state;
      })();
      return bootstrapPromise;
    },
    async login(credentials) {
      setState(anonymousState(null, true));
      try {
        await client.login(credentials);
        const user = await client.getMe();
        const nextEpoch = client.advanceAuthEpoch?.();
        sessionEpoch = Number.isInteger(nextEpoch) ? nextEpoch : sessionEpoch + 1;
        setState(authenticatedState(user));
        return user;
      } catch (error) {
        const kind = errorKind(error);
        setState(anonymousState(kind, false, kind === "locked" ? error.retryAfterSeconds : null));
        throw error;
      }
    },
    logout() {
      if (logoutPromise) return logoutPromise;
      if (!new Set(["authenticated", "forbidden"]).has(state.status)) return Promise.resolve();
      const authenticatedUser = state.user;
      setState(authenticatedState(authenticatedUser, { loggingOut: true }));
      logoutPromise = (async () => {
        try {
          await client.logout();
          client.clearCsrf?.();
          setState(anonymousState());
        } catch (error) {
          if (error instanceof ApiError && error.status === 401) {
            client.clearCsrf?.();
            if (state.status !== "anonymous" || state.error !== "expired") {
              setState(anonymousState("expired"));
            }
          } else if (
            (state.status === "authenticated" || state.status === "forbidden")
            && state.user === authenticatedUser
            && state.loggingOut
          ) {
            setState(authenticatedState(authenticatedUser, { error: "logout_failed" }));
          }
          throw error;
        } finally {
          logoutPromise = null;
        }
      })();
      return logoutPromise;
    },
  };
}

export const sessionController = createSessionController(apiClient);
