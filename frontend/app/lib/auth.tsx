import { createContext, useContext, useEffect, useState } from "react";
import { getSession, login, logout, type Session } from "./api";

type AuthContextValue = { session: Session | null; loading: boolean; signIn: (username: string, password: string) => Promise<Session>; signOut: () => Promise<void>; refresh: () => Promise<void> };
const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(true);
  const refresh = async () => { try { setSession(await getSession()); } catch { setSession(null); } finally { setLoading(false); } };
  useEffect(() => { void refresh(); }, []);
  const signIn = async (username: string, password: string) => { const next = await login(username, password); setSession(next); return next; };
  const signOut = async () => { try { await logout(); } finally { setSession(null); } };
  return <AuthContext.Provider value={{ session, loading, signIn, signOut, refresh }}>{children}</AuthContext.Provider>;
}

export function useAuth() { const value = useContext(AuthContext); if (!value) throw new Error("useAuth must be used within AuthProvider"); return value; }
