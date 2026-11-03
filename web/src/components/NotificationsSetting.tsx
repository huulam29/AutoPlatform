// Settings > General in a browser: turn system notifications on or off (spec 006 FR-022).
// Permission is asked only from the button: browsers allow the prompt only after a click.
import { useState } from "react";
import { notifyState, turnOffNotifications, turnOnNotifications, type NotifyState } from "../lib/notify";

const HELP: Record<NotifyState, string> = {
  unsupported: "This browser can't show notifications.",
  off: "Get a system notification when a gate waits for approval, a run finishes or fails, or a new problem report appears, while Studio is open in a tab.",
  on: "On while Studio is open in a tab. Chrome and Edge show Approve and Reject on gate notifications once Studio is installed as an app.",
  blocked: "Blocked for this site. Allow notifications in the browser's site settings (the icon left of the address), then reload.",
};

export function NotificationsSetting() {
  const [state, setState] = useState<NotifyState>(notifyState);
  return (
    <>
      <h3>Notifications</h3>
      <p className="small">{HELP[state]}</p>
      {state === "off" && (
        <div className="row">
          <button type="button" onClick={async () => setState(await turnOnNotifications())}>
            Turn on notifications
          </button>
        </div>
      )}
      {state === "on" && (
        <div className="row">
          <button
            type="button"
            onClick={() => {
              turnOffNotifications();
              setState(notifyState());
            }}
          >
            Turn off
          </button>
        </div>
      )}
    </>
  );
}
