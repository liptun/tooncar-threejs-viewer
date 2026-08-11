import { cn } from "../../lib/cn";

export function TrackSidebarHeader() {
  return (
    <header
      className={cn(
        "flex flex-col items-center justify-center px-2 py-1 text-center max-md:px-1",
        "border-b border-white/10 bg-linear-to-br from-white/10 to-transparent",
      )}
    >
      <img
        src="/tooncar-logo.png"
        alt="ToonCar"
        className="h-auto w-48 shrink-0 object-contain drop-shadow-logo max-md:w-24"
      />
      <div className="mb-2 flex w-full items-center gap-2 px-4 max-md:hidden">
        <span className="h-px flex-1 bg-linear-to-r from-transparent to-primary/45" />
        <h1 className="text-ui font-black uppercase tracking-display text-primary">
          Wybierz trasę
        </h1>
        <span className="h-px flex-1 bg-linear-to-l from-transparent to-primary/45" />
      </div>
    </header>
  );
}
