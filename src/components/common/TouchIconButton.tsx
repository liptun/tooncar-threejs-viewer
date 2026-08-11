import type { ButtonHTMLAttributes, ReactNode } from "react";
import { cn } from "@/lib/cn";

type Props = ButtonHTMLAttributes<HTMLButtonElement> & {
  icon: ReactNode;
};

export function TouchIconButton({ icon, className, ...buttonProps }: Props) {
  return (
    <button
      type="button"
      className={cn(
        "grid size-12 touch-manipulation place-items-center rounded-full border border-white/20",
        "bg-panel/65 text-white/80 shadow-lg backdrop-blur-sm",
        "active:border-white/30 active:bg-white/15 active:text-white",
        className,
      )}
      {...buttonProps}
    >
      {icon}
    </button>
  );
}
