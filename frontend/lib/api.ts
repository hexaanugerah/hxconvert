import type { ApiErrorBody } from "@/types";

function resolveApiUrl(): string {
  const configured = (process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000").replace(/\/+$/, "");
  if (typeof window === "undefined") return configured;
  try {
    const target = new URL(configured);
    const pageHost = window.location.hostname;
    const isLocalHost = (host: string) =>
      host === "localhost" || host === "127.0.0.1" || host === "::1";
    // Keep the browser -> API pairing same-site so the guest cookie stays a
    // first-party cookie. localhost:3000 -> 127.0.0.1:8000 is cross-site in
    // SameSite terms and cookies would not be sent back for the job polling.
    if (target.hostname !== pageHost && isLocalHost(target.hostname) && isLocalHost(pageHost)) {
      target.hostname = pageHost;
      return target.toString().replace(/\/+$/, "");
    }
  } catch {
    // malformed URL: fall through and use the configured value as-is
  }
  return configured;
}

export const API_URL = resolveApiUrl();
const TOKEN_KEY = "docuconvert.token";

export class ApiError extends Error {
  status: number;
  code: string;
  details?: unknown;

  constructor(message: string, status: number, code = "request_failed", details?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null): void {
  if (typeof window === "undefined") return;
  if (token) window.localStorage.setItem(TOKEN_KEY, token);
  else window.localStorage.removeItem(TOKEN_KEY);
}

async function parseError(response: Response): Promise<ApiError> {
  let body: ApiErrorBody | null = null;
  try {
    body = (await response.json()) as ApiErrorBody;
  } catch {
    body = null;
  }
  const message = body?.error?.message || `Permintaan gagal (HTTP ${response.status}).`;
  return new ApiError(message, response.status, body?.error?.code, body?.error?.details);
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  let response: Response;
  try {
    response = await fetch(`${API_URL}${path.startsWith("/") ? path : `/${path}`}`, {
      ...init,
      headers,
      credentials: "include",
      cache: "no-store",
    });
  } catch {
    throw new ApiError(
      "Tidak dapat terhubung ke server XConvert. Pastikan backend berjalan dan URL API benar.",
      0,
      "network_error",
    );
  }

  if (!response.ok) throw await parseError(response);
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  if (!text) return undefined as T;
  return JSON.parse(text) as T;
}

export async function downloadJobFile(
  downloadUrl: string,
  filename: string,
): Promise<void> {
  const headers = new Headers();
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);

  const response = await fetch(`${API_URL}${downloadUrl}`, {
    headers,
    credentials: "include",
  });
  if (!response.ok) throw await parseError(response);
  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(objectUrl), 4000);
}
