import { createContext, useContext, useEffect, useState } from "react";
import { getSession } from "./api";
import { useAuth } from "./auth";

type StreamState = "idle" | "connecting" | "connected" | "reconnecting";
type EventContextValue = { state: StreamState; revision: number };
const EventContext = createContext<EventContextValue>({ state: "idle", revision: 0 });

export function EventProvider({ children }: { children: React.ReactNode }) {
  const { session, refresh } = useAuth();
  const [state, setState] = useState<StreamState>("idle");
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    if (!session) { setState("idle"); return; }
    let closed = false;
    let checkingSession = false;
    let source: EventSource | null = null;
    let retryTimer: number | null = null;
    let failures = 0;
    let lastEventId = "";
    setState("connecting");
    const changed = (event: Event) => {
      const id = (event as MessageEvent).lastEventId;
      if (id) lastEventId = id;
      setRevision((value) => value + 1);
    };
    const connect = () => {
      if (closed) return;
      const url = lastEventId ? `/api/v1/events?cursor=${encodeURIComponent(lastEventId)}` : "/api/v1/events";
      source = new EventSource(url, { withCredentials: true });
      source.onopen = () => { if (!closed) { failures = 0; setState("connected"); changed(new Event("open")); } };
      source.addEventListener("snapshot", changed);
      source.addEventListener("resync_required", changed);
      source.addEventListener("task.updated", changed);
      source.addEventListener("queue.updated", changed);
      source.onerror = () => {
        if (closed) return;
        source?.close();
        source = null;
        setState("reconnecting");
        const delay = Math.min(30000, 1000 * 2 ** Math.min(failures++, 5));
        retryTimer = window.setTimeout(connect, Math.round(Math.min(30000, delay * (0.75 + Math.random() * 0.5))));
        if (checkingSession) return;
        checkingSession = true;
        void getSession().catch(async (error: { status?: number }) => {
          if (error.status === 401 && !closed) {
            if (retryTimer !== null) window.clearTimeout(retryTimer);
            retryTimer = null;
            source?.close();
            await refresh();
          }
        }).finally(() => { checkingSession = false; });
      };
    };
    connect();
    return () => { closed = true; source?.close(); if (retryTimer !== null) window.clearTimeout(retryTimer); };
  }, [session?.user.id]);

  return <EventContext.Provider value={{ state, revision }}>{children}</EventContext.Provider>;
}

export function useEvents() { return useContext(EventContext); }
