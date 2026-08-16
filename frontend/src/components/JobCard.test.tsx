import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Job } from "../api/types";
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
    expect(screen.getByRole("article")).toContainElement(
      screen.getByRole("link", { name: "Film 42 részletei" }),
    );
  });

  it("keeps controls outside the details link and delegates allowed actions", async () => {
    const user = userEvent.setup();
    const onAction = vi.fn();
    const job = {
      ...makeJob({ id: "active-1", name: "Aktív film", state: "ENCODING" }),
      control_state: "RUNNING",
      allowed_operations: ["pause", "cancel"],
    } as Job & { control_state: string; allowed_operations: string[] };

    renderApp(<JobCard job={job} onAction={onAction} />);

    const details = screen.getByRole("link", { name: "Aktív film részletei" });
    const pause = screen.getByRole("button", { name: "Aktív film: szüneteltetés" });
    expect(details).not.toContainElement(pause);

    await user.click(pause);
    expect(onAction).toHaveBeenCalledWith("pause", job);

    await user.click(screen.getByRole("button", { name: "Aktív film: további műveletek" }));
    await user.click(screen.getByRole("menuitem", { name: "Megszakítás" }));
    expect(onAction).toHaveBeenLastCalledWith("cancel", job);
  });

  it("offers resume for a paused job and maps its pipeline to resume_state", async () => {
    const user = userEvent.setup();
    const onAction = vi.fn();
    const job = {
      ...makeJob({ state: "NEEDS_REVIEW", resume_state: "MUXING" }),
      control_state: "PAUSED",
      allowed_operations: ["resume", "cancel"],
    } as Job & { control_state: string; allowed_operations: string[] };

    renderApp(<><JobCard job={job} onAction={onAction} /><PipelineSteps job={job} /></>);

    expect(screen.getByText("Szüneteltetve")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Mintafilm: folytatás" }));
    expect(onAction).toHaveBeenCalledWith("resume", job);
    expect(screen.getByText("Mux").closest("li")).toHaveAttribute("aria-current", "step");
  });

  it("opens and closes the overflow menu from the keyboard", async () => {
    const user = userEvent.setup();
    const job = {
      ...makeJob({ state: "COMPLETED" }),
      control_state: "RUNNING",
      allowed_operations: ["cleanup", "delete"],
    } as Job & { control_state: string; allowed_operations: string[] };

    renderApp(<JobCard job={job} onAction={vi.fn()} />);
    const trigger = screen.getByRole("button", { name: "Mintafilm: további műveletek" });
    trigger.focus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("menuitem", { name: "Takarítás" })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(trigger).toHaveFocus();
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("styles completed-release deletion as destructive", async () => {
    const user = userEvent.setup();
    const job = {
      ...makeJob({ state: "COMPLETED" }),
      control_state: "RUNNING",
      allowed_operations: ["delete_release"],
    } as Job & { control_state: string; allowed_operations: string[] };

    renderApp(<JobCard job={job} onAction={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "Mintafilm: további műveletek" }));

    expect(screen.getByRole("menuitem", { name: "Completed release törlése" })).toHaveClass("job-card__menu-item--danger");
  });

  it("marks every pipeline stage complete for a completed job", () => {
    renderApp(<PipelineSteps job={makeJob({ state: "COMPLETED" })} />);

    const pipeline = screen.getByRole("list", { name: "Kódolási folyamat" });
    expect(pipeline.querySelectorAll(".pipeline__step--complete")).toHaveLength(8);
    expect(screen.getByText("Kész")).toBeInTheDocument();
  });
});
