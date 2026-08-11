import { TrackListItem } from "@/components/sidebar/TrackListItem";
import { TrackSidebarHeader } from "@/components/sidebar/TrackSidebarHeader";
import { TrackSidebarToggle } from "@/components/sidebar/TrackSidebarToggle";
import { cn } from "@/lib/cn";
import type { Track } from "@/tracks";

type Props = {
  tracks: Track[];
  selectedTrackId: string;
  collapsed: boolean;
  onCollapsedChange: (collapsed: boolean) => void;
  onSelectTrack: (trackId: string) => void;
};

export function TrackSidebar({
  tracks,
  selectedTrackId,
  collapsed,
  onCollapsedChange,
  onSelectTrack,
}: Props) {
  return (
    <>
      <div
        onMouseLeave={() => onCollapsedChange(true)}
        className={cn(
          "touch-sidebar-shell fixed inset-y-0 left-0 z-40 w-sidebar-hover-zone max-md:w-screen",
          "transition-transform duration-300",
          collapsed === true ? "pointer-events-none -translate-x-full" : "translate-x-0",
        )}
      >
        <aside
          className={cn(
            "touch-sidebar-panel h-full w-sidebar overflow-hidden max-md:w-screen",
            "border-r border-white/10 bg-panel/70 shadow-2xl backdrop-blur-md",
          )}
        >
          <div className="touch-sidebar-content relative flex h-full w-sidebar flex-col max-md:w-screen">
            <button
              type="button"
              className={cn(
                "touch-sidebar-close absolute right-4 top-4 z-10 size-11 place-items-center",
                "rounded-full border border-white/15 bg-panel/80 text-white/75 backdrop-blur-sm",
              )}
              onClick={() => onCollapsedChange(true)}
              aria-label="Zamknij wybór tras"
            >
              <X size={22} aria-hidden="true" />
            </button>
            <TrackSidebarHeader />
            <section className="flex-1 overflow-y-auto">
              <div className="touch-track-grid flex flex-col gap-1">
                {tracks.map((track) => (
                  <TrackListItem
                    key={track.id}
                    track={track}
                    active={track.id === selectedTrackId}
                    onSelect={(trackId, touchscreenInteraction) => {
                      onSelectTrack(trackId);
                      if (touchscreenInteraction === true) onCollapsedChange(true);
                    }}
                  />
                ))}
              </div>
            </section>
          </div>
        </aside>
      </div>

      <TrackSidebarToggle visible={collapsed} onOpen={() => onCollapsedChange(false)} />
    </>
  );
}
import { X } from "lucide-react";
