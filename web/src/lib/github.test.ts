import { describe, expect, it } from "vitest";
import { detectedChoices, loginPending, setupStatus, tokenSourceLabel, type GithubSetup } from "./github";

const det: GithubSetup["detected"] = [
  { repo: "me/agentline", remote: "origin", protocol: "ssh", where: "agentline", url: "git@github.com:me/agentline.git" },
  { repo: "me/pipes", remote: "origin", protocol: "https", where: "pipelines folder", url: "https://github.com/me/pipes" },
];

describe("github setup", () => {
  it("names where the token comes from", () => {
    expect(tokenSourceLabel("gh", "octo")).toBe("GitHub CLI sign-in (octo)");
    expect(tokenSourceLabel("secrets")).toBe("pasted token (secrets file)");
    expect(tokenSourceLabel("env")).toBe("GITHUB_TOKEN in the environment");
    expect(tokenSourceLabel(null)).toBe("not signed in");
  });

  it("shows what's missing", () => {
    expect(setupStatus({ ready: true, repo: "o/r", token_source: "gh" })).toEqual({ s: "done", word: "Ready" });
    expect(setupStatus({ ready: false, repo: "o/r", token_source: null }).word).toBe("Sign in needed");
    expect(setupStatus({ ready: false, repo: null, token_source: "gh" }).word).toBe("Repo needed");
    expect(setupStatus({ ready: false, repo: null, token_source: null })).toEqual({ s: "waiting", word: "Not set up" });
  });

  it("offers detected SSH and HTTPS remotes not chosen yet", () => {
    expect(detectedChoices({ detected: det, repo: null }).map((d) => d.label)).toEqual([
      "me/agentline (agentline, SSH)",
      "me/pipes (pipelines folder, HTTPS)",
    ]);
    expect(detectedChoices({ detected: det, repo: "me/agentline" }).map((d) => d.repo)).toEqual(["me/pipes"]);
  });

  it("polls only while the sign-in waits", () => {
    expect(loginPending({ state: "waiting", code: "AB12-CD34", url: null, error: null })).toBe(true);
    expect(loginPending({ state: "done", code: null, url: null, error: null })).toBe(false);
    expect(loginPending(null)).toBe(false);
  });
});
