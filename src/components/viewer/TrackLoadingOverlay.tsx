import { Sparkles } from "lucide-react";
import { cn } from "@/lib/cn";

type Props = {
  progress: number;
  skyboxReady: boolean;
  error: string;
};

export function TrackLoadingOverlay({ progress, skyboxReady, error }: Props) {
  if (error.length > 0) {
    return (
      <div
        className={cn(
          "pointer-events-none absolute inset-0 grid place-items-center",
          "bg-base text-body text-red-300",
        )}
      >
        {error}
      </div>
    );
  }

  return (
    <div
      className={cn(
        "pointer-events-none absolute inset-0 grid place-items-center",
        "transition-colors duration-300",
        skyboxReady === true ? "bg-transparent" : "bg-base",
      )}
    >
      <div className="w-64 text-center">
        <div
          className={cn(
            "mx-auto mb-5 grid size-12 place-items-center rounded-2xl",
            "border border-primary/35 bg-primary/10 text-primary",
          )}
        >
          <Sparkles className="animate-pulse" size={22} />
        </div>
        <p className="text-caption font-bold uppercase tracking-display text-white/55">
          Wczytywanie trasy
        </p>
        <div className="mt-4 h-1 overflow-hidden rounded-full bg-white/8">
          <div
            className="transition-progress h-full rounded-full bg-primary duration-300"
            style={{ width: `${Math.max(progress, 4)}%` }}
          />
        </div>
        <p className="mt-2 font-mono text-ui text-white/25">{progress > 0 ? progress : "…"}%</p>
      </div>
    </div>
  );
}
