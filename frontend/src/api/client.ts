/** 统一错误类型与 API 客户端（fetch + cookie 凭证 + CSRF 双提交令牌）。 */

export class ApiError extends Error {
  code: string;
  status: number;
  requestId?: string;
  details?: Record<string, unknown>;
  constructor(status: number, code: string, message: string, requestId?: string, details?: Record<string, unknown>) {
    super(message);
    this.status = status;
    this.code = code;
    this.requestId = requestId;
    this.details = details;
  }
}

/** 读 CSRF 令牌：值＝可被 JS 读取的 mgus_csrf Cookie（服务端签发的会话明文）。
 *  服务端同时写入 HttpOnly 会话 Cookie；跨站页面无法读到该值，因此无法伪造请求头。 */
function csrfToken(): string {
  const m = document.cookie.match(/(?:^|;\s*)mgus_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

async function request<T>(method: string, path: string, body?: unknown, headers?: Record<string, string>): Promise<T> {
  const unsafe = method !== "GET" && method !== "HEAD" && method !== "OPTIONS";
  const token = unsafe ? csrfToken() : "";
  const res = await fetch(path, {
    method,
    credentials: "include",
    headers: {
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...(unsafe ? { "X-Requested-With": "mgus-fetch" } : {}),
      ...(token ? { "X-CSRF-Token": token } : {}),
      ...headers,
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  let data: unknown = null;
  try {
    data = await res.json();
  } catch {
    /* 空响应 */
  }
  if (!res.ok) {
    const err = (data ?? {}) as { code?: string; message?: string; request_id?: string; details?: Record<string, unknown> };
    throw new ApiError(res.status, err.code ?? `HTTP_${res.status}`, err.message ?? `请求失败（${res.status}）`, err.request_id, err.details);
  }
  return data as T;
}

export const api = {
  get: <T,>(path: string) => request<T>("GET", path),
  post: <T,>(path: string, body?: unknown, headers?: Record<string, string>) => request<T>("POST", path, body, headers),
  patch: <T,>(path: string, body?: unknown) => request<T>("PATCH", path, body),
  put: <T,>(path: string, body?: unknown) => request<T>("PUT", path, body),
  del: <T,>(path: string, body?: unknown) => request<T>("DELETE", path, body),
};

/** 非 api.* 通道（如 EventSource/裸 fetch）需要手动带令牌时使用。 */
export const getCsrfToken = csrfToken;
