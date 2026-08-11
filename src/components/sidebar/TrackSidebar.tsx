import { ChevronRight } from "lucide-react";
import { cn } from "../../lib/cn";
import type { Track } from "../../tracks";

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
            "border-r border-white/10 bg-panel/70",
            "shadow-2xl backdrop-blur-md",
          )}
        >
          <div className="flex h-full w-sidebar flex-col max-md:w-sidebar-compact">
            <header
              className={cn(
                "flex flex-col items-center justify-center px-2 py-1 text-center max-md:px-1",
                "border-b border-white/10",
                "bg-linear-to-br from-white/10 to-transparent",
              )}
            >
              <img
                src="/tooncar-logo.png"
                alt="ToonCar"
                className={cn(
                  "h-auto w-48 shrink-0 object-contain max-md:w-24",
                  "drop-shadow-logo",
                )}
              />
              <div className="mb-2 flex w-full items-center gap-2 px-4 max-md:hidden">
                <span className="h-px flex-1 bg-linear-to-r from-transparent to-primary/45" />
                <h1 className="text-ui font-black uppercase tracking-display text-primary">
                  Wybierz trasę
                </h1>
                <span className="h-px flex-1 bg-linear-to-l from-transparent to-primary/45" />
              </div>
            </header>

            <section className="flex-1 overflow-y-auto">
              <div className="space-y-1">
                {tracks.map((track) => {
                  const active = track.id === selectedTrackId;
                  return (
                    <button
                      key={track.id}
                      type="button"
                      onClick={() => onSelectTrack(track.id)}
                      className={cn(
                        "group relative flex min-h-20 w-full items-stretch gap-2 overflow-hidden",
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
                          className="size-full object-cover transition duration-300 group-hover:scale-105"
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
                        <span
                          className={cn(
                            "mt-0.5 block truncate text-ui uppercase",
                            "tracking-wider text-white/30",
                          )}
                        >
                          {track.originalName}
                        </span>
                      </span>
                    </button>
                  );
                })}
              </div>
            </section>
          </div>
        </aside>
      </div>

      {collapsed === true && (
        <>
          <div
            className="fixed inset-y-0 left-0 z-edge-trigger w-2"
            onMouseEnter={() => onCollapsedChange(false)}
            aria-hidden="true"
          />
          <button
            type="button"
            className={cn(
              "fixed left-0 top-1/2 z-50 flex h-36 w-10 -translate-y-1/2",
              "cursor-pointer flex-col items-center justify-center gap-2",
              "rounded-r-xl border-y border-r border-white/10 bg-panel/90",
              "text-white/70 shadow-lg backdrop-blur-sm transition-colors hover:text-white",
            )}
            onClick={() => onCollapsedChange(false)}
            aria-label="Pokaż listę tras"
          >
            <ChevronRight size={14} aria-hidden="true" />
            <span
              className={cn(
                "text-shadow-label writing-vertical",
                "text-ui font-bold uppercase tracking-ui",
              )}
            >
              Wybierz trasę
            </span>
            <ChevronRight size={14} aria-hidden="true" />
          </button>
        </>
      )}
    </>
  );
}
