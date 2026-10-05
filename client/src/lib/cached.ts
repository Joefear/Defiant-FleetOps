/** Cached facts are scoped to the verified actor; denied/missing entities never fall back. */
import { request, ApiError } from "./api";
import { cachePut, cacheGet, cacheDelete } from "./db";
import { scopeOf, type Session } from "./types";
export async function cached<T>(session: Session, path: string): Promise<T> {
  const scope = scopeOf(session);
  try {
    const value = await request<T>(path, session.access_token);
    await cachePut(scope, path, value);
    return value;
  } catch (error) {
    if (error instanceof ApiError && error.status < 500) {
      await cacheDelete(scope, path);
      throw error;
    }
    const old = await cacheGet<T>(scope, path);
    if (old !== undefined) return old;
    throw new Error("Connect once to load this scan before capturing offline");
  }
}
