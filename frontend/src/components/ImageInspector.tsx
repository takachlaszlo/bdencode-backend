import { ChevronLeft, ChevronRight } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import { Button, Modal } from "./ui";

export type InspectorZoom = "fit" | "1" | "2";

const ZOOM_LABELS: Array<[InspectorZoom, string]> = [
  ["fit", "Illesztés"],
  ["1", "100%"],
  ["2", "200%"],
];

/**
 * Pixel-level before/after viewer.  Both images sit in ONE scroll container, so
 * zooming and panning can never drift between source and encode; the divider is
 * expressed in image coordinates and moves with the content.
 */
export function ImageInspector({
  open,
  title,
  sourceUrl,
  encodeUrl,
  onClose,
  onPrevious,
  onNext,
}: {
  open: boolean;
  title: string;
  sourceUrl: string;
  encodeUrl: string;
  onClose: () => void;
  onPrevious?: () => void;
  onNext?: () => void;
}) {
  const [zoom, setZoom] = useState<InspectorZoom>("fit");
  const [position, setPosition] = useState(50);
  const [naturalWidth, setNaturalWidth] = useState<number | null>(null);
  const viewport = useRef<HTMLDivElement>(null);
  const drag = useRef<{ x: number; y: number; left: number; top: number } | null>(null);

  useEffect(() => {
    if (!open) return;
    setZoom("fit");
    setPosition(50);
    setNaturalWidth(null);
  }, [open, sourceUrl, encodeUrl]);

  useEffect(() => {
    if (!open) return;
    const handleKey = (event: KeyboardEvent) => {
      if (event.target instanceof HTMLInputElement || event.target instanceof HTMLSelectElement) return;
      if (event.key === "[" && onPrevious) onPrevious();
      else if (event.key === "]" && onNext) onNext();
      else if (event.key.toLowerCase() === "z") {
        setZoom((value) => (value === "fit" ? "1" : value === "1" ? "2" : "fit"));
      }
    };
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [open, onPrevious, onNext]);

  const scale = zoom === "fit" ? null : Number(zoom);
  const stackWidth = scale && naturalWidth ? `${naturalWidth * scale}px` : "100%";

  function startDrag(event: ReactPointerEvent<HTMLDivElement>) {
    const element = viewport.current;
    if (!element || zoom === "fit") return;
    drag.current = { x: event.clientX, y: event.clientY, left: element.scrollLeft, top: element.scrollTop };
    element.setPointerCapture?.(event.pointerId);
  }
  function moveDrag(event: ReactPointerEvent<HTMLDivElement>) {
    const element = viewport.current;
    const start = drag.current;
    if (!element || !start) return;
    element.scrollLeft = start.left - (event.clientX - start.x);
    element.scrollTop = start.top - (event.clientY - start.y);
  }
  function endDrag() {
    drag.current = null;
  }

  return (
    <Modal
      open={open}
      title={title}
      size="wide"
      onClose={onClose}
      footer={<Button variant="ghost" onClick={onClose}>Bezárás</Button>}
    >
      <div className="inspector-toolbar">
        <div className="compare-mode" role="group" aria-label="Nagyítás">
          {ZOOM_LABELS.map(([value, label]) => (
            <button
              type="button"
              key={value}
              className={zoom === value ? "active" : ""}
              aria-pressed={zoom === value}
              onClick={() => setZoom(value)}
            >
              {label}
            </button>
          ))}
        </div>
        <div className="inspector-nav" role="group" aria-label="Képpárok között">
          <Button variant="ghost" icon={<ChevronLeft size={16} />} disabled={!onPrevious} onClick={onPrevious}>Előző</Button>
          <Button variant="ghost" icon={<ChevronRight size={16} />} disabled={!onNext} onClick={onNext}>Következő</Button>
        </div>
      </div>

      <div
        ref={viewport}
        className={zoom === "fit" ? "inspector-viewport" : "inspector-viewport inspector-viewport--zoomed"}
        data-testid="inspector-viewport"
        onPointerDown={startDrag}
        onPointerMove={moveDrag}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
      >
        <div className="inspector-stack" style={{ width: stackWidth }}>
          <img
            className={scale && scale >= 2 ? "inspector-image inspector-image--pixelated" : "inspector-image"}
            src={encodeUrl}
            alt="Encode"
            draggable={false}
            onLoad={(event) => setNaturalWidth(event.currentTarget.naturalWidth || null)}
          />
          <img
            className={scale && scale >= 2 ? "inspector-image inspector-image--pixelated inspector-image--source" : "inspector-image inspector-image--source"}
            src={sourceUrl}
            alt="Forrás"
            draggable={false}
            style={{ clipPath: `inset(0 ${100 - position}% 0 0)` }}
          />
          <span className="inspector-line" style={{ left: `${position}%` }} aria-hidden="true" />
        </div>
      </div>

      <label className="field inspector-slider">
        <span>Source ◀ {Math.round(position)}% ▶ Encode</span>
        <input
          type="range"
          min={0}
          max={100}
          value={position}
          onChange={(event) => setPosition(Number(event.target.value))}
          aria-label="Source és encode elválasztása (nagyított nézet)"
        />
      </label>
      <p className="muted inspector-hint">Billentyűk: Z – nagyítás váltása · [ és ] – előző/következő pár · nagyítva húzással mozgatható</p>
    </Modal>
  );
}
