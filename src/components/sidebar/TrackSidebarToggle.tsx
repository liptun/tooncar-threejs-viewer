import { ChevronRight } from "lucide-react";
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
          className="fixed inset-y-0 left-0 z-edge-trigger w-2"
          onMouseEnter={onOpen}
          aria-hidden="true"
        />
      )}
      <button
        type="button"
        className={cn(
          "fixed left-0 top-1/2 z-50 flex h-36 w-10 -translate-y-1/2",
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
            "text-ui font-bold uppercase tracking-ui",
          )}
        >
          Wybierz trasę
        </span>
        <ChevronRight size={14} aria-hidden="true" />
      </button>
    </>
  );
}
