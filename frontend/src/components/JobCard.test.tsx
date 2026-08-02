import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { makeJob } from "../test/fixtures";
import { renderApp } from "../test/render";
import { JobCard, PipelineSteps } from "./JobCard";

describe("JobCard", () => {
  it("links to the job and exposes its state, message and exact progress", () => {
    const job = makeJob({
      id: "encode-42",
      name: "Film 42",
      state: "ENCODING",
      progress: 0.73,
      status_message: "Második passz",
    });

    renderApp(<JobCard job={job} />);

    expect(screen.getByRole("link", { name: /Film 42/ })).toHaveAttribute(
      "href",
      "/jobs/encode-42",
    );
    expect(screen.getByText("Videó kódolása")).toBeInTheDocument();
    expect(screen.getByText("Második passz")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Folyamat" })).toHaveAttribute(
      "aria-valuenow",
      "73",
    );
    expect(document.querySelector(".progress-value")).toHaveStyle({ width: "73%" });
  });

  it("marks every pipeline stage complete for a completed job", () => {
    renderApp(<PipelineSteps job={makeJob({ state: "COMPLETED" })} />);

    const pipeline = screen.getByRole("list", { name: "Kódolási folyamat" });
    expect(pipeline.querySelectorAll(".pipeline__step--complete")).toHaveLength(8);
    expect(screen.getByText("Kész")).toBeInTheDocument();
  });
});
