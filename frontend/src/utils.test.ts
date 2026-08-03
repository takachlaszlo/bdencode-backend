import { describe, expect, it } from "vitest";
import { makeJob } from "./test/fixtures";
import { formatEventMessage, formatStatusMessage, formatWorkerError, stageProgress } from "./utils";

describe("formatEventMessage", () => {
  it("localizes historical worker messages", () => {
    expect(formatEventMessage("job.state", "reference timeline prepared"))
      .toBe("A referencia-idővonal elkészült");
  });

  it("uses a Hungarian event-kind label when no message exists", () => {
    expect(formatEventMessage("artifact.created", null)).toBe("Melléklet létrehozva");
    expect(formatEventMessage("artifact.created", "artifact.created")).toBe("Melléklet létrehozva");
  });

  it("preserves useful messages it does not recognize", () => {
    expect(formatEventMessage("job.selection", "Operátori beállítások jóváhagyva"))
      .toBe("Operátori beállítások jóváhagyva");
  });

  it("localizes the same historical text when it is the active status", () => {
    expect(formatStatusMessage("reference timeline prepared", "Állapotfrissítésre vár"))
      .toBe("A referencia-idővonal elkészült");
    expect(formatStatusMessage("FileNotFoundError: /job/work/chapters.xml", "Állapotfrissítésre vár"))
      .toMatch(/fejezetlista létrehozása/i);
    expect(formatStatusMessage("retrying failed MUXING stage", "Állapotfrissítésre vár"))
      .toBe("MKV összeállítása: biztonságos folytatás");
  });

  it("explains the chapter retry failure without hiding its technical details", () => {
    expect(formatWorkerError("FileNotFoundError: /job/work/chapters.xml"))
      .toMatch(/fejezetlista létrehozása/i);
  });

  it("uses backend pipeline baselines for legacy jobs without progress", () => {
    expect(stageProgress(makeJob({ state: "MUXING", progress: null }))).toBe(0.78);
    expect(stageProgress(makeJob({ state: "FAILED", resume_state: "MUXING", progress: null }))).toBe(0.78);
  });
});
