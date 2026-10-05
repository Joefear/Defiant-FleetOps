/** All authenticated requests cross the same-origin fixed-upstream adapter. */
export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
  }
}
export async function request<T>(
  path: string,
  token: string | null,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", "Bearer " + token);
  const response = await fetch("/api" + path, {
    ...init,
    headers,
    cache: "no-store",
    credentials: "same-origin",
    signal: init.signal ?? AbortSignal.timeout(40_000),
  });
  if (!response.ok) {
    const value = await response.json().catch(() => ({}));
    const detail =
      typeof value.detail === "string" ? value.detail : "The request could not be accepted";
    throw new ApiError(response.status, detail);
  }
  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}
export function jsonBody(value: unknown): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(value),
  };
}
