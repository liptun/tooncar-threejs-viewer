import { cn } from "@/lib/cn";

export function ViewerWatermark() {
  return (
    <div
      className={cn(
        "viewer-watermark pointer-events-none absolute left-4 top-4 z-20 rounded-xl",
        "bg-panel/70 p-1.5 backdrop-blur-md",
        "transition-opacity duration-300",
      )}
      aria-hidden="true"
    >
      <img
        src="/tooncar-app-icon.png"
        alt=""
        className={cn("size-16 rounded-md object-cover opacity-100", "max-sm:size-14")}
      />
    </div>
  );
}
