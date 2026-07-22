import { createPortal } from "react-dom";

export function PagePrimaryAction({ host, children }) {
  if (!host || !children) return null;
  return createPortal(<div className="page-primary-action-content">{children}</div>, host);
}
