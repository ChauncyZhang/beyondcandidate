import { useEffect, useRef, useState } from "react";
import { Bell, ChevronRight, UserRound } from "lucide-react";

function badgeValue(total) {
  return total > 99 ? "99+" : String(total);
}

export function NotificationMenu({ groups, total, onOpenCandidate, onOpenGroup }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef(null);
  const buttonRef = useRef(null);
  const panelRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const focusFrame = window.requestAnimationFrame(() => panelRef.current?.focus());
    function handlePointerDown(event) {
      if (!rootRef.current?.contains(event.target)) setOpen(false);
    }
    function handleKeyDown(event) {
      if (event.key === "Escape") {
        setOpen(false);
        buttonRef.current?.focus();
      }
    }
    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [open]);

  function openCandidate(item) {
    setOpen(false);
    onOpenCandidate(item);
  }

  function openGroup(group) {
    setOpen(false);
    onOpenGroup(group.stage);
  }

  const label = total > 0 ? `查看 ${total} 项待处理事项` : "查看待处理事项";
  return (
    <div className="notification-center" ref={rootRef}>
      <button ref={buttonRef} className="icon-button notification-action" type="button" title={label} aria-label={label} aria-haspopup="dialog" aria-expanded={open} aria-controls="workbench-notification-panel" onClick={() => setOpen((value) => !value)}>
        <Bell size={19} />
        {total > 0 && <span>{badgeValue(total)}</span>}
      </button>
      {open && <section ref={panelRef} id="workbench-notification-panel" className="notification-panel" role="dialog" aria-label="待处理事项" tabIndex={-1}>
        <header className="notification-panel-header"><div><strong>待处理事项</strong><span>{total > 0 ? `共 ${total} 项需要处理` : "当前没有需要处理的事项"}</span></div></header>
        {groups.length > 0 ? <div className="notification-panel-body">
          {groups.map((group) => <section className="notification-group" key={group.key}>
            <header><span className={`status-dot ${group.tone}`} /><strong>{group.label}</strong><small>{group.count}</small><button type="button" onClick={() => openGroup(group)}>查看全部<ChevronRight size={14} /></button></header>
            {group.items.slice(0, 3).map((item) => <button className="notification-item" type="button" key={item.applicationId} onClick={() => openCandidate(item)}>
              <span className={`notification-item-icon ${group.tone}`}><UserRound size={16} /></span>
              <span><strong>{item.name}</strong><small>{item.position} · {item.city}</small>{item.aiLabel && <small role="status">{item.aiLabel}</small>}</span>
              <ChevronRight size={16} />
            </button>)}
          </section>)}
        </div> : <div className="notification-empty"><Bell size={22} /><strong>暂无待处理事项</strong><span>新的评审、面试和录用任务会显示在这里。</span></div>}
      </section>}
    </div>
  );
}
