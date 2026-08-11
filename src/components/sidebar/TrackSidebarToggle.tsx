import { ChevronRight, Map } from "lucide-react";
import { TouchIconButton } from "@/components/common/TouchIconButton";
import { cn } from "@/lib/cn";

type Props = {
  visible: boolean;
  onOpen: () => void;
};

export function TrackSidebarToggle({ visible, onOpen }: Props) {
  return (
    <>
      {visible === true && (
        <div
          className="desktop-ui fixed inset-y-0 left-0 z-edge-trigger w-2"
          onMouseEnter={onOpen}
          aria-hidden="true"
        />
      )}
      <button
        type="button"
        className={cn(
          "desktop-ui touch-sidebar-toggle fixed left-0 z-50 flex w-10 -translate-y-1/2",
          "cursor-pointer flex-col items-center justify-center gap-2",
          "rounded-r-xl border-y border-r border-white/10 bg-panel/90",
          "text-white/70 shadow-lg backdrop-blur-sm hover:text-white",
          "transition duration-300 ease-in-out will-change-transform",
          visible === true
            ? "translate-x-0 opacity-100 delay-700"
            : "pointer-events-none -translate-x-full opacity-0 delay-0",
        )}
        onClick={onOpen}
        aria-label="Pokaż listę tras"
        aria-hidden={visible === false}
        tabIndex={visible === true ? 0 : -1}
      >
        <ChevronRight size={14} aria-hidden="true" />
        <span
          className={cn(
            "text-shadow-label writing-vertical",
            "text-ui font-bold whitespace-nowrap uppercase tracking-ui",
          )}
        >
          Wybierz trasę
        </span>
        <ChevronRight size={14} aria-hidden="true" />
      </button>
      {visible === true && (
        <div className="touch-ui pointer-events-auto absolute left-4 top-4 z-30">
          <TouchIconButton
            icon={<Map size={21} aria-hidden="true" />}
            onClick={onOpen}
            aria-label="Wybierz trasę"
          />
        </div>
      )}
    </>
  );
}
