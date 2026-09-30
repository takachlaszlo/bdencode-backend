import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

// jsdom has no PointerEvent; a MouseEvent subclass carries the coordinates.
class TestPointerEvent extends MouseEvent {
  readonly pointerId: number;
  constructor(type: string, init: MouseEventInit & { pointerId?: number } = {}) {
    super(type, init);
    this.pointerId = init.pointerId ?? 1;
  }
}
vi.stubGlobal("PointerEvent", TestPointerEvent);
import { ImageInspector } from "./ImageInspector";

function inspector(props: Partial<Parameters<typeof ImageInspector>[0]> = {}) {
  return (
    <ImageInspector
      open
      title="I-frame #100 — pixelnézet"
      sourceUrl="/api/v1/artifacts/source/content"
      encodeUrl="/api/v1/artifacts/encode/content"
      onClose={vi.fn()}
      {...props}
    />
  );
}

describe("ImageInspector", () => {
  it("overlays both images in one scroll container and moves the divider with the slider", () => {
    render(inspector());

    const viewport = screen.getByTestId("inspector-viewport");
    const source = screen.getByAltText("Forrás");
    const encode = screen.getByAltText("Encode");
    expect(viewport.contains(source) && viewport.contains(encode)).toBe(true);
    expect(source).toHaveStyle({ clipPath: "inset(0 50% 0 0)" });

    fireEvent.change(screen.getByLabelText("Source és encode elválasztása (nagyított nézet)"), { target: { value: "20" } });
    expect(source).toHaveStyle({ clipPath: "inset(0 80% 0 0)" });
  });

  it("switches between fit, 100% and 200% and pixelates when magnified", async () => {
    const user = userEvent.setup();
    render(inspector());
    const encode = screen.getByAltText("Encode");
    Object.defineProperty(encode, "naturalWidth", { value: 1920, configurable: true });
    fireEvent.load(encode);

    expect(screen.getByRole("button", { name: "Illesztés" })).toHaveAttribute("aria-pressed", "true");
    await user.click(screen.getByRole("button", { name: "100%" }));
    expect(screen.getByTestId("inspector-viewport")).toHaveClass("inspector-viewport--zoomed");
    expect(screen.getByTestId("inspector-viewport").firstElementChild).toHaveStyle({ width: "1920px" });
    expect(encode).not.toHaveClass("inspector-image--pixelated");

    await user.click(screen.getByRole("button", { name: "200%" }));
    expect(screen.getByTestId("inspector-viewport").firstElementChild).toHaveStyle({ width: "3840px" });
    expect(encode).toHaveClass("inspector-image--pixelated");
    expect(screen.getByAltText("Forrás")).toHaveClass("inspector-image--pixelated");
  });

  it("navigates between pairs with the buttons and the [ ] keys, and cycles zoom with Z", async () => {
    const user = userEvent.setup();
    const onPrevious = vi.fn();
    const onNext = vi.fn();
    render(inspector({ onPrevious, onNext }));

    await user.click(screen.getByRole("button", { name: "Előző" }));
    await user.click(screen.getByRole("button", { name: "Következő" }));
    expect(onPrevious).toHaveBeenCalledTimes(1);
    expect(onNext).toHaveBeenCalledTimes(1);

    await user.keyboard("[[");
    await user.keyboard("]");
    expect(onPrevious).toHaveBeenCalledTimes(2);
    expect(onNext).toHaveBeenCalledTimes(2);

    await user.keyboard("z");
    expect(screen.getByRole("button", { name: "100%" })).toHaveAttribute("aria-pressed", "true");
    await user.keyboard("z");
    expect(screen.getByRole("button", { name: "200%" })).toHaveAttribute("aria-pressed", "true");
    await user.keyboard("z");
    expect(screen.getByRole("button", { name: "Illesztés" })).toHaveAttribute("aria-pressed", "true");
  });

  it("disables missing neighbours, closes on Escape and stays hidden while closed", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const { rerender } = render(inspector({ onClose }));
    expect(screen.getByRole("button", { name: "Előző" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Következő" })).toBeDisabled();

    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalled();

    rerender(inspector({ open: false, onClose }));
    expect(screen.queryByTestId("inspector-viewport")).not.toBeInTheDocument();
  });

  it("pans the scroll container by dragging while magnified", async () => {
    const user = userEvent.setup();
    render(inspector());
    await user.click(screen.getByRole("button", { name: "100%" }));
    const viewport = screen.getByTestId("inspector-viewport");
    viewport.scrollLeft = 100;
    viewport.scrollTop = 50;

    fireEvent.pointerDown(viewport, { clientX: 300, clientY: 200, pointerId: 1 });
    fireEvent.pointerMove(viewport, { clientX: 260, clientY: 180, pointerId: 1 });
    expect(viewport.scrollLeft).toBe(140);
    expect(viewport.scrollTop).toBe(70);
    fireEvent.pointerUp(viewport, { pointerId: 1 });
    fireEvent.pointerMove(viewport, { clientX: 0, clientY: 0, pointerId: 1 });
    expect(viewport.scrollLeft).toBe(140);
  });
});
