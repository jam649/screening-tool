export type User = {
  id: string;
  email: string;
  display_name: string;
  role: "admin" | "reviewer";
  active: boolean;
};
export type Session = { user: User; csrf_token: string };
export type SetupStatus = {
  needs_setup: boolean;
  product_name: string;
  disclaimer: string;
  disclaimer_url: string;
};

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

let csrfToken: string | undefined;
export function setSession(session: Session | null) {
  csrfToken = session?.csrf_token;
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData))
    headers.set("Content-Type", "application/json");
  if (csrfToken && init.method && init.method !== "GET")
    headers.set("X-CSRF-Token", csrfToken);
  let response: Response;
  try {
    response = await fetch(`/api/v1${path}`, {
      ...init,
      credentials: "same-origin",
      headers,
    });
  } catch {
    throw new ApiError(
      0,
      "Unable to reach the server. Check your connection and try again.",
    );
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    const detail = payload?.detail;
    const message =
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? detail
              .map((item: { msg?: string }) => item.msg ?? "Invalid value")
              .join(". ")
          : `The request could not be completed (${response.status}).`;
    throw new ApiError(response.status, message);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const setupQuery = () => api<SetupStatus>("/setup");
export async function sessionQuery(): Promise<Session | null> {
  try {
    const session = await api<Session>("/auth/me");
    setSession(session);
    return session;
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      setSession(null);
      return null;
    }
    throw error;
  }
}
