import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useEffect, type ReactNode } from "react";

import { api, ApiError, post } from "./api";
import type { Me } from "./types";

type AuthState = {
  user: Me | null;
  loading: boolean;
  refresh: () => Promise<unknown>;
  logout: () => Promise<void>;
};

const AuthContext = createContext<AuthState>({ user: null, loading: true, refresh: async () => {}, logout: async () => {} });

export function AuthProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const me = useQuery({
    queryKey: ["me"],
    queryFn: async () => {
      try {
        return await api<Me>("/auth/me");
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) return null;
        throw e;
      }
    },
    staleTime: 60_000,
    retry: false,
  });

  useEffect(() => {
    // Herhangi bir istek 401 dönerse oturum düşmüş demektir: tüm önbelleği temizle
    const expired = () => {
      qc.setQueryData(["me"], null);
      qc.removeQueries({ predicate: (q) => q.queryKey[0] !== "me" });
    };
    const mustChange = () => me.refetch();
    window.addEventListener("auth:expired", expired);
    window.addEventListener("auth:password-change", mustChange);
    return () => {
      window.removeEventListener("auth:expired", expired);
      window.removeEventListener("auth:password-change", mustChange);
    };
  }, [qc, me]);

  const logout = async () => {
    try {
      await post("/auth/logout");
    } catch {
      // oturum zaten düşmüş olabilir; yine de yerel durumu temizle
    } finally {
      // Önce oturum bilgisini sıfırla (dinleyen bileşenler hemen giriş ekranına döner), sonra diğer verileri sil
      qc.setQueryData(["me"], null);
      qc.removeQueries({ predicate: (q) => q.queryKey[0] !== "me" });
      // Sayfayı baştan yükle: bellekte önceki oturuma ait hiçbir veri kalmasın
      window.location.replace("/");
    }
  };

  return (
    <AuthContext.Provider value={{ user: me.data ?? null, loading: me.isLoading, refresh: me.refetch, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export const useAuth = () => useContext(AuthContext);
