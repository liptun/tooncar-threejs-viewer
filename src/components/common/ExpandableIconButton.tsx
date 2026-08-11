import type { ButtonHTMLAttributes, ReactNode } from "react";
import { cn } from "../../lib/cn";

type Props = ButtonHTMLAttributes<HTMLButtonElement> & {
  icon: ReactNode;
  label: string;
  expandedClassName?: string;
  labelClassName?: string;
};

export function ExpandableIconButton({
  icon,
  label,
  expandedClassName = "hover:w-36",
  labelClassName = "group-hover:max-w-24",
  className = "",
  ...buttonProps
}: Props) {
  return (
    <button
      type="button"
      className={cn(
        "group flex h-10 w-10 items-center justify-center overflow-hidden whitespace-nowrap",
        "cursor-pointer rounded-xl border border-white/10 bg-black/30 px-2.5",
        "text-white/75 backdrop-blur-sm",
        "transition-control duration-300",
        "hover:bg-panel/90 hover:text-white",
        expandedClassName,
        className,
      )}
      {...buttonProps}
    >
      <span className="grid shrink-0 place-items-center">{icon}</span>
      <span
        className={cn(
          "ml-0 max-w-0 overflow-hidden opacity-0",
          "text-ui font-bold uppercase tracking-ui",
          "text-shadow-label transition-label duration-300",
          "group-hover:ml-2 group-hover:opacity-100",
          labelClassName,
        )}
      >
        {label}
      </span>
    </button>
  );
}
