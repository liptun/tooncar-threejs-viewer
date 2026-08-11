import { Gauge } from "lucide-react";
import { cn } from "../../lib/cn";

type Props = {
  speed: number;
  visible: boolean;
};

export function SpeedIndicator({ speed, visible }: Props) {
  return (
    <div
      className={cn(
        "pointer-events-none absolute bottom-3 left-3 z-20 w-44",
        "bg-panel/45 px-3 py-2 shadow-speed backdrop-blur-sm",
        "transition-opacity duration-300",
        visible === true ? "opacity-100" : "opacity-0",
      )}
      aria-hidden={visible === false}
    >
      <div
        className={cn(
          "mb-1.5 flex items-center justify-between",
          "text-ui font-bold uppercase tracking-ui text-white/55",
        )}
      >
        <span className="flex items-center gap-1">
          <Gauge size={10} /> Prędkość lotu
        </span>
        <span className="font-mono text-primary">{speed}</span>
      </div>
      <div className="h-1 overflow-hidden bg-white/10">
        <div
          className="transition-progress h-full bg-primary duration-150"
          style={{ width: `${((speed - 5) / 195) * 100}%` }}
        />
      </div>
    </div>
  );
}
