// The engine's app info (GET /api/app/info) for a browser, fetched once per page load and
// shared; null in the Mac app (it asks the native bridge) or while loading.
import { useEffect, useState } from "react";
import { api, type AppInfoApi } from "../api";
import { isMacApp } from "./native";

let cached: Promise<AppInfoApi> | null = null;

export function useAppInfo(): AppInfoApi | null {
  const [info, setInfo] = useState<AppInfoApi | null>(null);
  useEffect(() => {
    if (isMacApp()) return;
    cached ??= api.appInfo();
    let live = true;
    cached
      .then((i) => live && setInfo(i))
      .catch(() => {
        cached = null;
      });
    return () => {
      live = false;
    };
  }, []);
  return info;
}
