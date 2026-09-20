export function connectRuntimeStream(pluginId, handlers = {}) {
  if (typeof window === "undefined" || typeof window.EventSource !== "function") {
    return null;
  }

  const encodedPluginId = encodeURIComponent(pluginId || "PanSearch");
  const source = new window.EventSource(`/api/v1/plugin/${encodedPluginId}/runtime/stream`, {withCredentials: true});

  source.onopen = () => handlers.onOpen?.();
  source.onerror = (event) => handlers.onError?.(event);
  source.onmessage = (event) => {
    try {
      handlers.onRuntime?.(JSON.parse(event.data));
    } catch (error) {
      handlers.onInvalidData?.(error);
    }
  }
  return source;
}
