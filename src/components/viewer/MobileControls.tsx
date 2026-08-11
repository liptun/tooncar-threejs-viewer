import { ArrowDown, ArrowUp, Eye, Move } from "lucide-react";
import type { PointerEvent, ReactNode } from "react";
import { VirtualJoystick } from "@/components/viewer/VirtualJoystick";
import { cn } from "@/lib/cn";

type Props = {
  onMove: (sideways: number, forward: number) => void;
  onLook: (x: number, y: number) => void;
  onVerticalChange: (vertical: number) => void;
};

type HoldButtonProps = {
  label: string;
  children: ReactNode;
  onPressedChange: (pressed: boolean) => void;
  className?: string;
};

function HoldButton({ label, children, onPressedChange, className }: HoldButtonProps) {
  const release = (event: PointerEvent<HTMLButtonElement>) => {
    if (event.currentTarget.hasPointerCapture(event.pointerId) === true)
      event.currentTarget.releasePointerCapture(event.pointerId);
    onPressedChange(false);
  };

  return (
    <button
      type="button"
      aria-label={label}
      className={cn(
        "touch-control-button grid touch-none place-items-center rounded-full border border-white/20",
        "bg-panel/60 text-white/80 shadow-lg backdrop-blur-sm select-none",
        "active:border-white/30 active:bg-white/15 active:text-white",
        className,
      )}
      onPointerDown={(event) => {
        event.currentTarget.setPointerCapture(event.pointerId);
        onPressedChange(true);
      }}
      onPointerUp={release}
      onPointerCancel={release}
    >
      {children}
    </button>
  );
}

export function MobileControls({ onMove, onLook, onVerticalChange }: Props) {
  return (
    <div className="touch-controls pointer-events-none absolute inset-0 z-30">
      <VirtualJoystick
        label="Ruch kamery"
        icon={<Move size={22} aria-hidden="true" />}
        className="pointer-events-auto absolute bottom-20 left-5"
        onChange={(x, y) => onMove(x, -y)}
      />
      <VirtualJoystick
        label="Obrót kamery"
        icon={<Eye size={22} aria-hidden="true" />}
        className="pointer-events-auto absolute bottom-20 right-5"
        onChange={(x, y) => onLook(x, y)}
      />
      <div className="touch-vertical-controls pointer-events-auto absolute flex gap-2">
        <HoldButton
          label="Leć w dół"
          onPressedChange={(pressed) => onVerticalChange(pressed ? -0.25 : 0)}
        >
          <ArrowDown size={20} aria-hidden="true" />
        </HoldButton>
        <HoldButton
          label="Leć w górę"
          onPressedChange={(pressed) => onVerticalChange(pressed ? 0.25 : 0)}
        >
          <ArrowUp size={20} aria-hidden="true" />
        </HoldButton>
      </div>
    </div>
  );
}
