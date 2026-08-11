import { useRef, useState, type PointerEvent, type ReactNode } from "react";
import { cn } from "@/lib/cn";

type Props = {
  label: string;
  icon: ReactNode;
  onChange: (x: number, y: number) => void;
  className?: string;
};

type Position = {
  x: number;
  y: number;
};

const idlePosition: Position = { x: 0, y: 0 };
const handleRadiusRatio = 7 / 36;

export function VirtualJoystick({ label, icon, onChange, className }: Props) {
  const pointerIdRef = useRef<number | null>(null);
  const [position, setPosition] = useState(idlePosition);

  const updatePosition = (event: PointerEvent<HTMLDivElement>) => {
    const bounds = event.currentTarget.getBoundingClientRect();
    const radius = bounds.width / 2;
    const maximumDistance = radius - bounds.width * handleRadiusRatio;
    const offsetX = event.clientX - (bounds.left + radius);
    const offsetY = event.clientY - (bounds.top + radius);
    const distance = Math.hypot(offsetX, offsetY);
    const scale = distance > maximumDistance ? maximumDistance / distance : 1;
    const nextPosition = {
      x: offsetX * scale,
      y: offsetY * scale,
    };

    setPosition(nextPosition);
    onChange(nextPosition.x / maximumDistance, nextPosition.y / maximumDistance);
  };

  const release = (event: PointerEvent<HTMLDivElement>) => {
    if (pointerIdRef.current !== event.pointerId) return;
    pointerIdRef.current = null;
    setPosition(idlePosition);
    onChange(0, 0);
  };

  return (
    <div
      role="group"
      aria-label={label}
      className={cn(
        "virtual-joystick relative touch-none rounded-full border border-white/20 bg-panel/45",
        "shadow-lg backdrop-blur-sm select-none",
        className,
      )}
      onPointerDown={(event) => {
        pointerIdRef.current = event.pointerId;
        event.currentTarget.setPointerCapture(event.pointerId);
        updatePosition(event);
      }}
      onPointerMove={(event) => {
        if (pointerIdRef.current === event.pointerId) updatePosition(event);
      }}
      onPointerUp={release}
      onPointerCancel={release}
    >
      <div
        className="virtual-joystick-handle pointer-events-none absolute left-1/2 top-1/2 grid place-items-center rounded-full border border-white/25 bg-white/20 text-white/65 shadow-md"
        style={{
          transform: `translate(calc(-50% + ${position.x}px), calc(-50% + ${position.y}px))`,
        }}
      >
        {icon}
      </div>
    </div>
  );
}
