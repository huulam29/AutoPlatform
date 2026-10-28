import { describe, expect, it } from "vitest";
import { oneLine, skillLibraryLabel, skillOriginTitle, stripFrontMatter } from "./SettingsExtra";

describe("stripFrontMatter", () => {
  it("drops the --- block", () => {
    expect(stripFrontMatter("---\nname: x\ndescription: y\n---\n\n# Body")).toBe("# Body");
    expect(stripFrontMatter("# no front matter")).toBe("# no front matter");
  });
});

describe("skill rows", () => {
  it("names the library a skill came from", () => {
    expect(skillLibraryLabel("fetched", "addyosmani/agent-skills")).toBe("agent-skills");
    expect(skillLibraryLabel("local", "addyosmani/agent-skills")).toBe("yours");
    expect(skillLibraryLabel(undefined, "")).toBe("library");
    expect(skillLibraryLabel("builtin", "addyosmani/agent-skills")).toBe("built-in");
    const from = { source: "docker/skills/skills/x", repo: "docker/skills", ref: "HEAD", path: "skills/x", sha: "0123456789", installed_at: 0 };
    expect(skillLibraryLabel("installed", "addyosmani/agent-skills", from)).toBe("docker/skills");
    expect(skillLibraryLabel("installed", "addyosmani/agent-skills")).toBe("installed");
    expect(skillOriginTitle("installed", "", from)).toBe("installed from docker/skills/skills/x, commit 0123456");
  });
  it("puts a description on one line", () => {
    expect(oneLine("  Use when\n  reviewing   code.\n\nMore. ")).toBe("Use when reviewing code. More.");
  });
});
