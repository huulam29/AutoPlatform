// Save a Blob as a file: a normal download in a browser; in the Mac app's web view (which has
// no downloads) through its `agentlineSave` message handler, which shows a Save panel.

type SaveHandler = { postMessage: (m: { filename: string; base64: string }) => void };

function macHandler(): SaveHandler | null {
  const w = window as unknown as {
    webkit?: { messageHandlers?: { agentlineSave?: SaveHandler } };
  };
  return w.webkit?.messageHandlers?.agentlineSave ?? null;
}

export function toBase64(bytes: Uint8Array): string {
  let out = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    out += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(out);
}

export async function saveBlob(blob: Blob, filename: string): Promise<void> {
  const mac = macHandler();
  if (mac) {
    mac.postMessage({ filename, base64: toBase64(new Uint8Array(await blob.arrayBuffer())) });
    return;
  }
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
