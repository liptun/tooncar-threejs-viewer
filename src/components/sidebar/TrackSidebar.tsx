import { cn } from "../../lib/cn";
import type { Track } from "../../tracks";
import { TrackListItem } from "./TrackListItem";
import { TrackSidebarHeader } from "./TrackSidebarHeader";
import { TrackSidebarToggle } from "./TrackSidebarToggle";

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
          "fixed inset-y-0 left-0 z-40 w-sidebar-hover-zone max-md:w-sidebar-hover-zone-compact",
          "transition-transform duration-300",
          collapsed === true ? "pointer-events-none -translate-x-full" : "translate-x-0",
        )}
      >
        <aside
          className={cn(
            "h-full w-sidebar overflow-hidden max-md:w-sidebar-compact",
            "border-r border-white/10 bg-panel/70 shadow-2xl backdrop-blur-md",
          )}
        >
          <div className="flex h-full w-sidebar flex-col max-md:w-sidebar-compact">
            <TrackSidebarHeader />
            <section className="flex-1 overflow-y-auto">
              <div className="space-y-1">
                {tracks.map((track) => (
                  <TrackListItem
                    key={track.id}
                    track={track}
                    active={track.id === selectedTrackId}
                    onSelect={onSelectTrack}
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
