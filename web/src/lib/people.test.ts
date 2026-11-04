import { describe, expect, it } from "vitest";
import { identityLabel, personNameProblem } from "./people";

describe("people", () => {
  it("checks names like the engine", () => {
    expect(personNameProblem("alice")).toBeNull();
    expect(personNameProblem("a.b-c_d@x")).toBeNull();
    expect(personNameProblem("a b")).toMatch(/no spaces/);
    expect(personNameProblem("")).toMatch(/1-64/);
    expect(personNameProblem("x".repeat(65))).toMatch(/1-64/);
    expect(personNameProblem("Shared-Token")).toMatch(/reserved/);
    expect(personNameProblem("local")).toMatch(/reserved/);
    expect(personNameProblem("Bob", ["bob"])).toMatch(/already/);
  });

  it("labels who is signed in", () => {
    expect(identityLabel({ kind: "person", name: "alice", admin: true })).toBe("alice (admin)");
    expect(identityLabel({ kind: "person", name: "carol", admin: false })).toBe("carol");
    expect(identityLabel({ kind: "shared", name: "shared-token", admin: true })).toMatch(/shared/);
    expect(identityLabel({ kind: "local", name: "huu", admin: true })).toBe("huu on the engine's machine");
  });
});
