import { AlertTriangle, CheckCircle2, Info, LoaderCircle, XCircle } from "lucide-react";
import { useEffect, useId, useRef } from "react";
import type { ButtonHTMLAttributes, PropsWithChildren, ReactNode } from "react";
import clsx from "clsx";

export function Card({
  children,
  className,
  interactive = false,
}: PropsWithChildren<{ className?: string; interactive?: boolean }>) {
  return <section className={clsx("card", interactive && "card--interactive", className)}>{children}</section>;
}

export function Button({
  children,
  className,
  variant = "primary",
  icon,
  loading = false,
  type = "button",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  icon?: ReactNode;
  loading?: boolean;
}) {
  return (
    <button
      type={type}
      className={clsx("button", `button--${variant}`, className)}
      {...props}
      disabled={loading || props.disabled}
    >
      {loading ? <LoaderCircle size={17} className="spin" aria-hidden="true" /> : icon}
      <span>{children}</span>
    </button>
  );
}

export function Badge({
  children,
  tone = "neutral",
}: PropsWithChildren<{ tone?: "neutral" | "info" | "success" | "warning" | "danger" }>) {
  return <span className={clsx("badge", `badge--${tone}`)}>{children}</span>;
}

export function ProgressBar({ value, label }: { value: number; label?: string }) {
  const percent = Math.round(Math.max(0, Math.min(1, value)) * 100);
  return (
    <div
      className="progress-wrap"
      role="progressbar"
      aria-label={label || "Folyamat"}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={percent}
    >
      <div className="progress-track">
        <span className="progress-value" style={{ width: `${percent}%` }} />
      </div>
      {label && <span className="progress-label">{label}</span>}
    </div>
  );
}

export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon?: ReactNode;
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      {icon && <div className="empty-state__icon" aria-hidden="true">{icon}</div>}
      <h3>{title}</h3>
      <p>{description}</p>
      {action}
    </div>
  );
}

export function Notice({
  tone = "info",
  title,
  children,
}: PropsWithChildren<{
  tone?: "info" | "success" | "warning" | "danger";
  title?: string;
}>) {
  const Icon = tone === "success" ? CheckCircle2 : tone === "warning" ? AlertTriangle : tone === "danger" ? XCircle : Info;
  return (
    <div className={clsx("notice", `notice--${tone}`)} role={tone === "danger" ? "alert" : "status"}>
      <Icon size={19} aria-hidden="true" />
      <div>
        {title && <strong>{title}</strong>}
        <div>{children}</div>
      </div>
    </div>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <span className={clsx("skeleton", className)} aria-hidden="true" />;
}

export function LoadingPanel({ label = "Betöltés…" }: { label?: string }) {
  return (
    <div className="loading-panel" role="status">
      <LoaderCircle className="spin" size={22} aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow?: string;
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div>
        {eyebrow && <span className="eyebrow">{eyebrow}</span>}
        <h1>{title}</h1>
        {description && <p>{description}</p>}
      </div>
      {actions && <div className="page-header__actions">{actions}</div>}
    </header>
  );
}

export function Modal({
  open,
  title,
  children,
  footer,
  onClose,
}: PropsWithChildren<{
  open: boolean;
  title: string;
  footer?: ReactNode;
  onClose: () => void;
}>) {
  const titleId = useId();
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const onCloseRef = useRef(onClose);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    if (!open) return;
    const previouslyFocused = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCloseRef.current();
    };
    document.addEventListener("keydown", handleKeyDown);
    closeButtonRef.current?.focus();
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      previouslyFocused?.focus();
    };
  }, [open]);

  if (!open) return null;
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="modal__header">
          <h2 id={titleId}>{title}</h2>
          <button ref={closeButtonRef} type="button" className="icon-button" onClick={onClose} aria-label="Bezárás">×</button>
        </div>
        <div className="modal__body">{children}</div>
        {footer && <div className="modal__footer">{footer}</div>}
      </div>
    </div>
  );
}
