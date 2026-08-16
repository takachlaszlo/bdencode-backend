import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { Modal } from "./ui";

function ModalHarness() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>Megnyitás</button>
      <Modal
        open={open}
        title="Megerősítés"
        onClose={() => setOpen(false)}
        footer={<button type="button">Mentés</button>}
      >
        <button type="button">Mégse</button>
      </Modal>
    </>
  );
}

describe("Modal", () => {
  it("traps keyboard focus and restores it after closing", async () => {
    const user = userEvent.setup();
    render(<ModalHarness />);

    const trigger = screen.getByRole("button", { name: "Megnyitás" });
    await user.click(trigger);

    const closeButton = screen.getByRole("button", { name: "Bezárás" });
    const cancelButton = screen.getByRole("button", { name: "Mégse" });
    const saveButton = screen.getByRole("button", { name: "Mentés" });
    expect(closeButton).toHaveFocus();

    await user.tab({ shift: true });
    expect(saveButton).toHaveFocus();
    await user.tab();
    expect(closeButton).toHaveFocus();
    await user.tab();
    expect(cancelButton).toHaveFocus();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("supports an accessible description and ignores close requests while busy", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(
      <Modal
        open
        busy
        title="Takarítás"
        ariaDescribedBy="cleanup-description"
        onClose={onClose}
      >
        <p id="cleanup-description">A művelet folyamatban van.</p>
      </Modal>,
    );

    const dialog = screen.getByRole("dialog", { name: "Takarítás" });
    expect(dialog).toHaveAttribute("aria-describedby", "cleanup-description");
    expect(dialog).toHaveAttribute("aria-busy", "true");
    expect(dialog).toHaveFocus();
    expect(screen.getByRole("button", { name: "Bezárás" })).toBeDisabled();

    await user.keyboard("{Escape}");
    fireEvent.mouseDown(document.querySelector(".modal-backdrop")!);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("closes when the backdrop itself is pressed", () => {
    const onClose = vi.fn();
    render(
      <Modal open title="Részletek" onClose={onClose}>
        <p>Tartalom</p>
      </Modal>,
    );

    fireEvent.mouseDown(document.querySelector(".modal-backdrop")!);
    expect(onClose).toHaveBeenCalledOnce();
  });
});
