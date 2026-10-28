import { describe, expect, it } from "vitest";
import { clashNote, formatInstalls, looksLikeSource, resultSource } from "./skills";

describe("add skills", () => {
  it("tells a source from search words", () => {
    for (const s of ["docker/skills", "owner/repo@skill", "npx skills add docker/skills", "https://github.com/a/b", "github:a/b"])
      expect(looksLikeSource(s), s).toBe(true);
    for (const s of ["docker", "docker compose", "", "ci/cd pipelines"]) expect(looksLikeSource(s), s).toBe(false);
  });
  it("formats install counts", () => {
    expect(formatInstalls(950)).toBe("950");
    expect(formatInstalls(1234)).toBe("1.2k");
    expect(formatInstalls(29633)).toBe("30k");
    expect(formatInstalls(1_500_000)).toBe("1.5M");
  });
  it("says what a name clash does", () => {
    expect(clashNote(null)).toBeNull();
    expect(clashNote("local")).toMatch(/your own/);
    expect(clashNote("installed", "a/b")).toBe("replaces the one from a/b");
  });
  it("installs one search result", () => {
    expect(resultSource({ source: "docker/skills", skill: "docker-build" })).toBe("docker/skills@docker-build");
  });
});
