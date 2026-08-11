import { cn } from "@/lib/cn";
import type { Track } from "@/tracks";

type Props = {
  track: Track;
  active: boolean;
  onSelect: (trackId: string) => void;
};

export function TrackListItem({ track, active, onSelect }: Props) {
  return (
    <button
      type="button"
      onClick={() => onSelect(track.id)}
      className={cn(
        "relative flex min-h-20 w-full items-stretch gap-2 overflow-hidden",
        "cursor-pointer border-l-4 p-0 text-left transition-all",
        active === true
          ? "border-l-primary bg-linear-to-r from-primary/18 to-transparent"
          : "border-l-transparent hover:bg-white/5",
      )}
    >
      <span className="relative w-24 shrink-0 overflow-hidden max-md:w-16">
        <img
          src={track.thumbnailUrl}
          alt=""
          className="size-full object-cover transition duration-300"
        />
      </span>
      <span className="min-w-0 flex-1 self-center max-md:hidden">
        <span
          className={cn(
            "block truncate text-body font-bold",
            active === true ? "text-white" : "text-white/65",
          )}
        >
          {track.name}
        </span>
        <span className="mt-0.5 block truncate text-ui uppercase tracking-wider text-white/30">
          {track.originalName}
        </span>
      </span>
    </button>
  );
}
