'use client';

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';

import { api, getToken, onUnauthorized, setToken } from './api';
import type { UserInfo } from './types';

interface AuthValue {
  user: UserInfo | null;
  loading: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
  setUser: (user: UserInfo | null) => void;
}

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<UserInfo | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    onUnauthorized(() => setUser(null));
    return () => onUnauthorized(null);
  }, []);

  const refresh = useCallback(async () => {
    if (!getToken()) {
      setUser(null);
      setLoading(false);
      return;
    }
    try {
      setUser(await api.me());
    } catch {
      setToken(null);
      setUser(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const login = useCallback(async (username: string, password: string) => {
    const result = await api.login(username, password);
    setToken(result.token);
    setUser(result.user);
    setLoading(false);
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } catch {
      /* the token may already be invalid */
    }
    setToken(null);
    setUser(null);
    if (typeof window !== 'undefined') window.location.href = '/login';
  }, []);

  const value = useMemo(
    () => ({ user, loading, login, logout, refresh, setUser }),
    [user, loading, login, logout, refresh],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const context = useContext(AuthContext);
  if (!context) throw new Error('useAuth must be used inside AuthProvider');
  return context;
}

/** Redirects unauthenticated visitors to the login page. */
export function useRequireAuth(): AuthValue {
  const auth = useAuth();
  useEffect(() => {
    if (!auth.loading && !auth.user && typeof window !== 'undefined') {
      const next = `${window.location.pathname}${window.location.search}`;
      window.location.replace(`/login?next=${encodeURIComponent(next)}`);
    }
  }, [auth.loading, auth.user]);
  return auth;
}
