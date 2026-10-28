import { describe, expect, it } from "vitest";
import { toBase64 } from "./download";

describe("toBase64", () => {
  it("encodes bytes, including large ones in chunks", () => {
    expect(toBase64(new Uint8Array([104, 105]))).toBe("aGk=");
    const big = new Uint8Array(100_000).fill(65);
    expect(atob(toBase64(big)).length).toBe(100_000);
  });
});
