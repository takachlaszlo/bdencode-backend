import { describe, expect, it } from "vitest";
import { makeJob } from "./test/fixtures";
import { formatEventMessage, formatStatusMessage, formatWorkerError, isFastComparisonTimeoutReview, stageProgress } from "./utils";

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

  it("explains an audio spectrum failure as a safe QC continuation", () => {
    const failure = "ProcessFailure: ffmpeg -filter_complex showspectrumpic ... audio-01-source-spectrum.png";
    expect(formatWorkerError(failure)).toMatch(/spektrumképének elkészítése/i);
    expect(formatStatusMessage(failure, "Állapotfrissítésre vár"))
      .toMatch(/QC szakasztól biztonságosan folytatható/i);
  });

  it("localizes fast comparison progress and recognizes its resumable timeout", () => {
    expect(formatStatusMessage("fast comparison: preparing bounded samples", ""))
      .toBe("Gyors comparison: a rövid videóminták előkészítése");
    expect(formatStatusMessage("fast comparison: pair 3/5 complete", ""))
      .toBe("Gyors comparison: 3/5 képpár elkészült");
    const timeout = "fast comparison exceeded its bounded command/time budget";
    expect(isFastComparisonTimeoutReview(timeout)).toBe(true);
    expect(formatStatusMessage(timeout, "")).toMatch(/ötperces időkorlátot/i);
  });

  it("uses backend pipeline baselines for legacy jobs without progress", () => {
    expect(stageProgress(makeJob({ state: "MUXING", progress: null }))).toBe(0.78);
    expect(stageProgress(makeJob({ state: "FAILED", resume_state: "MUXING", progress: null }))).toBe(0.78);
  });
});
